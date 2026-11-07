"""Acceptance regressions for supervisor catalog boundaries."""
import hashlib
import json
import sys
from pathlib import Path

import pytest
from hermes_cli import kanban_development_catalog as c


@pytest.fixture
def fixture(tmp_path):
    worker = tmp_path / 'worker'
    worker.mkdir()
    trusted = tmp_path / 'trusted'
    trusted.mkdir()
    oracle = trusted / 'oracle.py'
    oracle.write_text('pass\n')
    entry = {'id': 'one', 'argv': [sys.executable, str(oracle), '{workspace}'],
             'timeout': 1, 'protected_artifacts': [{'path': str(oracle),
             'sha256': hashlib.sha256(oracle.read_bytes()).hexdigest()}],
             'outcomes': ['success', 'failure']}
    data = {'schema_version': 1, 'checks': [entry]}
    path = trusted / 'catalog.json'
    path.write_text(json.dumps(data))
    return worker, trusted, oracle, data, path


def test_catalog_cannot_be_worker_owned(fixture):
    worker, _, _, data, _ = fixture
    path = worker / 'catalog.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        c.load_catalog(path, worker)


def test_mixed_timeouts_are_rejected_not_increased(fixture):
    worker, _, _, data, path = fixture
    data['checks'].append(dict(data['checks'][0], id='two', timeout=900))
    path.write_text(json.dumps(data))
    loaded = c.load_catalog(path, worker)
    with pytest.raises(ValueError):
        c.compile_selection(loaded, worker, ['one', 'two'])


def test_valid_multiple_checks_preserve_outcomes(fixture):
    worker, _, _, data, path = fixture
    data['checks'].append(dict(data['checks'][0], id='two'))
    path.write_text(json.dumps(data))
    result = c.compile_selection(c.load_catalog(path, worker), worker, ['one', 'two'])
    assert result.contract.timeout == 1
    assert tuple(check.name for check in result.contract.checks) == ('one', 'two')
    assert result.outcomes == (('one', ('success', 'failure')), ('two', ('success', 'failure')))
    c.revalidate_protected_artifacts(result)


@pytest.mark.parametrize('change', ['duplicate', 'placeholder', 'executable'])
def test_invalid_catalog_boundaries(fixture, change):
    worker, _, _, data, path = fixture
    if change == 'duplicate':
        raw = json.dumps(data).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    else:
        data['checks'][0]['argv'][0 if change == 'executable' else 2] = (
            '/nonexistent/catalog-interpreter' if change == 'executable' else '--path={workspace}')
        raw = json.dumps(data)
    path.write_text(raw)
    with pytest.raises(ValueError):
        c.load_catalog(path, worker)


@pytest.mark.parametrize('target', ['catalog', 'artifact'])
def test_symlink_ancestor_rejected(fixture, tmp_path, target):
    worker, trusted, oracle, data, path = fixture
    alias = tmp_path / 'alias'
    alias.symlink_to(trusted, target_is_directory=True)
    if target == 'catalog':
        path = alias / path.name
    else:
        data['checks'][0]['protected_artifacts'][0]['path'] = str(alias / oracle.name)
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        c.load_catalog(path, worker)


def test_revalidation_rejects_replaced_parent(fixture):
    worker, trusted, _, _, path = fixture
    compiled = c.compile_selection(c.load_catalog(path, worker), worker, ['one'])
    moved = worker / 'moved'
    trusted.rename(moved)
    trusted.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError):
        c.revalidate_protected_artifacts(compiled)


def test_catalog_read_is_bounded(fixture, monkeypatch):
    worker, _, _, _, path = fixture
    monkeypatch.setattr(c, 'MAX_CATALOG_BYTES', 16)
    original = Path.open
    reads = []
    class Reader:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def read(self, size=-1):
            reads.append(size)
            assert 0 <= size <= 17, 'unbounded catalog read'
            return self.stream.read(size)
    def instrumented(self, *args, **kwargs):
        stream = original(self, *args, **kwargs)
        return Reader(stream) if self == path else stream
    monkeypatch.setattr(Path, 'open', instrumented)
    with pytest.raises(ValueError):
        c.load_catalog(path, worker)
    assert reads == [17]
