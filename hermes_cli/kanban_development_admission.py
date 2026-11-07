"""Board-local checked-work admission; trusted supervisor, not OS isolation."""
from __future__ import annotations

import json
import os


MODES = frozenset({"observe", "enforce", "paused"})


def install_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS kanban_dev_policy (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        mode TEXT NOT NULL CHECK (mode IN ('observe', 'enforce', 'paused'))
    )""")
    conn.execute("CREATE TABLE IF NOT EXISTS kanban_dev_grandfathered (task_id TEXT PRIMARY KEY)")
    conn.execute("""CREATE TABLE IF NOT EXISTS kanban_dev_inflight (
        task_id TEXT NOT NULL, run_id INTEGER NOT NULL, PRIMARY KEY(task_id, run_id)
    )""")


def mode(conn):
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_dev_policy'"
    ).fetchone()
    if present is None:
        return None
    row = conn.execute("SELECT mode FROM kanban_dev_policy WHERE singleton=1").fetchone()
    if row is None:
        return None
    if row["mode"] not in MODES:
        raise ValueError("invalid persisted admission mode")
    return row["mode"]


def set_mode(conn, value):
    from hermes_cli.kanban_db_connect import write_txn

    if value not in MODES:
        raise ValueError("unsupported admission mode")
    if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"):
        raise PermissionError("admission policy is supervisor-owned")
    with write_txn(conn):
        install_schema(conn)
        previous_mode = mode(conn)
        if previous_mode is None:
            conn.execute("INSERT INTO kanban_dev_grandfathered SELECT id FROM tasks")
            conn.execute("INSERT INTO kanban_dev_policy VALUES(1, ?)", (value,))
        else:
            if previous_mode == "observe" and value in {"enforce", "paused"}:
                conn.execute(
                    "INSERT OR IGNORE INTO kanban_dev_inflight "
                    "SELECT id, current_run_id FROM tasks WHERE status='running' "
                    "AND current_run_id IS NOT NULL AND id NOT IN "
                    "(SELECT task_id FROM kanban_dev_contracts) "
                    "AND id NOT IN (SELECT task_id FROM kanban_dev_grandfathered)"
                )
            conn.execute("UPDATE kanban_dev_policy SET mode=? WHERE singleton=1", (value,))


def governed(conn, task_id):
    if mode(conn) is None:
        return False
    task = conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        return False
    return conn.execute(
        "SELECT 1 FROM kanban_dev_grandfathered WHERE task_id=?", (task_id,)
    ).fetchone() is None


def claim_reason(conn, task_id):
    from hermes_cli.kanban_development_contract import get_contract

    current = mode(conn)
    if current is None or current == "observe" or not governed(conn, task_id):
        return None
    if current == "paused":
        return "admission_paused"
    if get_contract(conn, task_id) is None:
        return "contract_required"
    return None


def allows_unenrolled_finish(conn, task_id, run_id=None):
    current = mode(conn)
    if current not in {"enforce", "paused"} or not governed(conn, task_id):
        return True
    if run_id is None:
        return False
    return conn.execute(
        "SELECT 1 FROM kanban_dev_inflight WHERE task_id=? AND run_id=?",
        (task_id, int(run_id)),
    ).fetchone() is not None


def admit_claim(conn, task_id):
    """Called inside the native claim transaction; no new run on denial."""
    from hermes_cli.kanban_development_contract import get_contract
    from hermes_cli.kanban_db import _append_event

    current = mode(conn)
    reason = claim_reason(conn, task_id)
    observed_missing = current == "observe" and governed(conn, task_id) and get_contract(conn, task_id) is None
    if reason or observed_missing:
        payload = {"reason": reason or "would_require_contract"}
        encoded = json.dumps(payload, sort_keys=True)
        previous = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='development_admission' ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        if previous is None or json.dumps(json.loads(previous["payload"]), sort_keys=True) != encoded:
            _append_event(conn, task_id, "development_admission", payload)
    return reason is None
