"""Typed records and public errors for the authorization layer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AuthError(RuntimeError):
    """Base error for account and authorization operations."""


class AuthenticationError(AuthError):
    """Raised when authentication or session validation fails."""


class AuthorizationError(AuthError):
    """Raised when an authenticated user lacks required access."""


class ValidationError(AuthError):
    """Raised when an account-management input is invalid."""


@dataclass(frozen=True)
class User:
    """A local ChatBI account hydrated from the metadata database."""

    id: int
    username: str
    display_name: str
    email: str
    status: str
    must_change_password: bool
    session_version: int
    failed_login_attempts: int
    locked_until: str | None
    last_login_at: str | None
    created_at: str
    role_codes: tuple[str, ...]

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def primary_role(self) -> str:
        return self.role_codes[0] if self.role_codes else ""


@dataclass(frozen=True)
class DataSourceRecord:
    """A registered business-data snapshot resolved inside the upload root."""

    id: int
    storage_name: str
    display_name: str
    path: Path
    created_by: int | None
    status: str
    version: int
    file_size: int
    file_mtime_ns: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AuditRecord:
    """A normalized audit row for the administrator UI."""

    id: int
    created_at: str
    actor_username: str
    action: str
    outcome: str
    target_type: str
    target_id: str
    datasource_name: str
    details: dict[str, Any]
