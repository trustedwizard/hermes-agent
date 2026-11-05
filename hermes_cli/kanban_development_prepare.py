"""Deterministic supervisor preparation, separate from implementation runs."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_development_admission as admission
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_development_catalog import load_catalog, compile_selection, revalidate_protected_artifacts
from hermes_cli.kanban_development_contract import enroll_checked, fingerprint, get_contract
from hermes_cli.kanban_development_workspace import resolve_preclaim_workspace


@dataclass(frozen=True)
class PreparationSpec:
    source_repo: str
    catalog_path: str
    selected_check_ids: tuple[str, ...]
    requested_outcome: str = 'local_verified'


def install_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS kanban_dev_preparations (
        task_id TEXT PRIMARY KEY, status TEXT NOT NULL, token TEXT NOT NULL,
        lease_until REAL NOT NULL, attempts INTEGER NOT NULL,
        input_digest TEXT NOT NULL, next_eligible REAL NOT NULL DEFAULT 0,
        reason TEXT, receipt TEXT
    )""")


def _supervisor():
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise PermissionError('preparation is supervisor-owned')


def _eligibility(conn, task, spec):
    if task is None or admission.mode(conn) != 'enforce' or not admission.governed(conn, task.id):
        return 'ineligible'
    if task.status not in ('ready', 'todo', 'triage') or task.current_run_id is not None or task.claim_lock or task.worker_pid:
        return 'ineligible'
    if spec.requested_outcome != 'local_verified' or task.completion_contract not in (None, 'local-only'):
        return 'unsupported_outcome'
    if task.workspace_kind != 'worktree' or task.project_id or task.branch_name not in (None, f'wt/{task.id}'):
        return 'unsupported_workspace'
    return None


def _digest(task, spec, catalog_digest):
    fields = ('id', 'title', 'body', 'assignee', 'status', 'workspace_kind', 'workspace_path',
              'branch_name', 'project_id', 'completion_contract', 'block_kind')
    data = {'task': {key: getattr(task, key) for key in fields}, 'spec': asdict(spec), 'catalog': catalog_digest}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _catalog(spec):
    loaded = load_catalog(spec.catalog_path, spec.source_repo)
    # Check selected IDs and timeout compatibility before any reservation or Git write.
    compile_selection(loaded, spec.source_repo, spec.selected_check_ids)
    return loaded


def reserve(conn, task_id, spec, *, now=None, lease_seconds=300):
    _supervisor()
    now = time.time() if now is None else now
    if not 1 <= lease_seconds <= 900:
        raise ValueError('bounded preparation lease required')
    catalog = _catalog(spec)
    with write_txn(conn):
        install_schema(conn)
        task = kb.get_task(conn, task_id)
        reason = _eligibility(conn, task, spec)
        if reason:
            return {'status': reason}
        digest = _digest(task, spec, catalog.digest)
        row = conn.execute('SELECT * FROM kanban_dev_preparations WHERE task_id=?', (task_id,)).fetchone()
        if row and row['status'] == 'prepared':
            found = get_contract(conn, task_id)
            receipt = json.loads(row['receipt'])
            if row['input_digest'] == digest and found and found[1] == receipt['contract_digest']:
                return {'status': 'prepared'}
            return {'status': 'input_changed'}
        if row and row['status'] == 'preparing' and row['lease_until'] > now:
            return {'status': 'preparing'}
        if row and row['attempts'] >= 3:
            return {'status': 'budget_exhausted'}
        if row and row['next_eligible'] > now:
            return {'status': 'retry_wait'}
        if get_contract(conn, task_id):
            return {'status': 'existing_contract'}
        token = uuid.uuid4().hex
        attempts = row['attempts'] + 1 if row else 1
        conn.execute("""INSERT INTO kanban_dev_preparations
            (task_id,status,token,lease_until,attempts,input_digest,next_eligible,reason,receipt)
            VALUES(?, 'preparing', ?, ?, ?, ?, 0, NULL, NULL)
            ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,token=excluded.token,
            lease_until=excluded.lease_until,attempts=excluded.attempts,input_digest=excluded.input_digest,
            next_eligible=0,reason=NULL,receipt=NULL""",
            (task_id, token, now + lease_seconds, attempts, digest))
        return {'status': 'reserved', 'token': token}


