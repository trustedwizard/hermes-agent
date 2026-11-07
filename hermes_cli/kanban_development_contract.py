"""Native opt-in checked-work contracts; trusted supervisor, not root confinement."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from dataclasses import asdict, dataclass

MAX_ATTEMPTS = 3
MAX_OUTPUT = 1024 * 1024

@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]

@dataclass(frozen=True)
class Contract:
    workspace: str
    checks: tuple[Check, ...]
    timeout: int = 60

    def __post_init__(self):
        if not Path(self.workspace).is_absolute() or not 1 <= self.timeout <= 900:
            raise ValueError('absolute workspace and bounded timeout required')
        if not self.checks or len(self.checks) > 32 or len({c.name for c in self.checks}) != len(self.checks):
            raise ValueError('one to 32 uniquely named checks required')
        for c in self.checks:
            if not c.name or not c.argv or not all(isinstance(a, str) and a and '\0' not in a for a in c.argv):
                raise ValueError('named argv check required')

    def json(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(',', ':'))


def install_schema(conn):
    for sql in (
        'CREATE TABLE IF NOT EXISTS kanban_dev_contracts (task_id TEXT PRIMARY KEY, contract_json TEXT NOT NULL, digest TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS kanban_dev_attempts (task_id TEXT NOT NULL, run_id INTEGER NOT NULL UNIQUE, PRIMARY KEY(task_id, run_id))',
        '''CREATE TABLE IF NOT EXISTS kanban_dev_checks (task_id TEXT NOT NULL, run_id INTEGER NOT NULL,
        check_name TEXT NOT NULL, contract_digest TEXT NOT NULL, fingerprint TEXT NOT NULL,
        status TEXT NOT NULL, exit_code INTEGER, output TEXT, PRIMARY KEY(task_id,run_id,check_name))''',
    ):
        conn.execute(sql)


def get_contract(conn, task_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_dev_contracts'").fetchone():
        return None
    row = conn.execute('SELECT contract_json,digest FROM kanban_dev_contracts WHERE task_id=?', (task_id,)).fetchone()
    if not row:
        return None
    data = json.loads(row['contract_json'])
    return Contract(data['workspace'], tuple(Check(c['name'], tuple(c['argv'])) for c in data['checks']), data['timeout']), row['digest']


def fingerprint(workspace):
    root = Path(workspace).resolve(strict=True)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], timeout=10)
    if Path(git('rev-parse', '--show-toplevel').decode().strip()).resolve() != root:
        raise ValueError('workspace must be repository root')
    head = git('rev-parse', 'HEAD').strip()
    paths = sorted(set(git('ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')) - {b''})
    digest = hashlib.sha256(head)
    size = 0
    for raw in paths:
        p = root / os.fsdecode(raw)
        digest.update(raw + b'\0')
        if p.is_symlink():
            data = os.readlink(p).encode()
        elif p.is_file():
            size += p.stat().st_size
            if size > 128 * 1024 * 1024:
                raise ValueError('verification source exceeds 128 MiB')
            data = p.read_bytes()
        elif not p.exists():
            data = b'<deleted>'
        else:
            raise ValueError('unsupported source entry')
        digest.update(str(p.lstat().st_mode if p.exists() else 0).encode() + b'\0' + hashlib.sha256(data).digest())
    return digest.hexdigest()


def enroll(conn, task_id, contract):
    from hermes_cli.kanban_db_connect import write_txn
    fingerprint(contract.workspace)
    with write_txn(conn):
        return enroll_checked(conn, task_id, contract)


def enroll_checked(conn, task_id, contract):
    """Persist a supervisor-validated workspace within its publication transaction."""
    if not conn.in_transaction:
        raise RuntimeError('enrollment publication requires a write transaction')
    raw = contract.json()
    digest = hashlib.sha256(raw.encode()).hexdigest()
    row = conn.execute('SELECT current_run_id,status FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not row or row['current_run_id'] is not None or row['status'] not in ('ready', 'todo', 'triage', 'blocked'):
        raise ValueError('enroll existing unclaimed task only')
    conn.execute('INSERT INTO kanban_dev_contracts VALUES(?,?,?)', (task_id, raw, digest))
    return digest


def attempts_remaining(conn, task_id):
    row = conn.execute('SELECT COUNT(*) n FROM kanban_dev_attempts WHERE task_id=?', (task_id,)).fetchone()
    return max(0, MAX_ATTEMPTS - row['n'])


def stop_reason(conn, task_id):
    if attempts_remaining(conn, task_id) <= 0:
        return 'attempts_exhausted'
    found = get_contract(conn, task_id)
    if found is None:
        return None
    contract, _ = found
    recent = conn.execute('SELECT run_id FROM kanban_dev_attempts WHERE task_id=? ORDER BY run_id DESC LIMIT 2', (task_id,)).fetchall()
    if len(recent) < 2:
        return None
    signatures = []
    for attempt in recent:
        rows = conn.execute('SELECT check_name,exit_code,fingerprint,status FROM kanban_dev_checks WHERE task_id=? AND run_id=? ORDER BY check_name', (task_id,attempt['run_id'])).fetchall()
        if len(rows) != len(contract.checks) or any(r['status'] != 'finished' for r in rows) or all(r['exit_code'] == 0 for r in rows):
            return None
        signatures.append([(r['check_name'],r['exit_code'],r['fingerprint']) for r in rows])
    if signatures[0] == signatures[1] and all(x[2] == fingerprint(contract.workspace) for x in signatures[0]):
        return 'no_progress'
    return None


def guard_claim(conn, task_id):
    found = get_contract(conn, task_id)
    reason = stop_reason(conn, task_id) if found is not None else None
    if reason:
        from hermes_cli.kanban_db import _append_event
        conn.execute("UPDATE tasks SET status = 'blocked', block_kind = 'needs_input' WHERE id = ? AND status = 'ready'", (task_id,))
        _append_event(conn, task_id, "development_attempts_exhausted", {"limit": MAX_ATTEMPTS, "reason": reason})
        return False
    return True


def record_attempt(conn, task_id, run_id):
    if get_contract(conn, task_id):
        conn.execute('INSERT INTO kanban_dev_attempts VALUES(?,?)', (task_id, run_id))


def current_run(conn, task_id, run_id):
    return conn.execute('''SELECT 1 FROM tasks t JOIN task_runs r ON r.id=t.current_run_id
        WHERE t.id=? AND t.current_run_id=? AND t.status='running' AND r.ended_at IS NULL''', (task_id, run_id)).fetchone() is not None


def execute(check, contract):
    """Bound combined output and wall time; terminate descendants after checking."""
    kill_group = getattr(os, 'killpg', None)
    kill_signal = getattr(signal, 'SIGKILL', None)
    if os.name != 'posix' or kill_group is None or kill_signal is None:
        raise OSError('checked execution requires POSIX process-group cleanup')
    output = bytearray()
    proc = subprocess.Popen(check.argv, cwd=contract.workspace, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    assert proc.stdout is not None
    deadline = time.monotonic() + contract.timeout
    code = None
    with selectors.DefaultSelector() as sel:
        sel.register(proc.stdout, selectors.EVENT_READ)
        while sel.get_map():
            if time.monotonic() >= deadline:
                code = 124
                break
            for key, _ in sel.select(min(.1, max(0, deadline-time.monotonic()))):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > MAX_OUTPUT:
                    code = 125
                    break
            if code is not None:
                break
    if code is None:
        try:
            code = proc.wait(timeout=max(.01, deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            code = 124
    try:
        kill_group(proc.pid, kill_signal)
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)
    proc.stdout.close()
    return code, bytes(output[:MAX_OUTPUT]).decode('utf-8', 'replace')


def run_checks(conn, task_id, run_id):
    from hermes_cli.kanban_db_connect import write_txn
    found = get_contract(conn, task_id)
    if found is None:
        raise ValueError('task not enrolled')
    contract, digest = found
    from hermes_cli.kanban_development_prepare import protected_checks_valid
    if not protected_checks_valid(conn, task_id, contract, digest):
        raise ValueError('protected verification artifacts changed')
    source = fingerprint(contract.workspace)
    results = []
    for check in contract.checks:
        with write_txn(conn):
            if not current_run(conn, task_id, run_id):
                raise ValueError('not current running owner')
            old = conn.execute('SELECT * FROM kanban_dev_checks WHERE task_id=? AND run_id=? AND check_name=?', (task_id, run_id, check.name)).fetchone()
            if old:
                results.append(dict(old))
                continue
            conn.execute("INSERT INTO kanban_dev_checks VALUES(?,?,?,?,?,'running',NULL,NULL)", (task_id, run_id, check.name, digest, source))
        try:
            code, output = execute(check, contract)
        except (OSError, ValueError) as exc:
            code, output = 126, str(exc)
        with write_txn(conn):
            if not current_run(conn, task_id, run_id):
                raise ValueError('check owner changed before receipt publication')
            if fingerprint(contract.workspace) != source:
                code, output = 127, output + '\nSource changed during verification.'
            if not protected_checks_valid(conn, task_id, contract, digest):
                code, output = 128, 'Protected verification artifacts changed.'
            updated = conn.execute("UPDATE kanban_dev_checks SET status='finished',exit_code=?,output=? WHERE task_id=? AND run_id=? AND check_name=? AND status='running'", (code, output, task_id, run_id, check.name))
            if updated.rowcount != 1:
                raise ValueError('check reservation changed')
        results.append({'check_name':check.name, 'exit_code':code, 'output':output})
    return results


def verified(conn, task_id, run_id):
    found = get_contract(conn, task_id)
    if found is None:
        from hermes_cli.kanban_development_admission import allows_unenrolled_finish
        return allows_unenrolled_finish(conn, task_id, run_id)
    if not current_run(conn, task_id, run_id):
        return False
    contract, digest = found
    from hermes_cli.kanban_development_prepare import protected_checks_valid
    if not protected_checks_valid(conn, task_id, contract, digest):
        return False
    try:
        source = fingerprint(contract.workspace)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return evidence_matches(conn, task_id, run_id, contract, digest, source) and review_binding_valid(conn, task_id, run_id, contract, digest, source)


def evidence_matches(conn, task_id, run_id, contract, digest, source):
    rows = conn.execute('SELECT * FROM kanban_dev_checks WHERE task_id=? AND run_id=?', (task_id, run_id)).fetchall()
    return (len(rows) == len(contract.checks) and {r['check_name'] for r in rows} == {c.name for c in contract.checks}
            and all(r['status']=='finished' and r['exit_code']==0 and r['contract_digest']==digest and r['fingerprint']==source for r in rows))


def review_binding_valid(conn, task_id, run_id, contract, digest, source):
    if conn.execute('SELECT 1 FROM kanban_dev_attempts WHERE task_id=? AND run_id=?', (task_id,run_id)).fetchone():
        return True
    # Review runs do not spend repair attempts and cannot introduce a new repair.
    previous = conn.execute('SELECT run_id FROM kanban_dev_attempts WHERE task_id=? ORDER BY run_id DESC LIMIT 1', (task_id,)).fetchone()
    return bool(previous and evidence_matches(conn, task_id, previous['run_id'], contract, digest, source))


def ensure_checks(conn, task_id, run_id):
    if get_contract(conn, task_id) is None:
        from hermes_cli.kanban_development_admission import allows_unenrolled_finish
        return allows_unenrolled_finish(conn, task_id, run_id)
    if run_id is None or not current_run(conn, task_id, run_id):
        return False
    from hermes_cli.kanban_development_prepare import protected_checks_valid
    contract, digest = get_contract(conn, task_id)
    if not protected_checks_valid(conn, task_id, contract, digest):
        return False
    run_checks(conn, task_id, run_id)
    return verified(conn, task_id, run_id)
