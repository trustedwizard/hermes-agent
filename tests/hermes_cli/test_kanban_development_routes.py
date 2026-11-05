import json
import sqlite3

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_development_admission as admission
from hermes_cli import kanban_development_prepare as preparation
from hermes_cli import kanban_development_routes as routes


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    path = tmp_path / "board.db"
    kb.init_db(path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    admission.set_mode(conn, "enforce")
    repo = tmp_path / "repo"
    repo.mkdir()
    catalog = tmp_path / "catalog.json"
    # Catalog validation runs before any route write; this fixture exercises refusal safely.
    catalog.write_text(json.dumps({"schema_version": 1, "checks": []}))
    yield conn, repo, catalog
    conn.close()


def test_route_rejects_invalid_catalog_without_persisting(board):
    conn, repo, catalog = board
    spec = preparation.PreparationSpec(str(repo), str(catalog), ("missing",))
    with pytest.raises(ValueError):
        routes.configure_route(conn, spec)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_dev_routes'").fetchone() is None


def test_prepare_pending_is_inert_without_route(board):
    conn, _, _ = board
    assert routes.prepare_pending(conn, now=100) == []
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM kanban_dev_preparations").fetchone()[0] == 0


def test_observe_mode_has_no_effect(board):
    conn, _, _ = board
    admission.set_mode(conn, "observe")
    assert routes.prepare_pending(conn, now=100) == []
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
