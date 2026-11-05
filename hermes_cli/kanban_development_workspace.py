"""Safe workspace preparation for unclaimed development tasks."""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path


def resolve_preclaim_workspace(task, *, source_repo, board=None):
    """Create/recover a task's canonical worktree below a trusted Git checkout."""
    from hermes_cli import kanban_db_workspace as kbw

    root_arg = Path(source_repo).expanduser().absolute()
    if any(part.is_symlink() for part in (root_arg, *root_arg.parents)):
        raise ValueError("source_repo contains a symlink")
    root = root_arg.resolve(strict=True)
    if not root.is_dir() or kbw._git_toplevel(root) != root:
        raise ValueError("source_repo must be a real Git repository root")
    task_id = str(task.id)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", task_id) or task_id in {".", ".."}:
        raise ValueError("task id is not a safe path component")
    branch = (task.branch_name or "").strip() or f"wt/{task_id}"
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith("-"):
        raise ValueError("task branch name is invalid")
    target = root / ".worktrees" / task_id
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("canonical workspace path contains a symlink")
    if target.resolve(strict=False) != target:
        raise ValueError("canonical workspace escapes its source repository")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        actual_branch = kbw._git_current_branch(target)
        if not kbw._is_linked_worktree_checkout(target) or actual_branch != branch:
            raise ValueError("canonical workspace is occupied by a different checkout or branch")
    prepared = replace(task, workspace_path=str(root))
    path, resolved_branch = kbw._resolve_worktree_workspace(prepared, board=board)
    path = Path(path).resolve(strict=True)
    if (path != target.resolve(strict=True)
            or kbw._git_toplevel(path) != path
            or kbw._git_common_dir(path) != kbw._git_common_dir(root)
            or kbw._git_current_branch(path) != branch
            or path.is_symlink()):
        raise ValueError("resolved workspace failed repository, containment, or branch validation")
    if resolved_branch != branch:
        raise ValueError("resolver returned an unexpected branch")
    return path, branch
