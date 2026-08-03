"""Regression tests for dashboard page session-state behavior."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from types import SimpleNamespace
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
