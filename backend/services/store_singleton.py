"""Process-wide RunStore for tools (ADK ToolContext cannot reach FastAPI app.state)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .run_store import RunStore

_store: RunStore | None = None


def set_run_store(store: RunStore) -> None:
    global _store
    _store = store


def get_run_store() -> RunStore:
    global _store
    if _store is None:
        from ..config import get_database_url
        from .run_store import RunStore

        _store = RunStore(get_database_url())
    return _store
