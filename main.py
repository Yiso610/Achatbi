"""
Production-grade features:
- Responsive layout
- Loading states
- Error boundaries
- Session state management
- Professional styling
"""

import html
import re

import streamlit as st
import pandas as pd
import plotly.express as px
from pathlib import Path
from typing import Dict, Any, List

from agents.graph import run_agent
from infrastructure.data_sources import (
    DataSourceError,
    database_display_name,
    delete_database_source,
    inspect_sqlite_database,
    list_database_sources,
    list_remote_databases,
    snapshot_remote_database,
)
from infrastructure.db_manager import DatabaseManager
from infrastructure.validators import InputValidator, QueryResultValidator
from infrastructure.langsmith_config import setup_langsmith


# Initialize validators
input_validator = InputValidator()
result_validator = QueryResultValidator()

# Setup LangSmith tracing for observability
setup_langsmith(project_name="text2sql-production", enabled=True)



# Page configuration
st.set_page_config(
    page_title="智能数据分析平台",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed"
)


# Custom CSS for modern styling
st.markdown("""
<style>
    /* Main container */
    .main {
        padding: 2rem;
    }
    
    /* Headers */
    h1 {
        color: #1f77b4;
        font-weight: 700;
        margin-bottom: 1.5rem;
        text-align: center;
    }
    
    h2 {
        color: #2c3e50;
        font-weight: 600;
        margin-top: 2rem;
        margin-bottom: 1rem;
    }
    
    h3 {
        color: #34495e;
        font-weight: 500;
    }
    
    /* Code blocks */
    .stCodeBlock {
        background-color: #f8f9fa;
        border-left: 4px solid #1f77b4;
        padding: 1rem;
        border-radius: 0.5rem;
    }
    
    /* Success messages */
    .success-box {
        background-color: #d4edda;
        border-left: 4px solid #28a745;
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 1rem 0;
    }
    
    /* Error messages */
    .error-box {
        background-color: #f8d7da;
        border-left: 4px solid #dc3545;
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 1rem 0;
    }
    
    /* Info boxes */
    .info-box {
        background-color: #d1ecf1;
        border-left: 4px solid #17a2b8;
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 1rem 0;
    }
    
    /* Buttons */
    .stButton > button {
        background-color: #1f77b4;
        color: white;
        font-weight: 600;
        padding: 0.5rem 2rem;
        border-radius: 0.5rem;
        border: none;
        transition: all 0.3s ease;
    }
    
    .stButton > button:hover {
        background-color: #155a8a;
        transform: translateY(-2px);
        box-shadow: 0 4px 8px rgba(0,0,0,0.2);
    }
    
    /* Current database context */
    .database-context {
        color: #6b7280;
        font-size: 0.98rem;
        text-align: center;
        margin: -0.65rem 0 0.9rem;
    }

    /* GPT-style question composer */
    div[data-testid="stForm"] {
        background: #ffffff;
        border: 1px solid #d9dce3;
        border-radius: 1.15rem;
        box-shadow: 0 4px 16px rgba(15, 23, 42, 0.07);
        padding: 6px 8px 6px 16px;
        transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }

    div[data-testid="stForm"]:focus-within {
        border-color: #aeb4bf;
        box-shadow: 0 0 0 3px rgba(17, 24, 39, 0.06),
                    0 4px 16px rgba(15, 23, 42, 0.07);
    }

    div[data-testid="stForm"] div[data-testid="stTextInput"]
    [data-baseweb="base-input"] {
        min-height: 52px;
        background: transparent;
        border: 0 !important;
        border-radius: 0;
        box-shadow: none !important;
    }

    div[data-testid="stForm"] div[data-testid="stTextInputRootElement"] {
        min-height: 52px !important;
        height: 52px !important;
        background: transparent !important;
        border: 0 !important;
        border-radius: 0 !important;
        box-shadow: none !important;
        align-items: center !important;
    }

    div[data-testid="stForm"] div[data-testid="stTextInput"]
    [data-baseweb="base-input"]:focus-within {
        border: 0 !important;
        box-shadow: none !important;
    }

    div[data-testid="stForm"] div[data-testid="stTextInput"] input {
        min-height: 52px !important;
        height: 52px !important;
        padding: 0 4px !important;
        font-size: 1.08rem !important;
        line-height: 52px !important;
        background: transparent !important;
    }

    div[data-testid="stForm"] div[data-testid="InputInstructions"] {
        display: none !important;
    }

    div[data-testid="stFormSubmitButton"] {
        display: flex;
        align-items: center;
        justify-content: flex-end;
    }

    div[data-testid="stFormSubmitButton"] button,
    div[data-testid="stFormSubmitButton"] button:hover,
    div[data-testid="stFormSubmitButton"] button:focus,
    div[data-testid="stFormSubmitButton"] button:active {
        width: 42px !important;
        min-width: 42px !important;
        height: 42px !important;
        min-height: 42px !important;
        padding: 0 !important;
        background: #111827 !important;
        border: 0 !important;
        border-radius: 999px !important;
        color: #ffffff !important;
        box-shadow: none !important;
        transform: none !important;
    }

    div[data-testid="stFormSubmitButton"] button p {
        color: #ffffff !important;
        font-size: 1.3rem !important;
        font-weight: 600;
        line-height: 1;
    }

    div[data-testid="stForm"]:has(input:placeholder-shown)
    div[data-testid="stFormSubmitButton"] button {
        background: #d1d5db !important;
    }

    div[data-testid="stForm"]:has(input:placeholder-shown)
    div[data-testid="stFormSubmitButton"] button p {
        color: #ffffff !important;
    }

    /* Schema-based suggested questions */
    .suggestion-label {
        color: #6b7280;
        font-size: 0.95rem;
        margin: 1.05rem 0 0.55rem;
    }

    div[class*="st-key-suggested_question_"] .stButton > button,
    div[class*="st-key-suggested_question_"] .stButton > button:hover,
    div[class*="st-key-suggested_question_"] .stButton > button:focus,
    div[class*="st-key-suggested_question_"] .stButton > button:active {
        min-height: 54px;
        width: 100%;
        background: #ffffff !important;
        color: #374151 !important;
        border: 1px solid #e1e4e9 !important;
        border-radius: 0.9rem !important;
        box-shadow: none !important;
        padding: 0.75rem 1rem !important;
        transform: none !important;
        text-align: left;
        white-space: normal;
    }

    div[class*="st-key-suggested_question_"] {
        margin-bottom: 0.25rem;
    }

    div[class*="st-key-suggested_question_"] .stButton > button:hover {
        background: #f7f7f8 !important;
        border-color: #cfd3da !important;
    }

    div[class*="st-key-suggested_question_"] .stButton > button p {
        color: #374151 !important;
        line-height: 1.45;
        text-align: left;
        white-space: normal;
    }

    /* Compact data-catalog sidebar */
    section[data-testid="stSidebar"] [data-testid="stSidebarCollapseButton"] {
        display: flex !important;
        position: absolute !important;
        top: 20px;
        right: 20px;
        z-index: 20;
        width: 36px;
        height: 36px;
        align-items: center;
        justify-content: center;
    }

    section[data-testid="stSidebar"]
    [data-testid="stSidebarCollapseButton"] button {
        display: inline-flex !important;
        width: 36px !important;
        min-width: 36px !important;
        height: 36px !important;
        padding: 0 !important;
        align-items: center;
        justify-content: center;
        border-radius: 0.55rem;
    }

    section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
        margin-top: -62px;
        padding-left: 20px;
        padding-right: 20px;
    }

    section[data-testid="stSidebar"] h2 {
        min-height: 36px;
        margin: 0 52px 0 0 !important;
        padding: 0 !important;
        line-height: 36px;
    }

    section[data-testid="stSidebar"] button[data-testid="stPopoverButton"] {
        width: 38px !important;
        min-width: 38px !important;
        min-height: 38px;
        height: 38px;
        padding: 0 !important;
        border-radius: 0.5rem;
        justify-content: center;
    }

    section[data-testid="stSidebar"]
    button[data-testid="stPopoverButton"] svg {
        display: none !important;
    }

    section[data-testid="stSidebar"]
    button[data-testid="stPopoverButton"] p {
        margin: 0;
        font-size: 1.25rem;
        line-height: 1;
    }

    .database-list-label {
        color: #4b5563;
        font-size: 0.88rem;
        font-weight: 600;
        line-height: 40px;
    }

    section[data-testid="stSidebar"]
    div[class*="st-key-database_source_"] .stButton > button,
    section[data-testid="stSidebar"]
    div[class*="st-key-active_database_source_"] .stButton > button {
        min-height: 38px;
        width: 100%;
        padding: 0.45rem 0.7rem !important;
        border: 1px solid transparent !important;
        border-radius: 0.55rem !important;
        box-shadow: none !important;
        transform: none !important;
        justify-content: flex-start;
        text-align: left;
    }

    section[data-testid="stSidebar"]
    div[class*="st-key-database_source_"] .stButton > button {
        background: transparent !important;
        color: #374151 !important;
    }

    section[data-testid="stSidebar"]
    div[class*="st-key-database_source_"] .stButton > button:hover {
        background: #f3f4f6 !important;
        border-color: #e5e7eb !important;
    }

    section[data-testid="stSidebar"]
    div[class*="st-key-active_database_source_"] .stButton > button {
        background: #e8f2fb !important;
        border-color: #bdd8ee !important;
        color: #155a8a !important;
    }

    section[data-testid="stSidebar"]
    div[class*="st-key-database_source_"] .stButton > button p,
    section[data-testid="stSidebar"]
    div[class*="st-key-active_database_source_"] .stButton > button p {
        width: 100%;
        overflow: hidden;
        color: inherit !important;
        font-size: 0.93rem;
        font-weight: 500;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    section[data-testid="stSidebar"]
    div[class*="st-key-delete_database_item_"] .stButton > button,
    section[data-testid="stSidebar"]
    div[class*="st-key-protected_database_item_"] .stButton > button {
        min-height: 38px;
        width: 38px;
        min-width: 38px;
        padding: 0 !important;
        background: transparent !important;
        border: 1px solid transparent !important;
        border-radius: 0.55rem !important;
        box-shadow: none !important;
        transform: none !important;
        justify-content: center;
    }

    section[data-testid="stSidebar"]
    div[class*="st-key-delete_database_item_"] .stButton > button {
        color: #9ca3af !important;
    }

    section[data-testid="stSidebar"]
    div[class*="st-key-delete_database_item_"] .stButton > button:hover {
        background: #fef2f2 !important;
        border-color: #fecaca !important;
        color: #dc2626 !important;
    }

    section[data-testid="stSidebar"]
    div[class*="st-key-protected_database_item_"] .stButton > button {
        color: #d1d5db !important;
    }

    section[data-testid="stSidebar"]
    div[class*="st-key-delete_database_item_"] .stButton > button p,
    section[data-testid="stSidebar"]
    div[class*="st-key-protected_database_item_"] .stButton > button p {
        color: inherit !important;
        font-size: 1.25rem;
        font-weight: 400;
        line-height: 1;
    }

    /* Use click cursors for database and expandable catalog rows */
    div[data-baseweb="select"],
    div[data-baseweb="select"] *,
    div[data-testid="stExpander"] details > summary,
    div[data-testid="stExpander"] details > summary * {
        cursor: pointer !important;
    }

    /* Streamlit adds its own Plotly fullscreen control outside the locale map. */
    .modebar-btn[data-title="Fullscreen"]::before,
    .modebar-btn[data-title="Fullscreen"]:hover::before,
    .modebar-btn[data-title="Enter fullscreen"]::before,
    .modebar-btn[data-title="Enter fullscreen"]:hover::before {
        content: "全屏查看" !important;
    }

    .modebar-btn[data-title="Exit fullscreen"]::before,
    .modebar-btn[data-title="Exit fullscreen"]:hover::before {
        content: "退出全屏" !important;
    }
</style>
""", unsafe_allow_html=True)


