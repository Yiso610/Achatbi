"""Regression tests for dashboard page session-state behavior."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch


os.environ["LLM_PROVIDER"] = "qwen"
os.environ["DASHSCOPE_API_KEY"] = "test-key-for-offline-dashboard-tests"
for _chain_variable in (
    "ROUTER_MODEL_CHAIN",
    "SQL_GENERATOR_MODEL_CHAIN",
    "REFLECTOR_MODEL_CHAIN",
    "VISUALIZER_MODEL_CHAIN",
):
    os.environ[_chain_variable] = ""


import dashboard_ui
from application.dashboard_service import default_dashboard_layout
from infrastructure.data_sources import RemoteSyncResult


class DashboardUiTestCase(unittest.TestCase):
    def test_query_picker_request_is_consumed_once(self) -> None:
        user = SimpleNamespace(id=37)
        picker_key = dashboard_ui._picker_key(user.id)
        session_state = {picker_key: "slot-a"}

        with (
            patch.object(dashboard_ui.st, "session_state", session_state),
            patch.object(dashboard_ui, "_render_query_picker") as picker,
            patch.object(dashboard_ui, "_render_dashboard_fragment"),
        ):
            dashboard_ui.render_dashboard_page(Mock(), user)

        self.assertNotIn(picker_key, session_state)
        picker.assert_called_once_with(unittest.mock.ANY, user, "slot-a")

    def test_resize_at_minimum_size_is_silently_ignored(self) -> None:
        user = SimpleNamespace(id=38, session_version=1)
        layout = default_dashboard_layout()
        split_id = layout["children"][0]["id"]
        session_state = {
            dashboard_ui._draft_key(user.id): deepcopy(layout),
        }
        event = json.dumps(
            {
                "action": "resize",
                "event_id": "resize-too-small",
                "split_id": split_id,
                "sizes": [0.01, 1, 1, 1],
            }
        )

        with (
            patch.object(dashboard_ui.st, "session_state", session_state),
            patch.object(dashboard_ui.st, "rerun"),
        ):
            dashboard_ui._apply_canvas_event(
                event,
                Mock(),
                user,
                layout,
            )

        self.assertNotIn("dashboard_notice", session_state)
        self.assertEqual(
            session_state[dashboard_ui._draft_key(user.id)],
            layout,
        )

    def test_reconnect_event_requests_password_dialog(self) -> None:
        user = SimpleNamespace(id=38, session_version=1)
        layout = default_dashboard_layout()
        session_state = {}
        event = json.dumps(
            {
                "action": "reconnect",
                "event_id": "reconnect-click",
            }
        )

        with (
            patch.object(dashboard_ui.st, "session_state", session_state),
            patch.object(dashboard_ui.st, "rerun"),
        ):
            dashboard_ui._apply_canvas_event(
                event,
                Mock(),
                user,
                layout,
            )

        self.assertTrue(
            session_state[dashboard_ui._reconnect_key(user.id)]
        )

    def test_dashboard_syncs_each_remote_source_before_querying(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "snapshot.db"
            database_path.write_bytes(b"snapshot")
            credentials = dashboard_ui.RemoteDatabaseCredentials(
                database_type="MySQL",
                host="10.0.0.8",
                port=3306,
                user="reader",
                password="secret",
                database="sales",
            )
            user = SimpleNamespace(id=39, session_version=1)
            query = SimpleNamespace(
                id=7,
                datasource_id=4,
                title="销售趋势",
                datasource_name="销售库",
                sql_query="SELECT 1 AS value",
                visualization_spec={"chart_type": "none"},
            )
            source = SimpleNamespace(
                id=4,
                version=1,
                display_name="销售库",
                path=database_path,
            )
            service = Mock()
            service.queries_by_id.return_value = {7: query}
            auth_service = Mock()
            auth_service.resolve_authorized_datasource.return_value = source
            session_state = {"remote_sync_credentials": {4: credentials}}
            sync_result = RemoteSyncResult(
                attempted=True,
                changed=True,
                rows_synced=2,
                data_version=2,
                synced_at="2026-08-04T10:05:00",
                message="已同步 2 条变更记录。",
            )

            with (
                patch.object(dashboard_ui.st, "session_state", session_state),
                patch.object(
                    dashboard_ui,
                    "snapshot_sync_status",
                    side_effect=[
                        {
                            "remote": True,
                            "enabled": True,
                            "data_version": 1,
                        },
                        {
                            "remote": True,
                            "enabled": True,
                            "data_version": 2,
                        },
                    ],
                ),
                patch.object(
                    dashboard_ui,
                    "sync_remote_database_snapshot",
                    return_value=sync_result,
                ) as sync,
                patch.object(
                    dashboard_ui,
                    "_execute_dashboard_query",
                    return_value=([{"value": 1}], None, "10:05:01"),
                ) as execute,
            ):
                payload, refresh_times, summary = (
                    dashboard_ui._query_payload(
                        service,
                        auth_service,
                        user,
                        {"id": "slot", "kind": "slot", "widget_id": 7},
                    )
                )

        sync.assert_called_once_with(
            database_path,
            credentials,
            minimum_interval_seconds=dashboard_ui.REFRESH_SECONDS,
        )
        self.assertEqual(execute.call_args.args[4], 2)
        self.assertEqual(payload["7"]["rows"], [{"value": 1}])
        self.assertEqual(refresh_times, ["10:05:01"])
        self.assertEqual(summary["status"], "ok")
        self.assertIn("10:05:00", summary["message"])

    def test_missing_credentials_offers_quick_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "snapshot.db"
            database_path.write_bytes(b"snapshot")
            user = SimpleNamespace(id=40, session_version=1)
            query = SimpleNamespace(
                id=8,
                datasource_id=5,
                title="销售趋势",
                datasource_name="销售库",
                sql_query="SELECT 1 AS value",
                visualization_spec={"chart_type": "none"},
            )
            source = SimpleNamespace(
                id=5,
                version=1,
                display_name="销售库",
                path=database_path,
            )
            service = Mock()
            service.queries_by_id.return_value = {8: query}
            auth_service = Mock()
            auth_service.resolve_authorized_datasource.return_value = source

            with (
                patch.object(dashboard_ui.st, "session_state", {}),
                patch.object(
                    dashboard_ui,
                    "snapshot_sync_status",
                    side_effect=[
                        {
                            "remote": True,
                            "enabled": True,
                            "data_version": 1,
                        },
                        {
                            "remote": True,
                            "enabled": True,
                            "data_version": 1,
                        },
                    ],
                ),
                patch.object(
                    dashboard_ui,
                    "_execute_dashboard_query",
                    return_value=([{"value": 1}], None, "10:05:01"),
                ),
            ):
                _, _, summary = dashboard_ui._query_payload(
                    service,
                    auth_service,
                    user,
                    {"id": "slot", "kind": "slot", "widget_id": 8},
                )

        self.assertEqual(summary["status"], "warning")
        self.assertEqual(
            summary["reconnect_sources"],
            [{"id": 5, "name": "销售库"}],
        )
        self.assertIn("缺少当前会话凭据", summary["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
