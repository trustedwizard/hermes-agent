"""Supervisor-owned, local-only intake routes for checked development tasks."""
from __future__ import annotations

import json
import os
import time

from hermes_cli import kanban_development_admission as admission
from hermes_cli import kanban_development_prepare as preparation
from hermes_cli.kanban_db import _append_event
from hermes_cli.kanban_db_connect import write_txn


_ROUTE_TABLE = "kanban_dev_routes"
_EVENT = "development_preparation"


def _supervisor():
    if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"):
        raise PermissionError("development routes are supervisor-owned")


def _install(conn):
    preparation.install_schema(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS kanban_dev_routes (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        source_repo TEXT NOT NULL, catalog_path TEXT NOT NULL,
        selected_check_ids TEXT NOT NULL, requested_outcome TEXT NOT NULL
    )""")


def configure_route(conn, spec):
    """Validate the selected catalog entries before replacing the board route."""
    _supervisor()
    preparation._catalog(spec)
    with write_txn(conn):
        _install(conn)
        conn.execute("""INSERT INTO kanban_dev_routes
            (singleton,source_repo,catalog_path,selected_check_ids,requested_outcome)
            VALUES(1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET
            source_repo=excluded.source_repo,catalog_path=excluded.catalog_path,
            selected_check_ids=excluded.selected_check_ids,requested_outcome=excluded.requested_outcome""",
            (spec.source_repo, spec.catalog_path,
             json.dumps(list(spec.selected_check_ids), separators=(",", ":")), spec.requested_outcome))
    return spec


def _route(conn):
    found = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_ROUTE_TABLE,)).fetchone()
    if not found:
        return None
    row = conn.execute("SELECT * FROM kanban_dev_routes WHERE singleton=1").fetchone()
    if row is None:
        return None
    return preparation.PreparationSpec(row["source_repo"], row["catalog_path"],
                                       tuple(json.loads(row["selected_check_ids"])), row["requested_outcome"])


def _event_result(conn, task_id, result):
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    prior = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                         (task_id, _EVENT)).fetchone()
    if prior and json.dumps(json.loads(prior["payload"]), sort_keys=True, separators=(",", ":")) == encoded:
        return False
    _append_event(conn, task_id, _EVENT, result)
    return True


def prepare_pending(conn, limit=2, now=None, *, dry_run=False):
    """Prepare at most `limit` eligible assigned tasks in stable creation order."""
    if dry_run:
        return []
    _supervisor()
    if type(limit) is not int or not 0 <= limit <= 32:
        raise ValueError("limit must be an integer from zero to 32")
    now = time.time() if now is None else now
    if limit == 0 or admission.mode(conn) != "enforce":
        return []
    with write_txn(conn):
        _install(conn)
        spec = _route(conn)
        if spec is None:
            return []
        if spec.requested_outcome != "local_verified":
            return []
        candidates = conn.execute("""SELECT t.id FROM tasks t
            LEFT JOIN kanban_dev_preparations p ON p.task_id=t.id
            WHERE t.status IN ('ready','todo') AND t.assignee IS NOT NULL AND t.assignee!=''
            AND t.workspace_kind='worktree' AND t.workspace_path=? AND t.project_id IS NULL
            AND t.current_run_id IS NULL AND t.claim_lock IS NULL AND t.worker_pid IS NULL
            AND (t.branch_name IS NULL OR t.branch_name='wt/' || t.id)
            AND (t.completion_contract IS NULL OR t.completion_contract='local-only')
            AND NOT EXISTS(SELECT 1 FROM kanban_dev_grandfathered g WHERE g.task_id=t.id)
            AND NOT EXISTS(SELECT 1 FROM kanban_dev_contracts c WHERE c.task_id=t.id)
            AND (p.task_id IS NULL OR (p.status!='prepared' AND p.attempts<3
                AND p.next_eligible<=? AND (p.status!='preparing' OR p.lease_until<=?)))
            ORDER BY t.created_at,t.id LIMIT ?""", (spec.source_repo, now, now, limit)).fetchall()
        selected = [row['id'] for row in candidates]
    results = []
    for task_id in selected:
        result = preparation.prepare_development_task(conn, task_id, spec)
        # Expose only a bounded status enum; exceptions/errors/details never enter task events.
        status = result.get("status", "failed")
        safe = {"status": status if status in {"prepared", "ineligible", "unsupported_outcome", "unsupported_workspace", "workspace_unavailable", "superseded", "input_changed", "budget_exhausted", "retry_wait", "existing_contract", "preparing"} else "failed"}
        with write_txn(conn):
            _event_result(conn, task_id, safe)
        results.append({"task_id": task_id, **safe})
    return results
