"""Deterministic normalisation of :class:`RawJob` into :class:`NormalizedJob`.

Also owns the hashing primitives for the three deduplication layers:

1. source identity  -> ``(source_id, external_id)`` (enforced by DB constraint)
2. canonical URL    -> :func:`canonicalize_url`
3. fingerprint      -> SHA-256(company_domain | normalized_title | normalized_location)
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from jobpulse_core.domain.models import NormalizedJob, RawJob, RemotePolicy, Seniority
from jobpulse_core.ingestion.html import html_to_text, sanitize_html
from jobpulse_core.ingestion.urls import canonicalize_url, registrable_domain

TITLE_ABBREVIATIONS: dict[str, str] = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "jnr": "junior",
    "eng": "engineer",
    "engr": "engineer",
    "dev": "developer",
    "mgr": "manager",
    "ai": "artificial intelligence",
    "ml": "machine learning",
    "swe": "software engineer",
    "sde": "software engineer",
    "fe": "frontend",
    "be": "backend",
    "fullstack": "full stack",
    "full-stack": "full stack",
    "ii": "2",
    "iii": "3",
    "iv": "4",
}
LOCATION_ALIASES: dict[str, str] = {
    "us": "united states",
    "usa": "united states",
    "u.s.": "united states",
    "u.s.a.": "united states",
    "united states of america": "united states",
    "uk": "united kingdom",
    "u.k.": "united kingdom",
    "great britain": "united kingdom",
    "uae": "united arab emirates",
    "anywhere": "worldwide",
    "global": "worldwide",
    "remote - worldwide": "worldwide",
    "bengaluru": "bangalore",
}

_NON_ALNUM = re.compile(r"[^a-z0-9+#.\s-]")
_MULTISPACE = re.compile(r"\s+")
_PAREN = re.compile(r"\([^)]*\)|\[[^]]*\]")

REMOTE_PATTERNS = re.compile(
    r"\b(fully[\s-]remote|100%\s*remote|remote[\s-]first|remote|work\s+from\s+home|wfh|distributed\s+team|anywhere)\b",
    re.IGNORECASE,
)
HYBRID_PATTERNS = re.compile(r"\b(hybrid|\d\s*days?\s+(a|per)\s+week\s+in[\s-]office)\b", re.IGNORECASE)
ONSITE_PATTERNS = re.compile(
    r"\b(on[\s-]?site|in[\s-]office|office[\s-]based|relocat(e|ion)\s+required)\b", re.IGNORECASE
)

SENIORITY_PATTERNS: tuple[tuple[re.Pattern[str], Seniority], ...] = (
    (re.compile(r"\b(intern|internship|trainee)\b", re.I), Seniority.INTERN),
    (re.compile(r"\b(director|head of|vp|vice president)\b", re.I), Seniority.DIRECTOR),
    (re.compile(r"\bprincipal\b", re.I), Seniority.PRINCIPAL),
    (re.compile(r"\bstaff\b", re.I), Seniority.STAFF),
    (re.compile(r"\b(engineering manager|manager)\b", re.I), Seniority.MANAGER),
    (re.compile(r"\b(lead|tech lead)\b", re.I), Seniority.LEAD),
    (re.compile(r"\b(senior|sr\.?|snr)\b", re.I), Seniority.SENIOR),
    (re.compile(r"\b(junior|jr\.?|graduate|entry[\s-]level|associate)\b", re.I), Seniority.JUNIOR),
    (re.compile(r"\b(mid[\s-]level|intermediate|engineer ii|developer ii)\b", re.I), Seniority.MID),
)


def sha256_hex(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def canonical_json(payload: Any) -> str:
    """Stable JSON used for content hashing (sorted keys, no whitespace)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def normalize_title(title: str) -> str:
    """``"Sr. AI Engineer (Remote)"`` -> ``"senior artificial intelligence engineer"``."""
    folded = _PAREN.sub(" ", _fold(title))
    folded = folded.replace("/", " ").replace(",", " ").replace("|", " ")
    folded = _NON_ALNUM.sub(" ", folded)
    tokens: list[str] = []
    for raw_token in folded.split():
        token = raw_token.strip(".-")
        if not token or token in {"remote", "hybrid", "onsite"}:
            continue
        tokens.append(TITLE_ABBREVIATIONS.get(token, token))
    return _MULTISPACE.sub(" ", " ".join(tokens)).strip()