# 渲染页面顶部区域
def render_header():
    st.title("智能数据分析平台")


# 分情况处理服务器地址
def _parse_server_address(
    server_address: str,
    default_port: int,
) -> tuple[str, int]:
    value = server_address.strip()

    # IPv6
    if value.startswith("[") and "]" in value:
        closing_bracket = value.index("]")
        host = value[1:closing_bracket]
        remainder = value[closing_bracket + 1:]
        if remainder.startswith(":") and remainder[1:].isdigit():
            return host, int(remainder[1:])
        return host, default_port

    # host-only
    if value.count(":") == 1:
        host, port_text = value.rsplit(":", 1)
        if host and port_text.isdigit():
            return host, int(port_text)

    return value, default_port


# 删除数据库弹窗和确认流程
@st.dialog("确认删除数据库")
def _confirm_database_deletion(
    database_path: str,
) -> None:
    path = Path(database_path)
    database_name = database_display_name(path)
    st.write(f"是否确认删除数据库“{database_name}”？")
    st.caption("删除后，本机保存的分析副本将被移除，此操作无法撤销。")

    cancel_column, confirm_column = st.columns(2)
    with cancel_column:
        if st.button(
            "取消",
            key="cancel_database_delete",
            use_container_width=True,
        ):
            st.rerun()
    with confirm_column:
        if st.button(
            "确认删除",
            key="confirm_database_delete",
            type="primary",
            use_container_width=True,
        ):
            try:
                delete_database_source(path)
            except DataSourceError as error:
                st.error(str(error))
                return

            if st.session_state.get("active_database_path") == database_path:
                st.session_state.pop("active_database_path", None)
                st.session_state.question_draft = ""
                st.session_state.results = None
            st.session_state.database_notice = (
                f"数据库“{database_name}”已删除"
            )
            st.rerun()

