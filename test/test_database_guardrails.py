"""Offline tests for the final SQLite execution boundary."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


os.environ["LLM_PROVIDER"] = "qwen"
os.environ["DASHSCOPE_API_KEY"] = "test-key-for-offline-tests"
for _chain_variable in (
    "ROUTER_MODEL_CHAIN",
    "SQL_GENERATOR_MODEL_CHAIN",
    "REFLECTOR_MODEL_CHAIN",
    "VISUALIZER_MODEL_CHAIN",
):
    os.environ[_chain_variable] = ""

from infrastructure import data_sources, db_manager  # noqa: E402


class _GuardrailConfig:
    max_result_rows = 3
    max_result_bytes = 256
    sql_query_timeout_seconds = 2
    max_sql_query_bytes = 100_000
    max_result_columns = 20


class DatabaseGuardrailTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.database_path = self.root / "guardrails.db"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("CREATE TABLE items (id INTEGER, value TEXT)")
            connection.executemany(
                "INSERT INTO items VALUES (?, ?)",
                [(index, f"v{index}") for index in range(10)],
            )
        self.directory_patch = patch.object(
            db_manager,
            "DATABASE_DIRECTORY",
            self.root,
        )
        self.config_patch = patch.object(
            db_manager,
            "get_config",
            return_value=_GuardrailConfig(),
        )
        self.directory_patch.start()
        self.config_patch.start()
        self.manager = db_manager.DatabaseManager(self.database_path)

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.directory_patch.stop()
        self.temporary_directory.cleanup()

    def test_explicit_limit_and_legacy_flag_cannot_bypass_ceiling(
        self,
    ) -> None:
        results, error = self.manager.execute_query(
            "SELECT id FROM items ORDER BY id LIMIT 999",
            enforce_limit=False,
        )
        self.assertIsNone(error)
        self.assertEqual([row["id"] for row in results], [0, 1, 2])

    def test_forbidden_sqlite_operations_are_rejected(self) -> None:
        results, error = self.manager.execute_query(
            "ATTACH DATABASE '/tmp/other.db' AS other"
        )
        self.assertEqual(results, [])
        self.assertIn("Only SELECT", str(error))

    def test_approximate_result_size_is_capped(self) -> None:
        results, error = self.manager.execute_query(
            "SELECT printf('%0100d', id) AS value FROM items"
        )
        self.assertEqual(results, [])
        self.assertIn("memory-size limit", str(error))

    def test_large_blob_is_rejected_by_sqlite_before_result_materialization(
        self,
    ) -> None:
        results, error = self.manager.execute_query(
            "SELECT randomblob(5000000) AS oversized"
        )
        self.assertEqual(results, [])
        self.assertIn("too big", str(error).lower())

    def test_remote_database_hosts_use_an_allowlist(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DATASOURCE_ALLOWED_HOSTS": (
                    "db.example.internal:3306,*.corp.test:5432"
                )
            },
        ):
            self.assertTrue(
                data_sources._remote_host_is_allowed(
                    "db.example.internal",
                    3306,
                )
            )
            self.assertTrue(
                data_sources._remote_host_is_allowed(
                    "postgres.corp.test",
                    5432,
                )
            )
            self.assertFalse(
                data_sources._remote_host_is_allowed(
                    "db.example.internal",
                    22,
                )
            )
            with self.assertRaises(data_sources.DataSourceError):
                data_sources._validate_remote_connection(
                    "MySQL",
                    "169.254.169.254",
                    3306,
                    "reader",
                )
        with patch.dict(
            os.environ,
            {"DATASOURCE_ALLOWED_HOSTS": "localhost:3306"},
        ):
            with self.assertRaises(data_sources.DataSourceError):
                data_sources._validate_remote_connection(
                    "MySQL",
                    "localhost",
                    22,
                    "reader",
                )

    def test_titanic_field_names_are_accepted_without_comments(self) -> None:
        field_names = (
            "PassengerId",
            "Survived",
            "Pclass",
            "Name",
            "Sex",
            "Age",
            "SibSp",
            "Parch",
            "Ticket",
            "Fare",
            "Cabin",
            "Embarked",
        )
        table_specs = [
            {
                "local_name": "titanic_passengers",
                "columns": [
                    {
                        "name": field_name,
                        "source_type": "text",
                        "comment": "",
                    }
                    for field_name in field_names
                ],
            }
        ]

        report = data_sources.validate_table_specs_readiness(table_specs)

        self.assertTrue(report.is_ready)
        self.assertEqual(report.issues, ())
        for field_name in field_names:
            self.assertFalse(
                data_sources.field_name_is_ambiguous(field_name),
                field_name,
            )

    def test_only_high_confidence_opaque_fields_require_review(self) -> None:
        table_specs = [
            {
                "local_name": "events",
                "columns": [
                    {"name": "age", "source_type": "int", "comment": ""},
                    {"name": "c1", "source_type": "text", "comment": ""},
                    {"name": "field_2", "source_type": "int", "comment": ""},
                    {"name": "x_03", "source_type": "text", "comment": ""},
                    {"name": "d3x", "source_type": "text", "comment": ""},
                    {
                        "name": "c5",
                        "source_type": "text",
                        "comment": "设备所在区域",
                    },
                ],
            }
        ]

        report = data_sources.validate_table_specs_readiness(table_specs)
        self.assertTrue(report.needs_review)
        self.assertEqual(
            [issue.column_name for issue in report.issues],
            ["c1", "field_2", "x_03", "d3x"],
        )

        descriptions = {
            ("events", str(issue.column_name)): f"{issue.column_name} 的业务说明"
            for issue in report.issues
            if issue.column_name
        }
        completed = data_sources.validate_table_specs_readiness(
            table_specs,
            descriptions,
        )
        self.assertTrue(completed.is_ready)

    def test_missing_fields_enter_review_instead_of_becoming_active(self) -> None:
        report = data_sources.validate_table_specs_readiness(
            [{"local_name": "empty_table", "columns": []}],
        )
        self.assertTrue(report.needs_review)
        self.assertEqual(report.issues[0].code, "no_fields")

    def test_review_description_is_copied_to_analysis_snapshot(self) -> None:
        snapshot_path = self.root / "review_snapshot.db"
        with sqlite3.connect(snapshot_path) as connection:
            data_sources._initialize_snapshot_metadata(
                connection,
                "MySQL",
                "events",
            )
            data_sources._create_snapshot_table(
                connection,
                "events",
                [
                    {
                        "name": "c1",
                        "source_type": "varchar(20)",
                        "comment": "",
                    }
                ],
                field_descriptions={
                    ("events", "c1"): "设备所在区域",
                },
            )
            row = connection.execute(
                """
                SELECT description
                FROM _chatbi_column_metadata
                WHERE table_name = 'events' AND column_name = 'c1'
                """
            ).fetchone()
        self.assertEqual(row[0], "设备所在区域")


if __name__ == "__main__":
    unittest.main(verbosity=2)
