import hashlib
import json

import pytest

from hermes_cli import kanban_development_catalog as catalog


def make_catalog(tmp_path, workspace, artifact):
    data = {
        "schema_version": 1,
        "checks": [{
            "id": "unit", "argv": ["/usr/bin/python3", "-m", "pytest", "{workspace}"],
            "timeout": 30,
            "protected_artifacts": [{"path": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}],
            "outcomes": ["success", "failure"],
        }],
    }
    path = tmp_path / "trusted.json"
    path.write_text(json.dumps(data))
    return path, data


def test_load_compile_and_revalidate(tmp_path):
    workspace = tmp_path / "worker"
    workspace.mkdir()
    artifact = tmp_path / "trusted-check.py"
    artifact.write_bytes(b"trusted bytes\n")
    path, _ = make_catalog(tmp_path, workspace, artifact)
    loaded = catalog.load_catalog(path, workspace)
    result = catalog.compile_selection(loaded, str(workspace), ["unit"])
    assert result.contract.checks[0].argv == ("/usr/bin/python3", "-m", "pytest", str(workspace))
    assert result.catalog_digest == hashlib.sha256(path.read_bytes()).hexdigest()
    catalog.revalidate_protected_artifacts(result)
    artifact.write_bytes(b"changed")
    with pytest.raises(ValueError):
        catalog.revalidate_protected_artifacts(result)


def test_rejects_invalid_catalog_and_model_argv(tmp_path):
    workspace = tmp_path / "worker"
    workspace.mkdir()
    artifact = tmp_path / "trusted.py"
    artifact.write_bytes(b"x")
    path, data = make_catalog(tmp_path, workspace, artifact)
    data["checks"] = []
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        catalog.load_catalog(path, workspace)
    path, data = make_catalog(tmp_path, workspace, artifact)
    loaded = catalog.load_catalog(path, workspace)
    with pytest.raises(ValueError):
        catalog.compile_selection(loaded, str(workspace), [{"id": "unit", "argv": ["evil"]}])
    with pytest.raises(ValueError):
        catalog.compile_selection(loaded, str(workspace), ["missing"])


def test_rejects_workspace_artifact_symlink_and_bad_hash(tmp_path):
    workspace = tmp_path / "worker"
    workspace.mkdir()
    inside = workspace / "source.py"
    inside.write_bytes(b"x")
    path, data = make_catalog(tmp_path, workspace, inside)
    with pytest.raises(ValueError):
        catalog.load_catalog(path, workspace)
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"x")
    link = tmp_path / "link.py"
    link.symlink_to(outside)
    path, data = make_catalog(tmp_path, workspace, outside)
    data["checks"][0]["protected_artifacts"][0] = {"path": str(link), "sha256": hashlib.sha256(b"x").hexdigest()}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        catalog.load_catalog(path, workspace)
    data["checks"][0]["protected_artifacts"][0] = {"path": str(outside), "sha256": "0" * 64}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        catalog.load_catalog(path, workspace)


def test_rejects_unknown_fields_and_nonexact_substitution(tmp_path):
    workspace = tmp_path / "worker"
    workspace.mkdir()
    artifact = tmp_path / "trusted.py"
    artifact.write_bytes(b"x")
    path, data = make_catalog(tmp_path, workspace, artifact)
    data["unexpected"] = True
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        catalog.load_catalog(path, workspace)
