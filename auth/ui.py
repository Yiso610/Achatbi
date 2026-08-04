"""Streamlit UI helpers for login, session handling, users, and audits."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Literal

import streamlit as st

from auth.constants import (
    MANAGE_DATASOURCE_ACCESS,
    MANAGE_USERS,
    ROLE_ADMIN,
    ROLE_LABELS,
    VIEW_AUDIT_LOGS,
)
from auth.models import AuthError, AuthenticationError, User
from auth.service import AuthService


AUTH_USER_ID_KEY = "auth_user_id"
AUTH_SESSION_VERSION_KEY = "auth_session_version"
AUTH_LAST_ACTIVITY_KEY = "auth_last_activity"

BUSINESS_SESSION_KEYS = {
    "results",
    "results_owner_user_id",
    "results_datasource_id",
    "results_datasource_version",
    "active_datasource_id",
    "active_database_path",
    "question_draft",
    "queued_question",
    "remote_database_catalog",
    "remote_sync_credentials",
    "database_notice",
    "remote_database_type",
    "mysql_server",
    "mysql_user",
    "mysql_password",
    "mysql_database_name",
    "postgresql_server",
    "postgresql_user",
    "postgresql_password",
    "postgresql_database_name",
    "create_username",
    "create_display_name",
    "create_email",
    "create_role",
    "create_password",
    "create_force_change",
    "create_datasource_ids",
    "login_username",
    "login_password",
    "auth_sensitive_keys_to_clear",
}
BUSINESS_SESSION_PREFIXES = (
    "dashboard_reconnect_password_",
    "edit_name_",
    "edit_email_",
    "edit_role_",
    "edit_status_",
    "grant_ids_",
    "reset_value_",
    "reset_confirmation_",
)

AUDIT_ACTION_LABELS = {
    "bootstrap_admin": "初始化管理员",
    "login": "登录",
    "logout": "退出登录",
    "session_timeout": "会话超时",
    "change_password": "修改密码",
    "create_user": "新增账号",
    "update_user": "更新账号",
    "reset_password": "重置密码",
    "update_datasource_access": "更新数据源授权",
    "import_datasource": "导入数据源",
    "import_datasource_cleanup": "清理旧数据源版本",
    "sync_datasource": "同步数据源",
    "reconnect_datasource": "重新连接数据源",
    "delete_datasource": "删除数据源",
    "delete_datasource_cleanup": "清理已删除数据源",
    "query_data": "查询数据",
    "datasource_access": "访问数据源",
    "permission_check": "权限校验",
}

OUTCOME_LABELS = {
    "success": "成功",
    "failure": "失败",
    "denied": "拒绝",
}


def _markdown_cell(value: object) -> str:
    """Keep generated Markdown tables well-formed without using Arrow."""
    text = str(value if value is not None else "").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()#+.!<>|\-])", r"\\\1", text)


def _render_markdown_table(
    headers: list[str],
    rows: list[list[object]],
) -> None:
    if not rows:
        return
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend(
        "| " + " | ".join(_markdown_cell(cell) for cell in row) + " |"
        for row in rows
    )
    st.markdown("\n".join(lines))


def _session_timeout_seconds() -> int:
    try:
        minutes = int(os.getenv("AUTH_SESSION_TIMEOUT_MINUTES", "30"))
    except ValueError:
        minutes = 30
    return min(max(minutes, 5), 1440) * 60


def clear_user_session() -> None:
    """Remove auth and all user-bound data from the Streamlit session."""
    explicit_keys = BUSINESS_SESSION_KEYS | {
        AUTH_USER_ID_KEY,
        AUTH_SESSION_VERSION_KEY,
        AUTH_LAST_ACTIVITY_KEY,
        "auth_navigation",
    }
    dynamic_keys = {
        key
        for key in list(st.session_state)
        if str(key).startswith(BUSINESS_SESSION_PREFIXES)
    }
    for key in explicit_keys | dynamic_keys:
        st.session_state.pop(key, None)


def _start_user_session(user: User) -> None:
    clear_user_session()
    st.session_state[AUTH_USER_ID_KEY] = user.id
    st.session_state[AUTH_SESSION_VERSION_KEY] = user.session_version
    st.session_state[AUTH_LAST_ACTIVITY_KEY] = time.time()


def _current_user(service: AuthService) -> User | None:
    user_id = st.session_state.get(AUTH_USER_ID_KEY)
    session_version = st.session_state.get(AUTH_SESSION_VERSION_KEY)
    last_activity = st.session_state.get(AUTH_LAST_ACTIVITY_KEY)
    if user_id is None or session_version is None or last_activity is None:
        return None

    if time.time() - float(last_activity) > _session_timeout_seconds():
        try:
            service.record_audit(
                actor_user_id=int(user_id),
                action="session_timeout",
                outcome="success",
                target_type="user",
                target_id=int(user_id),
            )
        finally:
            clear_user_session()
            st.session_state.auth_notice = "登录已超时，请重新登录。"
        return None

    try:
        user = service.validate_session(
            int(user_id),
            int(session_version),
        )
    except AuthenticationError:
        clear_user_session()
        st.session_state.auth_notice = "账号状态或权限已变化，请重新登录。"
        return None

    st.session_state[AUTH_LAST_ACTIVITY_KEY] = time.time()
    return user


def _render_login(service: AuthService) -> None:
    st.title("智能数据分析平台")
    notice = st.session_state.pop("auth_notice", None)
    if notice:
        st.info(notice)

    left, center, right = st.columns([1, 1.15, 1])
    del left, right
    with center:
        with st.form("local_login_form", clear_on_submit=True):
            username = st.text_input(
                "用户名",
                key="login_username",
                max_chars=50,
            )
            password = st.text_input(
                "密码",
                type="password",
                key="login_password",
                max_chars=128,
            )
            submitted = st.form_submit_button(
                "登录",
                type="primary",
                use_container_width=True,
            )

        if submitted:
            user = service.authenticate(username, password)
            if user is None:
                st.error("用户名或密码错误，或账号暂不可用。")
                return
            _start_user_session(user)
            st.rerun()


def _render_uninitialized() -> None:
    st.title("智能数据分析平台")
    st.warning("权限系统尚未初始化，平台已拒绝匿名访问。")
    st.write("请在项目目录的本机终端创建首个系统管理员：")
    st.code("python scripts/manage_auth.py init-admin", language="bash")
    st.caption(
        "首个管理员只能从服务器本机初始化，避免部署后被网页访客抢先创建。"
    )


def _render_forced_password_change(
    service: AuthService,
    user: User,
) -> None:
    st.title("首次登录：修改密码")
    st.info("管理员为你设置了临时密码。继续使用前，请先设置新密码。")
    with st.form(
        "forced_password_change_form",
        clear_on_submit=True,
    ):
        current_password = st.text_input(
            "当前临时密码",
            type="password",
            max_chars=128,
        )
        new_password = st.text_input(
            "新密码",
            type="password",
            max_chars=128,
            help="至少 10 个字符，并同时包含字母和数字。",
        )
        confirmation = st.text_input(
            "确认新密码",
            type="password",
            max_chars=128,
        )
        submitted = st.form_submit_button(
            "保存新密码",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        if new_password != confirmation:
            st.error("两次输入的新密码不一致。")
            return
        try:
            updated = service.change_own_password(
                user.id,
                current_password,
                new_password,
            )
        except AuthError as error:
            st.error(str(error))
            return
        _start_user_session(updated)
        st.session_state.auth_notice = "密码修改成功。"
        st.rerun()

    if st.button("退出登录", key="forced_password_logout"):
        clear_user_session()
        st.rerun()


def render_auth_gate(service: AuthService) -> User | None:
    """Render the appropriate auth screen and return a validated user."""
    if not service.has_users():
        _render_uninitialized()
        return None

    user = _current_user(service)
    if user is None:
        _render_login(service)
        return None
    if user.must_change_password:
        _render_forced_password_change(service, user)
        return None
    return user


def render_account_sidebar(
    service: AuthService,
    user: User,
) -> Literal["query", "users", "audit"]:
    """Render account identity and secondary administration navigation."""
    role_label = ROLE_LABELS.get(user.primary_role, user.primary_role)
    with st.sidebar:
        st.markdown(f"**{user.display_name}**")
        st.caption(f"{user.username} · {role_label}")
        if st.button(
            "退出登录",
            key="logout_button",
            use_container_width=True,
        ):
            try:
                service.record_audit(
                    actor_user_id=user.id,
                    action="logout",
                    outcome="success",
                    target_type="user",
                    target_id=user.id,
                )
            finally:
                clear_user_session()
                st.session_state.auth_notice = "已安全退出登录。"
            st.rerun()
        st.divider()

        current_page = str(
            st.session_state.get("account_page", "query")
        )
        if current_page != "query":
            if st.button(
                "← 返回工作台",
                key="return_to_workspace",
                use_container_width=True,
            ):
                st.session_state.account_page = "query"
                st.rerun()

        if service.has_permission(user.id, MANAGE_USERS):
            if st.button(
                "用户与权限管理",
                key="open_user_management",
                use_container_width=True,
            ):
                st.session_state.account_page = "users"
                st.rerun()
        if service.has_permission(user.id, VIEW_AUDIT_LOGS):
            if st.button(
                "审计日志",
                key="open_audit_logs",
                use_container_width=True,
            ):
                st.session_state.account_page = "audit"
                st.rerun()

        allowed_pages = {"query"}
        if service.has_permission(user.id, MANAGE_USERS):
            allowed_pages.add("users")
        if service.has_permission(user.id, VIEW_AUDIT_LOGS):
            allowed_pages.add("audit")
        if current_page not in allowed_pages:
            current_page = "query"
            st.session_state.account_page = "query"
    return current_page


def _role_format(role_code: str) -> str:
    return ROLE_LABELS.get(role_code, role_code)


def render_user_management(service: AuthService, actor: User) -> None:
    """Render admin-only account creation, edits, grants, and resets."""
    service.require_permission(actor.id, MANAGE_USERS)
    for sensitive_key in st.session_state.pop(
        "auth_sensitive_keys_to_clear",
        [],
    ):
        st.session_state.pop(str(sensitive_key), None)
    st.title("用户与权限管理")
    notice = st.session_state.pop("user_admin_notice", None)
    if notice:
        st.success(notice)

    roles = service.list_roles()
    role_codes = [code for code, _ in roles]
    datasources = (
        service.list_all_datasources(actor.id)
        if service.has_permission(actor.id, MANAGE_DATASOURCE_ACCESS)
        else []
    )
    datasource_labels = {
        datasource.id: datasource.display_name
        for datasource in datasources
    }

    with st.expander("新增账号", expanded=False):
        with st.form("create_user_form", clear_on_submit=True):
            first_column, second_column = st.columns(2)
            with first_column:
                username = st.text_input(
                    "用户名",
                    key="create_username",
                    help="3–50 位字母、数字、点、下划线或短横线。",
                )
                display_name = st.text_input(
                    "显示名称",
                    key="create_display_name",
                )
                email = st.text_input(
                    "邮箱（可选）",
                    key="create_email",
                )
            with second_column:
                role_code = st.selectbox(
                    "账号类型",
                    role_codes,
                    format_func=_role_format,
                    key="create_role",
                )
                temporary_password = st.text_input(
                    "临时密码",
                    type="password",
                    key="create_password",
                    help="至少 10 个字符，并同时包含字母和数字。",
                )
                force_change = st.checkbox(
                    "首次登录强制修改密码",
                    value=True,
                    key="create_force_change",
                )
            granted_ids = st.multiselect(
                "可访问的数据源",
                options=list(datasource_labels),
                format_func=lambda item: datasource_labels[item],
                key="create_datasource_ids",
                help="系统管理员始终可以访问全部数据源。",
            )
            create_submitted = st.form_submit_button(
                "创建账号",
                type="primary",
                use_container_width=True,
            )

        if create_submitted:
            try:
                created = service.create_user(
                    actor.id,
                    actor.session_version,
                    username=username,
                    display_name=display_name,
                    email=email,
                    password=temporary_password,
                    role_code=role_code,
                    datasource_ids=granted_ids,
                    must_change_password=force_change,
                )
            except AuthError as error:
                st.error(str(error))
            else:
                st.session_state.auth_sensitive_keys_to_clear = [
                    "create_password",
                ]
                st.session_state.user_admin_notice = (
                    f"账号“{created.username}”已创建。"
                )
                st.rerun()

    users = service.list_users(actor.id)
    summary_rows = [
        [
            user.username,
            user.display_name,
            _role_format(user.primary_role),
            "启用" if user.is_active else "禁用",
            "是" if user.must_change_password else "否",
            user.last_login_at or "从未登录",
        ]
        for user in users
    ]
    _render_markdown_table(
        ["用户名", "显示名称", "账号类型", "状态", "首次改密", "最近登录"],
        summary_rows,
    )

    st.subheader("编辑账号")
    for target in users:
        status_label = "启用" if target.is_active else "禁用"
        expander_label = (
            f"{target.display_name}（{target.username} · "
            f"{_role_format(target.primary_role)} · {status_label}）"
        )
        with st.expander(expander_label, expanded=False):
            with st.form(f"edit_user_{target.id}"):
                profile_column, access_column = st.columns(2)
                with profile_column:
                    edited_name = st.text_input(
                        "显示名称",
                        value=target.display_name,
                        key=f"edit_name_{target.id}",
                    )
                    edited_email = st.text_input(
                        "邮箱",
                        value=target.email,
                        key=f"edit_email_{target.id}",
                    )
                with access_column:
                    current_role_index = (
                        role_codes.index(target.primary_role)
                        if target.primary_role in role_codes
                        else 0
                    )
                    edited_role = st.selectbox(
                        "账号类型",
                        role_codes,
                        index=current_role_index,
                        format_func=_role_format,
                        key=f"edit_role_{target.id}",
                    )
                    edited_status_label = st.selectbox(
                        "账号状态",
                        ["启用", "禁用"],
                        index=0 if target.is_active else 1,
                        key=f"edit_status_{target.id}",
                    )
                update_submitted = st.form_submit_button(
                    "保存账号资料",
                    use_container_width=True,
                )

            if update_submitted:
                try:
                    service.update_user(
                        actor.id,
                        actor.session_version,
                        target.id,
                        display_name=edited_name,
                        email=edited_email,
                        role_code=edited_role,
                        status=(
                            "active"
                            if edited_status_label == "启用"
                            else "disabled"
                        ),
                    )
                except AuthError as error:
                    st.error(str(error))
                else:
                    st.session_state.user_admin_notice = (
                        f"账号“{target.username}”已更新。"
                    )
                    st.rerun()

            if target.primary_role == ROLE_ADMIN:
                st.info("系统管理员自动拥有全部数据源访问权限。")
            else:
                current_grants = service.datasource_ids_for_user(
                    actor.id,
                    target.id,
                )
                with st.form(f"datasource_access_{target.id}"):
                    selected_ids = st.multiselect(
                        "可访问的数据源",
                        options=list(datasource_labels),
                        default=[
                            datasource_id
                            for datasource_id in current_grants
                            if datasource_id in datasource_labels
                        ],
                        format_func=lambda item: datasource_labels[item],
                        key=f"grant_ids_{target.id}",
                    )
                    grant_submitted = st.form_submit_button(
                        "保存数据源授权",
                        use_container_width=True,
                    )
                if grant_submitted:
                    try:
                        service.replace_datasource_access(
                            actor.id,
                            actor.session_version,
                            target.id,
                            selected_ids,
                        )
                    except AuthError as error:
                        st.error(str(error))
                    else:
                        st.session_state.user_admin_notice = (
                            f"账号“{target.username}”的数据源授权已更新。"
                        )
                        st.rerun()

            with st.form(
                f"reset_password_{target.id}",
                clear_on_submit=True,
            ):
                new_password = st.text_input(
                    "新临时密码",
                    type="password",
                    key=f"reset_value_{target.id}",
                    help="保存后，目标账号的现有会话立即失效。",
                )
                password_confirmation = st.text_input(
                    "确认临时密码",
                    type="password",
                    key=f"reset_confirmation_{target.id}",
                )
                reset_submitted = st.form_submit_button(
                    "重置密码",
                    use_container_width=True,
                )
            if reset_submitted:
                if new_password != password_confirmation:
                    st.error("两次输入的临时密码不一致。")
                else:
                    try:
                        service.reset_password(
                            actor.id,
                            actor.session_version,
                            target.id,
                            new_password,
                        )
                    except AuthError as error:
                        st.error(str(error))
                    else:
                        st.session_state.auth_sensitive_keys_to_clear = [
                            f"reset_value_{target.id}",
                            f"reset_confirmation_{target.id}",
                        ]
                        st.session_state.user_admin_notice = (
                            f"账号“{target.username}”的密码已重置。"
                        )
                        st.rerun()


def render_audit_logs(service: AuthService, actor: User) -> None:
    """Render the latest sanitized authorization and operation events."""
    service.require_permission(actor.id, VIEW_AUDIT_LOGS)
    st.title("审计日志")
    filter_column, limit_column = st.columns(2)
    with filter_column:
        outcome_label = st.selectbox(
            "结果",
            ["全部", "成功", "失败", "拒绝"],
            key="audit_outcome",
        )
    with limit_column:
        limit = st.select_slider(
            "显示条数",
            options=[50, 100, 200, 500],
            value=200,
            key="audit_limit",
        )
    outcome_by_label = {
        "全部": None,
        "成功": "success",
        "失败": "failure",
        "拒绝": "denied",
    }
    records = service.list_audit_logs(
        actor.id,
        limit=limit,
        outcome=outcome_by_label[outcome_label],
    )
    rows = [
        [
            record.created_at,
            record.actor_username,
            AUDIT_ACTION_LABELS.get(record.action, record.action),
            OUTCOME_LABELS.get(record.outcome, record.outcome),
            record.datasource_name,
            (
                f"{record.target_type}:{record.target_id}"
                if record.target_type or record.target_id
                else ""
            ),
            (
                json.dumps(
                    record.details,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if record.details
                else ""
            ),
        ]
        for record in records
    ]
    if rows:
        _render_markdown_table(
            [
                "时间（UTC）",
                "操作者",
                "操作",
                "结果",
                "数据源",
                "目标",
                "详情",
            ],
            rows,
        )
    else:
        st.info("暂无符合条件的审计记录。")
    st.caption(
        "审计日志不保存密码、数据库连接凭据、查询结果或完整自然语言问题。"
    )
