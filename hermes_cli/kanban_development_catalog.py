"""Strict supervisor-owned catalog for bounded trusted checks."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import NoReturn
import re
from hermes_cli.kanban_development_contract import Check, Contract

MAX_CATALOG_BYTES = 1024 * 1024
MAX_ENTRIES = 32
MAX_ARTIFACTS = 32
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_STRING = 4096
_SUPPORTED_OUTCOMES = frozenset({"success", "failure", "needs_input", "capability", "transient", "review"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

@dataclass(frozen=True)
class ProtectedArtifact:
    path: str
    sha256: str

@dataclass(frozen=True)
class CatalogEntry:
    id: str
    argv: tuple[str, ...]
    timeout: int
    protected_artifacts: tuple[ProtectedArtifact, ...]
    outcomes: tuple[str, ...]

@dataclass(frozen=True)
class Catalog:
    digest: str
    entries: tuple[CatalogEntry, ...]
    workspace: str

@dataclass(frozen=True)
class CompiledSelection:
    contract: Contract
    catalog_digest: str
    protected_artifacts: tuple[ProtectedArtifact, ...]
    outcomes: tuple[tuple[str, tuple[str, ...]], ...]


def _fail() -> NoReturn:
    raise ValueError("invalid trusted check catalog") from None


def _root(workspace):
    try:
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir():
            _fail()
        return root
    except (OSError, ValueError, RuntimeError):
        _fail()


def _trusted_file(path, workspace):
    try:
        candidate = Path(path)
        if not candidate.is_absolute() or any(p.is_symlink() for p in (candidate, *candidate.parents)):
            _fail()
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file() or resolved.is_relative_to(Path(workspace)):
            _fail()
        return resolved
    except (OSError, ValueError, RuntimeError):
        _fail()


def _read_bounded(path, limit):
    try:
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            _fail()
        return data
    except OSError:
        _fail()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _artifacts(items, workspace):
    if not isinstance(items, list) or not items or len(items) > MAX_ARTIFACTS:
        _fail()
    result, total = [], 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            _fail()
        path, expected = item["path"], item["sha256"]
        if not isinstance(path, str) or len(path) > MAX_STRING or not Path(path).is_absolute() or not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            _fail()
        file_path = _trusted_file(path, workspace)
        data = _read_bounded(file_path, min(MAX_ARTIFACT_BYTES, MAX_TOTAL_ARTIFACT_BYTES - total))
        total += len(data)
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            _fail()
        result.append(ProtectedArtifact(path, expected))
    return tuple(result)

def load_catalog(path, workspace) -> Catalog:
    root = _root(workspace)
    catalog_path = _trusted_file(path, root)
    raw = _read_bounded(catalog_path, MAX_CATALOG_BYTES)
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail()
    if not isinstance(data, dict) or set(data) != {"schema_version", "checks"} or type(data["schema_version"]) is not int or data["schema_version"] != 1:
        _fail()
    checks = data["checks"]
    if not isinstance(checks, list) or not checks or len(checks) > MAX_ENTRIES:
        _fail()
    entries, seen = [], set()
    for item in checks:
        if not isinstance(item, dict) or set(item) != {"id", "argv", "timeout", "protected_artifacts", "outcomes"}:
            _fail()
        ident, argv, timeout, outcomes = item["id"], item["argv"], item["timeout"], item["outcomes"]
        if not isinstance(ident, str) or not ident or len(ident) > 128 or ident in seen or not isinstance(argv, list) or not argv or len(argv) > 64:
            _fail()
        seen.add(ident)
        if type(timeout) is not int or not 1 <= timeout <= 900:
            _fail()
        if not all(isinstance(arg, str) and arg and len(arg) <= MAX_STRING and "\0" not in arg for arg in argv):
            _fail()
        if not os.path.isabs(argv[0]) or not isinstance(outcomes, list) or not outcomes or len(outcomes) > len(_SUPPORTED_OUTCOMES) or not all(isinstance(o, str) and o in _SUPPORTED_OUTCOMES for o in outcomes) or len(set(outcomes)) != len(outcomes):
            _fail()
        if not Path(argv[0]).is_file() or not os.access(argv[0], os.X_OK):
            _fail()
        if any("{workspace}" in arg and arg != "{workspace}" for arg in argv):
            _fail()
        entries.append(CatalogEntry(ident, tuple(argv), timeout, _artifacts(item["protected_artifacts"], root), tuple(outcomes)))
    return Catalog(hashlib.sha256(raw).hexdigest(), tuple(entries), str(root))

def compile_selection(catalog: Catalog, workspace, selected_ids) -> CompiledSelection:
    root = str(_root(workspace))
    if root != catalog.workspace or not isinstance(selected_ids, (list, tuple)) or not selected_ids or len(selected_ids) > MAX_ENTRIES or not all(isinstance(i, str) for i in selected_ids) or len(set(selected_ids)) != len(selected_ids):
        _fail()
    by_id = {entry.id: entry for entry in catalog.entries}
    checks, artifacts = [], []
    for ident in selected_ids:
        entry = by_id.get(ident)
        if entry is None:
            _fail()
        argv = tuple(root if arg == "{workspace}" else arg for arg in entry.argv)
        checks.append(Check(ident, argv))
        artifacts.extend(entry.protected_artifacts)
    timeouts = {by_id[i].timeout for i in selected_ids}
    if len(timeouts) != 1:
        _fail()
    contract = Contract(root, tuple(checks), timeouts.pop())
    result = CompiledSelection(contract, catalog.digest, tuple(dict.fromkeys(artifacts)),
                               tuple((i, by_id[i].outcomes) for i in selected_ids))
    revalidate_protected_artifacts(result)
    return result

def revalidate_protected_artifacts(compiled: CompiledSelection) -> None:
    total = 0
    for artifact in compiled.protected_artifacts:
        path = _trusted_file(artifact.path, compiled.contract.workspace)
        if not _SHA256.fullmatch(artifact.sha256):
            _fail()
        data = _read_bounded(path, min(MAX_ARTIFACT_BYTES, MAX_TOTAL_ARTIFACT_BYTES - total))
        total += len(data)
        if hashlib.sha256(data).hexdigest() != artifact.sha256:
            _fail()
