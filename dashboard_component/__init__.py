"""Bidirectional Streamlit component used by the dashboard canvas."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit.components.v1 as components


_FRONTEND = Path(__file__).resolve().parent / "frontend"
_dashboard_canvas = components.declare_component(
    "chatbi_dashboard_canvas",
    path=str(_FRONTEND),
)


def dashboard_canvas(
    *,
    payload: dict[str, Any],
    edit_mode: bool,
    key: str,
) -> str | None:
    """Render the canvas and return a serialized interaction event."""
    return _dashboard_canvas(
        payload=payload,
        editMode=bool(edit_mode),
        key=key,
        default=None,
    )
