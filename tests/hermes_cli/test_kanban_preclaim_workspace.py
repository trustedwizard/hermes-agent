"""B1 qualification: resolving a task workspace before an implementation claim."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli.kanban_development_workspace import resolve_preclaim_workspace


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(tmp_path, "init", "-b", "main", str(repo))
    (repo / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
    return repo


def _tracked_digest(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        check=True, capture_output=True,
    )
    digest = hashlib.sha256()
    for name in result.stdout.split(b"\0"):
        if name:
            digest.update(name)
            digest.update(b"\0")
            digest.update((repo / name.decode()).read_bytes())
    return digest.hexdigest()


def test_unclaimed_task_resolves_owned_worktree_without_running_implementation(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    import shlex
    marker = tmp_path / "hook-ran"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\ntouch " + shlex.quote(str(marker)) + "\n")
    hook.chmod(0o755)
    before = (_git(repo, "rev-parse", "HEAD"), _git(repo, "branch", "--show-current"), _tracked_digest(repo))
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="preclaim fixture", workspace_kind="worktree",
            workspace_path=str(repo), initial_status="blocked",
        )
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "ready"
        workspace, branch = resolve_preclaim_workspace(task, source_repo=repo)
        workspace = workspace.resolve()
        assert workspace == (repo / ".worktrees" / task_id).resolve()
        assert branch == f"wt/{task_id}"
        assert _git(workspace, "branch", "--show-current") == branch
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0] == 0
        assert kb.get_task(conn, task_id).status == "ready"
        assert resolve_preclaim_workspace(task, source_repo=repo) == (workspace, branch)
        assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2
    import sqlite3
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert not marker.exists()
    after = (_git(repo, "rev-parse", "HEAD"), _git(repo, "branch", "--show-current"), _tracked_digest(repo))
    assert after == before


def test_preparation_rejects_occupied_canonical_branch(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="occupied fixture", workspace_kind="worktree",
            workspace_path=str(repo), initial_status="blocked",
        )
        task = kb.get_task(conn, task_id)
        occupied = repo / ".worktrees" / task_id
        occupied.parent.mkdir()
        _git(repo, "worktree", "add", str(occupied), "-b", "wt/other", "HEAD")
        with pytest.raises(ValueError, match="occupied"):
            resolve_preclaim_workspace(task, source_repo=repo)
        assert _git(occupied, "branch", "--show-current") == "wt/other"
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0


def test_preparation_rejects_symlink_parent_before_writes(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".worktrees").symlink_to(outside, target_is_directory=True)
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="symlink fixture", workspace_kind="worktree",
            workspace_path=str(repo), initial_status="blocked",
        )
        with pytest.raises(ValueError):
            resolve_preclaim_workspace(kb.get_task(conn, task_id), source_repo=repo)
    assert list(outside.iterdir()) == []
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
