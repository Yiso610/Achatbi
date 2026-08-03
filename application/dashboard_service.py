"""Persistence, authorization, and layout rules for the single user dashboard."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Iterable
from uuid import uuid4

from auth.constants import QUERY_DATA
from auth.models import AuthError, ValidationError
from auth.service import AuthService, utc_timestamp


DASHBOARD_WIDTH_UNITS = 24.0
DASHBOARD_HEIGHT_UNITS = 22.0
MIN_SLOT_WIDTH_UNITS = 6.0
MIN_SLOT_HEIGHT_UNITS = 3.0
MAX_LAYOUT_NODES = 64
MAX_LAYOUT_DEPTH = 8


@dataclass(frozen=True)
class SavedDashboardQuery:
    id: int
    owner_user_id: int
    datasource_id: int
    datasource_version: int
    datasource_name: str
    title: str
    question: str
    sql_query: str
    visualization_spec: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Dashboard:
    owner_user_id: int
    title: str
    layout: dict[str, Any]
    updated_at: str


def _node_id() -> str:
    return uuid4().hex


def _slot(widget_id: int | None = None) -> dict[str, Any]:
    return {
        "id": _node_id(),
        "kind": "slot",
        "widget_id": widget_id,
    }


def _split(
    direction: str,
    children: list[dict[str, Any]],
    sizes: list[float],
) -> dict[str, Any]:
    return {
        "id": _node_id(),
        "kind": "split",
        "direction": direction,
        "sizes": sizes,
        "children": children,
    }


def default_dashboard_layout() -> dict[str, Any]:
    """Create the 4 / 2 / 1 starting layout discussed for the first version."""
    return _split(
        "vertical",
        [
            _split("horizontal", [_slot() for _ in range(4)], [1, 1, 1, 1]),
            _split("horizontal", [_slot() for _ in range(2)], [1, 1]),
            _slot(),
        ],
        [6, 7, 9],
    )


def iter_slots(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    if node.get("kind") == "slot":
        yield node
        return
    for child in node.get("children", []):
        yield from iter_slots(child)


def slot_split_capabilities(
    layout: dict[str, Any],
) -> dict[str, list[str]]:
    """Return the edges each slot may split without violating minimum size."""
    capabilities: dict[str, list[str]] = {}

    def visit(node: dict[str, Any], width: float, height: float) -> None:
        if node.get("kind") == "slot":
            edges: list[str] = []
            if width / 2 + 1e-6 >= MIN_SLOT_WIDTH_UNITS:
                edges.extend(("left", "right"))
            if height / 2 + 1e-6 >= MIN_SLOT_HEIGHT_UNITS:
                edges.extend(("top", "bottom"))
            capabilities[str(node["id"])] = edges
            return
        children = node.get("children", [])
        sizes = _normalized_sizes(node)
        total = sum(sizes)
        for child, size in zip(children, sizes):
            ratio = size / total
            visit(
                child,
                width * ratio
                if node.get("direction") == "horizontal"
                else width,
                height * ratio
                if node.get("direction") == "vertical"
                else height,
            )

    visit(layout, DASHBOARD_WIDTH_UNITS, DASHBOARD_HEIGHT_UNITS)
    return capabilities


def find_node(
    node: dict[str, Any],
    node_id: str,
) -> dict[str, Any] | None:
    if node.get("id") == node_id:
        return node
    for child in node.get("children", []):
        match = find_node(child, node_id)
        if match is not None:
            return match
    return None


def _replace_node(
    node: dict[str, Any],
    node_id: str,
    replacement: dict[str, Any],
) -> bool:
    children = node.get("children", [])
    for index, child in enumerate(children):
        if child.get("id") == node_id:
            children[index] = replacement
            return True
        if _replace_node(child, node_id, replacement):
            return True
    return False


def _normalized_sizes(node: dict[str, Any]) -> list[float]:
    children = node.get("children", [])
    raw_sizes = node.get("sizes", [])
    if (
        not isinstance(raw_sizes, list)
        or len(raw_sizes) != len(children)
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or float(value) <= 0
            for value in raw_sizes
        )
    ):
        return [1.0] * len(children)
    return [float(value) for value in raw_sizes]


def validate_layout(layout: dict[str, Any]) -> None:
    """Validate structure and every leaf's computed virtual screen size."""
    if not isinstance(layout, dict):
        raise ValidationError("大屏布局格式无效。")

    node_ids: set[str] = set()
    node_count = 0

    def visit(
        node: dict[str, Any],
        width: float,
        height: float,
        depth: int,
    ) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_LAYOUT_NODES:
            raise ValidationError("大屏分块数量过多。")
        if depth > MAX_LAYOUT_DEPTH:
            raise ValidationError("大屏分块层级过深。")
        if not isinstance(node, dict):
            raise ValidationError("大屏布局节点格式无效。")

        node_id = str(node.get("id", ""))
        if not node_id or node_id in node_ids:
            raise ValidationError("大屏布局包含无效或重复的节点。")
        node_ids.add(node_id)

        kind = node.get("kind")
        if kind == "slot":
            widget_id = node.get("widget_id")
            if widget_id is not None and (
                isinstance(widget_id, bool)
                or not isinstance(widget_id, int)
                or widget_id <= 0
            ):
                raise ValidationError("大屏格子绑定的图表无效。")
            if (
                width + 1e-6 < MIN_SLOT_WIDTH_UNITS
                or height + 1e-6 < MIN_SLOT_HEIGHT_UNITS
            ):
                raise ValidationError(
                    "分割后的区域小于最小展示尺寸，无法继续分割。"
                )
            return

        if kind != "split":
            raise ValidationError("大屏布局节点类型无效。")
        direction = node.get("direction")
        if direction not in {"horizontal", "vertical"}:
            raise ValidationError("大屏分割方向无效。")
        children = node.get("children")
        if not isinstance(children, list) or len(children) < 2:
            raise ValidationError("一个分割区域至少需要两个子区域。")

        sizes = _normalized_sizes(node)
        node["sizes"] = sizes
        total = sum(sizes)
        for child, size in zip(children, sizes):
            ratio = size / total
            child_width = width * ratio if direction == "horizontal" else width
            child_height = (
                height * ratio if direction == "vertical" else height
            )
            visit(child, child_width, child_height, depth + 1)

    visit(
        layout,
        DASHBOARD_WIDTH_UNITS,
        DASHBOARD_HEIGHT_UNITS,
        0,
    )


