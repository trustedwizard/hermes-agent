"""Compatibility adapter for dispatcher spawn callbacks."""
from __future__ import annotations
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task

def _call_spawn_fn(spawn_fn, task: Task, workspace: str, board: Optional[str]) -> Optional[int]:
    """Back-compat: older spawn_fn signatures (and test stubs) accept only
    ``(task, workspace)``; pass ``board`` only when the callable supports it."""
    import inspect
    try:
        sig = inspect.signature(spawn_fn)
        if "board" in sig.parameters:
            return spawn_fn(task, workspace, board=board)
        return spawn_fn(task, workspace)
    except (TypeError, ValueError):
        return spawn_fn(task, workspace)