def normalize_location(location: str | None) -> str | None:
    if not location:
        return None
    folded = _MULTISPACE.sub(" ", _fold(location)).strip(" ,;-")
    if not folded:
        return None
    if folded in LOCATION_ALIASES:
        return LOCATION_ALIASES[folded]
    parts = [part.strip() for part in re.split(r"[,;/|]", folded) if part.strip()]
    mapped = [LOCATION_ALIASES.get(part, part) for part in parts]
    return ", ".join(dict.fromkeys(mapped))


def infer_remote_policy(*texts: str | None, remote_hint: bool | None = None) -> RemotePolicy:
    """Classify work model from structured hints and free text. Returns UNKNOWN when unsure."""
    joined = " ".join(t for t in texts if t)
    if HYBRID_PATTERNS.search(joined):
        return RemotePolicy.HYBRID
    has_remote = remote_hint is True or bool(REMOTE_PATTERNS.search(joined))
    has_onsite = bool(ONSITE_PATTERNS.search(joined))
    if has_remote and not has_onsite:
        return RemotePolicy.REMOTE
    if has_onsite and not has_remote:
        return RemotePolicy.ONSITE
    if has_remote and has_onsite:
        return RemotePolicy.HYBRID
    if remote_hint is False:
        return RemotePolicy.ONSITE
    return RemotePolicy.UNKNOWN


def infer_seniority(title: str) -> Seniority:
    for pattern, level in SENIORITY_PATTERNS:
        if pattern.search(title):
            return level
    return Seniority.UNKNOWN


def compute_fingerprint(company_domain: str, normalized_title: str, normalized_location: str | None) -> str:
    material = "|".join(
        [registrable_domain(company_domain), normalized_title, normalized_location or ""],
    )
    return sha256_hex(material)


def normalize_job(raw: RawJob) -> NormalizedJob:
    """Pure function: RawJob -> NormalizedJob. Raises ValidationError on unusable URLs."""
    sanitized = sanitize_html(raw.description_html or "")
    text = raw.description_text or html_to_text(sanitized)
    normalized_title = normalize_title(raw.title) or raw.title.strip().lower()
    normalized_location = normalize_location(raw.location)
    remote_policy = infer_remote_policy(
        raw.location,
        raw.title,
        text[:4000],
        remote_hint=raw.remote_hint,
    )
    content_hash = sha256_hex(
        canonical_json(
            {
                "title": raw.title.strip(),
                "location": raw.location,
                "description": text,
                "department": raw.department,
                "employment_type": raw.employment_type,
            },
        ),
    )
    return NormalizedJob(
        external_id=raw.external_id,
        title=raw.title.strip(),
        normalized_title=normalized_title,
        canonical_url=canonicalize_url(raw.url),
        company_name=raw.company_name.strip(),
        company_domain=registrable_domain(raw.company_domain),
        location=raw.location.strip() if raw.location else None,
        normalized_location=normalized_location,
        department=raw.department,
        employment_type=raw.employment_type,
        description_html=sanitized,
        description_text=text,
        published_at=raw.published_at,
        remote_policy=remote_policy,
        seniority=infer_seniority(raw.title),
        content_hash=content_hash,
        fingerprint=compute_fingerprint(raw.company_domain, normalized_title, normalized_location),
        raw_hash=sha256_hex(canonical_json(raw.model_dump(mode="json"))),
    )