def assign_widget(
    layout: dict[str, Any],
    slot_id: str,
    widget_id: int | None,
) -> dict[str, Any]:
    updated = deepcopy(layout)
    target = find_node(updated, slot_id)
    if target is None or target.get("kind") != "slot":
        raise ValidationError("目标大屏区域不存在。")
    target["widget_id"] = widget_id
    validate_layout(updated)
    return updated


def assign_to_first_empty(
    layout: dict[str, Any],
    widget_id: int,
) -> tuple[dict[str, Any], bool]:
    updated = deepcopy(layout)
    if any(
        slot.get("widget_id") == widget_id
        for slot in iter_slots(updated)
    ):
        return updated, True
    for slot in iter_slots(updated):
        if slot.get("widget_id") is None:
            slot["widget_id"] = widget_id
            validate_layout(updated)
            return updated, True
    return updated, False


def move_widget(
    layout: dict[str, Any],
    source_slot_id: str,
    target_slot_id: str,
) -> dict[str, Any]:
    updated = deepcopy(layout)
    source = find_node(updated, source_slot_id)
    target = find_node(updated, target_slot_id)
    if (
        source is None
        or target is None
        or source.get("kind") != "slot"
        or target.get("kind") != "slot"
    ):
        raise ValidationError("拖动的源区域或目标区域不存在。")
    source["widget_id"], target["widget_id"] = (
        target.get("widget_id"),
        source.get("widget_id"),
    )
    validate_layout(updated)
    return updated


def split_slot(
    layout: dict[str, Any],
    slot_id: str,
    edge: str,
) -> dict[str, Any]:
    updated = deepcopy(layout)
    target = find_node(updated, slot_id)
    if target is None or target.get("kind") != "slot":
        raise ValidationError("要分割的大屏区域不存在。")
    if edge not in {"top", "right", "bottom", "left"}:
        raise ValidationError("大屏分割方向无效。")

    current = _slot(target.get("widget_id"))
    empty = _slot()
    direction = "horizontal" if edge in {"left", "right"} else "vertical"
    children = (
        [empty, current]
        if edge in {"left", "top"}
        else [current, empty]
    )
    replacement = _split(direction, children, [1, 1])
    if updated.get("id") == slot_id:
        updated = replacement
    elif not _replace_node(updated, slot_id, replacement):
        raise ValidationError("要分割的大屏区域不存在。")
    validate_layout(updated)
    return updated


def resize_split(
    layout: dict[str, Any],
    split_id: str,
    sizes: list[float],
) -> dict[str, Any]:
    updated = deepcopy(layout)
    target = find_node(updated, split_id)
    if target is None or target.get("kind") != "split":
        raise ValidationError("要调整的大屏分割区域不存在。")
    if len(sizes) != len(target.get("children", [])):
        raise ValidationError("大屏分割比例无效。")
    target["sizes"] = [round(float(value), 6) for value in sizes]
    validate_layout(updated)
    return updated


