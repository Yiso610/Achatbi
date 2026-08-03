"""Offline Streamlit smoke tests for role-based page visibility."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest

from streamlit.testing.v1 import AppTest

from auth.constants import ROLE_DATA_MANAGER, ROLE_VIEWER
from auth.database import MetaDatabase
from auth.passwords import PasswordService
from auth.service import AuthService, get_auth_service


PROJECT_ROOT = Path(__file__).resolve().parent.parent

_OFFLINE_ENVIRONMENT = {
    "LLM_PROVIDER": "qwen",
    "DASHSCOPE_API_KEY": "sk-offline-auth-ui-test",
    "ROUTER_MODEL_CHAIN": "",
    "SQL_GENERATOR_MODEL_CHAIN": "",
    "REFLECTOR_MODEL_CHAIN": "",
    "VISUALIZER_MODEL_CHAIN": "",
    "LANGSMITH_API_KEY": "",
    "LANGCHAIN_API_KEY": "",
    "LANGSMITH_TRACING": "false",
    "LANGCHAIN_TRACING_V2": "false",
}


class AuthUiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        meta_path = Path(cls.temporary_directory.name) / "chatbi_meta.db"
        cls.original_environment = {
            name: os.environ.get(name)
            for name in _OFFLINE_ENVIRONMENT
        }
        os.environ.update(_OFFLINE_ENVIRONMENT)
        os.environ["CHATBI_META_DB_PATH"] = str(meta_path)
        get_auth_service.cache_clear()
        service = AuthService(
            MetaDatabase(meta_path),
            password_service=PasswordService(
                time_cost=1,
                memory_cost=8192,
                parallelism=1,
            ),
        )
        cls.admin = service.bootstrap_admin(
            username="admin",
            display_name="系统管理员",
            password="AdminPassword123",
        )
        cls.viewer = service.create_user(
            cls.admin.id,
            cls.admin.session_version,
            username="viewer",
            display_name="查询用户",
            email="",
            password="ViewerPassword123",
            role_code=ROLE_VIEWER,
            must_change_password=False,
        )
        cls.manager = service.create_user(
            cls.admin.id,
            cls.admin.session_version,
            username="manager",
            display_name="数据管理员",
            email="",
            password="ManagerPassword123",
            role_code=ROLE_DATA_MANAGER,
            must_change_password=False,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        get_auth_service.cache_clear()
        os.environ.pop("CHATBI_META_DB_PATH", None)
        for name, original_value in cls.original_environment.items():
            if original_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original_value
        cls.temporary_directory.cleanup()

    @staticmethod
    def _app_for(user_id: int, session_version: int) -> AppTest:
        app = AppTest.from_file(
            str(PROJECT_ROOT / "main.py"),
            default_timeout=20,
        )
        app.session_state["auth_user_id"] = user_id
        app.session_state["auth_session_version"] = session_version
        app.session_state["auth_last_activity"] = time.time()
        app.run()
        return app

    def test_admin_can_open_user_and_audit_pages(self) -> None:
        app = self._app_for(
            self.admin.id,
            self.admin.session_version,
        )
        self.assertFalse(app.exception)
        self.assertEqual(
            app.radio[0].options,
            ["智能问数", "可视化大屏"],
        )

        next(
            button
            for button in app.button
            if button.label == "用户与权限管理"
        ).click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.title[0].value, "用户与权限管理")
        self.assertIn("新增账号", [item.label for item in app.expander])

        next(
            button
            for button in app.button
            if button.label == "← 返回工作台"
        ).click().run()
        next(
            button
            for button in app.button
            if button.label == "审计日志"
        ).click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.title[0].value, "审计日志")

    def test_viewer_and_manager_controls_are_distinct(self) -> None:
        viewer_app = self._app_for(
            self.viewer.id,
            self.viewer.session_version,
        )
        self.assertFalse(viewer_app.exception)
        self.assertEqual(
            viewer_app.radio[0].options,
            ["智能问数", "可视化大屏"],
        )
        viewer_buttons = {button.label for button in viewer_app.button}
        self.assertNotIn("连接并读取数据库", viewer_buttons)
        self.assertNotIn("×", viewer_buttons)
        self.assertNotIn("用户与权限管理", viewer_buttons)

        manager_app = self._app_for(
            self.manager.id,
            self.manager.session_version,
        )
        self.assertFalse(manager_app.exception)
        self.assertEqual(
            manager_app.radio[0].options,
            ["智能问数", "可视化大屏"],
        )
        manager_buttons = {button.label for button in manager_app.button}
        self.assertIn("＋", manager_buttons)
        self.assertNotIn("×", manager_buttons)

        next(
            button for button in manager_app.button if button.label == "＋"
        ).click().run()
        self.assertFalse(manager_app.exception)
        add_type = next(
            item
            for item in manager_app.selectbox
            if item.label == "选择添加方式"
        )
        self.assertEqual(add_type.options, ["数据库", "Excel 文件"])
        self.assertIn(
            "连接并读取数据库",
            {button.label for button in manager_app.button},
        )



if __name__ == "__main__":
    unittest.main(verbosity=2)
