"""Tenant scope for database transactions (enforced by PostgreSQL row-level security).

Every transaction runs with exactly one scope:

* **workspace** - tenant tables show and accept only that workspace's rows. API requests
  and per-profile evaluation run here.
* **system**    - cross-tenant access for catalogue maintenance and fan-out (ingestion,
  polling bootstrap, seeding). Never used for anything a customer request can steer.
* **none**      - the default. Tenant tables return no rows and reject writes (fail closed),
  so a code path that forgets to choose a scope cannot leak data.

The scope travels in a ContextVar (async-task local) and is applied at the start of each
transaction with transaction-local ``set_config`` calls, which is safe behind PgBouncer /
Neon transaction pooling. In the same statement the session switches to the non-owner
application role, because table owners and superusers would otherwise bypass RLS.
"""

from __future__ import annotations

import functools
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobpulse_core.errors import JobPulseError

ROLE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# One round trip: role switch + scope. set_config(..., true) == SET LOCAL (transaction-scoped).
_APPLY_SCOPE_WITH_ROLE = text(
    "SELECT set_config('role', :role, true), "
    "set_config('app.workspace_id', :workspace_id, true), "
    "set_config('app.system_scope', :system, true)"
)
_APPLY_SCOPE = text(
    "SELECT set_config('app.workspace_id', :workspace_id, true), set_config('app.system_scope', :system, true)"
)


class TenantScopeError(JobPulseError):
    """A tenant-scoped operation was attempted without a valid scope."""


@dataclass(frozen=True, slots=True)
class TenantScope:
    workspace_id: uuid.UUID | None = None
    system: bool = False

    def __post_init__(self) -> None:
        if self.system and self.workspace_id is not None:
            msg = "a scope is either system-wide or one workspace, never both"
            raise TenantScopeError(msg)


NO_SCOPE = TenantScope()
SYSTEM_SCOPE = TenantScope(system=True)

_current: ContextVar[TenantScope] = ContextVar("jobpulse_tenant_scope", default=NO_SCOPE)


def current_scope() -> TenantScope:
    return _current.get()


@contextmanager
def scoped(scope: TenantScope) -> Iterator[TenantScope]:
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)


def system_scoped[**P, R](func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Run a catalogue / fan-out operation with cross-tenant (system) scope."""

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with scoped(SYSTEM_SCOPE):
            return await func(*args, **kwargs)

    return wrapper


def workspace_scope(workspace_id: uuid.UUID | str) -> TenantScope:
    return TenantScope(workspace_id=uuid.UUID(str(workspace_id)))


def validate_role(role: str | None) -> str | None:
    if role is not None and not ROLE_NAME_RE.fullmatch(role):
        msg = f"invalid database role name: {role!r}"
        raise TenantScopeError(msg)
    return role


async def apply_scope(session: AsyncSession, scope: TenantScope, *, role: str | None) -> None:
    """Apply ``scope`` (and the RLS-subject role) to the session's current transaction."""
    params = {
        "workspace_id": str(scope.workspace_id) if scope.workspace_id else "",
        "system": "on" if scope.system else "off",
    }
    if role is None:
        await session.execute(_APPLY_SCOPE, params)
    else:
        await session.execute(_APPLY_SCOPE_WITH_ROLE, {**params, "role": role})
