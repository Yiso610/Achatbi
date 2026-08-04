"""Offline tests for five-minute remote snapshot synchronization."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from infrastructure import data_sources


class _FakeRemoteCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._source_rows = rows
        self._rows: list[tuple[object, ...]] = []
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, query: str, parameters=()) -> None:
        del query
        rows = list(self._source_rows)
        if len(parameters) == 3:
            last_time, _, last_id = parameters
            rows = [
                row
                for row in rows
                if row[2] > last_time
                or (row[2] == last_time and row[0] > last_id)
            ]
            rows.sort(key=lambda row: (row[2], row[0]))
        elif len(parameters) == 1:
            rows = [row for row in rows if row[0] > parameters[0]]
            rows.sort(key=lambda row: row[0])
        self._rows = rows
        self._offset = 0

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        rows = self._rows[self._offset:self._offset + size]
        self._offset += len(rows)
        return rows


class _FakeRemoteConnection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.closed = False

    def cursor(self) -> _FakeRemoteCursor:
        return _FakeRemoteCursor(self.rows)

    def close(self) -> None:
        self.closed = True


class RemoteIncrementalSyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.upload_directory = Path(self.temporary_directory.name).resolve()
        self.directory_patch = patch.object(
            data_sources,
            "UPLOAD_DIRECTORY",
            self.upload_directory,
        )
        self.directory_patch.start()
        self.credentials = data_sources.RemoteDatabaseCredentials(
            database_type="MySQL",
            host="10.0.0.8",
            port=3306,
            user="reader",
            password="secret",
            database="sales",
        )
        self.snapshot_path = self.upload_directory / ".sales.snapshot.db"
        with sqlite3.connect(self.snapshot_path) as local:
            data_sources._initialize_snapshot_metadata(
                local,
                "MySQL",
                "sales",
                self.credentials,
            )
            local.execute(
                "UPDATE _chatbi_source_metadata "
                "SET value = '1000' WHERE key = 'last_sync_epoch'"
            )
            data_sources._initialize_remote_sync_schema(local)
            columns = [
                {
                    "name": "id",
                    "source_type": "bigint",
                    "primary_key": True,
                    "auto_increment": True,
                },
                {"name": "value", "source_type": "varchar(100)"},
                {"name": "updated_at", "source_type": "datetime"},
            ]
            data_sources._create_snapshot_table(local, "items", columns)
            local.execute(
                "INSERT INTO items VALUES (?, ?, ?)",
                (1, "old", "2026-08-04 10:00:00"),
            )
            local.execute(
                """
                INSERT INTO _chatbi_sync_state (
                    local_table_name,
                    source_schema,
                    source_table_name,
                    sync_mode,
                    primary_key_column,
                    cursor_column,
                    last_cursor_json,
                    last_primary_key_json,
                    last_synced_at,
                    rows_synced
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "items",
                    "sales",
                    "items",
                    "updated",
                    "id",
                    "updated_at",
                    '"2026-08-04 10:00:00"',
                    "1",
                    "2026-08-04T10:00:00",
                    1,
                ),
            )
        self.snapshot_path.chmod(0o600)

    def tearDown(self) -> None:
        self.directory_patch.stop()
        self.temporary_directory.cleanup()

    def test_strategy_prefers_timestamp_then_append_then_full(self) -> None:
        updated = [
            {
                "name": "id",
                "source_type": "bigint",
                "primary_key": True,
                "auto_increment": True,
            },
            {"name": "updated_at", "source_type": "timestamp"},
        ]
        append = [updated[0], {"name": "value", "source_type": "text"}]
        full = [{"name": "value", "source_type": "text"}]

        self.assertEqual(
            data_sources._remote_sync_strategy(updated),
            ("updated", "id", "updated_at"),
        )
        self.assertEqual(
            data_sources._remote_sync_strategy(append),
            ("append", "id", "id"),
        )
        self.assertEqual(
            data_sources._remote_sync_strategy(full),
            ("full", "", ""),
        )

    def test_reconnect_validates_password_and_returns_session_credentials(
        self,
    ) -> None:
        remote = Mock()
        with (
            patch.object(
                data_sources,
                "_remote_host_is_allowed",
                return_value=True,
            ),
            patch.object(
                data_sources,
                "_resolve_remote_host",
                side_effect=lambda host, port: host,
            ),
            patch.object(
                data_sources,
                "_connect_remote_snapshot",
                return_value=remote,
            ) as connect,
        ):
            credentials = (
                data_sources.reconnect_remote_database_snapshot(
                    self.snapshot_path,
                    "new-secret",
                )
            )

        self.assertEqual(credentials.database_type, "MySQL")
        self.assertEqual(credentials.host, "10.0.0.8")
        self.assertEqual(credentials.port, 3306)
        self.assertEqual(credentials.user, "reader")
        self.assertEqual(credentials.database, "sales")
        self.assertEqual(credentials.password, "new-secret")
        connect.assert_called_once_with(credentials)
        remote.close.assert_called_once_with()
        with sqlite3.connect(self.snapshot_path) as local:
            metadata_values = [
                str(row[0])
                for row in local.execute(
                    "SELECT value FROM _chatbi_source_metadata"
                ).fetchall()
            ]
        self.assertNotIn("new-secret", metadata_values)

    def test_reconnect_masks_password_in_connection_error(self) -> None:
        with (
            patch.object(
                data_sources,
                "_remote_host_is_allowed",
                return_value=True,
            ),
            patch.object(
                data_sources,
                "_resolve_remote_host",
                side_effect=lambda host, port: host,
            ),
            patch.object(
                data_sources,
                "_connect_remote_snapshot",
                side_effect=RuntimeError("access denied for new-secret"),
            ),
        ):
            with self.assertRaises(data_sources.DataSourceError) as context:
                data_sources.reconnect_remote_database_snapshot(
                    self.snapshot_path,
                    "new-secret",
                )

        self.assertNotIn("new-secret", str(context.exception))
        self.assertIn("******", str(context.exception))

    def test_due_sync_upserts_rows_and_advances_version(self) -> None:
        remote = _FakeRemoteConnection(
            [
                (1, "new", "2026-08-04 10:05:00"),
                (2, "created", "2026-08-04 10:06:00"),
            ]
        )
        with (
            patch.object(
                data_sources,
                "_connect_remote_snapshot",
                return_value=remote,
            ),
            patch.object(data_sources.time_module, "time", return_value=2000),
        ):
            result = data_sources.sync_remote_database_snapshot(
                self.snapshot_path,
                self.credentials,
                minimum_interval_seconds=300,
            )

        self.assertTrue(result.attempted)
        self.assertTrue(result.changed)
        self.assertEqual(result.rows_synced, 2)
        self.assertEqual(result.data_version, 2)
        self.assertTrue(remote.closed)
        with sqlite3.connect(self.snapshot_path) as local:
            rows = local.execute(
                "SELECT id, value, updated_at FROM items ORDER BY id"
            ).fetchall()
            metadata = dict(
                local.execute(
                    "SELECT key, value FROM _chatbi_source_metadata"
                ).fetchall()
            )
            state = local.execute(
                "SELECT last_cursor_json, last_primary_key_json "
                "FROM _chatbi_sync_state WHERE local_table_name = 'items'"
            ).fetchone()
        self.assertEqual(
            rows,
            [
                (1, "new", "2026-08-04 10:05:00"),
                (2, "created", "2026-08-04 10:06:00"),
            ],
        )
        self.assertEqual(metadata["data_version"], "2")
        self.assertEqual(state, ('"2026-08-04 10:06:00"', "2"))

        connector = Mock()
        with (
            patch.object(
                data_sources,
                "_connect_remote_snapshot",
                connector,
            ),
            patch.object(data_sources.time_module, "time", return_value=2100),
        ):
            skipped = data_sources.sync_remote_database_snapshot(
                self.snapshot_path,
                self.credentials,
                minimum_interval_seconds=300,
            )
        self.assertFalse(skipped.attempted)
        connector.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
