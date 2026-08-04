"""Offline tests for local authentication, RBAC, data grants, and audits."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from application.query_service import AuthorizedQueryService
from auth.constants import (
    DELETE_DATASOURCE,
    IMPORT_DATASOURCE,
    MANAGE_USERS,
    QUERY_DATA,
    ROLE_ADMIN,
    ROLE_DATA_MANAGER,
    ROLE_VIEWER,
)
from auth.database import CURRENT_SCHEMA_VERSION, MetaDatabase
from auth.models import (
    AuthenticationError,
    AuthorizationError,
    ValidationError,
)
from auth.passwords import PasswordService
from auth.service import AuthService


class FakeQueryResult:
    error = ""
    validation_error = ""
    query_result = [{"total": 1}]


class AuthServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.upload_directory = root / "uploads"
        self.upload_directory.mkdir()
        password_service = PasswordService(
            time_cost=1,
            memory_cost=8192,
            parallelism=1,
        )
        self.service = AuthService(
            MetaDatabase(root / "chatbi_meta.db"),
            upload_directory=self.upload_directory,
            password_service=password_service,
            max_failed_attempts=3,
            lockout_minutes=15,
        )
        self.admin = self.service.bootstrap_admin(
            username="admin",
            display_name="系统管理员",
            email="admin@example.com",
            password="AdminPassword123",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def create_database(self, name: str) -> Path:
        path = self.upload_directory / name
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE metrics (id INTEGER PRIMARY KEY, value REAL)"
            )
            connection.execute(
                "INSERT INTO metrics (value) VALUES (1.5)"
            )
        return path

    def create_user(
        self,
        username: str,
        role_code: str,
    ):
        return self.service.create_user(
            self.admin.id,
            self.admin.session_version,
            username=username,
            display_name=username.title(),
            email=f"{username}@example.com",
            password=f"{username.title()}Password123",
            role_code=role_code,
        )

    def test_password_is_argon2id_and_login_is_generic(self) -> None:
        with self.service.database.connection() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id = ?",
                (self.admin.id,),
            ).fetchone()
        encoded_hash = str(row["password_hash"])
        self.assertTrue(encoded_hash.startswith("$argon2id$"))
        self.assertNotIn("AdminPassword123", encoded_hash)

        authenticated = self.service.authenticate(
            "ADMIN",
            "AdminPassword123",
        )
        self.assertIsNotNone(authenticated)
        self.assertIsNone(
            self.service.authenticate("admin", "incorrect-password")
        )
        self.assertIsNone(
            self.service.authenticate("unknown", "incorrect-password")
        )

    def test_v1_metadata_database_migrates_source_keys(self) -> None:
        legacy_path = (
            Path(self.temporary_directory.name) / "legacy_meta.db"
        )
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                """
                CREATE TABLE schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO schema_version VALUES (1, '2026-01-01')"
            )
            connection.execute(
                """
                CREATE TABLE datasources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    storage_name TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    version INTEGER NOT NULL DEFAULT 1,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    file_mtime_ns INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO datasources (
                    storage_name,
                    display_name,
                    created_at,
                    updated_at
                )
                VALUES ('legacy.db', '旧数据源', '2026-01-01', '2026-01-01')
                """
            )

        migrated = MetaDatabase(legacy_path)
        with migrated.connection() as connection:
            row = connection.execute(
                """
                SELECT source_key
                FROM datasources
                WHERE storage_name = 'legacy.db'
                """
            ).fetchone()
            latest = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
        self.assertEqual(str(row["source_key"]), "legacy.db")
        self.assertEqual(int(latest), CURRENT_SCHEMA_VERSION)

    def test_pending_field_review_lifecycle_and_access_isolation(self) -> None:
        source_key = "mysql_events-a1b2c3.db"
        report = {
            "status": "needs_review",
            "table_count": 1,
            "column_count": 2,
            "issues": [
                {
                    "table_name": "events",
                    "column_name": "c1",
                    "code": "ambiguous_field_name",
                    "message": "字段名称过于模糊",
                }
            ],
        }
        pending = self.service.upsert_pending_datasource_review(
            self.admin.id,
            self.admin.session_version,
            source_key=source_key,
            display_name="events",
            provider="MySQL",
            validation_report=report,
        )
        self.assertEqual(pending.validation_report, report)

        self.service.replace_pending_field_descriptions(
            self.admin.id,
            self.admin.session_version,
            source_key=source_key,
            descriptions={("events", "c1"): "设备所在区域"},
        )
        self.assertEqual(
            self.service.pending_field_descriptions(
                self.admin.id,
                source_key,
            ),
            {("events", "c1"): "设备所在区域"},
        )

        manager = self.create_user("manager", ROLE_DATA_MANAGER)
        with self.assertRaises(AuthorizationError):
            self.service.pending_field_descriptions(manager.id, source_key)

        self.service.finish_pending_datasource_review(
            self.admin.id,
            self.admin.session_version,
            source_key=source_key,
        )
        self.assertIsNone(
            self.service.get_pending_datasource_review(
                self.admin.id,
                source_key,
            )
        )

    def test_role_permission_matrix(self) -> None:
        viewer = self.create_user("viewer", ROLE_VIEWER)
        manager = self.create_user("manager", ROLE_DATA_MANAGER)

        self.assertTrue(self.service.has_permission(viewer.id, QUERY_DATA))
        self.assertFalse(
            self.service.has_permission(viewer.id, IMPORT_DATASOURCE)
        )
        self.assertTrue(
            self.service.has_permission(manager.id, IMPORT_DATASOURCE)
        )
        self.assertFalse(
            self.service.has_permission(manager.id, DELETE_DATASOURCE)
        )
        self.assertTrue(
            self.service.has_permission(self.admin.id, MANAGE_USERS)
        )
        self.assertTrue(
            self.service.has_permission(self.admin.id, DELETE_DATASOURCE)
        )

    def test_datasource_grant_and_authorized_query_boundary(self) -> None:
        path = self.create_database("business.db")
        datasource = self.service.register_datasource(
            self.admin.id,
            self.admin.session_version,
            path,
            "业务数据",
        )
        viewer = self.create_user("viewer", ROLE_VIEWER)
        ungranted = self.create_user("other", ROLE_VIEWER)
        self.assertEqual(self.service.list_visible_datasources(viewer.id), [])

        self.service.replace_datasource_access(
            self.admin.id,
            self.admin.session_version,
            viewer.id,
            [datasource.id],
        )
        self.assertEqual(
            [item.id for item in self.service.list_visible_datasources(viewer.id)],
            [datasource.id],
        )

        calls: list[tuple[str, Path]] = []

        def fake_runner(question: str, database_path: Path) -> FakeQueryResult:
            calls.append((question, database_path))
            return FakeQueryResult()

        query_service = AuthorizedQueryService(
            self.service,
            runner=fake_runner,
        )
        result, resolved = query_service.execute(
            user_id=viewer.id,
            session_version=viewer.session_version + 1,
            datasource_id=datasource.id,
            question="统计记录数量",
        )
        self.assertEqual(result.query_result, [{"total": 1}])
        self.assertEqual(resolved.id, datasource.id)
        self.assertEqual(calls, [("统计记录数量", path.resolve())])

        with self.assertRaises(AuthorizationError):
            query_service.execute(
                user_id=ungranted.id,
                session_version=ungranted.session_version,
                datasource_id=datasource.id,
                question="越权查询",
            )
        self.assertEqual(len(calls), 1)

    def test_manager_imports_own_source_but_cannot_delete(self) -> None:
        manager = self.create_user("manager", ROLE_DATA_MANAGER)
        manager_path = self.create_database("manager.db")
        datasource = self.service.register_datasource(
            manager.id,
            manager.session_version,
            manager_path,
            "经理数据源",
        )
        self.assertEqual(
            [item.id for item in self.service.list_visible_datasources(manager.id)],
            [datasource.id],
        )
        with self.assertRaises(AuthorizationError):
            self.service.delete_datasource(
                manager.id,
                manager.session_version,
                datasource.id,
            )
        self.assertTrue(manager_path.exists())

        admin_path = self.create_database("admin.db")
        self.service.register_datasource(
            self.admin.id,
            self.admin.session_version,
            admin_path,
            "管理员数据源",
        )
        with self.assertRaises(AuthorizationError):
            self.service.register_datasource(
                manager.id,
                manager.session_version,
                admin_path,
                "尝试覆盖",
            )

    def test_last_admin_and_session_invalidation(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.update_user(
                self.admin.id,
                self.admin.session_version,
                self.admin.id,
                display_name=self.admin.display_name,
                email=self.admin.email,
                role_code=ROLE_VIEWER,
                status="active",
            )

        viewer = self.create_user("viewer", ROLE_VIEWER)
        original_session_version = viewer.session_version
        reset_user = self.service.reset_password(
            self.admin.id,
            self.admin.session_version,
            viewer.id,
            "ResetPassword123",
        )
        self.assertTrue(reset_user.must_change_password)
        self.assertGreater(
            reset_user.session_version,
            original_session_version,
        )
        authenticated = self.service.authenticate(
            "viewer",
            "ResetPassword123",
        )
        self.assertIsNotNone(authenticated)
        changed = self.service.change_own_password(
            viewer.id,
            "ResetPassword123",
            "ChangedPassword123",
        )
        self.assertFalse(changed.must_change_password)

    def test_stale_admin_session_cannot_mutate_accounts(self) -> None:
        self.service.reset_password(
            self.admin.id,
            self.admin.session_version,
            self.admin.id,
            "NewAdminPassword123",
        )
        with self.assertRaises(AuthenticationError):
            self.service.create_user(
                self.admin.id,
                self.admin.session_version,
                username="blocked",
                display_name="Blocked",
                email="",
                password="BlockedPassword123",
                role_code=ROLE_VIEWER,
            )

    def test_lockout_and_audit_redaction(self) -> None:
        viewer = self.create_user("viewer", ROLE_VIEWER)
        for _ in range(3):
            self.assertIsNone(
                self.service.authenticate("viewer", "WrongPassword123")
            )
        locked = self.service.get_user(viewer.id)
        self.assertIsNotNone(locked)
        self.assertIsNotNone(locked.locked_until)
        self.assertIsNone(
            self.service.authenticate("viewer", "ViewerPassword123")
        )

        self.service.record_audit(
            actor_user_id=self.admin.id,
            action="redaction_test",
            outcome="success",
            details={
                "password": "DoNotStoreThis123",
                "nested": {"api_key": "also-secret"},
                "safe": "kept",
            },
        )
        records = self.service.list_audit_logs(self.admin.id, limit=100)
        record = next(
            item for item in records if item.action == "redaction_test"
        )
        serialized = json.dumps(record.details)
        self.assertNotIn("DoNotStoreThis123", serialized)
        self.assertNotIn("also-secret", serialized)
        self.assertEqual(record.details["safe"], "kept")

    def test_path_outside_upload_directory_is_rejected(self) -> None:
        outside_path = Path(self.temporary_directory.name) / "outside.db"
        with sqlite3.connect(outside_path) as connection:
            connection.execute("CREATE TABLE forbidden (id INTEGER)")
        with self.assertRaises(ValidationError):
            self.service.register_datasource(
                self.admin.id,
                self.admin.session_version,
                outside_path,
                "越界路径",
            )

    def test_publish_and_delete_datasource_are_atomic_and_private(self) -> None:
        staged_path = self.upload_directory / ".customer.db.test.staged"
        with sqlite3.connect(staged_path) as connection:
            connection.execute(
                "CREATE TABLE customer (id INTEGER PRIMARY KEY, name TEXT)"
            )
        target_path = self.upload_directory / "customer.db"

        datasource = self.service.publish_staged_datasource(
            self.admin.id,
            self.admin.session_version,
            staged_path=staged_path,
            target_path=target_path,
            display_name="客户数据",
        )
        self.assertFalse(staged_path.exists())
        published_path = datasource.path
        self.assertTrue(published_path.exists())
        self.assertFalse(target_path.exists())
        self.assertEqual(published_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            self.upload_directory.stat().st_mode & 0o777,
            0o700,
        )
        self.assertEqual(
            self.service.resolve_authorized_datasource(
                self.admin.id,
                datasource.id,
            ).path,
            published_path.resolve(),
        )
        self.service.sync_datasources(
            self.admin.id,
            self.admin.session_version,
            [],
        )
        self.assertEqual(
            self.service.resolve_authorized_datasource(
                self.admin.id,
                datasource.id,
            ).version,
            datasource.version,
        )
        leftover_path = (
            self.upload_directory
            / ".customer.0123456789abcdef0123456789abcdef.db"
        )
        with sqlite3.connect(leftover_path) as connection:
            connection.execute(
                "CREATE TABLE stale_copy (id INTEGER PRIMARY KEY)"
            )
        self.service.sync_datasources(
            self.admin.id,
            self.admin.session_version,
            [(leftover_path, "不应登记的旧版本")],
        )
        with self.service.database.connection() as connection:
            active_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM datasources
                WHERE status = 'active'
                """
            ).fetchone()[0]
        self.assertEqual(int(active_count), 1)

        with patch.object(
            self.service,
            "_audit",
            side_effect=RuntimeError("forced audit failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.service.delete_datasource(
                    self.admin.id,
                    self.admin.session_version,
                    datasource.id,
                )
        self.assertTrue(published_path.exists())
        self.assertEqual(
            self.service.resolve_authorized_datasource(
                self.admin.id,
                datasource.id,
            ).id,
            datasource.id,
        )

        self.service.delete_datasource(
            self.admin.id,
            self.admin.session_version,
            datasource.id,
        )
        self.assertFalse(published_path.exists())
        self.assertEqual(
            self.service.list_visible_datasources(self.admin.id),
            [],
        )
        with sqlite3.connect(target_path) as connection:
            connection.execute(
                "CREATE TABLE leftover (id INTEGER PRIMARY KEY)"
            )
        self.service.sync_datasources(
            self.admin.id,
            self.admin.session_version,
            [(target_path, "不应复活的客户数据")]
        )
        self.assertEqual(
            self.service.list_visible_datasources(self.admin.id),
            [],
        )

    def test_publish_restores_staged_file_if_registry_write_fails(
        self,
    ) -> None:
        staged_path = self.upload_directory / ".rollback.db.test.staged"
        with sqlite3.connect(staged_path) as connection:
            connection.execute(
                "CREATE TABLE rollback_test (id INTEGER PRIMARY KEY)"
            )
        target_path = self.upload_directory / "rollback.db"

        with patch.object(
            self.service,
            "_audit",
            side_effect=RuntimeError("forced audit failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.service.publish_staged_datasource(
                    self.admin.id,
                    self.admin.session_version,
                    staged_path=staged_path,
                    target_path=target_path,
                    display_name="回滚测试",
                )

        self.assertTrue(staged_path.exists())
        self.assertFalse(target_path.exists())
        with self.service.database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM datasources WHERE storage_name = ?",
                (target_path.name,),
            ).fetchone()
        self.assertIsNone(row)

    def test_failed_refresh_contents_are_never_visible_to_queries(
        self,
    ) -> None:
        target_path = self.upload_directory / "concurrent.db"
        with sqlite3.connect(target_path) as connection:
            connection.execute("CREATE TABLE secrets (value TEXT)")
            connection.execute("INSERT INTO secrets VALUES ('OLD')")
        datasource = self.service.register_datasource(
            self.admin.id,
            self.admin.session_version,
            target_path,
            "并发发布测试",
        )
        viewer = self.create_user("concurrentviewer", ROLE_VIEWER)
        self.service.replace_datasource_access(
            self.admin.id,
            self.admin.session_version,
            viewer.id,
            [datasource.id],
        )
        viewer = self.service.get_user(viewer.id)
        assert viewer is not None

        staged_path = self.upload_directory / ".concurrent.db.test.staged"
        with sqlite3.connect(staged_path) as connection:
            connection.execute("CREATE TABLE secrets (value TEXT)")
            connection.execute(
                "INSERT INTO secrets VALUES ('UNCOMMITTED_SECRET')"
            )

        publish_reached_audit = threading.Event()
        allow_publish_failure = threading.Event()
        runner_read = threading.Event()
        publish_errors: list[Exception] = []
        query_results: list[FakeQueryResult] = []
        observed_values: list[str] = []
        original_audit = self.service._audit

        def guarded_audit(connection, **kwargs) -> None:
            if kwargs.get("action") == "import_datasource":
                publish_reached_audit.set()
                allow_publish_failure.wait(timeout=5)
                raise RuntimeError("forced publish rollback")
            original_audit(connection, **kwargs)

        def publish_worker() -> None:
            try:
                self.service.publish_staged_datasource(
                    self.admin.id,
                    self.admin.session_version,
                    staged_path=staged_path,
                    target_path=target_path,
                    display_name="并发发布测试",
                )
            except Exception as error:
                publish_errors.append(error)

        def query_runner(
            question: str,
            database_path: Path,
        ) -> FakeQueryResult:
            del question
            with sqlite3.connect(database_path) as connection:
                observed_values.append(
                    str(
                        connection.execute(
                            "SELECT value FROM secrets"
                        ).fetchone()[0]
                    )
                )
            runner_read.set()
            return FakeQueryResult()

        def query_worker() -> None:
            result, _ = AuthorizedQueryService(
                self.service,
                runner=query_runner,
            ).execute(
                user_id=viewer.id,
                session_version=viewer.session_version,
                datasource_id=datasource.id,
                question="读取并发数据",
            )
            query_results.append(result)

        with patch.object(
            self.service,
            "_audit",
            side_effect=guarded_audit,
        ):
            publisher = threading.Thread(target=publish_worker)
            publisher.start()
            self.assertTrue(publish_reached_audit.wait(timeout=5))

            query = threading.Thread(target=query_worker)
            query.start()
            self.assertTrue(runner_read.wait(timeout=5))
            allow_publish_failure.set()
            publisher.join(timeout=5)
            query.join(timeout=5)

        self.assertFalse(publisher.is_alive())
        self.assertFalse(query.is_alive())
        self.assertEqual(observed_values, ["OLD"])
        self.assertEqual(len(query_results), 1)
        self.assertIsInstance(publish_errors[0], RuntimeError)
        self.assertEqual(
            self.service.resolve_authorized_datasource(
                viewer.id,
                datasource.id,
            ).version,
            datasource.version,
        )

    def test_query_result_is_blocked_when_session_changes_mid_query(
        self,
    ) -> None:
        path = self.create_database("session-change.db")
        datasource = self.service.register_datasource(
            self.admin.id,
            self.admin.session_version,
            path,
            "会话测试",
        )
        viewer = self.create_user("sessionviewer", ROLE_VIEWER)
        self.service.replace_datasource_access(
            self.admin.id,
            self.admin.session_version,
            viewer.id,
            [datasource.id],
        )
        viewer = self.service.get_user(viewer.id)
        assert viewer is not None

        def resetting_runner(
            question: str,
            database_path: Path,
        ) -> FakeQueryResult:
            del question, database_path
            self.service.reset_password(
                self.admin.id,
                self.admin.session_version,
                viewer.id,
                "ResetDuringQuery123",
            )
            return FakeQueryResult()

        query_service = AuthorizedQueryService(
            self.service,
            runner=resetting_runner,
        )
        with self.assertRaises(AuthenticationError):
            query_service.execute(
                user_id=viewer.id,
                session_version=viewer.session_version,
                datasource_id=datasource.id,
                question="查询期间重置密码",
            )

    def test_query_result_is_blocked_when_source_changes_mid_query(
        self,
    ) -> None:
        target_path = self.create_database("source-change.db")
        datasource = self.service.register_datasource(
            self.admin.id,
            self.admin.session_version,
            target_path,
            "版本测试",
        )
        viewer = self.create_user("versionviewer", ROLE_VIEWER)
        self.service.replace_datasource_access(
            self.admin.id,
            self.admin.session_version,
            viewer.id,
            [datasource.id],
        )
        viewer = self.service.get_user(viewer.id)
        assert viewer is not None

        def replacing_runner(
            question: str,
            database_path: Path,
        ) -> FakeQueryResult:
            del question, database_path
            staged_path = (
                self.upload_directory / ".source-change.db.test.staged"
            )
            with sqlite3.connect(staged_path) as connection:
                connection.execute(
                    "CREATE TABLE replacement (id INTEGER PRIMARY KEY)"
                )
            self.service.publish_staged_datasource(
                self.admin.id,
                self.admin.session_version,
                staged_path=staged_path,
                target_path=target_path,
                display_name="版本测试（已刷新）",
            )
            return FakeQueryResult()

        query_service = AuthorizedQueryService(
            self.service,
            runner=replacing_runner,
        )
        with self.assertRaises(ValidationError):
            query_service.execute(
                user_id=viewer.id,
                session_version=viewer.session_version,
                datasource_id=datasource.id,
                question="查询期间刷新数据源",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