# 渲染侧边栏界面，处理侧边栏中的数据库用户操作
def render_sidebar() -> Path | None:
    sources = list_database_sources()
    source_values = [str(path) for path in sources]

    active_value = st.session_state.get(
        "active_database_path",
        source_values[0] if source_values else "",
    )
    if active_value not in source_values:
        active_value = source_values[0] if source_values else ""
        st.session_state.active_database_path = active_value

    with st.sidebar:
        st.header("数据目录")

        notice = st.session_state.pop("database_notice", None)
        if notice:
            st.toast(notice, icon="✅")

        database_label_column, add_column = st.columns(
            [0.84, 0.16],
            gap="small",
            vertical_alignment="center",
        )

        with database_label_column:
            st.markdown(
                '<div class="database-list-label">数据库列表</div>',
                unsafe_allow_html=True,
            )

        with add_column:
            with st.popover(
                "＋",
                help="添加数据库",
                use_container_width=False,
            ):
                st.markdown("**添加数据库连接**")
                database_type = st.selectbox(
                    "数据库类型",
                    options=("MySQL", "PostgreSQL"),
                    key="remote_database_type",
                )
                provider_key = database_type.lower()
                default_port = 3306 if database_type == "MySQL" else 5432

                remote_server = st.text_input(
                    "服务器地址",
                    placeholder=f"例如：127.0.0.1:{default_port}",
                    key=f"{provider_key}_server",
                )
                remote_user = st.text_input(
                    "用户名",
                    key=f"{provider_key}_user",
                )
                remote_password = st.text_input(
                    "密码",
                    type="password",
                    key=f"{provider_key}_password",
                )
                st.caption(
                    f"端口可省略，默认使用 {default_port}；"
                    "密码不会写入项目文件。"
                )

                remote_host, remote_port_number = _parse_server_address(
                    remote_server,
                    default_port,
                )

                connection_key = "|".join(
                    (
                        database_type,
                        remote_host.strip(),
                        str(remote_port_number),
                        remote_user.strip(),
                    )
                )
                can_connect = bool(
                    remote_host
                    and remote_user.strip()
                    and 1 <= remote_port_number <= 65535
                )
                discover_databases = st.button(
                    "连接并读取数据库",
                    key=f"discover_{provider_key}_databases",
                    disabled=not can_connect,
                    use_container_width=True,
                )

                if discover_databases:
                    try:
                        with st.spinner("正在连接数据库…"):
                            remote_databases = list_remote_databases(
                                database_type,
                                remote_host,
                                remote_port_number,
                                remote_user,
                                remote_password,
                            )
                        st.session_state.remote_database_catalog = {
                            "connection_key": connection_key,
                            "databases": remote_databases,
                        }
                        st.success(
                            f"连接成功，发现 {len(remote_databases)} 个数据库。"
                        )
                    except DataSourceError as error:
                        st.session_state.pop(
                            "remote_database_catalog",
                            None,
                        )
                        st.error(str(error))

                remote_catalog = st.session_state.get(
                    "remote_database_catalog",
                )
                if (
                    remote_catalog
                    and remote_catalog.get("connection_key")
                    == connection_key
                ):
                    selected_remote_database = st.selectbox(
                        "选择数据库",
                        options=remote_catalog["databases"],
                        key=f"{provider_key}_database_name",
                    )
                    st.caption("添加后会生成本机只读分析副本，最大 100 MB。")
                    add_database = st.button(
                        "添加并使用",
                        key=f"add_{provider_key}_database",
                        use_container_width=True,
                    )
                    if add_database:
                        try:
                            with st.spinner("正在同步数据库结构和数据…"):
                                saved_path = snapshot_remote_database(
                                    database_type,
                                    remote_host,
                                    remote_port_number,
                                    remote_user,
                                    remote_password,
                                    selected_remote_database,
                                )
                            st.session_state.active_database_path = str(
                                saved_path
                            )
                            st.session_state.question_draft = ""
                            st.session_state.results = None
                            st.success("数据库已添加")
                            st.rerun()
                        except DataSourceError as error:
                            st.error(str(error))

        for source_index, source in enumerate(sources):
            source_value = str(source)
            is_active = source_value == active_value
            database_column, delete_column = st.columns(
                [0.84, 0.16],
                gap="small",
                vertical_alignment="center",
            )

            with database_column:
                button_prefix = (
                    "active_database_source"
                    if is_active
                    else "database_source"
                )
                selected = st.button(
                    database_display_name(source),
                    key=f"{button_prefix}_{source_index}",
                    help=f"切换到数据库：{database_display_name(source)}",
                    use_container_width=True,
                )
                if selected and not is_active:
                    st.session_state.active_database_path = source_value
                    st.session_state.question_draft = ""
                    st.session_state.results = None
                    active_value = source_value
                    st.rerun()

            with delete_column:
                if st.button(
                    "×",
                    key=f"delete_database_item_{source_index}",
                    help=f"删除数据库：{database_display_name(source)}",
                ):
                    _confirm_database_deletion(source_value)

        active_database = Path(active_value) if active_value else None

        if active_database is None:
            st.info("暂无可用数据库，请点击“＋”添加数据库。")
        else:
            try:
                catalog = inspect_sqlite_database(active_database)
                st.caption(f"{len(catalog['tables'])} 张数据表")
                for table in catalog["tables"]:
                    with st.expander(f"{table['name']}"):
                        for column in table["columns"]:
                            key_marker = " · 主键" if column["primary_key"] else ""
                            st.markdown(
                                f"`{column['name']}`  "
                                f"<span style='color:#7b8494'>"
                                f"{column['type']}{key_marker}</span>",
                                unsafe_allow_html=True,
                            )
            except DataSourceError as error:
                st.error(str(error))

    return active_database


