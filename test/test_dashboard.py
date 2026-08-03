"""Offline tests for dashboard persistence and recursive layout limits."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from application.dashboard_service import (
    DashboardService,
    find_node,
    iter_slots,
    move_widget,
    slot_split_capabilities,
    split_slot,
)
from auth.constants import ROLE_VIEWER
from auth.database import MetaDatabase
from auth.models import ValidationError
from auth.passwords import PasswordService
from auth.service import AuthService


class DashboardServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        upload_directory = root / "uploads"
        upload_directory.mkdir()
        self.auth = AuthService(
            MetaDatabase(root / "chatbi_meta.db"),
            upload_directory=upload_directory,
            password_service=PasswordService(
                time_cost=1,
                memory_cost=8192,
                parallelism=1,
            ),
        )
        self.admin = self.auth.bootstrap_admin(
            username="admin",
            display_name="系统管理员",
            password="AdminPassword123",
        )
        database_path = upload_directory / "business.db"
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "CREATE TABLE metrics (category TEXT, value REAL)"
            )
            connection.executemany(
                "INSERT INTO metrics VALUES (?, ?)",
                [("A", 10), ("B", 20)],
            )
        self.datasource = self.auth.register_datasource(
            self.admin.id,
            self.admin.session_version,
            database_path,
            "业务数据",
        )
        self.viewer = self.auth.create_user(
            self.admin.id,
            self.admin.session_version,
            username="viewer",
            display_name="查询用户",
            email="",
            password="ViewerPassword123",
            role_code=ROLE_VIEWER,
            datasource_ids=[self.datasource.id],
            must_change_password=False,
        )
        self.dashboard = DashboardService(self.auth)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_default_layout_is_four_two_one_and_small_cell_splits_once(self) -> None:
        dashboard = self.dashboard.get_dashboard(
            self.viewer.id,
            self.viewer.session_version,
        )
        root = dashboard.layout
        self.assertEqual(
            [len(row.get("children", [])) for row in root["children"][:2]],
            [4, 2],
        )
        self.assertEqual(root["children"][2]["kind"], "slot")

        small_slot_id = root["children"][0]["children"][0]["id"]
        capabilities = slot_split_capabilities(root)
        self.assertEqual(
            set(capabilities[small_slot_id]),
            {"top", "bottom"},
        )
        once = split_slot(root, small_slot_id, "bottom")
        split = find_node(once, small_slot_id)
        self.assertIsNone(split)
        first_minimum_slot = once["children"][0]["children"][0][
            "children"
        ][0]
        self.assertEqual(
            slot_split_capabilities(once)[first_minimum_slot["id"]],
            [],
        )
        with self.assertRaises(ValidationError):
            split_slot(once, first_minimum_slot["id"], "bottom")
        with self.assertRaises(ValidationError):
            split_slot(root, small_slot_id, "right")

    def test_saved_query_is_added_and_layout_move_is_persisted(self) -> None:
        saved = self.dashboard.save_query(
            user_id=self.viewer.id,
            session_version=self.viewer.session_version,
            datasource_id=self.datasource.id,
            datasource_version=self.datasource.version,
            title="分类指标",
            question="按分类统计指标",
            sql_query="SELECT category, value FROM metrics",
            visualization_spec={
                "chart_type": "bar",
                "x_column": "category",
                "y_column": "value",
            },
        )
        dashboard, placed = self.dashboard.add_query_to_dashboard(
            self.viewer.id,
            self.viewer.session_version,
            saved.id,
        )
        self.assertTrue(placed)
        slots = list(iter_slots(dashboard.layout))
        source = next(slot for slot in slots if slot["widget_id"] == saved.id)
        target = next(slot for slot in slots if slot["widget_id"] is None)
        moved = move_widget(dashboard.layout, source["id"], target["id"])
        persisted = self.dashboard.save_layout(
            self.viewer.id,
            self.viewer.session_version,
            moved,
        )
        moved_target = find_node(persisted.layout, target["id"])
        self.assertEqual(moved_target["widget_id"], saved.id)

    def test_non_read_only_query_cannot_be_saved(self) -> None:
        with self.assertRaises(ValidationError):
            self.dashboard.save_query(
                user_id=self.viewer.id,
                session_version=self.viewer.session_version,
                datasource_id=self.datasource.id,
                datasource_version=self.datasource.version,
                title="危险查询",
                question="删除数据",
                sql_query="DELETE FROM metrics",
                visualization_spec={"chart_type": "none"},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