class DashboardService:
    """Own the current user's saved chart library and single dashboard."""

    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service

    @staticmethod
    def _signature(
        datasource_id: int,
        sql_query: str,
        visualization_spec: dict[str, Any],
    ) -> str:
        canonical = json.dumps(
            {
                "datasource_id": int(datasource_id),
                "sql_query": str(sql_query).strip(),
                "visualization_spec": visualization_spec,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _hydrate_query(row: Any) -> SavedDashboardQuery:
        try:
            visualization_spec = json.loads(
                str(row["visualization_spec_json"] or "{}")
            )
        except json.JSONDecodeError:
            visualization_spec = {}
        if not isinstance(visualization_spec, dict):
            visualization_spec = {}
        return SavedDashboardQuery(
            id=int(row["id"]),
            owner_user_id=int(row["owner_user_id"]),
            datasource_id=int(row["datasource_id"]),
            datasource_version=int(row["datasource_version"]),
            datasource_name=str(row["datasource_name"]),
            title=str(row["title"]),
            question=str(row["question"]),
            sql_query=str(row["sql_query"]),
            visualization_spec=visualization_spec,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get_dashboard(
        self,
        user_id: int,
        session_version: int,
    ) -> Dashboard:
        self.auth_service.validate_session(user_id, session_version)
        with self.auth_service.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT owner_user_id, title, layout_json, updated_at
                FROM dashboard_layouts
                WHERE owner_user_id = ?
                """,
                (int(user_id),),
            ).fetchone()
            if row is None:
                layout = default_dashboard_layout()
                now = utc_timestamp()
                connection.execute(
                    """
                    INSERT INTO dashboard_layouts (
                        owner_user_id,
                        title,
                        layout_json,
                        updated_at
                    )
                    VALUES (?, '可视化数据大屏', ?, ?)
                    """,
                    (
                        int(user_id),
                        json.dumps(layout, ensure_ascii=False),
                        now,
                    ),
                )
                return Dashboard(
                    owner_user_id=int(user_id),
                    title="可视化数据大屏",
                    layout=layout,
                    updated_at=now,
                )

        try:
            layout = json.loads(str(row["layout_json"]))
            validate_layout(layout)
        except (json.JSONDecodeError, ValidationError):
            layout = default_dashboard_layout()
        return Dashboard(
            owner_user_id=int(row["owner_user_id"]),
            title=str(row["title"]),
            layout=layout,
            updated_at=str(row["updated_at"]),
        )

    def save_layout(
        self,
        user_id: int,
        session_version: int,
        layout: dict[str, Any],
    ) -> Dashboard:
        self.auth_service.validate_session(user_id, session_version)
        candidate = deepcopy(layout)
        validate_layout(candidate)
        widget_ids = sorted(
            {
                int(slot["widget_id"])
                for slot in iter_slots(candidate)
                if slot.get("widget_id") is not None
            }
        )
        now = utc_timestamp()
        with self.auth_service.database.transaction() as connection:
            self.auth_service._validate_session_in_connection(
                connection,
                user_id,
                session_version,
            )
            if widget_ids:
                placeholders = ",".join("?" for _ in widget_ids)
                rows = connection.execute(
                    f"""
                    SELECT id
                    FROM saved_dashboard_queries
                    WHERE owner_user_id = ? AND id IN ({placeholders})
                    """,
                    [int(user_id), *widget_ids],
                ).fetchall()
                if {int(row["id"]) for row in rows} != set(widget_ids):
                    raise ValidationError(
                        "大屏中包含不属于当前账号的查询图表。"
                    )
            connection.execute(
                """
                INSERT INTO dashboard_layouts (
                    owner_user_id,
                    title,
                    layout_json,
                    updated_at
                )
                VALUES (?, '可视化数据大屏', ?, ?)
                ON CONFLICT(owner_user_id) DO UPDATE SET
                    layout_json = excluded.layout_json,
                    updated_at = excluded.updated_at
                """,
                (
                    int(user_id),
                    json.dumps(candidate, ensure_ascii=False),
                    now,
                ),
            )
            self.auth_service._audit(
                connection,
                actor_user_id=user_id,
                action="save_dashboard_layout",
                outcome="success",
                target_type="dashboard",
                target_id=user_id,
                details={"widget_count": len(widget_ids)},
            )
        return Dashboard(
            owner_user_id=int(user_id),
            title="可视化数据大屏",
            layout=candidate,
            updated_at=now,
        )

    def save_query(
        self,
        *,
        user_id: int,
        session_version: int,
        datasource_id: int,
        datasource_version: int,
        title: str,
        question: str,
        sql_query: str,
        visualization_spec: dict[str, Any],
    ) -> SavedDashboardQuery:
        self.auth_service.validate_session(user_id, session_version)
        datasource = self.auth_service.resolve_authorized_datasource(
            user_id,
            datasource_id,
            QUERY_DATA,
        )
        normalized_title = str(title or question or "查询图表").strip()[:120]
        normalized_question = str(question or "").strip()[:2_000]
        normalized_sql = str(sql_query or "").strip()
        if not normalized_title or not normalized_sql:
            raise ValidationError("当前查询缺少可保存的标题或 SQL。")
        if not isinstance(visualization_spec, dict):
            raise ValidationError("当前查询的图表配置无效。")
        from infrastructure.db_manager import DatabaseManager

        sql_error = DatabaseManager._read_only_query_error(normalized_sql)
        if sql_error:
            raise ValidationError(
                f"当前查询不能保存到大屏：{sql_error}"
            )
        signature = self._signature(
            datasource_id,
            normalized_sql,
            visualization_spec,
        )
        now = utc_timestamp()

        with self.auth_service.database.transaction() as connection:
            self.auth_service._validate_session_in_connection(
                connection,
                user_id,
                session_version,
            )
            connection.execute(
                """
                INSERT INTO saved_dashboard_queries (
                    owner_user_id,
                    datasource_id,
                    datasource_version,
                    signature,
                    title,
                    question,
                    sql_query,
                    visualization_spec_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_user_id, signature) DO UPDATE SET
                    datasource_version = excluded.datasource_version,
                    title = excluded.title,
                    question = excluded.question,
                    updated_at = excluded.updated_at
                """,
                (
                    int(user_id),
                    datasource.id,
                    int(datasource.version),
                    signature,
                    normalized_title,
                    normalized_question,
                    normalized_sql,
                    json.dumps(visualization_spec, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT q.*, d.display_name AS datasource_name
                FROM saved_dashboard_queries q
                JOIN datasources d ON d.id = q.datasource_id
                WHERE q.owner_user_id = ? AND q.signature = ?
                """,
                (int(user_id), signature),
            ).fetchone()
            self.auth_service._audit(
                connection,
                actor_user_id=user_id,
                action="save_dashboard_query",
                outcome="success",
                target_type="dashboard_query",
                target_id=int(row["id"]),
                datasource_id=datasource.id,
            )
        return self._hydrate_query(row)

    def list_queries(
        self,
        user_id: int,
        session_version: int,
    ) -> list[SavedDashboardQuery]:
        self.auth_service.validate_session(user_id, session_version)
        visible_ids = {
            item.id
            for item in self.auth_service.list_visible_datasources(user_id)
        }
        if not visible_ids:
            return []
        placeholders = ",".join("?" for _ in visible_ids)
        with self.auth_service.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT q.*, d.display_name AS datasource_name
                FROM saved_dashboard_queries q
                JOIN datasources d ON d.id = q.datasource_id
                WHERE q.owner_user_id = ?
                  AND q.datasource_id IN ({placeholders})
                  AND d.status = 'active'
                ORDER BY q.updated_at DESC, q.id DESC
                """,
                [int(user_id), *sorted(visible_ids)],
            ).fetchall()
        return [self._hydrate_query(row) for row in rows]

    def queries_by_id(
        self,
        user_id: int,
        session_version: int,
        query_ids: Iterable[int],
    ) -> dict[int, SavedDashboardQuery]:
        requested = sorted({int(query_id) for query_id in query_ids})
        if not requested:
            return {}
        available = {
            query.id: query
            for query in self.list_queries(user_id, session_version)
        }
        return {
            query_id: available[query_id]
            for query_id in requested
            if query_id in available
        }

    def add_query_to_dashboard(
        self,
        user_id: int,
        session_version: int,
        query_id: int,
    ) -> tuple[Dashboard, bool]:
        dashboard = self.get_dashboard(user_id, session_version)
        available = self.queries_by_id(
            user_id,
            session_version,
            [query_id],
        )
        if int(query_id) not in available:
            raise ValidationError("所选查询图表不存在或当前账号无权使用。")
        layout, placed = assign_to_first_empty(
            dashboard.layout,
            int(query_id),
        )
        if not placed:
            return dashboard, False
        return self.save_layout(
            user_id,
            session_version,
            layout,
        ), True
