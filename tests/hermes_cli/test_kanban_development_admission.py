import sqlite3
import subprocess
import sys
import threading

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_development_admission as admission
from hermes_cli.kanban_development_contract import Check, Contract, enroll, run_checks


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    path = tmp_path / "board.sqlite"
    kb.init_db(path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    admission.install_schema(conn)
    yield conn
    conn.close()


def make_task(conn, **kwargs):
    return kb.create_task(conn, title="admission fixture", assignee="fixture", **kwargs)


def make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    git("init")
    (repo / "answer.txt").write_text("correct")
    git("add", "answer.txt")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "fixture")
    return repo


def enrolled(conn, tmp_path, *, status="ready"):
    name = f"repo-{conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]}"
    repo = make_repo(tmp_path, name)
    task = make_task(conn, workspace_kind="dir", workspace_path=str(repo))
    enroll(conn, task, Contract(str(repo), (Check("literal", (sys.executable, "-c", "from pathlib import Path; assert Path('answer.txt').read_text() == 'correct'")),)))
    return task


def current_run(conn, task):
    return conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task,)).fetchone()[0]


def test_only_existing_ids_are_grandfathered(board):
    old = make_task(board)
    admission.set_mode(board, "observe")
    new = make_task(board)
    admission.set_mode(board, "enforce")
    assert admission.claim_reason(board, old) is None
    assert admission.claim_reason(board, new) == "contract_required"
    admission.set_mode(board, "paused")
    admission.set_mode(board, "enforce")
    assert admission.claim_reason(board, new) == "contract_required"


def test_observe_does_not_prevent_work(board):
    admission.set_mode(board, "observe")
    task = make_task(board)
    assert admission.claim_reason(board, task) is None
    assert admission.allows_unenrolled_finish(board, task)


def test_enforce_and_pause_refuse_unenrolled_finish(board):
    admission.set_mode(board, "enforce")
    task = make_task(board)
    assert not admission.allows_unenrolled_finish(board, task)
    admission.set_mode(board, "paused")
    assert admission.claim_reason(board, task) == "admission_paused"
    assert not admission.allows_unenrolled_finish(board, task)


def test_no_policy_preserves_legacy_behavior(board):
    task = make_task(board)
    assert admission.claim_reason(board, task) is None
    assert admission.allows_unenrolled_finish(board, task)


def test_invalid_mode_and_worker_policy_change_are_refused(board, monkeypatch):
    with pytest.raises(ValueError):
        admission.set_mode(board, "disabled-by-proposal")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "worker")
    with pytest.raises(PermissionError):
        admission.set_mode(board, "observe")


def test_native_claim_does_not_open_a_run_without_contract(board):
    admission.set_mode(board, "enforce")
    task = make_task(board)
    assert kb.claim_task(board, task) is None
    assert board.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task,)).fetchone()[0] == 0
    assert kb.get_task(board, task).status == "ready"


def test_native_review_claim_cannot_bypass_admission(board):
    admission.set_mode(board, "enforce")
    task = make_task(board)
    board.execute("UPDATE tasks SET status='review' WHERE id=?", (task,))
    assert kb.claim_review_task(board, task) is None
    assert board.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task,)).fetchone()[0] == 0


def test_missing_contract_cannot_certify_completion(board):
    from hermes_cli.kanban_development_contract import ensure_checks, verified
    admission.set_mode(board, "enforce")
    task = make_task(board)
    assert not ensure_checks(board, task, None)
    assert not verified(board, task, None)


def test_grandfathered_claim_still_works(board):
    task = make_task(board)
    admission.set_mode(board, "enforce")
    assert kb.claim_task(board, task) is not None


def test_public_completion_and_review_routes_deny_unenrolled_work(board):
    admission.set_mode(board, "enforce")
    completion = make_task(board)
    assert not kb.complete_task(board, completion, result="not admitted")
    assert kb.get_task(board, completion).status == "ready"

    review = make_task(board)
    assert not kb.request_review(board, review, summary="not admitted")
    assert kb.get_task(board, review).status == "ready"
    assert board.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (review,)).fetchone()[0] == 0


def test_observe_claim_can_finish_after_enforce_without_exempting_new_queue(board):
    admission.set_mode(board, "observe")
    claimed = make_task(board)
    queued = make_task(board)
    assert kb.claim_task(board, claimed) is not None
    run = current_run(board, claimed)
    admission.set_mode(board, "enforce")
    assert kb.complete_task(board, claimed, result="finished observed work", expected_run_id=run)
    assert kb.claim_task(board, queued) is None
    assert not kb.complete_task(board, queued, result="must be denied")
    assert board.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (queued,)).fetchone()[0] == 0


def test_enrolled_unit_completes_and_requests_review_under_enforce(board, tmp_path):
    task = enrolled(board, tmp_path)
    admission.set_mode(board, "enforce")
    assert kb.claim_task(board, task) is not None
    run = current_run(board, task)
    assert kb.complete_task(board, task, result="checked complete", expected_run_id=run)
    assert kb.get_task(board, task).status == "done"

    reviewable = enrolled(board, tmp_path, status="ready")
    assert kb.claim_task(board, reviewable) is not None
    review_run = current_run(board, reviewable)
    assert kb.request_review(board, reviewable, summary="checked handoff", reviewer="reviewer", expected_run_id=review_run)
    assert kb.get_task(board, reviewable).status == "review"


def test_enrolled_inflight_run_finishes_while_paused(board, tmp_path):
    task = enrolled(board, tmp_path)
    admission.set_mode(board, "enforce")
    assert kb.claim_task(board, task) is not None
    run = current_run(board, task)
    admission.set_mode(board, "paused")
    another = enrolled(board, tmp_path)
    assert kb.claim_task(board, another) is None
    assert kb.complete_task(board, task, result="paused policy preserves current work", expected_run_id=run)


def test_admission_activation_serializes_with_other_connection(board, tmp_path, monkeypatch):
    path = tmp_path / "board.sqlite"
    attempt = threading.Event()
    results = []

    def contender():
        conn = sqlite3.connect(path, isolation_level=None, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            attempt.set()
            task = make_task(conn)
            results.append((task, kb.claim_task(conn, task)))
        finally:
            conn.close()

    worker = threading.Thread(target=contender)
    original_install = admission.install_schema

    def install_then_race(conn):
        original_install(conn)
        if conn is board:
            worker.start()
            assert attempt.wait(2)

    monkeypatch.setattr(admission, "install_schema", install_then_race)
    admission.set_mode(board, "enforce")
    worker.join(5)
    assert not worker.is_alive()
    assert len(results) == 1 and results[0][1] is None
    created = results[0][0]
    assert board.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (created,)).fetchone()[0] == 0


@pytest.mark.parametrize('transition', [('paused',), ('paused', 'enforce')])
@pytest.mark.parametrize('review', [False, True])
def test_observed_inflight_survives_pause_transitions(board, transition, review):
    admission.set_mode(board, 'observe')
    task = make_task(board)
    queued = make_task(board)
    if review:
        assert kb.claim_task(board, task) is not None
        assert kb.request_review(board, task, summary='ready for review', reviewer='reviewer')
        assert kb.claim_review_task(board, task) is not None
    else:
        assert kb.claim_task(board, task) is not None
    run = current_run(board, task)
    for mode in transition:
        admission.set_mode(board, mode)
    assert kb.complete_task(board, task, result='existing work finished', expected_run_id=run)
    assert kb.claim_task(board, queued) is None
    assert not admission.allows_unenrolled_finish(board, queued)
