"""SQLite metadata database for accounts, RBAC, data access, and audits."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
from typing import Iterator

from dotenv import load_dotenv

from auth.constants import (
    PERMISSIONS,
    ROLE_DESCRIPTIONS,
    ROLE_LABELS,
    ROLE_PERMISSIONS,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_META_DATABASE = PROJECT_ROOT / "data" / "system" / "chatbi_meta.db"
CURRENT_SCHEMA_VERSION = 4
load_dotenv(PROJECT_ROOT / ".env")


class MetaDatabase:
    """Own short-lived SQLite connections and idempotent schema setup."""

    def __init__(self, path: Path | str | None = None) -> None:
        configured_path = Path(path or os.getenv(
            "CHATBI_META_DB_PATH",
            str(DEFAULT_META_DATABASE),
        )).expanduser()
        if not configured_path.is_absolute():
            configured_path = PROJECT_ROOT / configured_path
        self.path = configured_path.resolve()
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed or self.path == DEFAULT_META_DATABASE.resolve():
            self.path.parent.chmod(0o700)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version_table_exists = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'schema_version'
                """
            ).fetchone()
            if version_table_exists:
                latest_version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_version"
                ).fetchone()[0]
                if int(latest_version) > CURRENT_SCHEMA_VERSION:
                    raise RuntimeError(
                        "权限元数据库版本高于当前程序支持的版本。"
                    )

            try:
                # executescript normally commits a pending transaction first,
                # so the script itself opens the exclusive initialization
                # transaction. Schema setup and RBAC seeding remain atomic.
                connection.executescript(
                    """
                BEGIN IMMEDIATE;

                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    display_name TEXT NOT NULL,
                    email TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'disabled')),
                    must_change_password INTEGER NOT NULL DEFAULT 1,
                    failed_login_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    last_login_at TEXT,
                    session_version INTEGER NOT NULL DEFAULT 1,
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS roles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    is_system INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS permissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_roles (
                    user_id INTEGER NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,
                    role_id INTEGER NOT NULL
                        REFERENCES roles(id) ON DELETE CASCADE,
                    PRIMARY KEY (user_id, role_id)
                );

                CREATE TABLE IF NOT EXISTS role_permissions (
                    role_id INTEGER NOT NULL
                        REFERENCES roles(id) ON DELETE CASCADE,
                    permission_id INTEGER NOT NULL
                        REFERENCES permissions(id) ON DELETE CASCADE,
                    PRIMARY KEY (role_id, permission_id)
                );

                CREATE TABLE IF NOT EXISTS datasources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT NOT NULL UNIQUE,
                    storage_name TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'deleted')),
                    version INTEGER NOT NULL DEFAULT 1,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    file_mtime_ns INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS datasource_permissions (
                    user_id INTEGER NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,
                    datasource_id INTEGER NOT NULL
                        REFERENCES datasources(id) ON DELETE CASCADE,
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, datasource_id)
                );

                CREATE TABLE IF NOT EXISTS pending_datasource_imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'discarded')),
                    validation_report_json TEXT NOT NULL DEFAULT '{}',
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_datasource_column_metadata (
                    pending_import_id INTEGER NOT NULL
                        REFERENCES pending_datasource_imports(id)
                        ON DELETE CASCADE,
                    table_name TEXT NOT NULL,
                    column_name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (pending_import_id, table_name, column_name)
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_user_id INTEGER
                        REFERENCES users(id) ON DELETE SET NULL,
                    action TEXT NOT NULL,
                    outcome TEXT NOT NULL
                        CHECK (outcome IN ('success', 'failure', 'denied')),
                    target_type TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    datasource_id INTEGER
                        REFERENCES datasources(id) ON DELETE SET NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS saved_dashboard_queries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_user_id INTEGER NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,
                    datasource_id INTEGER NOT NULL
                        REFERENCES datasources(id) ON DELETE CASCADE,
                    datasource_version INTEGER NOT NULL,
                    signature TEXT NOT NULL,
                    title TEXT NOT NULL,
                    question TEXT NOT NULL,
                    sql_query TEXT NOT NULL,
                    visualization_spec_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner_user_id, signature)
                );

                CREATE TABLE IF NOT EXISTS dashboard_layouts (
                    owner_user_id INTEGER PRIMARY KEY
                        REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL DEFAULT '可视化数据大屏',
                    layout_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_audit_created_at
                    ON audit_logs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_actor
                    ON audit_logs(actor_user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_datasource_permissions_user
                    ON datasource_permissions(user_id);
                CREATE INDEX IF NOT EXISTS idx_pending_datasource_reviews_owner
                    ON pending_datasource_imports(created_by, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_dashboard_queries_owner
                    ON saved_dashboard_queries(owner_user_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_dashboard_queries_datasource
                    ON saved_dashboard_queries(datasource_id);
                """
                )

                datasource_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(datasources)"
                    ).fetchall()
                }
                if "source_key" not in datasource_columns:
                    connection.execute(
                        "ALTER TABLE datasources ADD COLUMN source_key TEXT"
                    )
                connection.execute(
                    """
                    UPDATE datasources
                    SET source_key = storage_name
                    WHERE source_key IS NULL OR source_key = ''
                    """
                )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_datasources_source_key
                    ON datasources(source_key)
                    """
                )

                connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_version (
                        version,
                        applied_at
                    )
                    VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    """,
                    (CURRENT_SCHEMA_VERSION,),
                )
                self._seed_rbac(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _seed_rbac(connection: sqlite3.Connection) -> None:
        for code, name in PERMISSIONS.items():
            connection.execute(
                """
                INSERT INTO permissions (code, name)
                VALUES (?, ?)
                ON CONFLICT(code) DO UPDATE SET name = excluded.name
                """,
                (code, name),
            )

        for code, label in ROLE_LABELS.items():
            connection.execute(
                """
                INSERT INTO roles (code, name, description, is_system)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    is_system = 1
                """,
                (code, label, ROLE_DESCRIPTIONS[code]),
            )

        for role_code, permission_codes in ROLE_PERMISSIONS.items():
            role_id = connection.execute(
                "SELECT id FROM roles WHERE code = ?",
                (role_code,),
            ).fetchone()["id"]
            connection.execute(
                "DELETE FROM role_permissions WHERE role_id = ?",
                (role_id,),
            )
            for permission_code in permission_codes:
                permission_id = connection.execute(
                    "SELECT id FROM permissions WHERE code = ?",
                    (permission_code,),
                ).fetchone()["id"]
                connection.execute(
                    """
                    INSERT INTO role_permissions (role_id, permission_id)
                    VALUES (?, ?)
                    """,
                    (role_id, permission_id),
                )