def publish(conn, task_id, spec, token, compiled, *, now=None):
    _supervisor()
    now = time.time() if now is None else now
    if compiled is not None:
        fingerprint(compiled.contract.workspace)
    with write_txn(conn):
        row = conn.execute('SELECT * FROM kanban_dev_preparations WHERE task_id=?', (task_id,)).fetchone()
        task = kb.get_task(conn, task_id)
        if not row or row['token'] != token or row['status'] != 'preparing' or row['lease_until'] <= now:
            return False
        if _eligibility(conn, task, spec):
            return False
        catalog = _catalog(spec)
        if row['input_digest'] != _digest(task, spec, catalog.digest):
            return False
        expected_workspace = Path(spec.source_repo).resolve() / '.worktrees' / task_id
        if compiled is None or Path(compiled.contract.workspace) != expected_workspace or compiled.catalog_digest != catalog.digest:
            return False
        expected = compile_selection(load_catalog(spec.catalog_path, expected_workspace), expected_workspace, spec.selected_check_ids)
        if compiled != expected:
            return False
        revalidate_protected_artifacts(compiled)
        digest = enroll_checked(conn, task_id, compiled.contract)
        conn.execute('UPDATE tasks SET workspace_path=?,branch_name=? WHERE id=?',
                     (str(expected_workspace), f'wt/{task_id}', task_id))
        receipt = json.dumps({'contract_digest': digest, 'catalog_digest': catalog.digest,
                              'requested_outcome': spec.requested_outcome,
                              'protected_artifacts': [asdict(a) for a in compiled.protected_artifacts]}, sort_keys=True)
        published_digest = _digest(kb.get_task(conn, task_id), spec, catalog.digest)
        conn.execute("UPDATE kanban_dev_preparations SET status='prepared',input_digest=?,receipt=?,reason=NULL WHERE task_id=? AND token=?",
                     (published_digest, receipt, task_id, token))
        return True


def protected_checks_valid(conn, task_id, contract, digest):
    """Validate the frozen supervisor artifacts at execution and finish boundaries."""
    from hermes_cli.kanban_development_catalog import CompiledSelection, ProtectedArtifact
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_dev_preparations'").fetchone():
        return True
    row = conn.execute('SELECT status,receipt FROM kanban_dev_preparations WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        return True  # Legacy supervisor enrollment has no preparation receipt.
    if row['status'] != 'prepared':
        return False
    try:
        data = json.loads(row['receipt'])
        if data['contract_digest'] != digest or not data['protected_artifacts']:
            return False
        artifacts = tuple(ProtectedArtifact(**item) for item in data['protected_artifacts'])
        compiled = CompiledSelection(contract, data['catalog_digest'], artifacts, ())
        revalidate_protected_artifacts(compiled)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return False
    return True


def prepare_development_task(conn, task_id, spec):
    reservation = reserve(conn, task_id, spec)
    if reservation['status'] != 'reserved':
        return reservation
    token = reservation['token']
    try:
        task = kb.get_task(conn, task_id)
        workspace, _ = resolve_preclaim_workspace(task, source_repo=spec.source_repo)
        compiled = compile_selection(load_catalog(spec.catalog_path, workspace), workspace, spec.selected_check_ids)
        if publish(conn, task_id, spec, token, compiled):
            return {'status': 'prepared'}
        reason = 'superseded'
    except (OSError, ValueError, RuntimeError) as exc:
        reason = 'workspace_unavailable'
        error_class = type(exc).__name__
        with write_txn(conn):
            conn.execute("UPDATE kanban_dev_preparations SET reason=? WHERE task_id=? AND token=? AND status='preparing'",
                         (error_class, task_id, token))
    with write_txn(conn):
        conn.execute("UPDATE kanban_dev_preparations SET status='failed',reason=?,next_eligible=? WHERE task_id=? AND token=? AND status='preparing'",
                     (reason, time.time() + 30, task_id, token))
    return {'status': reason}