# 判断数据库字段是否为数值类型（计算及柱状图）
def _is_numeric_type(column_type: str) -> bool:
    normalized = column_type.upper()
    return any(
        token in normalized
        for token in ("INT", "REAL", "FLOA", "DOUB", "DEC", "NUM")
    )


# 判断是否为时间类型（趋势图或横轴）
def _is_time_column(column_name: str) -> bool:
    normalized = column_name.lower()
    return any(
        token in normalized
        for token in ("time", "date", "created", "updated", "year", "month", "day")
    )


# 判断识别ID标识字段（不应该被用于计算）
def _is_identifier_column(column_name: str) -> bool:
    normalized = column_name.lower()
    return normalized == "id" or normalized.endswith("_id")


# 获取数据库信息，识别字段信息，进行问题推荐
@st.cache_data(show_spinner=False)
def generate_suggested_questions(
    database_path: str,
    database_modified_at: int,
) -> List[str]:
    del database_modified_at  # Included only to invalidate the cache on data updates.

    manager = DatabaseManager(Path(database_path))
    table_names = manager.get_table_names()
    profiles: List[Dict[str, Any]] = []
    category_hints = (
        "status",
        "type",
        "category",
        "region",
        "brand",
        "level",
        "name",
    )

    with manager.get_connection() as connection:
        for table_name in table_names:
            quoted_table = '"' + table_name.replace('"', '""') + '"'
            row_count = connection.execute(
                f"SELECT COUNT(*) FROM {quoted_table}"
            ).fetchone()[0]
            if row_count == 0:
                continue

            columns = connection.execute(
                f"PRAGMA table_info({quoted_table})"
            ).fetchall()
            column_info = [
                {"name": column[1], "type": column[2] or ""}
                for column in columns
            ]

            time_columns = [
                column["name"]
                for column in column_info
                if _is_time_column(column["name"])
            ]
            measure_columns = [
                column["name"]
                for column in column_info
                if _is_numeric_type(column["type"])
                and not _is_identifier_column(column["name"])
                and column["name"].lower()
                not in {"status", "type", "category", "level", "flag", "code"}
            ]
            dimension_columns = [
                column["name"]
                for column in column_info
                if column["name"].lower().endswith("_id")
                and column["name"].lower() != "id"
            ]
            category_columns = [
                column["name"]
                for hint in category_hints
                for column in column_info
                if hint in column["name"].lower()
                and not _is_time_column(column["name"])
                and column["name"] not in dimension_columns
            ]
            category_columns.extend(
                column["name"]
                for column in column_info
                if not _is_numeric_type(column["type"])
                and not _is_time_column(column["name"])
                and not _is_identifier_column(column["name"])
                and column["name"] not in category_columns
            )

            profiles.append(
                {
                    "table": table_name,
                    "time": time_columns,
                    "measures": measure_columns,
                    "dimensions": dimension_columns,
                    "categories": category_columns,
                }
            )

    questions: List[str] = []

    # 分配问题
    for profile in profiles:
        if profile["categories"]:
            category = profile["categories"][0]
            questions.append(
                f"统计 {profile['table']} 中不同 {category} 的记录数量"
            )
            break

    # 分组平均值问题
    grouped_profiles = sorted(
        profiles,
        key=lambda profile: (
            bool(profile["time"]),
            len(profile["measures"]),
            bool(profile["dimensions"]),
        ),
        reverse=True,
    )
    for profile in grouped_profiles:
        groups = profile["dimensions"] or profile["categories"]
        if groups and profile["measures"]:
            questions.append(
                f"按 {groups[0]} 统计 {profile['table']} 中 "
                f"{profile['measures'][0]} 的平均值"
            )
            break

    # 趋势问题
    for profile in grouped_profiles:
        if profile["time"] and profile["measures"]:
            questions.append(
                f"展示 {profile['table']} 中 {profile['measures'][0]} "
                f"随 {profile['time'][0]} 的变化趋势"
            )
            break

    # 安全回滚
    for profile in profiles:
        fallback = f"统计 {profile['table']} 的记录总数"
        if fallback not in questions:
            questions.append(fallback)
        if len(questions) >= 3:
            break

    return questions[:3]


