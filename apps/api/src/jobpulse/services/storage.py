"""Immutable snapshot storage: Cloudflare R2 (S3 API) in deployed envs, filesystem locally."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from jobpulse.core.config import Settings
from jobpulse_core.errors import ConfigurationError, SnapshotNotFoundError, StorageError

SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,1023}$")
MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024


def validate_key(key: str) -> str:
    if not SAFE_KEY_RE.fullmatch(key) or ".." in key.split("/") or "//" in key:
        raise StorageError("invalid snapshot key", context={"key": key[:200]})
    return key


def safe_segment(value: str) -> str:
    """Make an arbitrary identifier safe for use as one key segment."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", value).strip(".-")
    return cleaned[:120] or "unknown"


@runtime_checkable
class SnapshotStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...


class LocalSnapshotStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self._root / validate_key(key)).resolve()
        if not path.is_relative_to(self._root):
            raise StorageError("snapshot key escapes storage root", context={"key": key[:200]})
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise StorageError("snapshot too large", context={"key": key, "bytes": len(data)})
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            raise StorageError("failed to write snapshot", context={"key": key}) from exc

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise SnapshotNotFoundError("snapshot not found", context={"key": key}) from exc
        except OSError as exc:
            raise StorageError("failed to read snapshot", context={"key": key}) from exc


class R2SnapshotStore:
    def __init__(
        self,
        *,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        timeout_seconds: float,
    ) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=BotoConfig(
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise StorageError("snapshot too large", context={"key": key, "bytes": len(data)})
        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self._bucket,
                Key=validate_key(key),
                Body=data,
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("R2 put failed", context={"key": key}) from exc

    async def get(self, key: str) -> bytes:
        try:
            response = await asyncio.to_thread(self._client.get_object, Bucket=self._bucket, Key=validate_key(key))
            body = response["Body"]
            try:
                data: bytes = await asyncio.to_thread(body.read, MAX_SNAPSHOT_BYTES + 1)
            finally:
                body.close()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                raise SnapshotNotFoundError("snapshot not found", context={"key": key}) from exc
            raise StorageError("R2 get failed", context={"key": key}) from exc
        except BotoCoreError as exc:
            raise StorageError("R2 get failed", context={"key": key}) from exc
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise StorageError("snapshot too large", context={"key": key})
        return data


def build_snapshot_store(settings: Settings) -> SnapshotStore:
    if settings.storage_backend == "local":
        return LocalSnapshotStore(settings.local_storage_path)
    if not (
        settings.r2_account_id and settings.r2_access_key_id and settings.r2_secret_access_key and settings.r2_bucket
    ):
        raise ConfigurationError("R2 storage is not fully configured")
    return R2SnapshotStore(
        account_id=settings.r2_account_id,
        access_key_id=settings.r2_access_key_id.get_secret_value(),
        secret_access_key=settings.r2_secret_access_key.get_secret_value(),
        bucket=settings.r2_bucket,
        timeout_seconds=settings.r2_timeout_seconds,
    )
