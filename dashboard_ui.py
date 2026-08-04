"""Streamlit UI for the single interactive visualization dashboard."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import math
import os
from pathlib import Path
from typing import Any

import streamlit as st

from application.dashboard_service import (
    DashboardService,
    SavedDashboardQuery,
    assign_widget,
    iter_slots,
    move_widget,
    resize_split,
    slot_split_capabilities,
    split_slot,
)
from auth.constants import QUERY_DATA
from auth.models import AuthError, User, ValidationError
from auth.service import AuthService
from dashboard_component import dashboard_canvas
from infrastructure.data_sources import (
    DataSourceError,
    RemoteDatabaseCredentials,
    snapshot_sync_status,
    sync_remote_database_snapshot,
)
from infrastructure.db_manager import DatabaseManager


def _refresh_seconds() -> int:
    try:
        configured = int(os.getenv("DASHBOARD_REFRESH_SECONDS", "300"))
    except ValueError:
        configured = 300
    return min(max(configured, 30), 3600)


REFRESH_SECONDS = _refresh_seconds()
CACHE_TTL_SECONDS = max(1, REFRESH_SECONDS - 10)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _execute_dashboard_query(
    datasource_id: int,
    datasource_version: int,
    database_path: str,
    database_modified_at: int,
    data_version: int,
    sql_query: str,
) -> tuple[list[dict[str, Any]], str | None, str]:
    """Execute a saved read-only query at most once per five-minute window."""
    del (
        datasource_id,
        datasource_version,
        database_modified_at,
        data_version,
    )
    manager = DatabaseManager(Path(database_path))
    rows, error = manager.execute_query(sql_query)
    return rows, error, datetime.now().strftime("%H:%M:%S")


def _draft_key(user_id: int) -> str:
    return f"dashboard_draft_{user_id}"


def _edit_key(user_id: int) -> str:
    return f"dashboard_edit_mode_{user_id}"


def _dirty_key(user_id: int) -> str:
    return f"dashboard_dirty_{user_id}"


def _picker_key(user_id: int) -> str:
    return f"dashboard_picker_slot_{user_id}"


def _event_key(user_id: int) -> str:
    return f"dashboard_last_event_{user_id}"


def _history_key(user_id: int) -> str:
    return f"dashboard_undo_history_{user_id}"


def _push_undo_snapshot(user_id: int, layout: dict[str, Any]) -> None:
    history = st.session_state.get(_history_key(user_id))
    if not isinstance(history, list):
        history = []
    history.append(deepcopy(layout))
    st.session_state[_history_key(user_id)] = history[-50:]


def _notice(message: str, icon: str = "✅") -> None:
    st.session_state.dashboard_notice = (message, icon)


def _set_workspace_page(page: str) -> None:
    st.session_state.workspace_page = page
    st.session_state.account_page = "query"


def _query_payload(
    service: DashboardService,
    auth_service: AuthService,
    current_user: User,
    layout: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, str]]:
    def json_safe_rows(
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        safe_rows: list[dict[str, Any]] = []
        for row in rows:
            safe_row: dict[str, Any] = {}
            for key, value in row.items():
                if value is None or isinstance(value, (str, bool, int)):
                    safe_value = value
                elif isinstance(value, float):
                    safe_value = value if math.isfinite(value) else None
                elif isinstance(value, bytes):
                    safe_value = f"[二进制数据 · {len(value)} 字节]"
                else:
                    safe_value = str(value)
                safe_row[str(key)] = safe_value
            safe_rows.append(safe_row)
        return safe_rows

    widget_ids = {
        int(slot["widget_id"])
        for slot in iter_slots(layout)
        if slot.get("widget_id") is not None
    }
    widgets = service.queries_by_id(
        current_user.id,
        current_user.session_version,
        widget_ids,
    )
    datasource_ids = {
        query.datasource_id for query in widgets.values()
    }
    resolved_sources: dict[int, Any] = {}
    source_errors: dict[int, str] = {}
    source_sync_statuses: dict[int, dict[str, Any]] = {}
    sync_warnings: list[str] = []
    sync_successes: list[str] = []
    raw_credentials = st.session_state.get(
        "remote_sync_credentials",
        {},
    )
    credentials_by_id = (
        raw_credentials if isinstance(raw_credentials, dict) else {}
    )

    for datasource_id in sorted(datasource_ids):
        try:
            datasource = auth_service.resolve_authorized_datasource(
                current_user.id,
                datasource_id,
                QUERY_DATA,
            )
            resolved_sources[datasource_id] = datasource
        except (AuthError, OSError) as error:
            source_errors[datasource_id] = str(error)
            sync_warnings.append(str(error))
            continue

        try:
            status = snapshot_sync_status(datasource.path)
            if status.get("remote"):
                credentials = credentials_by_id.get(
                    datasource_id,
                    credentials_by_id.get(str(datasource_id)),
                )
                if (
                    status.get("enabled")
                    and isinstance(credentials, RemoteDatabaseCredentials)
                ):
                    result = sync_remote_database_snapshot(
                        datasource.path,
                        credentials,
                        minimum_interval_seconds=REFRESH_SECONDS,
                    )
                    if result.attempted:
                        sync_successes.append(result.synced_at)
                        try:
                            auth_service.record_audit(
                                actor_user_id=current_user.id,
                                action="sync_datasource",
                                outcome="success",
                                target_type="datasource",
                                target_id=datasource.id,
                                datasource_id=datasource.id,
                                details={
                                    "rows_synced": result.rows_synced,
                                    "data_version": result.data_version,
                                },
                            )
                        except AuthError:
                            pass
                elif status.get("enabled"):
                    sync_warnings.append(
                        f"“{datasource.display_name}”缺少当前会话凭据，"
                        "正在显示最近成功快照。"
                    )
                else:
                    sync_warnings.append(
                        f"“{datasource.display_name}”是旧版远程快照，"
                        "重新添加后才能自动同步。"
                    )
                status = snapshot_sync_status(datasource.path)
            source_sync_statuses[datasource_id] = status
        except (DataSourceError, OSError) as error:
            source_sync_statuses[datasource_id] = snapshot_sync_status(
                datasource.path
            )
            sync_warnings.append(str(error))
            try:
                auth_service.record_audit(
                    actor_user_id=current_user.id,
                    action="sync_datasource",
                    outcome="failure",
                    target_type="datasource",
                    target_id=datasource.id,
                    datasource_id=datasource.id,
                    details={"error_type": error.__class__.__name__},
                )
            except AuthError:
                pass

    payload: dict[str, dict[str, Any]] = {}
    refresh_times: list[str] = []
    for widget_id in widget_ids:
        query = widgets.get(widget_id)
        if query is None:
            payload[str(widget_id)] = {
                "title": "不可用的查询图表",
                "datasource_name": "权限已变化",
                "rows": [],
                "visualization_spec": {},
                "error": "查询不存在，或当前账号已失去对应数据源权限。",
            }
            continue
        try:
            if query.datasource_id in source_errors:
                raise AuthError(source_errors[query.datasource_id])
            datasource = resolved_sources[query.datasource_id]
            modified_at = (
                datasource.path.stat().st_mtime_ns
                if datasource.path.is_file()
                else 0
            )
            sync_status = source_sync_statuses.get(
                query.datasource_id,
                {},
            )
            rows, error, refreshed_at = _execute_dashboard_query(
                datasource.id,
                datasource.version,
                str(datasource.path),
                modified_at,
                int(sync_status.get("data_version", 0) or 0),
                query.sql_query,
            )
        except (AuthError, KeyError, OSError) as error:
            rows = []
            error = str(error)
            refreshed_at = datetime.now().strftime("%H:%M:%S")
        refresh_times.append(refreshed_at)
        payload[str(widget_id)] = {
            "title": query.title,
            "datasource_name": query.datasource_name,
            "rows": json_safe_rows(rows),
            "visualization_spec": query.visualization_spec,
            "error": error or "",
            "refreshed_at": refreshed_at,
        }
    sync_summary = {
        "status": "warning" if sync_warnings else "ok",
        "message": (
            sync_warnings[0]
            if sync_warnings
            else (
                "远程数据已同步至 "
                + max(sync_successes).replace("T", " ")[-8:]
                if sync_successes
                else ""
            )
        ),
    }
    return payload, refresh_times, sync_summary


@st.dialog("选择保存的查询图表", width="large")
def _render_query_picker(
    auth_service: AuthService,
    current_user: User,
    slot_id: str,
) -> None:
    service = DashboardService(auth_service)
    queries = service.list_queries(
        current_user.id,
        current_user.session_version,
    )
    if not queries:
        st.info("还没有保存的查询图表，请先到智能问数中生成并保存。")
        if st.button(
            "前往智能问数",
            key=f"picker_to_query_{current_user.id}",
            type="primary",
        ):
            st.session_state.pop(_picker_key(current_user.id), None)
            _set_workspace_page("智能问数")
            st.rerun()
        return

    search = st.text_input(
        "搜索",
        placeholder="按图表名称或数据源搜索",
        label_visibility="collapsed",
        key=f"dashboard_query_search_{current_user.id}",
    ).strip().lower()
    filtered = [
        query
        for query in queries
        if not search
        or search in query.title.lower()
        or search in query.datasource_name.lower()
    ]
    if not filtered:
        st.caption("没有匹配的查询图表。")
        return

    for query in filtered:
        label_column, action_column = st.columns(
            [0.78, 0.22],
            vertical_alignment="center",
        )
        with label_column:
            chart_type = str(
                query.visualization_spec.get("chart_type", "table")
            ).lower()
            type_labels = {
                "bar": "柱状图",
                "line": "折线图",
                "pie": "饼图",
                "scatter": "散点图",
                "heatmap": "热力图",
                "none": "数据表",
            }
            st.markdown(f"**{query.title}**")
            st.caption(
                f"{type_labels.get(chart_type, '数据表')} · "
                f"{query.datasource_name}"
            )
        with action_column:
            if st.button(
                "添加",
                key=f"pick_dashboard_query_{current_user.id}_{query.id}",
                use_container_width=True,
            ):
                draft = st.session_state.get(_draft_key(current_user.id))
                if not isinstance(draft, dict):
                    dashboard = service.get_dashboard(
                        current_user.id,
                        current_user.session_version,
                    )
                    draft = dashboard.layout
                updated = assign_widget(draft, slot_id, query.id)
                _push_undo_snapshot(current_user.id, draft)
                st.session_state[_draft_key(current_user.id)] = updated
                st.session_state[_dirty_key(current_user.id)] = True
                st.session_state.pop(_picker_key(current_user.id), None)
                _notice(f"已添加“{query.title}”，点击保存后生效。")
                st.rerun()
        st.divider()


def _apply_canvas_event(
    raw_event: str | None,
    service: DashboardService,
    current_user: User,
    persisted_layout: dict[str, Any],
) -> None:
    if not raw_event:
        return
    try:
        event = json.loads(raw_event)
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(event, dict):
        return
    event_id = str(event.get("event_id", ""))
    if not event_id or st.session_state.get(_event_key(current_user.id)) == event_id:
        return
    st.session_state[_event_key(current_user.id)] = event_id

    action = event.get("action")
    edit_key = _edit_key(current_user.id)
    draft_key = _draft_key(current_user.id)
    dirty_key = _dirty_key(current_user.id)

    try:
        if action == "enter_edit":
            st.session_state[edit_key] = True
            st.session_state[draft_key] = deepcopy(persisted_layout)
            st.session_state[dirty_key] = False
            st.session_state[_history_key(current_user.id)] = []
        elif action == "cancel":
            st.session_state[edit_key] = False
            st.session_state.pop(draft_key, None)
            st.session_state.pop(dirty_key, None)
            st.session_state.pop(_history_key(current_user.id), None)
        elif action == "save":
            draft = st.session_state.get(draft_key, persisted_layout)
            service.save_layout(
                current_user.id,
                current_user.session_version,
                draft,
            )
            st.session_state[edit_key] = False
            st.session_state.pop(draft_key, None)
            st.session_state.pop(dirty_key, None)
            st.session_state.pop(_history_key(current_user.id), None)
            _notice("大屏布局已保存。")
        elif action == "undo":
            history = st.session_state.get(_history_key(current_user.id))
            if isinstance(history, list) and history:
                previous = history.pop()
                st.session_state[draft_key] = previous
                st.session_state[_history_key(current_user.id)] = history
                st.session_state[dirty_key] = previous != persisted_layout
        elif action == "refresh":
            pass
        elif action == "add":
            st.session_state[_picker_key(current_user.id)] = str(
                event["slot_id"]
            )
        else:
            draft = st.session_state.get(draft_key)
            if not isinstance(draft, dict):
                return
            if action == "delete":
                updated = assign_widget(
                    draft,
                    str(event["slot_id"]),
                    None,
                )
            elif action == "move":
                updated = move_widget(
                    draft,
                    str(event["source_slot_id"]),
                    str(event["target_slot_id"]),
                )
            elif action == "split":
                updated = split_slot(
                    draft,
                    str(event["slot_id"]),
                    str(event["edge"]),
                )
            elif action == "resize":
                updated = resize_split(
                    draft,
                    str(event["split_id"]),
                    [float(value) for value in event["sizes"]],
                )
            else:
                return
            _push_undo_snapshot(current_user.id, draft)
            st.session_state[draft_key] = updated
            st.session_state[dirty_key] = True
    except (AuthError, ValidationError, KeyError, TypeError, ValueError) as error:
        # Resizing is a continuous direct-manipulation gesture. If it reaches
        # the minimum slot size, keep the last valid layout without interrupting
        # the user with a warning toast.
        if action != "resize":
            _notice(str(error), "⚠️")
    st.rerun()


def _render_dashboard_fragment(
    auth_service: AuthService,
    current_user: User,
) -> None:
    service = DashboardService(auth_service)
    dashboard = service.get_dashboard(
        current_user.id,
        current_user.session_version,
    )
    edit_mode = bool(
        st.session_state.get(_edit_key(current_user.id), False)
    )
    layout = (
        st.session_state.get(_draft_key(current_user.id), dashboard.layout)
        if edit_mode
        else dashboard.layout
    )
    if not isinstance(layout, dict):
        layout = dashboard.layout
    widgets, refresh_times, sync_summary = _query_payload(
        service,
        auth_service,
        current_user,
        layout,
    )
    payload = {
        "title": dashboard.title,
        "layout": layout,
        "widgets": widgets,
        "split_capabilities": slot_split_capabilities(layout),
        "refresh_time": (
            max(refresh_times)
            if refresh_times
            else datetime.now().strftime("%H:%M:%S")
        ),
        "refresh_seconds": REFRESH_SECONDS,
        "sync_status": sync_summary["status"],
        "sync_message": sync_summary["message"],
        "dirty": bool(
            st.session_state.get(_dirty_key(current_user.id), False)
        ),
        "can_undo": bool(
            st.session_state.get(_history_key(current_user.id), [])
        ),
    }
    event = dashboard_canvas(
        payload=payload,
        edit_mode=edit_mode,
        key=f"dashboard_canvas_{current_user.id}",
    )
    _apply_canvas_event(
        event,
        service,
        current_user,
        dashboard.layout,
    )


def render_dashboard_page(
    auth_service: AuthService,
    current_user: User,
) -> None:
    """Render the one dashboard, including picker and five-minute refresh."""
    notice = st.session_state.pop("dashboard_notice", None)
    if notice:
        message, icon = notice
        st.toast(message, icon=icon)

    # The picker request is a one-shot event. Removing it before opening the
    # dialog prevents a dismissed dialog from reopening on every canvas rerun.
    picker_slot = st.session_state.pop(_picker_key(current_user.id), None)
    if picker_slot:
        _render_query_picker(
            auth_service,
            current_user,
            str(picker_slot),
        )

    _render_dashboard_fragment(auth_service, current_user)


def save_result_and_place(
    *,
    auth_service: AuthService,
    current_user: User,
    datasource_id: int,
    datasource_version: int,
    title: str,
    question: str,
    sql_query: str,
    visualization_spec: dict[str, Any],
) -> tuple[SavedDashboardQuery, bool]:
    """Save one successful intelligent-query result and fill the first gap."""
    service = DashboardService(auth_service)
    saved = service.save_query(
        user_id=current_user.id,
        session_version=current_user.session_version,
        datasource_id=datasource_id,
        datasource_version=datasource_version,
        title=title,
        question=question,
        sql_query=sql_query,
        visualization_spec=visualization_spec,
    )
    draft = st.session_state.get(_draft_key(current_user.id))
    if (
        st.session_state.get(_edit_key(current_user.id), False)
        and isinstance(draft, dict)
    ):
        from application.dashboard_service import assign_to_first_empty

        updated, placed = assign_to_first_empty(draft, saved.id)
        if placed:
            _push_undo_snapshot(current_user.id, draft)
            st.session_state[_draft_key(current_user.id)] = updated
            st.session_state[_dirty_key(current_user.id)] = True
        return saved, placed
    _, placed = service.add_query_to_dashboard(
        current_user.id,
        current_user.session_version,
        saved.id,
    )
    return saved, placed
