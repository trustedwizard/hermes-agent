import sqlite3
import subprocess
import sys
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_development_contract import Check, Contract, attempts_remaining, enroll, run_checks, verified, install_schema

@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'home'))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    repo = tmp_path/'repo'
    repo.mkdir()
    def git(*args):
        subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True)
    git('init')
    (repo/'value.py').write_text('VALUE = 1\n')
    git('add','value.py')
    git('-c','user.name=Fixture','-c','user.email=fixture@localhost','commit','-m','fixture')
    path = tmp_path/'board.sqlite'
    kb.init_db(path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    task = kb.create_task(conn,title='contract task',assignee='fixture',workspace_kind='dir',workspace_path=str(repo))
    yield conn, task, path, repo
    conn.close()


def contract(repo, code='print(1)', timeout=60):
    return Contract(str(repo), (Check('unit',(sys.executable,'-B','-c',code)),), timeout)


def claim(conn, task):
    assert kb.claim_task(conn,task) is not None
    return conn.execute('SELECT current_run_id FROM tasks WHERE id=?',(task,)).fetchone()[0]


def reopen(conn, task):
    # Simulate recovered failed implementation without deleting native run history.
    conn.execute("UPDATE task_runs SET ended_at=unixepoch(),outcome='crashed',status='crashed' WHERE task_id=? AND ended_at IS NULL",(task,))
    conn.execute("UPDATE tasks SET status='ready',current_run_id=NULL,claim_lock=NULL WHERE id=?",(task,))


def test_reopen_and_verified_completion(board):
    conn, task, path, repo = board
    enroll(conn,task,contract(repo))
    conn.close()
    conn=sqlite3.connect(path, isolation_level=None)
    conn.row_factory=sqlite3.Row
    run=claim(conn,task)
    assert not verified(conn,task,run)
    assert run_checks(conn,task,run)[0]['exit_code']==0
    assert verified(conn,task,run)
    assert kb.complete_task(conn,task,result='verified',expected_run_id=run)
    assert kb.get_task(conn,task).status=='done'
    conn.close()


def test_failed_evidence_and_worker_claim_not_trusted(board):
    conn,task,_,repo=board
    enroll(conn,task,contract(repo,'raise SystemExit(2)'))
    run=claim(conn,task)
    assert not kb.complete_task(conn,task,result='passed',metadata={'checks':'passed'},expected_run_id=run)
    assert run_checks(conn,task,run)[0]['exit_code']==2
    assert not verified(conn,task,run)
    assert not kb.complete_task(conn,task,result='passed',expected_run_id=run)


def test_same_branch_changed_bytes_invalidate_and_new_attempt_can_pass(board):
    conn,task,_,repo=board
    enroll(conn,task,contract(repo))
    run=claim(conn,task)
    run_checks(conn,task,run)
    assert verified(conn,task,run)
    (repo/'value.py').write_text('VALUE = 2\n')
    assert not verified(conn,task,run)
    run_checks(conn,task,run)
    assert not verified(conn,task,run)
    reopen(conn,task)
    second=claim(conn,task)
    run_checks(conn,task,second)
    assert verified(conn,task,second)
    assert attempts_remaining(conn,task)==1


def test_frozen_contract_and_three_attempts(board):
    conn,task,_,repo=board
    enroll(conn,task,contract(repo))
    with pytest.raises(sqlite3.IntegrityError):
        enroll(conn,task,contract(repo,'print(2)'))
    for _ in range(3):
        claim(conn,task)
        reopen(conn,task)
    assert attempts_remaining(conn,task)==0
    assert kb.claim_task(conn,task) is None
    assert kb.get_task(conn,task).status=='blocked'


def test_duplicate_verification_is_not_reexecuted(board):
    conn,task,path,repo=board
    counter=path.parent/'counter'
    code=f"from pathlib import Path; p=Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
    enroll(conn,task,contract(repo,code))
    run=claim(conn,task)
    run_checks(conn,task,run)
    run_checks(conn,task,run)
    assert counter.read_text()=='x'
    assert verified(conn,task,run)


def test_wrong_owner_and_incomplete_reservation_fail_closed(board):
    conn,task,_,repo=board
    enroll(conn,task,contract(repo))
    run=claim(conn,task)
    with pytest.raises(ValueError,match='owner'):
        run_checks(conn,task,run+1)
    conn.execute("INSERT INTO kanban_dev_checks VALUES(?,?,?,'x','x','running',NULL,NULL)",(task,run,'unit'))
    run_checks(conn,task,run)
    assert not verified(conn,task,run)


def test_timeout_is_persisted(board):
    conn,task,_,repo=board
    enroll(conn,task,contract(repo,'import time; time.sleep(5)',1))
    run=claim(conn,task)
    assert run_checks(conn,task,run)[0]['exit_code']==124
    assert not verified(conn,task,run)


def test_unenrolled_completion_unchanged(board):
    conn,task,_,_=board
    run=claim(conn,task)
    assert verified(conn,task,run)
    assert kb.complete_task(conn,task,result='legacy complete',expected_run_id=run)


def test_schema_does_not_commit_callers_transaction(board):
    conn,task,_,_=board
    conn.execute('BEGIN')
    conn.execute("UPDATE tasks SET title='uncommitted' WHERE id=?",(task,))
    install_schema(conn)
    conn.rollback()
    assert kb.get_task(conn,task).title=='contract task'


def test_completion_executes_frozen_checks_without_worker_receipts(board):
    conn,task,_,repo=board
    enroll(conn,task,contract(repo))
    run=claim(conn,task)
    assert kb.complete_task(conn,task,result='request verification',expected_run_id=run)
    assert conn.execute('SELECT exit_code FROM kanban_dev_checks WHERE task_id=?',(task,)).fetchone()[0]==0


def test_check_mutation_is_recorded_as_failed(board):
    conn,task,_,repo=board
    enroll(conn,task,contract(repo,"from pathlib import Path; Path('value.py').write_text('VALUE=9')"))
    run=claim(conn,task)
    assert run_checks(conn,task,run)[0]['exit_code']==127
    (repo/'value.py').write_text('VALUE = 1\n')
    assert not verified(conn,task,run)


def test_ownership_loss_cannot_publish_finished_evidence(board,monkeypatch):
    from hermes_cli import kanban_development_contract as dev
    conn,task,_,repo=board
    enroll(conn,task,contract(repo))
    run=claim(conn,task)
    def lose_owner(check,spec):
        reopen(conn,task)
        return 0,'finished too late'
    monkeypatch.setattr(dev,'execute',lose_owner)
    with pytest.raises(ValueError,match='owner changed'):
        run_checks(conn,task,run)
    assert conn.execute('SELECT status FROM kanban_dev_checks WHERE task_id=?',(task,)).fetchone()[0]=='running'
    new=claim(conn,task)
    monkeypatch.setattr(dev,'execute',lambda c,s:(0,'fresh run'))
    run_checks(conn,task,new)
    assert verified(conn,task,new)


def test_inherited_pipe_cannot_escape_deadline(board):
    conn,task,_,repo=board
    code="import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])"
    enroll(conn,task,contract(repo,code,1))
    run=claim(conn,task)
    import time
    start=time.monotonic()
    assert run_checks(conn,task,run)[0]['exit_code']==124
    assert time.monotonic()-start < 5


def test_two_identical_failed_attempts_stop_without_third_spawn(board):
    conn,task,_,repo=board
    enroll(conn,task,contract(repo,'raise SystemExit(2)'))
    for _ in range(2):
        run=claim(conn,task)
        run_checks(conn,task,run)
        reopen(conn,task)
    assert attempts_remaining(conn,task)==1
    assert kb.claim_task(conn,task) is None
    assert kb.get_task(conn,task).status=='blocked'


def test_review_does_not_spend_repair_budget_and_cannot_change_source(board):
    conn,task,_,repo=board
    enroll(conn,task,contract(repo))
    run=claim(conn,task)
    assert kb.request_review(conn,task,summary='check me',reviewer='reviewer',expected_run_id=run)
    assert kb.claim_review_task(conn,task) is not None
    review=kb.get_task(conn,task).current_run_id
    assert attempts_remaining(conn,task)==2
    (repo/'value.py').write_text('VALUE=3')
    assert not kb.complete_task(conn,task,result='review changed implementation',expected_run_id=review)


def test_review_can_complete_unchanged_verified_source(board):
    conn,task,_,repo=board
    enroll(conn,task,contract(repo))
    run=claim(conn,task)
    assert kb.request_review(conn,task,summary='check me',reviewer='reviewer',expected_run_id=run)
    assert kb.claim_review_task(conn,task) is not None
    review=kb.get_task(conn,task).current_run_id
    assert kb.complete_task(conn,task,result='review accepted',expected_run_id=review)
    assert attempts_remaining(conn,task)==2