def set_question_draft(question: str) -> None:
    """Place a suggested question into the editable input without submitting."""
    st.session_state.question_draft = question


def queue_question_submission() -> None:
    """Capture the submitted question, then clear the visible input safely."""
    st.session_state.queued_question = str(
        st.session_state.get("question_draft", "")
    ).strip()
    st.session_state.question_draft = ""


def render_question_input(database_path: Path | None) -> str | None:
    suggestions: List[str] = []
    if database_path is not None and database_path.is_file():
        modified_at = database_path.stat().st_mtime_ns
        suggestions = generate_suggested_questions(
            str(database_path),
            modified_at,
        )

        database_name = html.escape(
            database_display_name(database_path)
        )
        st.markdown(
            f'<div class="database-context">在「{database_name}」中提问</div>',
            unsafe_allow_html=True,
        )
    else:
        st.info("暂无可用数据库，请先从数据目录添加数据库。")

    with st.form(
        "question_form",
        clear_on_submit=False,
        border=False,
    ):
        text_column, send_column = st.columns(
            [0.95, 0.05],
            gap="small",
            vertical_alignment="center",
        )
        with text_column:
            typed_question = st.text_input(
                "问题",
                key="question_draft",
                placeholder="请输入您的问题…",
                label_visibility="collapsed",
                disabled=database_path is None,
            )
        with send_column:
            submitted = st.form_submit_button(
                "↑",
                help="发送",
                on_click=queue_question_submission,
                use_container_width=False,
                disabled=database_path is None,
            )

    if suggestions:
        st.markdown(
            '<div class="suggestion-label">你可能想问：</div>',
            unsafe_allow_html=True,
        )
        for index, suggestion in enumerate(suggestions):
            st.button(
                suggestion,
                key=f"suggested_question_{index}",
                on_click=set_question_draft,
                args=(suggestion,),
                use_container_width=True,
            )

    if submitted:
        return st.session_state.pop("queued_question", "")
    return None


PLOTLY_ZH_CN_LOCALE = {
    "dictionary": {
        "Download plot as a png": "下载图表为 PNG 图片",
        "Download plot as a PNG": "下载图表为 PNG 图片",
        "Download plot": "下载图表",
        "Zoom": "缩放",
        "Pan": "平移",
        "Box Select": "矩形框选",
        "Lasso Select": "套索选择",
        "Zoom in": "放大",
        "Zoom out": "缩小",
        "Autoscale": "自动缩放",
        "Reset axes": "重置坐标轴",
        "Show closest data on hover": "悬停时显示最近的数据",
        "Compare data on hover": "悬停时比较数据",
        "Toggle Spike Lines": "切换数据辅助线",
        "Produced with Plotly.js": "由 Plotly.js 生成",
        "Fullscreen": "全屏查看",
        "Enter fullscreen": "全屏查看",
        "Exit fullscreen": "退出全屏",
    },
}


