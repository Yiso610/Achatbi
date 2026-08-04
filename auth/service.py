"""Local account, RBAC, data-source authorization, and audit services."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from auth.constants import (
    DELETE_DATASOURCE,
    IMPORT_DATASOURCE,
    MANAGE_DATASOURCE_ACCESS,
    MANAGE_USERS,
    QUERY_DATA,
    ROLE_ADMIN,
    ROLE_LABELS,
    VIEW_AUDIT_LOGS,
)
from auth.database import MetaDatabase, PROJECT_ROOT
from auth.models import (
    AuditRecord,
    AuthenticationError,
    AuthorizationError,
    DataSourceRecord,
    PendingDatasourceReview,
    User,
    ValidationError,
)
from auth.passwords import PasswordService


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
DEFAULT_UPLOAD_DIRECTORY = PROJECT_ROOT / "data" / "uploads"
SENSITIVE_DETAIL_TOKENS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "api_key",
)


def _bounded_environment_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


PASSWORD_VERIFY_SEMAPHORE = threading.BoundedSemaphore(
    _bounded_environment_int(
        "AUTH_MAX_CONCURRENT_PASSWORD_CHECKS",
        2,
        1,
        8,
    )
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class AuthService:
    """Enforce all local-account and data-source access rules."""

    def __init__(
        self,
        database: MetaDatabase | Path | str | None = None,
        *,
        upload_directory: Path | str | None = None,
        password_service: PasswordService | None = None,
        max_failed_attempts: int = 5,
        lockout_minutes: int = 15,
    ) -> None:
        self.database = (
            database
            if isinstance(database, MetaDatabase)
            else MetaDatabase(database)
        )
        self.upload_directory = Path(
            upload_directory or DEFAULT_UPLOAD_DIRECTORY
        ).expanduser().resolve()
        self.upload_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.upload_directory.chmod(0o700)
        for candidate in self.upload_directory.iterdir():
            if (
                candidate.is_file()
                and candidate.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
            ):
                candidate.chmod(0o600)
        self.passwords = password_service or PasswordService()
        self.max_failed_attempts = max(1, int(max_failed_attempts))
        self.lockout_minutes = max(1, int(lockout_minutes))
        self._dummy_password_hash = self.passwords.hash(
            "ChatBI-Dummy-Password-2026"
        )

    @staticmethod
    def _normalize_username(username: str) -> str:
        normalized = str(username or "").strip().lower()
        if not USERNAME_PATTERN.fullmatch(normalized):
            raise ValidationError(
                "用户名需为 3–50 位字母、数字、点、下划线或短横线。"
            )
        return normalized

    def _bounded_password_verify(
        self,
        encoded_hash: str,
        password: str,
    ) -> bool:
        """Bound concurrent Argon2 work so login floods cannot exhaust RAM."""
        acquired = PASSWORD_VERIFY_SEMAPHORE.acquire(timeout=1)
        if not acquired:
            return False
        try:
            return self.passwords.verify(encoded_hash, password)
        finally:
            PASSWORD_VERIFY_SEMAPHORE.release()

    @staticmethod
    def _validate_profile(display_name: str, email: str) -> tuple[str, str]:
        normalized_name = str(display_name or "").strip()
        normalized_email = str(email or "").strip()
        if not normalized_name:
            raise ValidationError("显示名称不能为空。")
        if len(normalized_name) > 100:
            raise ValidationError("显示名称不能超过 100 个字符。")
        if len(normalized_email) > 254:
            raise ValidationError("邮箱地址过长。")
        if normalized_email and (
            "@" not in normalized_email
            or normalized_email.startswith("@")
            or normalized_email.endswith("@")
        ):
            raise ValidationError("邮箱地址格式无效。")
        return normalized_name, normalized_email

    @staticmethod
    def _sanitize_details(value: Any, key: str = "") -> Any:
        if any(token in key.lower() for token in SENSITIVE_DETAIL_TOKENS):
            return "[REDACTED]"
        if isinstance(value, Mapping):
            return {
                str(item_key): AuthService._sanitize_details(
                    item_value,
                    str(item_key),
                )
                for item_key, item_value in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [
                AuthService._sanitize_details(item, key)
                for item in value
            ]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @staticmethod
    def _role_codes(
        connection: sqlite3.Connection,
        user_id: int,
    ) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT r.code
            FROM roles r
            JOIN user_roles ur ON ur.role_id = r.id
            WHERE ur.user_id = ?
            ORDER BY r.id
            """,
            (user_id,),
        ).fetchall()
        return tuple(str(row["code"]) for row in rows)

    def _hydrate_user(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> User:
        return User(
            id=int(row["id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            email=str(row["email"] or ""),
            status=str(row["status"]),
            must_change_password=bool(row["must_change_password"]),
            session_version=int(row["session_version"]),
            failed_login_attempts=int(row["failed_login_attempts"]),
            locked_until=row["locked_until"],
            last_login_at=row["last_login_at"],
            created_at=str(row["created_at"]),
            role_codes=self._role_codes(connection, int(row["id"])),
        )

    def _get_user(
        self,
        connection: sqlite3.Connection,
        user_id: int,
    ) -> User | None:
        row = connection.execute(
            "SELECT * FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        return self._hydrate_user(connection, row) if row else None

    def _validate_session_in_connection(
        self,
        connection: sqlite3.Connection,
        user_id: int,
        session_version: int,
    ) -> User:
        user = self._get_user(connection, int(user_id))
        if (
            user is None
            or not user.is_active
            or user.session_version != int(session_version)
        ):
            raise AuthenticationError("登录状态已失效，请重新登录。")
        return user

    @staticmethod
    def _user_has_permission(
        connection: sqlite3.Connection,
        user_id: int,
        permission_code: str,
    ) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM users u
            JOIN user_roles ur ON ur.user_id = u.id
            JOIN role_permissions rp ON rp.role_id = ur.role_id
            JOIN permissions p ON p.id = rp.permission_id
            WHERE u.id = ?
              AND u.status = 'active'
              AND p.code = ?
            LIMIT 1
            """,
            (int(user_id), permission_code),
        ).fetchone()
        return row is not None

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        actor_user_id: int | None,
        action: str,
        outcome: str,
        target_type: str = "",
        target_id: str | int = "",
        datasource_id: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        safe_details = AuthService._sanitize_details(dict(details or {}))
        cursor = connection.execute(
            """
            INSERT INTO audit_logs (
                actor_user_id,
                action,
                outcome,
                target_type,
                target_id,
                datasource_id,
                details_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor_user_id,
                action,
                outcome,
                target_type,
                str(target_id),
                datasource_id,
                json.dumps(safe_details, ensure_ascii=False, sort_keys=True),
                utc_timestamp(),
            ),
        )
        if int(cursor.lastrowid or 0) % 100 == 0:
            max_rows = _bounded_environment_int(
                "AUTH_AUDIT_MAX_ROWS",
                50_000,
                1_000,
                1_000_000,
            )
            retention_days = _bounded_environment_int(
                "AUTH_AUDIT_RETENTION_DAYS",
                180,
                1,
                3650,
            )
            cutoff = utc_timestamp(
                utc_now() - timedelta(days=retention_days)
            )
            connection.execute(
                "DELETE FROM audit_logs WHERE created_at < ?",
                (cutoff,),
            )
            connection.execute(
                """
                DELETE FROM audit_logs
                WHERE id IN (
                    SELECT id
                    FROM audit_logs
                    ORDER BY id DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (max_rows,),
            )

    def record_audit(
        self,
        *,
        actor_user_id: int | None,
        action: str,
        outcome: str,
        target_type: str = "",
        target_id: str | int = "",
        datasource_id: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                action=action,
                outcome=outcome,
                target_type=target_type,
                target_id=target_id,
                datasource_id=datasource_id,
                details=details,
            )

    def has_users(self) -> bool:
        with self.database.connection() as connection:
            return bool(
                connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            )

    def bootstrap_admin(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        email: str = "",
    ) -> User:
        normalized_username = self._normalize_username(username)
        normalized_name, normalized_email = self._validate_profile(
            display_name,
            email,
        )
        password_hash = self.passwords.hash(password)
        now = utc_timestamp()

        with self.database.transaction() as connection:
            if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise ValidationError("系统已经存在账号，不能再次初始化管理员。")

            cursor = connection.execute(
                """
                INSERT INTO users (
                    username,
                    display_name,
                    email,
                    password_hash,
                    status,
                    must_change_password,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'active', 0, ?, ?)
                """,
                (
                    normalized_username,
                    normalized_name,
                    normalized_email,
                    password_hash,
                    now,
                    now,
                ),
            )
            user_id = int(cursor.lastrowid)
            role_id = connection.execute(
                "SELECT id FROM roles WHERE code = ?",
                (ROLE_ADMIN,),
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)",
                (user_id, role_id),
            )
            self._audit(
                connection,
                actor_user_id=user_id,
                action="bootstrap_admin",
                outcome="success",
                target_type="user",
                target_id=user_id,
                details={"username": normalized_username},
            )
            user = self._get_user(connection, user_id)

        assert user is not None
        return user

    def authenticate(self, username: str, password: str) -> User | None:
        attempted_username = str(username or "").strip().lower()[:100]
        try:
            normalized_username = self._normalize_username(username)
        except ValidationError:
            self._bounded_password_verify(
                self._dummy_password_hash,
                str(password or ""),
            )
            self.record_audit(
                actor_user_id=None,
                action="login",
                outcome="failure",
                target_type="user",
                target_id=attempted_username,
                details={"reason": "invalid_credentials"},
            )
            return None

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id FROM users WHERE username = ?",
                (normalized_username,),
            ).fetchone()

        if row is None:
            self._bounded_password_verify(
                self._dummy_password_hash,
                str(password or ""),
            )
            self.record_audit(
                actor_user_id=None,
                action="login",
                outcome="failure",
                target_type="user",
                target_id=normalized_username,
                details={"reason": "invalid_credentials"},
            )
            return None

        user_id = int(row["id"])

        # Argon2 verification happens outside the SQLite write transaction.
        # A short compare-and-swap transaction then confirms that the exact
        # hash is still current before updating login state. A concurrent
        # password reset therefore cannot authenticate an old hash, while a
        # login flood cannot hold the metadata write lock during Argon2 work.
        for _ in range(3):
            with self.database.connection() as connection:
                snapshot = connection.execute(
                    "SELECT * FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
            if snapshot is None:
                return None

            snapshot_hash = str(snapshot["password_hash"])
            password_matches = self._bounded_password_verify(
                snapshot_hash,
                str(password or ""),
            )
            replacement_hash = snapshot_hash
            if (
                password_matches
                and self.passwords.needs_rehash(snapshot_hash)
            ):
                replacement_hash = self.passwords.hash(str(password))

            with self.database.transaction() as connection:
                current = connection.execute(
                    "SELECT * FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
                if current is None:
                    return None
                if str(current["password_hash"]) != snapshot_hash:
                    continue

                locked_until = parse_timestamp(current["locked_until"])
                is_locked = bool(
                    locked_until and locked_until > utc_now()
                )
                is_active = str(current["status"]) == "active"

                if not password_matches or is_locked or not is_active:
                    reason = "invalid_credentials"
                    if is_locked:
                        reason = "account_locked"
                    elif not is_active:
                        reason = "account_disabled"

                    if not password_matches and not is_locked:
                        failed_attempts = int(
                            current["failed_login_attempts"]
                        ) + 1
                        new_locked_until = None
                        if failed_attempts >= self.max_failed_attempts:
                            new_locked_until = utc_timestamp(
                                utc_now()
                                + timedelta(
                                    minutes=self.lockout_minutes
                                )
                            )
                        connection.execute(
                            """
                            UPDATE users
                            SET failed_login_attempts = ?,
                                locked_until = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                failed_attempts,
                                new_locked_until,
                                utc_timestamp(),
                                user_id,
                            ),
                        )

                    self._audit(
                        connection,
                        actor_user_id=user_id,
                        action="login",
                        outcome="failure",
                        target_type="user",
                        target_id=user_id,
                        details={"reason": reason},
                    )
                    return None

                now = utc_timestamp()
                connection.execute(
                    """
                    UPDATE users
                    SET password_hash = ?,
                        failed_login_attempts = 0,
                        locked_until = NULL,
                        last_login_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (replacement_hash, now, now, user_id),
                )
                self._audit(
                    connection,
                    actor_user_id=user_id,
                    action="login",
                    outcome="success",
                    target_type="user",
                    target_id=user_id,
                )
                return self._get_user(connection, user_id)

        self.record_audit(
            actor_user_id=user_id,
            action="login",
            outcome="failure",
            target_type="user",
            target_id=user_id,
            details={"reason": "credentials_changed"},
        )
        return None

    def get_user(self, user_id: int) -> User | None:
        with self.database.connection() as connection:
            return self._get_user(connection, int(user_id))

    def validate_session(
        self,
        user_id: int,
        session_version: int,
    ) -> User:
        user = self.get_user(user_id)
        if (
            user is None
            or not user.is_active
            or user.session_version != int(session_version)
        ):
            raise AuthenticationError("登录状态已失效，请重新登录。")
        return user

    def permissions_for_user(self, user_id: int) -> frozenset[str]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT p.code
                FROM permissions p
                JOIN role_permissions rp ON rp.permission_id = p.id
                JOIN user_roles ur ON ur.role_id = rp.role_id
                JOIN users u ON u.id = ur.user_id
                WHERE u.id = ? AND u.status = 'active'
                """,
                (int(user_id),),
            ).fetchall()
            return frozenset(str(row["code"]) for row in rows)

    def has_permission(self, user_id: int, permission_code: str) -> bool:
        with self.database.connection() as connection:
            return self._user_has_permission(
                connection,
                int(user_id),
                permission_code,
            )

    def require_permission(self, user_id: int, permission_code: str) -> None:
        if not self.has_permission(user_id, permission_code):
            self.record_audit(
                actor_user_id=user_id,
                action="permission_check",
                outcome="denied",
                target_type="permission",
                target_id=permission_code,
            )
            raise AuthorizationError("当前账号没有执行此操作的权限。")

    def list_roles(self) -> list[tuple[str, str]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT code, name FROM roles ORDER BY id"
            ).fetchall()
            return [(str(row["code"]), str(row["name"])) for row in rows]

    def list_users(self, actor_user_id: int) -> list[User]:
        self.require_permission(actor_user_id, MANAGE_USERS)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY username"
            ).fetchall()
            return [self._hydrate_user(connection, row) for row in rows]

    @staticmethod
    def _require_role(
        connection: sqlite3.Connection,
        role_code: str,
    ) -> int:
        row = connection.execute(
            "SELECT id FROM roles WHERE code = ?",
            (role_code,),
        ).fetchone()
        if row is None:
            raise ValidationError("所选账号类型不存在。")
        return int(row["id"])

    @staticmethod
    def _validate_datasource_ids(
        connection: sqlite3.Connection,
        datasource_ids: Sequence[int],
    ) -> list[int]:
        unique_ids = sorted({int(item) for item in datasource_ids})
        if not unique_ids:
            return []
        placeholders = ",".join("?" for _ in unique_ids)
        rows = connection.execute(
            f"""
            SELECT id
            FROM datasources
            WHERE status = 'active' AND id IN ({placeholders})
            """,
            unique_ids,
        ).fetchall()
        existing = {int(row["id"]) for row in rows}
        if existing != set(unique_ids):
            raise ValidationError("所选数据源不存在或已经停用。")
        return unique_ids

    def create_user(
        self,
        actor_user_id: int,
        actor_session_version: int,
        *,
        username: str,
        display_name: str,
        email: str,
        password: str,
        role_code: str,
        datasource_ids: Sequence[int] = (),
        must_change_password: bool = True,
    ) -> User:
        normalized_username = self._normalize_username(username)
        normalized_name, normalized_email = self._validate_profile(
            display_name,
            email,
        )
        password_hash = self.passwords.hash(password)
        now = utc_timestamp()

        with self.database.transaction() as connection:
            self._validate_session_in_connection(
                connection,
                actor_user_id,
                actor_session_version,
            )
            if not self._user_has_permission(
                connection,
                actor_user_id,
                MANAGE_USERS,
            ):
                raise AuthorizationError("当前账号没有管理用户的权限。")
            role_id = self._require_role(connection, role_code)
            valid_datasource_ids = self._validate_datasource_ids(
                connection,
                datasource_ids,
            )
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO users (
                        username,
                        display_name,
                        email,
                        password_hash,
                        status,
                        must_change_password,
                        created_by,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (
                        normalized_username,
                        normalized_name,
                        normalized_email,
                        password_hash,
                        int(bool(must_change_password)),
                        actor_user_id,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValidationError("该用户名已经存在。") from error

            user_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)",
                (user_id, role_id),
            )
            for datasource_id in valid_datasource_ids:
                connection.execute(
                    """
                    INSERT INTO datasource_permissions (
                        user_id,
                        datasource_id,
                        created_by,
                        created_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, datasource_id, actor_user_id, now),
                )
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                action="create_user",
                outcome="success",
                target_type="user",
                target_id=user_id,
                details={
                    "username": normalized_username,
                    "role": role_code,
                    "datasource_count": len(valid_datasource_ids),
                },
            )
            user = self._get_user(connection, user_id)

        assert user is not None
        return user

    def update_user(
        self,
        actor_user_id: int,
        actor_session_version: int,
        user_id: int,
        *,
        display_name: str,
        email: str,
        role_code: str,
        status: str,
    ) -> User:
        normalized_name, normalized_email = self._validate_profile(
            display_name,
            email,
        )
        if status not in {"active", "disabled"}:
            raise ValidationError("账号状态无效。")

        with self.database.transaction() as connection:
            self._validate_session_in_connection(
                connection,
                actor_user_id,
                actor_session_version,
            )
            if not self._user_has_permission(
                connection,
                actor_user_id,
                MANAGE_USERS,
            ):
                raise AuthorizationError("当前账号没有管理用户的权限。")
            target = self._get_user(connection, user_id)
            if target is None:
                raise ValidationError("目标账号不存在。")
            role_id = self._require_role(connection, role_code)

            removes_active_admin = (
                target.is_active
                and ROLE_ADMIN in target.role_codes
                and (role_code != ROLE_ADMIN or status != "active")
            )
            if removes_active_admin:
                active_admin_count = connection.execute(
                    """
                    SELECT COUNT(DISTINCT u.id) AS total
                    FROM users u
                    JOIN user_roles ur ON ur.user_id = u.id
                    JOIN roles r ON r.id = ur.role_id
                    WHERE u.status = 'active' AND r.code = ?
                    """,
                    (ROLE_ADMIN,),
                ).fetchone()["total"]
                if int(active_admin_count) <= 1:
                    raise ValidationError(
                        "不能禁用或降级系统中最后一个有效管理员。"
                    )

            role_changed = target.role_codes != (role_code,)
            status_changed = target.status != status
            new_session_version = target.session_version + int(
                role_changed or status_changed
            )
            connection.execute(
                """
                UPDATE users
                SET display_name = ?,
                    email = ?,
                    status = ?,
                    session_version = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized_name,
                    normalized_email,
                    status,
                    new_session_version,
                    utc_timestamp(),
                    user_id,
                ),
            )
            connection.execute(
                "DELETE FROM user_roles WHERE user_id = ?",
                (user_id,),
            )
            connection.execute(
                "INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)",
                (user_id, role_id),
            )
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                action="update_user",
                outcome="success",
                target_type="user",
                target_id=user_id,
                details={
                    "role": role_code,
                    "status": status,
                    "profile_changed": (
                        target.display_name != normalized_name
                        or target.email != normalized_email
                    ),
                },
            )
            updated = self._get_user(connection, user_id)

        assert updated is not None
        return updated

    def reset_password(
        self,
        actor_user_id: int,
        actor_session_version: int,
        user_id: int,
        new_password: str,
    ) -> User:
        password_hash = self.passwords.hash(new_password)
        with self.database.transaction() as connection:
            self._validate_session_in_connection(
                connection,
                actor_user_id,
                actor_session_version,
            )
            if not self._user_has_permission(
                connection,
                actor_user_id,
                MANAGE_USERS,
            ):
                raise AuthorizationError("当前账号没有管理用户的权限。")
            target = self._get_user(connection, user_id)
            if target is None:
                raise ValidationError("目标账号不存在。")
            connection.execute(
                """
                UPDATE users
                SET password_hash = ?,
                    must_change_password = 1,
                    failed_login_attempts = 0,
                    locked_until = NULL,
                    session_version = session_version + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (password_hash, utc_timestamp(), user_id),
            )
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                action="reset_password",
                outcome="success",
                target_type="user",
                target_id=user_id,
            )
            updated = self._get_user(connection, user_id)

        assert updated is not None
        return updated

    def change_own_password(
        self,
        user_id: int,
        current_password: str,
        new_password: str,
    ) -> User:
        self.passwords.validate(new_password)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ? AND status = 'active'",
                (int(user_id),),
            ).fetchone()
            if row is None or not self.passwords.verify(
                str(row["password_hash"]),
                current_password,
            ):
                raise AuthenticationError("当前密码不正确。")
            if self.passwords.verify(str(row["password_hash"]), new_password):
                raise ValidationError("新密码不能与当前密码相同。")

            connection.execute(
                """
                UPDATE users
                SET password_hash = ?,
                    must_change_password = 0,
                    failed_login_attempts = 0,
                    locked_until = NULL,
                    session_version = session_version + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    self.passwords.hash(new_password),
                    utc_timestamp(),
                    user_id,
                ),
            )
            self._audit(
                connection,
                actor_user_id=user_id,
                action="change_password",
                outcome="success",
                target_type="user",
                target_id=user_id,
            )
            updated = self._get_user(connection, user_id)

        assert updated is not None
        return updated

    def _safe_storage_path(self, storage_name: str) -> Path:
        if Path(storage_name).name != storage_name:
            raise AuthorizationError("数据源存储名称无效。")
        path = (self.upload_directory / storage_name).resolve()
        if path.parent != self.upload_directory:
            raise AuthorizationError("数据源路径越出了受控目录。")
        return path

    def _validate_source_path(self, path: Path | str) -> Path:
        resolved = Path(path).expanduser().resolve()
        if resolved.parent != self.upload_directory:
            raise ValidationError("只能登记平台数据目录中的数据库。")
        if resolved.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            raise ValidationError("数据源文件类型无效。")
        if not resolved.is_file():
            raise ValidationError("数据源文件不存在。")
        try:
            resolved.chmod(0o600)
        except OSError as error:
            raise ValidationError(
                "无法保护数据源文件权限。"
            ) from error
        return resolved

    def _hydrate_datasource(
        self,
        row: sqlite3.Row,
    ) -> DataSourceRecord:
        return DataSourceRecord(
            id=int(row["id"]),
            storage_name=str(row["storage_name"]),
            display_name=str(row["display_name"]),
            path=self._safe_storage_path(str(row["storage_name"])),
            created_by=(
                int(row["created_by"])
                if row["created_by"] is not None
                else None
            ),
            status=str(row["status"]),
            version=int(row["version"]),
            file_size=int(row["file_size"]),
            file_mtime_ns=int(row["file_mtime_ns"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _normalize_pending_source_key(source_key: str) -> str:
        normalized = str(source_key or "").strip()
        if (
            not normalized
            or len(normalized) > 255
            or Path(normalized).name != normalized
        ):
            raise ValidationError("待处理数据源标识无效。")
        return normalized

    @staticmethod
    def _normalize_field_descriptions(
        descriptions: Mapping[tuple[str, str], str],
    ) -> dict[tuple[str, str], str]:
        normalized: dict[tuple[str, str], str] = {}
        for raw_key, raw_description in descriptions.items():
            if not isinstance(raw_key, tuple) or len(raw_key) != 2:
                raise ValidationError("字段说明的定位信息无效。")
            table_name = str(raw_key[0] or "").strip()
            column_name = str(raw_key[1] or "").strip()
            description = str(raw_description or "").strip()
            if not table_name or not column_name:
                raise ValidationError("字段说明必须指定表名和字段名。")
            if not description:
                continue
            if len(description) > 500:
                raise ValidationError("单个字段说明不能超过 500 个字符。")
            normalized[(table_name, column_name)] = description
        return normalized

    def _can_access_pending_review(
        self,
        connection: sqlite3.Connection,
        actor_user_id: int,
        row: sqlite3.Row,
    ) -> bool:
        if self._user_has_permission(
            connection,
            actor_user_id,
            MANAGE_DATASOURCE_ACCESS,
        ):
            return True
        creator = row["created_by"]
        return creator is not None and int(creator) == int(actor_user_id)

    @staticmethod
    def _hydrate_pending_review(
        row: sqlite3.Row,
    ) -> PendingDatasourceReview:
        try:
            report = json.loads(str(row["validation_report_json"] or "{}"))
        except json.JSONDecodeError:
            report = {}
        if not isinstance(report, dict):
            report = {}
        return PendingDatasourceReview(
            id=int(row["id"]),
            source_key=str(row["source_key"]),
            display_name=str(row["display_name"]),
            provider=str(row["provider"]),
            validation_report=report,
            created_by=(
                int(row["created_by"])
                if row["created_by"] is not None
                else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get_pending_datasource_review(
        self,
        actor_user_id: int,
        source_key: str,
    ) -> PendingDatasourceReview | None:
        self.require_permission(actor_user_id, IMPORT_DATASOURCE)
        normalized_key = self._normalize_pending_source_key(source_key)
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM pending_datasource_imports
                WHERE source_key = ? AND status = 'pending'
                """,
                (normalized_key,),
            ).fetchone()
            if row is None:
                return None
            if not self._can_access_pending_review(
                connection,
                actor_user_id,
                row,
            ):
                raise AuthorizationError("当前账号不能查看该待处理数据源。")
        return self._hydrate_pending_review(row)

    def pending_field_descriptions(
        self,
        actor_user_id: int,
        source_key: str,
    ) -> dict[tuple[str, str], str]:
        pending = self.get_pending_datasource_review(
            actor_user_id,
            source_key,
        )
        if pending is None:
            return {}
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT table_name, column_name, description
                FROM pending_datasource_column_metadata
                WHERE pending_import_id = ?
                ORDER BY table_name, column_name
                """,
                (pending.id,),
            ).fetchall()
        return {
            (str(row["table_name"]), str(row["column_name"])):
            str(row["description"])
            for row in rows
        }

    def upsert_pending_datasource_review(
        self,
        actor_user_id: int,
        actor_session_version: int,
        *,
        source_key: str,
        display_name: str,
        provider: str,
        validation_report: Mapping[str, Any],
    ) -> PendingDatasourceReview:
        normalized_key = self._normalize_pending_source_key(source_key)
        normalized_name = str(display_name or "").strip()[:200]
        normalized_provider = str(provider or "").strip()[:50]
        if not normalized_name or not normalized_provider:
            raise ValidationError("待处理数据源缺少名称或类型。")
        report_json = json.dumps(
            self._sanitize_details(dict(validation_report)),
            ensure_ascii=False,
            sort_keys=True,
        )
        now = utc_timestamp()
        with self.database.transaction() as connection:
            self._validate_session_in_connection(
                connection,
                actor_user_id,
                actor_session_version,
            )
            if not self._user_has_permission(
                connection,
                actor_user_id,
                IMPORT_DATASOURCE,
            ):
                raise AuthorizationError("当前账号没有导入数据源的权限。")
            existing = connection.execute(
                "SELECT * FROM pending_datasource_imports WHERE source_key = ?",
                (normalized_key,),
            ).fetchone()
            if existing is not None and not self._can_access_pending_review(
                connection,
                actor_user_id,
                existing,
            ):
                raise AuthorizationError("当前账号不能修改该待处理数据源。")
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO pending_datasource_imports (
                        source_key, display_name, provider, status,
                        validation_report_json, created_by, created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        normalized_key,
                        normalized_name,
                        normalized_provider,
                        report_json,
                        actor_user_id,
                        now,
                        now,
                    ),
                )
                pending_id = int(cursor.lastrowid)
            else:
                pending_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE pending_datasource_imports
                    SET display_name = ?, provider = ?, status = 'pending',
                        validation_report_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        normalized_name,
                        normalized_provider,
                        report_json,
                        now,
                        pending_id,
                    ),
                )
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                action="datasource_field_preflight",
                outcome="success",
                target_type="pending_datasource_review",
                target_id=pending_id,
                details={"status": "needs_review"},
            )
            row = connection.execute(
                "SELECT * FROM pending_datasource_imports WHERE id = ?",
                (pending_id,),
            ).fetchone()
        assert row is not None
        return self._hydrate_pending_review(row)

    def replace_pending_field_descriptions(
        self,
        actor_user_id: int,
        actor_session_version: int,
        *,
        source_key: str,
        descriptions: Mapping[tuple[str, str], str],
    ) -> None:
        normalized_key = self._normalize_pending_source_key(source_key)
        normalized_descriptions = self._normalize_field_descriptions(
            descriptions,
        )
        now = utc_timestamp()
        with self.database.transaction() as connection:
            self._validate_session_in_connection(
                connection,
                actor_user_id,
                actor_session_version,
            )
            if not self._user_has_permission(
                connection,
                actor_user_id,
                IMPORT_DATASOURCE,
            ):
                raise AuthorizationError("当前账号没有导入数据源的权限。")
            pending = connection.execute(
                """
                SELECT * FROM pending_datasource_imports
                WHERE source_key = ? AND status = 'pending'
                """,
                (normalized_key,),
            ).fetchone()
            if pending is None:
                raise ValidationError("待处理数据源不存在或已失效。")
            if not self._can_access_pending_review(
                connection,
                actor_user_id,
                pending,
            ):
                raise AuthorizationError("当前账号不能修改该待处理数据源。")
            pending_id = int(pending["id"])
            connection.execute(
                """
                DELETE FROM pending_datasource_column_metadata
                WHERE pending_import_id = ?
                """,
                (pending_id,),
            )
            connection.executemany(
                """
                INSERT INTO pending_datasource_column_metadata (
                    pending_import_id, table_name, column_name, description,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        pending_id,
                        table_name,
                        column_name,
                        description,
                        now,
                    )
                    for (table_name, column_name), description
                    in normalized_descriptions.items()
                ],
            )
            connection.execute(
                """
                UPDATE pending_datasource_imports
                SET updated_at = ?
                WHERE id = ?
                """,
                (now, pending_id),
            )
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                action="update_pending_field_descriptions",
                outcome="success",
                target_type="pending_datasource_review",
                target_id=pending_id,
                details={"field_count": len(normalized_descriptions)},
            )

    def finish_pending_datasource_review(
        self,
        actor_user_id: int,
        actor_session_version: int,
        *,
        source_key: str,
    ) -> None:
        self._delete_pending_datasource_review(
            actor_user_id,
            actor_session_version,
            source_key=source_key,
            audit_action="complete_pending_datasource_review",
        )

    def discard_pending_datasource_review(
        self,
        actor_user_id: int,
        actor_session_version: int,
        *,
        source_key: str,
    ) -> None:
        self._delete_pending_datasource_review(
            actor_user_id,
            actor_session_version,
            source_key=source_key,
            audit_action="discard_pending_datasource_review",
        )

    def _delete_pending_datasource_review(
        self,
        actor_user_id: int,
        actor_session_version: int,
        *,
        source_key: str,
        audit_action: str,
    ) -> None:
        normalized_key = self._normalize_pending_source_key(source_key)
        with self.database.transaction() as connection:
            self._validate_session_in_connection(
                connection,
                actor_user_id,
                actor_session_version,
            )
            pending = connection.execute(
                """
                SELECT * FROM pending_datasource_imports
                WHERE source_key = ? AND status = 'pending'
                """,
                (normalized_key,),
            ).fetchone()
            if pending is None:
                return
            if not self._can_access_pending_review(
                connection,
                actor_user_id,
                pending,
            ):
                raise AuthorizationError("当前账号不能处理该待处理数据源。")
            pending_id = int(pending["id"])
            connection.execute(
                "DELETE FROM pending_datasource_imports WHERE id = ?",
                (pending_id,),
            )
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                action=audit_action,
                outcome="success",
                target_type="pending_datasource_review",
                target_id=pending_id,
            )

    def sync_datasources(
        self,
        actor_user_id: int,
        session_version: int,
        sources: Iterable[tuple[Path, str]],
    ) -> None:
        """Register legacy files and deactivate registry rows for missing files."""
        self.validate_session(actor_user_id, session_version)
        self.require_permission(
            actor_user_id,
            MANAGE_DATASOURCE_ACCESS,
        )
        normalized: dict[str, tuple[Path, str]] = {}
        for raw_path, raw_display_name in sources:
            try:
                path = self._validate_source_path(raw_path)
            except ValidationError:
                continue
            display_name = str(raw_display_name or path.stem).strip()[:200]
            normalized[path.name] = (path, display_name or path.stem)

        now = utc_timestamp()
        with self.database.transaction() as connection:
            self._validate_session_in_connection(
                connection,
                actor_user_id,
                session_version,
            )
            if not self._user_has_permission(
                connection,
                actor_user_id,
                MANAGE_DATASOURCE_ACCESS,
            ):
                raise AuthorizationError(
                    "当前账号没有管理数据源的权限。"
                )
            for storage_name, (path, display_name) in normalized.items():
                if not path.is_file():
                    continue
                stat = path.stat()
                existing = connection.execute(
                    """
                    SELECT id, file_size, file_mtime_ns, status
                    FROM datasources
                    WHERE storage_name = ?
                    """,
                    (storage_name,),
                ).fetchone()
                if existing is None:
                    if storage_name.startswith("."):
                        # Platform-managed immutable versions are hidden. An
                        # unreferenced version is a cleanup leftover, never a
                        # new logical data source eligible for auto-migration.
                        continue
                    logical_match = connection.execute(
                        """
                        SELECT 1
                        FROM datasources
                        WHERE source_key = ?
                        """,
                        (storage_name,),
                    ).fetchone()
                    if logical_match is not None:
                        # A leftover legacy file must not replace the immutable
                        # version currently selected by the registry.
                        continue
                    connection.execute(
                        """
                        INSERT INTO datasources (
                            source_key,
                            storage_name,
                            display_name,
                            status,
                            version,
                            file_size,
                            file_mtime_ns,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, 'active', 1, ?, ?, ?, ?)
                        """,
                        (
                            storage_name,
                            storage_name,
                            display_name,
                            int(stat.st_size),
                            int(stat.st_mtime_ns),
                            now,
                            now,
                        ),
                    )
                    continue

                # A registry tombstone is authoritative. Files left behind by
                # an interrupted cleanup must never silently revive a deleted
                # data source. A deliberate import can reactivate it through
                # publish_staged_datasource(), where authorization is checked.
                if str(existing["status"]) == "deleted":
                    continue

                file_changed = (
                    int(existing["file_size"]) != int(stat.st_size)
                    or int(existing["file_mtime_ns"]) != int(stat.st_mtime_ns)
                )
                connection.execute(
                    """
                    UPDATE datasources
                    SET display_name = ?,
                        version = version + ?,
                        file_size = ?,
                        file_mtime_ns = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        display_name,
                        int(file_changed),
                        int(stat.st_size),
                        int(stat.st_mtime_ns),
                        now,
                        int(existing["id"]),
                    ),
                )

            current_names = set(normalized)
            active_rows = connection.execute(
                """
                SELECT id, storage_name
                FROM datasources
                WHERE status = 'active'
                """
            ).fetchall()
            missing_ids = []
            for row in active_rows:
                storage_name = str(row["storage_name"])
                if storage_name in current_names:
                    continue
                # The filesystem list was collected before this write
                # transaction. Re-check the registry's current pointer now so
                # a concurrent publish cannot be tombstoned by a stale scan.
                try:
                    current_path = self._safe_storage_path(storage_name)
                except AuthorizationError:
                    current_path = None
                if current_path is not None and current_path.is_file():
                    continue
                missing_ids.append(int(row["id"]))
            for datasource_id in missing_ids:
                connection.execute(
                    """
                    UPDATE datasources
                    SET status = 'deleted',
                        version = version + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, datasource_id),
                )
                connection.execute(
                    """
                    DELETE FROM datasource_permissions
                    WHERE datasource_id = ?
                    """,
                    (datasource_id,),
                )

    def register_datasource(
        self,
        actor_user_id: int,
        actor_session_version: int,
        path: Path | str,
        display_name: str,
    ) -> DataSourceRecord:
        self.validate_session(
            actor_user_id,
            actor_session_version,
        )
        self.require_permission(actor_user_id, IMPORT_DATASOURCE)
        resolved = self._validate_source_path(path)
        normalized_name = str(display_name or resolved.stem).strip()[:200]
        if not normalized_name:
            normalized_name = resolved.stem
        stat = resolved.stat()
        now = utc_timestamp()

        with self.database.transaction() as connection:
            self._validate_session_in_connection(
                connection,
                actor_user_id,
                actor_session_version,
            )
            if not self._user_has_permission(
                connection,
                actor_user_id,
                IMPORT_DATASOURCE,
            ):
                raise AuthorizationError("当前账号没有导入数据源的权限。")
            existing = connection.execute(
                "SELECT * FROM datasources WHERE source_key = ?",
                (resolved.name,),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO datasources (
                        source_key,
                        storage_name,
                        display_name,
                        status,
                        version,
                        file_size,
                        file_mtime_ns,
                        created_by,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, 'active', 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved.name,
                        resolved.name,
                        normalized_name,
                        int(stat.st_size),
                        int(stat.st_mtime_ns),
                        actor_user_id,
                        now,
                        now,
                    ),
                )
                datasource_id = int(cursor.lastrowid)
            else:
                can_manage_existing = self._user_has_permission(
                    connection,
                    actor_user_id,
                    MANAGE_DATASOURCE_ACCESS,
                )
                existing_creator = existing["created_by"]
                if (
                    str(existing["status"]) == "active"
                    and not can_manage_existing
                    and (
                        existing_creator is None
                        or int(existing_creator) != int(actor_user_id)
                    )
                ):
                    raise AuthorizationError(
                        "当前账号不能覆盖其他用户登记的数据源。"
                    )
                datasource_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE datasources
                    SET storage_name = ?,
                        display_name = ?,
                        status = 'active',
                        version = version + 1,
                        file_size = ?,
                        file_mtime_ns = ?,
                        created_by = COALESCE(created_by, ?),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        resolved.name,
                        normalized_name,
                        int(stat.st_size),
                        int(stat.st_mtime_ns),
                        actor_user_id,
                        now,
                        datasource_id,
                    ),
                )

            connection.execute(
                """
                INSERT INTO datasource_permissions (
                    user_id,
                    datasource_id,
                    created_by,
                    created_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, datasource_id) DO NOTHING
                """,
                (
                    actor_user_id,
                    datasource_id,
                    actor_user_id,
                    now,
                ),
            )
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                action="import_datasource",
                outcome="success",
                target_type="datasource",
                target_id=datasource_id,
                datasource_id=datasource_id,
                details={"display_name": normalized_name},
            )
            row = connection.execute(
                "SELECT * FROM datasources WHERE id = ?",
                (datasource_id,),
            ).fetchone()

        assert row is not None
        return self._hydrate_datasource(row)

    def publish_staged_datasource(
        self,
        actor_user_id: int,
        session_version: int,
        *,
        staged_path: Path | str,
        target_path: Path | str,
        display_name: str,
    ) -> DataSourceRecord:
        """Atomically authorize, publish, register, grant, and audit an import."""
        staged = Path(staged_path).expanduser().resolve()
        target = Path(target_path).expanduser().resolve()
        if (
            staged.parent != self.upload_directory
            or not staged.name.startswith(".")
            or not staged.name.endswith(".staged")
            or not staged.is_file()
        ):
            raise ValidationError("待发布的数据源文件无效。")
        if (
            target.parent != self.upload_directory
            or target.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}
        ):
            raise ValidationError("数据源发布路径无效。")
        staged.chmod(0o600)
        normalized_name = str(display_name or target.stem).strip()[:200]
        if not normalized_name:
            normalized_name = target.stem

        published_path = (
            self.upload_directory
            / (
                f".{target.stem}.{uuid4().hex}"
                f"{target.suffix.lower()}"
            )
        )
        published = False
        old_path: Path | None = None
        row: sqlite3.Row | None = None
        try:
            with self.database.transaction() as connection:
                self._validate_session_in_connection(
                    connection,
                    actor_user_id,
                    session_version,
                )
                if not self._user_has_permission(
                    connection,
                    actor_user_id,
                    IMPORT_DATASOURCE,
                ):
                    raise AuthorizationError(
                        "当前账号没有导入数据源的权限。"
                    )

                existing = connection.execute(
                    "SELECT * FROM datasources WHERE source_key = ?",
                    (target.name,),
                ).fetchone()
                can_manage_existing = self._user_has_permission(
                    connection,
                    actor_user_id,
                    MANAGE_DATASOURCE_ACCESS,
                )
                if (
                    (target.exists() or existing is not None)
                    and not can_manage_existing
                ):
                    raise AuthorizationError(
                        "该数据库已有分析副本；"
                        "只有系统管理员可以刷新现有副本。"
                    )

                if existing is not None:
                    old_path = self._safe_storage_path(
                        str(existing["storage_name"])
                    )
                elif target.exists():
                    old_path = target

                # Publish to a new immutable filename. Until the metadata
                # transaction commits, all readers continue to resolve and
                # open the previous storage_name, so uncommitted snapshot
                # contents can never leak through the stable registry ID.
                os.replace(staged, published_path)
                published_path.chmod(0o600)
                published = True
                stat = published_path.stat()
                now = utc_timestamp()

                if existing is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO datasources (
                            source_key,
                            storage_name,
                            display_name,
                            status,
                            version,
                            file_size,
                            file_mtime_ns,
                            created_by,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, 'active', 1, ?, ?, ?, ?, ?)
                        """,
                        (
                            target.name,
                            published_path.name,
                            normalized_name,
                            int(stat.st_size),
                            int(stat.st_mtime_ns),
                            actor_user_id,
                            now,
                            now,
                        ),
                    )
                    datasource_id = int(cursor.lastrowid)
                else:
                    datasource_id = int(existing["id"])
                    connection.execute(
                        """
                        UPDATE datasources
                        SET storage_name = ?,
                            display_name = ?,
                            status = 'active',
                            version = version + 1,
                            file_size = ?,
                            file_mtime_ns = ?,
                            created_by = COALESCE(created_by, ?),
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            published_path.name,
                            normalized_name,
                            int(stat.st_size),
                            int(stat.st_mtime_ns),
                            actor_user_id,
                            now,
                            datasource_id,
                        ),
                    )

                connection.execute(
                    """
                    INSERT INTO datasource_permissions (
                        user_id,
                        datasource_id,
                        created_by,
                        created_at
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, datasource_id) DO NOTHING
                    """,
                    (
                        actor_user_id,
                        datasource_id,
                        actor_user_id,
                        now,
                    ),
                )
                self._audit(
                    connection,
                    actor_user_id=actor_user_id,
                    action="import_datasource",
                    outcome="success",
                    target_type="datasource",
                    target_id=datasource_id,
                    datasource_id=datasource_id,
                    details={"display_name": normalized_name},
                )
                row = connection.execute(
                    "SELECT * FROM datasources WHERE id = ?",
                    (datasource_id,),
                ).fetchone()
        except Exception as error:
            if published and published_path.exists():
                os.replace(published_path, staged)
                staged.chmod(0o600)
            if isinstance(error, OSError):
                raise ValidationError(
                    "数据源发布失败，请检查服务器存储。"
                ) from error
            raise
        else:
            if (
                old_path is not None
                and old_path != published_path
                and old_path.exists()
            ):
                try:
                    old_path.unlink()
                    for suffix in ("-wal", "-shm", "-journal"):
                        Path(f"{old_path}{suffix}").unlink(missing_ok=True)
                except OSError:
                    # The registry already points at the new private version.
                    # Hidden managed leftovers are ignored by reconciliation
                    # and may be cleaned by an operator later.
                    try:
                        self.record_audit(
                            actor_user_id=actor_user_id,
                            action="import_datasource_cleanup",
                            outcome="failure",
                            target_type="datasource",
                            target_id=(
                                int(row["id"]) if row is not None else ""
                            ),
                            details={"error_type": "OSError"},
                        )
                    except Exception:
                        pass

        assert row is not None
        return self._hydrate_datasource(row)

    def list_all_datasources(
        self,
        actor_user_id: int,
    ) -> list[DataSourceRecord]:
        self.require_permission(actor_user_id, MANAGE_DATASOURCE_ACCESS)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM datasources
                WHERE status = 'active'
                ORDER BY display_name COLLATE NOCASE, id
                """
            ).fetchall()
        return [
            self._hydrate_datasource(row)
            for row in rows
            if self._safe_storage_path(str(row["storage_name"])).is_file()
        ]

    def list_visible_datasources(
        self,
        user_id: int,
    ) -> list[DataSourceRecord]:
        if not self.has_permission(user_id, QUERY_DATA):
            return []
        can_manage_all = self.has_permission(
            user_id,
            MANAGE_DATASOURCE_ACCESS,
        )
        with self.database.connection() as connection:
            if can_manage_all:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM datasources
                    WHERE status = 'active'
                    ORDER BY display_name COLLATE NOCASE, id
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT d.*
                    FROM datasources d
                    JOIN datasource_permissions dp
                      ON dp.datasource_id = d.id
                    WHERE d.status = 'active'
                      AND dp.user_id = ?
                    ORDER BY d.display_name COLLATE NOCASE, d.id
                    """,
                    (int(user_id),),
                ).fetchall()
        visible = []
        for row in rows:
            record = self._hydrate_datasource(row)
            if record.path.is_file():
                visible.append(record)
        return visible

    def resolve_authorized_datasource(
        self,
        user_id: int,
        datasource_id: int,
        permission_code: str = QUERY_DATA,
    ) -> DataSourceRecord:
        self.require_permission(user_id, permission_code)
        can_manage_all = self.has_permission(
            user_id,
            MANAGE_DATASOURCE_ACCESS,
        )
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM datasources
                WHERE id = ? AND status = 'active'
                """,
                (int(datasource_id),),
            ).fetchone()
            if row is None:
                raise AuthorizationError("数据源不存在或已经停用。")
            if not can_manage_all:
                grant = connection.execute(
                    """
                    SELECT 1
                    FROM datasource_permissions
                    WHERE user_id = ? AND datasource_id = ?
                    """,
                    (int(user_id), int(datasource_id)),
                ).fetchone()
                if grant is None:
                    self._audit(
                        connection,
                        actor_user_id=user_id,
                        action="datasource_access",
                        outcome="denied",
                        target_type="datasource",
                        target_id=datasource_id,
                        datasource_id=datasource_id,
                    )
                    connection.commit()
                    raise AuthorizationError("当前账号没有访问该数据源的权限。")

        record = self._hydrate_datasource(row)
        if not record.path.is_file():
            raise AuthorizationError("数据源文件不存在。")
        return record

    def delete_datasource(
        self,
        actor_user_id: int,
        session_version: int,
        datasource_id: int,
    ) -> None:
        """Atomically authorize, quarantine, unregister, and delete a source."""
        quarantine: Path | None = None
        original: Path | None = None
        moved = False
        committed = False

        try:
            with self.database.transaction() as connection:
                self._validate_session_in_connection(
                    connection,
                    actor_user_id,
                    session_version,
                )
                if not self._user_has_permission(
                    connection,
                    actor_user_id,
                    DELETE_DATASOURCE,
                ):
                    raise AuthorizationError(
                        "当前账号没有删除数据源的权限。"
                    )

                can_manage_all = self._user_has_permission(
                    connection,
                    actor_user_id,
                    MANAGE_DATASOURCE_ACCESS,
                )
                if not can_manage_all:
                    grant = connection.execute(
                        """
                        SELECT 1
                        FROM datasource_permissions
                        WHERE user_id = ? AND datasource_id = ?
                        """,
                        (int(actor_user_id), int(datasource_id)),
                    ).fetchone()
                    if grant is None:
                        raise AuthorizationError(
                            "当前账号没有访问该数据源的权限。"
                        )

                row = connection.execute(
                    """
                    SELECT *
                    FROM datasources
                    WHERE id = ? AND status = 'active'
                    """,
                    (int(datasource_id),),
                ).fetchone()
                if row is None:
                    raise ValidationError(
                        "数据源不存在或已经停用。"
                    )

                original = self._safe_storage_path(
                    str(row["storage_name"])
                )
                if not original.is_file():
                    raise ValidationError("数据源文件不存在。")
                quarantine = (
                    self.upload_directory
                    / f".{original.name}.{uuid4().hex}.deleting"
                )

                # Moving inside the same directory is atomic. New queries can
                # no longer open the source while the registry transaction is
                # being committed; an error restores the exact original file.
                os.replace(original, quarantine)
                quarantine.chmod(0o600)
                moved = True

                connection.execute(
                    """
                    UPDATE datasources
                    SET status = 'deleted',
                        version = version + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_timestamp(), int(datasource_id)),
                )
                connection.execute(
                    """
                    DELETE FROM datasource_permissions
                    WHERE datasource_id = ?
                    """,
                    (int(datasource_id),),
                )
                self._audit(
                    connection,
                    actor_user_id=actor_user_id,
                    action="delete_datasource",
                    outcome="success",
                    target_type="datasource",
                    target_id=datasource_id,
                    datasource_id=datasource_id,
                )
            committed = True
        except Exception as error:
            if (
                moved
                and not committed
                and quarantine is not None
                and quarantine.exists()
                and original is not None
                and not original.exists()
            ):
                os.replace(quarantine, original)
                original.chmod(0o600)
            try:
                self.record_audit(
                    actor_user_id=(
                        actor_user_id
                        if self.get_user(actor_user_id) is not None
                        else None
                    ),
                    action="delete_datasource",
                    outcome=(
                        "denied"
                        if isinstance(
                            error,
                            (AuthenticationError, AuthorizationError),
                        )
                        else "failure"
                    ),
                    target_type="datasource",
                    target_id=datasource_id,
                    details={"error_type": error.__class__.__name__},
                )
            except Exception:
                # Preserve the authorization or file-system error even if the
                # best-effort failure audit cannot itself be persisted.
                pass
            if isinstance(error, OSError):
                raise ValidationError(
                    "数据库删除失败，请检查服务器存储。"
                ) from error
            raise

        assert quarantine is not None
        try:
            quarantine.unlink(missing_ok=True)
            assert original is not None
            for suffix in ("-wal", "-shm", "-journal"):
                Path(f"{original}{suffix}").unlink(missing_ok=True)
        except OSError as error:
            # The source is already tombstoned and the leftover file is
            # private, hidden, and outside all listing/query paths.
            self.record_audit(
                actor_user_id=actor_user_id,
                action="delete_datasource_cleanup",
                outcome="failure",
                target_type="datasource",
                target_id=datasource_id,
                details={"error_type": error.__class__.__name__},
            )

    def datasource_ids_for_user(
        self,
        actor_user_id: int,
        target_user_id: int,
    ) -> set[int]:
        self.require_permission(actor_user_id, MANAGE_DATASOURCE_ACCESS)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT datasource_id
                FROM datasource_permissions
                WHERE user_id = ?
                """,
                (int(target_user_id),),
            ).fetchall()
            return {int(row["datasource_id"]) for row in rows}

    def replace_datasource_access(
        self,
        actor_user_id: int,
        actor_session_version: int,
        target_user_id: int,
        datasource_ids: Sequence[int],
    ) -> None:
        with self.database.transaction() as connection:
            self._validate_session_in_connection(
                connection,
                actor_user_id,
                actor_session_version,
            )
            if not self._user_has_permission(
                connection,
                actor_user_id,
                MANAGE_DATASOURCE_ACCESS,
            ):
                raise AuthorizationError("当前账号没有分配数据源的权限。")
            target = self._get_user(connection, target_user_id)
            if target is None:
                raise ValidationError("目标账号不存在。")
            valid_ids = self._validate_datasource_ids(
                connection,
                datasource_ids,
            )
            now = utc_timestamp()
            connection.execute(
                "DELETE FROM datasource_permissions WHERE user_id = ?",
                (int(target_user_id),),
            )
            for datasource_id in valid_ids:
                connection.execute(
                    """
                    INSERT INTO datasource_permissions (
                        user_id,
                        datasource_id,
                        created_by,
                        created_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        int(target_user_id),
                        datasource_id,
                        actor_user_id,
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE users
                SET session_version = session_version + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, int(target_user_id)),
            )
            self._audit(
                connection,
                actor_user_id=actor_user_id,
                action="update_datasource_access",
                outcome="success",
                target_type="user",
                target_id=target_user_id,
                details={"datasource_count": len(valid_ids)},
            )

    def list_audit_logs(
        self,
        actor_user_id: int,
        *,
        limit: int = 200,
        outcome: str | None = None,
    ) -> list[AuditRecord]:
        self.require_permission(actor_user_id, VIEW_AUDIT_LOGS)
        safe_limit = min(max(int(limit), 1), 1000)
        parameters: list[Any] = []
        outcome_filter = ""
        if outcome in {"success", "failure", "denied"}:
            outcome_filter = "AND a.outcome = ?"
            parameters.append(outcome)
        parameters.append(safe_limit)

        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    a.*,
                    COALESCE(u.username, 'system') AS actor_username,
                    COALESCE(d.display_name, '') AS datasource_name
                FROM audit_logs a
                LEFT JOIN users u ON u.id = a.actor_user_id
                LEFT JOIN datasources d ON d.id = a.datasource_id
                WHERE 1 = 1
                  {outcome_filter}
                ORDER BY a.id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()

        records = []
        for row in rows:
            try:
                details = json.loads(str(row["details_json"] or "{}"))
            except json.JSONDecodeError:
                details = {}
            records.append(
                AuditRecord(
                    id=int(row["id"]),
                    created_at=str(row["created_at"]),
                    actor_username=str(row["actor_username"]),
                    action=str(row["action"]),
                    outcome=str(row["outcome"]),
                    target_type=str(row["target_type"]),
                    target_id=str(row["target_id"]),
                    datasource_name=str(row["datasource_name"]),
                    details=details,
                )
            )
        return records


def _environment_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@lru_cache(maxsize=1)
def get_auth_service() -> AuthService:
    """Return the process-wide service; every operation still uses a new DB connection."""
    return AuthService(
        max_failed_attempts=_environment_int(
            "AUTH_MAX_FAILED_ATTEMPTS",
            5,
        ),
        lockout_minutes=_environment_int("AUTH_LOCKOUT_MINUTES", 15),
    )
