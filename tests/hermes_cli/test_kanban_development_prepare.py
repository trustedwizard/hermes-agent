import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_development_admission as admission
from hermes_cli import kanban_development_prepare as prep


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD_CONTEXT', raising=False)
    db = tmp_path / 'board.db'
    kb.init_db(db)
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    admission.set_mode(conn, 'enforce')
    repo = tmp_path / 'source'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-b', 'main')
    (repo / 'answer.txt').write_text('correct')
    git('add', 'answer.txt')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-m', 'fixture')
    oracle = tmp_path / 'oracle.py'
    oracle.write_text("from pathlib import Path\nassert Path('answer.txt').read_text() == 'correct'\n")
    catalog = tmp_path / 'checks.json'
    catalog.write_text(json.dumps({'schema_version': 1, 'checks': [{
        'id': 'answer', 'argv': [sys.executable, str(oracle)], 'timeout': 10,
        'protected_artifacts': [{'path': str(oracle), 'sha256': hashlib.sha256(oracle.read_bytes()).hexdigest()}],
        'outcomes': ['success', 'failure']}]}))
    task = kb.create_task(conn, title='check answer', assignee='fixture', workspace_kind='worktree', workspace_path=str(repo))
    spec = prep.PreparationSpec(str(repo), str(catalog), ('answer',))
    yield conn, db, repo, task, spec, git
    conn.close()


def test_prepare_enrolls_without_run_and_is_idempotent(setup):
    conn, _, repo, task, spec, git = setup
    before = git('rev-parse', 'HEAD'), git('branch', '--show-current'), (repo / 'answer.txt').read_bytes()
    assert prep.prepare_development_task(conn, task, spec)['status'] == 'prepared'
    assert prep.prepare_development_task(conn, task, spec)['status'] == 'prepared'
    assert conn.execute('SELECT attempts FROM kanban_dev_preparations WHERE task_id=?', (task,)).fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0
    assert before == (git('rev-parse', 'HEAD'), git('branch', '--show-current'), (repo / 'answer.txt').read_bytes())
    claimed = kb.claim_task(conn, task)
    assert claimed is not None
    assert kb.complete_task(conn, task, result='verified', expected_run_id=claimed.current_run_id)


def test_two_connections_reserve_once_and_expiry_preserves_budget(setup):
    conn, db, _, task, spec, _ = setup
    first = prep.reserve(conn, task, spec, now=100, lease_seconds=10)
    other = sqlite3.connect(db, isolation_level=None)
    other.row_factory = sqlite3.Row
    try:
        assert prep.reserve(other, task, spec, now=101, lease_seconds=10)['status'] == 'preparing'
        second = prep.reserve(other, task, spec, now=111, lease_seconds=10)
        assert second['token'] != first['token']
        assert not prep.publish(conn, task, spec, first['token'], None, now=112)
    finally:
        other.close()
    third = prep.reserve(conn, task, spec, now=122, lease_seconds=10)
    assert third['status'] == 'reserved'
    assert prep.reserve(conn, task, spec, now=133, lease_seconds=10)['status'] == 'budget_exhausted'
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0


@pytest.mark.parametrize('mutation', ['paused', 'blocked', 'input'])
def test_publish_rechecks_authority_and_inputs(setup, monkeypatch, mutation):
    conn, _, _, task, spec, _ = setup
    original = prep.resolve_preclaim_workspace
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if mutation == 'paused':
            admission.set_mode(conn, 'paused')
        elif mutation == 'blocked':
            conn.execute("UPDATE tasks SET status='blocked',block_kind='needs_input' WHERE id=?", (task,))
        else:
            conn.execute("UPDATE tasks SET body='new input' WHERE id=?", (task,))
        return result
    monkeypatch.setattr(prep, 'resolve_preclaim_workspace', changed)
    assert prep.prepare_development_task(conn, task, spec)['status'] == 'superseded'
    assert conn.execute('SELECT COUNT(*) FROM kanban_dev_contracts').fetchone()[0] == 0


def test_sticky_block_and_external_outcome_are_not_downgraded(setup):
    conn, _, _, task, spec, _ = setup
    conn.execute("UPDATE tasks SET status='blocked',block_kind='needs_input' WHERE id=?", (task,))
    assert prep.prepare_development_task(conn, task, spec)['status'] == 'ineligible'
    conn.execute("UPDATE tasks SET status='ready',completion_contract='owner/repo' WHERE id=?", (task,))
    assert prep.prepare_development_task(conn, task, spec)['status'] == 'unsupported_outcome'
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0


@pytest.mark.parametrize('cached', [False, True])
def test_changed_oracle_blocks_actual_completion(setup, cached):
    from hermes_cli.kanban_development_contract import run_checks
    conn, _, _, task, spec, _ = setup
    assert prep.prepare_development_task(conn, task, spec)['status'] == 'prepared'
    claimed = kb.claim_task(conn, task)
    assert claimed is not None
    if cached:
        assert all(r['exit_code'] == 0 for r in run_checks(conn, task, claimed.current_run_id))
    oracle = Path(json.loads(Path(spec.catalog_path).read_text())['checks'][0]['protected_artifacts'][0]['path'])
    oracle.write_text('pass\n')
    assert not kb.complete_task(conn, task, result='must reject changed verifier', expected_run_id=claimed.current_run_id)
    assert kb.get_task(conn, task).status == 'running'


def test_route_prepares_eligible_work_after_older_ineligible_row(setup):
    from hermes_cli import kanban_development_routes as routes
    conn, _, repo, task, spec, _ = setup
    old = kb.create_task(conn, title='unassigned', workspace_kind='worktree', workspace_path=str(repo))
    conn.execute('UPDATE tasks SET created_at=0 WHERE id=?', (old,))
    routes.configure_route(conn, spec)
    assert routes.prepare_pending(conn, limit=1) == [{'task_id': task, 'status': 'prepared'}]
    assert routes.prepare_pending(conn, limit=1) == []
    assert kb.get_task(conn, old).current_run_id is None
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0
    with pytest.raises(ValueError):
        routes.prepare_pending(conn, limit=1000000)


def test_route_event_deduplication(setup):
    from hermes_cli import kanban_development_routes as routes
    conn, _, _, task, _, _ = setup
    assert routes._event_result(conn, task, {'status': 'prepared'})
    assert not routes._event_result(conn, task, {'status': 'prepared'})


@pytest.mark.parametrize('dry_run', [False, True])
def test_common_dispatch_tick_prepares_before_dispatch(setup, monkeypatch, dry_run):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_development_routes as routes
    from hermes_cli.kanban_development_contract import get_contract
    conn, db, _, task, spec, _ = setup
    routes.configure_route(conn, spec)
    observations = []
    def dispatch_boundary(connection, **kwargs):
        observations.append(get_contract(connection, task) is not None)
        return dispatch.DispatchResult()
    monkeypatch.setattr(dispatch, '_dispatch_once_locked', dispatch_boundary)
    monkeypatch.setattr(kb, 'kanban_db_path', lambda **kwargs: db)
    monkeypatch.setattr(kb, '_fire_dispatch_tick_hook', lambda *args, **kwargs: None)
    dispatch.dispatch_once(conn, dry_run=dry_run)
    assert observations == [not dry_run]
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0