# 检测回答中是否有中文
def _contains_chinese(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(value)))


def _chinese_chart_title(title: Any, question: str) -> str:
    proposed_title = str(title or "").strip()
    if proposed_title and _contains_chinese(proposed_title):
        localized_title = proposed_title
    elif question and _contains_chinese(question):
        localized_title = question
    else:
        localized_title = "数据分析结果"

    return localized_title


def _chinese_reasoning(reasoning: str) -> str:
    if reasoning and _contains_chinese(reasoning):
        return reasoning
    return "系统已根据当前数据库结构识别相关数据表和字段，并生成只读 SQL 查询。"


def _limit_summary_sentences(summary: str, maximum: int = 10) -> str:
    normalized = str(summary or "").strip()
    normalized = re.sub(
        r"^\s*(?:#{1,6}\s*)?(?:结论总结|分析结论|数据结论|总结)\s*[:：]?\s*",
        "",
        normalized,
    )
    normalized = re.sub(
        r"(?m)^\s*(?:[-*•]\s+|\d+[.、]\s*)",
        "",
        normalized,
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return ""

    sentences = [
        sentence.strip()
        for sentence in re.findall(r"[^。！？!?\n]+(?:[。！？!?]+|$)", normalized)
        if sentence.strip()
    ][:maximum]
    return "".join(
        sentence
        if sentence.endswith(("。", "！", "？", "!", "?"))
        else f"{sentence}。"
        for sentence in sentences
    )


def _format_summary_value(value: Any) -> str:
    if value is None or pd.isna(value):
        return "空值"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        numeric_value = float(value)
        if numeric_value.is_integer():
            return f"{int(numeric_value):,}"
        return f"{numeric_value:,.4f}".rstrip("0").rstrip(".")
    return str(value)


def _local_result_summary(
    results: List[Dict[str, Any]],
    viz_spec: Dict[str, Any],
) -> str:
    if not results:
        return "本次查询没有返回可供分析的数据。"

    df = pd.DataFrame(results)
    row_count = len(df)
    sentences = [f"本次查询共返回 {row_count} 条结果。"]
    numeric_columns = list(df.select_dtypes(include="number").columns)
    if not numeric_columns:
        fields = "、".join(
            str(column)
            for column in list(df.columns)[:3]
        )
        sentences.append(f"结果主要包含{fields}等字段。")
        return "".join(sentences)

    chart_type = str(viz_spec.get("chart_type", "")).lower()
    requested_y = (
        viz_spec.get("z_column")
        if chart_type == "heatmap"
        else viz_spec.get("y_column")
    )
    y_column = (
        requested_y
        if requested_y in numeric_columns
        else numeric_columns[0]
    )
    requested_x = viz_spec.get("x_column")
    x_column = (
        requested_x
        if requested_x in df.columns and requested_x != y_column
        else next(
            (column for column in df.columns if column != y_column),
            None,
        )
    )

    values = pd.to_numeric(df[y_column], errors="coerce")
    valid_values = values.dropna()
    if valid_values.empty:
        return "".join(sentences)

    y_label = str(y_column)
    maximum_index = valid_values.idxmax()
    minimum_index = valid_values.idxmin()
    maximum_value = _format_summary_value(valid_values.loc[maximum_index])
    minimum_value = _format_summary_value(valid_values.loc[minimum_index])

    if x_column is not None:
        x_label = str(x_column)
        maximum_category = _format_summary_value(
            df.loc[maximum_index, x_column]
        )
        minimum_category = _format_summary_value(
            df.loc[minimum_index, x_column]
        )
        if maximum_value == minimum_value:
            sentences.append(
                f"各{x_label}对应的{y_label}相同，均为 {maximum_value}。"
            )
        else:
            sentences.append(
                f"{x_label}为“{maximum_category}”时{y_label}最高，"
                f"为 {maximum_value}；为“{minimum_category}”时最低，"
                f"为 {minimum_value}。"
            )
    else:
        average_value = _format_summary_value(valid_values.mean())
        sentences.append(
            f"{y_label}最高为 {maximum_value}，最低为 {minimum_value}，"
            f"平均为 {average_value}。"
        )

    is_trend = (
        str(viz_spec.get("chart_type", "")).lower() == "line"
        or (x_column is not None and _is_time_column(str(x_column)))
    )
    if is_trend and len(valid_values) >= 2:
        first_value = float(valid_values.iloc[0])
        last_value = float(valid_values.iloc[-1])
        change = last_value - first_value
        if abs(change) < 1e-12:
            sentences.append(f"首末两条记录的{y_label}保持不变。")
        else:
            direction = "上升" if change > 0 else "下降"
            sentences.append(
                f"从首条到末条记录，{y_label}由"
                f" {_format_summary_value(first_value)} {direction}至"
                f" {_format_summary_value(last_value)}，变化幅度为"
                f" {_format_summary_value(abs(change))}。"
            )

    return "".join(sentences[:10])


def _result_summary(
    model_summary: str,
    results: List[Dict[str, Any]],
    viz_spec: Dict[str, Any],
) -> str:
    normalized_summary = _limit_summary_sentences(model_summary)
    if normalized_summary and _contains_chinese(normalized_summary):
        return normalized_summary
    return _local_result_summary(results, viz_spec)


def render_visualization(
    results: List[Dict[str, Any]],
    viz_spec: Dict[str, Any],
    question: str = "",
):
    """
    Render a chart for every non-empty query result.

    If the model returns an invalid or incomplete chart specification, select
    a deterministic fallback from the result columns instead of showing only
    a table.
    
    Args:
        results: Query results
        viz_spec: Visualization specification from agent
    """
    if not results:
        st.info("没有可用于生成图表的数据。")
        return
    
    # Convert to DataFrame
    df = pd.DataFrame(results)
    
    chart_type = str(viz_spec.get("chart_type", "")).lower()
    x_col = viz_spec.get("x_column")
    y_col = viz_spec.get("y_column")
    z_col = viz_spec.get("z_column")
    title = _chinese_chart_title(
        viz_spec.get("title", "查询结果"),
        question,
    )

    numeric_columns = list(df.select_dtypes(include="number").columns)
    category_columns = [
        column for column in df.columns if column not in numeric_columns
    ]
    allowed_chart_types = {"bar", "line", "pie", "scatter", "heatmap"}

    explicit_heatmap = re.search(
        r"用\s*([A-Za-z_][A-Za-z0-9_]*)\s*为横轴、"
        r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*为纵轴、"
        r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*为颜色值绘制热力图",
        question,
    )
    if explicit_heatmap:
        requested_x, requested_y, requested_z = explicit_heatmap.groups()
        if (
            requested_x in df.columns
            and requested_y in df.columns
            and requested_z in numeric_columns
            and requested_x != requested_y
        ):
            chart_type = "heatmap"
            x_col = requested_x
            y_col = requested_y
            z_col = requested_z

    explicit_chart = re.search(
        r"用\s*([A-Za-z_][A-Za-z0-9_]*)\s*为横轴、"
        r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*为纵轴绘制"
        r"(柱状图|折线图|饼图|散点图)",
        question,
    )
    if explicit_chart:
        requested_x, requested_y, requested_chart = explicit_chart.groups()
        chart_names = {
            "柱状图": "bar",
            "折线图": "line",
            "饼图": "pie",
            "散点图": "scatter",
        }
        requested_chart_type = chart_names[requested_chart]
        explicit_columns_valid = (
            requested_x in df.columns
            and requested_y in numeric_columns
            and requested_x != requested_y
            and (
                requested_chart_type != "scatter"
                or requested_x in numeric_columns
            )
        )
        if explicit_columns_valid:
            chart_type = requested_chart_type
            x_col = requested_x
            y_col = requested_y

    explicit_title = re.search(
        r"图表标题为[“\"]([^”\"]+)[”\"]",
        question,
    )
    if explicit_title:
        title = explicit_title.group(1).strip()

    if chart_type == "heatmap":
        has_valid_columns = (
            x_col in df.columns
            and y_col in df.columns
            and z_col in numeric_columns
            and x_col != y_col
        )
    else:
        has_valid_columns = (
            x_col in df.columns
            and y_col in df.columns
            and x_col != y_col
            and y_col in numeric_columns
        )
    if chart_type == "scatter":
        has_valid_columns = has_valid_columns and x_col in numeric_columns

    if chart_type not in allowed_chart_types or not has_valid_columns:
        if category_columns and numeric_columns:
            chart_type = "bar"
            x_col = category_columns[0]
            y_col = numeric_columns[0]
        elif len(numeric_columns) >= 2:
            chart_type = "scatter"
            x_col, y_col = numeric_columns[:2]
        elif numeric_columns:
            chart_type = "bar"
            row_column = "序号"
            while row_column in df.columns:
                row_column = f"_{row_column}"
            df.insert(0, row_column, range(1, len(df) + 1))
            x_col = row_column
            y_col = numeric_columns[0]
        else:
            chart_type = "bar"
            source_column = df.columns[0]
            df = (
                df[source_column]
                .astype(str)
                .value_counts(dropna=False)
                .rename_axis("Value")
                .reset_index(name="Count")
            )
            x_col = "Value"
            y_col = "Count"

    labels = {
        column: str(column)
        for column in df.columns
    }

    if chart_type == "heatmap":
        matrix = df.pivot_table(
            index=y_col,
            columns=x_col,
            values=z_col,
            aggfunc="mean",
            sort=False,
        )
        heatmap_range = (0, 100) if str(z_col).endswith("_pct") else None
        fig = px.imshow(
            matrix,
            text_auto=".1f",
            aspect="auto",
            color_continuous_scale="RdYlGn",
            range_color=heatmap_range,
            title=title,
            labels={
                "x": labels.get(x_col, str(x_col)),
                "y": labels.get(y_col, str(y_col)),
                "color": labels.get(z_col, str(z_col)),
            },
        )
        fig.update_traces(
            hovertemplate=(
                f"{labels.get(x_col, str(x_col))}=%{{x}}<br>"
                f"{labels.get(y_col, str(y_col))}=%{{y}}<br>"
                f"{labels.get(z_col, str(z_col))}=%{{z:.2f}}"
                "<extra></extra>"
            ),
        )
    elif chart_type == "line":
        fig = px.line(
            df,
            x=x_col,
            y=y_col,
            title=title,
            markers=True,
            labels=labels,
        )
        fig.update_traces(
            line={"color": "#1976d2", "width": 3},
            marker={"color": "#ffffff", "line": {"color": "#1976d2", "width": 2}, "size": 8},
        )
    elif chart_type == "pie":
        fig = px.pie(
            df,
            names=x_col,
            values=y_col,
            color=x_col,
            color_discrete_sequence=px.colors.qualitative.Set2,
            title=title,
            labels=labels,
        )
        fig.update_traces(
            hole=0.32,
            textposition="inside",
            textinfo="label+percent",
            marker={"line": {"color": "#ffffff", "width": 2}},
        )
    elif chart_type == "scatter":
        fig = px.scatter(
            df,
            x=x_col,
            y=y_col,
            title=title,
            labels=labels,
        )
    else:
        fig = px.bar(
            df,
            x=x_col,
            y=y_col,
            color=y_col,
            color_continuous_scale=[
                (0.0, "#bbdefb"),
                (0.5, "#42a5f5"),
                (1.0, "#0d47a1"),
            ],
            text=y_col,
            title=title,
            labels=labels,
        )
        fig.update_traces(
            texttemplate="%{y:,.2f}",
            textposition="outside",
            cliponaxis=False,
        )
        fig.update_yaxes(rangemode="tozero")

    fig.update_layout(
        template="plotly_white",
        title_font_size=18,
        title_font_color="#1f77b4",
        height=480,
        coloraxis_showscale=chart_type == "heatmap",
        margin={"l": 40, "r": 30, "t": 70, "b": 90},
        hovermode="x unified" if chart_type == "line" else "closest",
    )
    if chart_type == "bar":
        fig.update_xaxes(
            categoryorder="array",
            categoryarray=df[x_col].astype(str).tolist(),
            tickangle=-20 if len(df) > 6 else 0,
        )
    st.plotly_chart(
        fig,
        use_container_width=True,
        config={
            "displaylogo": False,
            "locale": "zh-CN",
            "locales": {
                "zh-CN": PLOTLY_ZH_CN_LOCALE,
            },
        },
    )


def main():
    # 初始化session state
    if "results" not in st.session_state:
        st.session_state.results = None
    
    # 渲染UI
    render_header()
    active_database = render_sidebar()
    
    question = render_question_input(active_database)
    
    # Handle form submission
    if question:
        if active_database is None:
            st.error("请先从数据目录添加并选择数据库。")
            st.session_state.results = None
            return

        # Validate and sanitize input
        is_valid, sanitized_question, error_msg = input_validator.validate_question(question)
        
        if not is_valid:
            st.error(f"输入内容无效：{error_msg}")
            st.session_state.results = None
        else:
            # Run agent with sanitized question
            with st.spinner("正在分析问题…"):
                try:
                    result_state = run_agent(
                        sanitized_question,
                        active_database,
                    )
                    
                    # Validate results before storing
                    if result_state.query_result:
                        is_valid_results, validation_error = result_validator.validate_results(result_state.query_result)
                        if not is_valid_results:
                            st.error(f"查询结果校验失败：{validation_error}")
                            st.session_state.results = None
                            return
                    
                    st.session_state.results = result_state
                    
                except Exception as e:
                    print(f"[ERROR] Query processing error: {e}")
                    st.error("处理问题时发生错误，请稍后重试。")
                    st.session_state.results = None
    
    # Display results
    if st.session_state.results:
        state = st.session_state.results
        
        # 检查问题相关性
        if not state.is_relevant:
            message = state.final_response
            if not _contains_chinese(message):
                message = "当前问题超出了所选数据库的数据范围，请重新提问。"
            st.warning(message)
            return
        
        # 检查错误情况
        error_markers = ("Error", "Failed", "错误", "失败")
        has_error = bool(
            state.error
            or state.validation_error
            or (
                state.final_response
                and any(
                    marker in state.final_response
                    for marker in error_markers
                )
            )
        )
        if has_error:
            error_message = state.final_response
            if not _contains_chinese(error_message):
                error_message = "查询处理失败，请调整问题后重试。"
            st.error(error_message)
            
            # 展示错误查询
            if state.sql_query:
                st.subheader("失败的 SQL 查询")
                st.code(state.sql_query, language="sql")
            
            return
        
        st.success(f"查询成功，共找到 {len(state.query_result)} 条结果。")
        
        # 查询思路
        if state.reasoning:
            with st.expander("查询思路", expanded=False):
                st.markdown(_chinese_reasoning(state.reasoning))
        
        # 成功查询
        st.subheader("生成的 SQL 查询")
        st.code(state.sql_query, language="sql")
        
        # 结果展示
        if state.query_result:
            st.subheader("查询结果")
            
            viz_spec = state.visualization_spec or {"chart_type": "bar"}
            render_visualization(
                state.query_result,
                viz_spec,
                state.question,
            )
            
            # 结果行数展示
            row_count = len(state.query_result)
            st.caption(f"共显示 {row_count} 条结果")

            st.subheader("结论总结")
            st.markdown(
                _result_summary(
                    getattr(state, "analysis_summary", ""),
                    state.query_result,
                    viz_spec,
                )
            )
        else:
            st.info("查询执行成功，但没有返回数据。")



if __name__ == "__main__":
    main()
