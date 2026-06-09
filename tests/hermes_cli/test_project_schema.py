from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.project import (
    SUPPORTED_SCHEMA_VERSION,
    ProjectState,
    discover_project_for_task,
    load_project,
    load_project_json,
    parse_task_contract,
    validate_project_schema,
    validate_task_contract,
)


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects" / "active"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_PROJECTS_HOME", str(root))
    return root


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _contract(tmp_path: Path, task_id: str = "t_root") -> dict:
    return {
        "expected_outputs": [str(tmp_path / "status" / f"{task_id}.md")],
        "workspace_kind": "worktree",
        "workspace_path": str(tmp_path / "worktree"),
        "branch_name": "feature/project-schema",
        "completion_contract": {
            "status_report": str(tmp_path / "status" / f"{task_id}.md"),
            "tests": ["python -m pytest tests/hermes_cli/test_project_schema.py -q -o addopts="],
        },
    }


def _project_json(tmp_path: Path, **overrides) -> dict:
    task_id = overrides.pop("root_task_id", "t_root")
    data = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "slug": "proj",
        "board_slug": "default",
        "root_task_id": task_id,
        "project_home": str(tmp_path / "projects" / "active" / "proj"),
        "lifecycle_state": "READY",
        "execution_policy": {"requires_task_contracts": True},
        "worker_policy": {"allowed_profiles": ["codex"]},
        "task_contracts": {task_id: _contract(tmp_path, task_id)},
        "failure_state": {},
    }
    data.update(overrides)
    return data


def _write_project(home: Path, data: dict) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data.setdefault("project_home", str(home))
    (home / "project.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return home


def test_p000_01_valid_minimal_strict_project_schema_passes(tmp_path):
    result = validate_project_schema(_project_json(tmp_path), strict=True)
    assert result.ok is True
    assert result.lifecycle_state is ProjectState.READY
    assert result.autopilot_confidence == "operational"


def test_p000_02_missing_project_home_returns_recovery_required(tmp_path):
    result = load_project(tmp_path / "missing-project")
    assert result.ok is False
    assert result.lifecycle_state is ProjectState.RECOVERY_REQUIRED
    assert "project home does not exist" in result.errors[0]
    assert result.autopilot_confidence != "operational"


def test_p000_03_missing_project_json_returns_broken_invariant(tmp_path):
    home = tmp_path / "proj"
    home.mkdir()
    result = load_project(home)
    assert result.ok is False
    assert result.lifecycle_state is ProjectState.BROKEN_INVARIANT
    assert "missing project.json" in result.errors[0]


def test_p000_04_invalid_json_returns_broken_invariant(tmp_path):
    home = tmp_path / "proj"
    home.mkdir()
    (home / "project.json").write_text("{ nope", encoding="utf-8")
    result = load_project(home)
    assert result.ok is False
    assert result.lifecycle_state is ProjectState.BROKEN_INVARIANT
    assert "invalid JSON" in result.errors[0]
    with pytest.raises(ValueError, match="invalid JSON"):
        load_project_json(home)


def test_p000_05_unsupported_schema_version_fails_closed(tmp_path):
    data = _project_json(tmp_path, schema_version="project-autopilot/v999")
    result = validate_project_schema(data, strict=True)
    assert result.ok is False
    assert result.lifecycle_state is ProjectState.BROKEN_INVARIANT
    assert any("unsupported schema_version" in e for e in result.errors)


def test_p000_06_legacy_loose_schema_is_degraded_not_operational(tmp_path):
    data = {"state": "active", "task_graph": {"root": "t_root"}, "slug": "legacy"}
    result = validate_project_schema(data, strict=False)
    assert result.ok is False
    assert result.autopilot_confidence == "degraded"
    assert result.lifecycle_state is ProjectState.UNKNOWN
    assert result.warnings


def test_p000_07_missing_required_strict_fields_are_named(tmp_path):
    data = _project_json(tmp_path)
    del data["root_task_id"]
    del data["execution_policy"]
    del data["task_contracts"]
    result = validate_project_schema(data, strict=True)
    assert result.ok is False
    assert "missing required field: root_task_id" in result.errors
    assert "missing required field: execution_policy" in result.errors
    assert "missing required field: task_contracts" in result.errors


def test_p000_08_task_contract_missing_expected_outputs_is_invalid(tmp_path):
    data = _project_json(tmp_path)
    data["task_contracts"]["t_root"].pop("expected_outputs")
    contract = parse_task_contract("t_root", data)
    result = validate_task_contract(contract)
    assert result.ok is False
    assert any("expected_outputs" in e for e in result.errors)


def test_p000_09_ambiguous_output_contract_is_invalid(tmp_path):
    data = _project_json(tmp_path)
    data["task_contracts"]["t_root"]["expected_outputs"] = [
        "/tmp/status.md or /tmp/fallback.md"
    ]
    result = validate_task_contract(parse_task_contract("t_root", data))
    assert result.ok is False
    assert any("ambiguous expected_outputs" in e for e in result.errors)


def test_p000_10_invalid_workspace_kind_is_invalid(tmp_path):
    data = _project_json(tmp_path)
    data["task_contracts"]["t_root"]["workspace_kind"] = "banana"
    result = validate_task_contract(parse_task_contract("t_root", data))
    assert result.ok is False
    assert any("unknown workspace_kind" in e for e in result.errors)


def test_p000_11_worktree_path_must_be_absolute(tmp_path):
    data = _project_json(tmp_path)
    data["task_contracts"]["t_root"]["workspace_path"] = "relative/worktree"
    result = validate_task_contract(parse_task_contract("t_root", data))
    assert result.ok is False
    assert any("workspace_path must be absolute" in e for e in result.errors)


def test_p000_12_worktree_branch_is_required(tmp_path):
    data = _project_json(tmp_path)
    data["task_contracts"]["t_root"].pop("branch_name")
    result = validate_task_contract(parse_task_contract("t_root", data))
    assert result.ok is False
    assert any("branch_name is required" in e for e in result.errors)


def test_p000_13_project_contract_references_absent_board_task(tmp_path, kanban_home):
    with kb.connect() as conn:
        result = validate_project_schema(
            _project_json(tmp_path), strict=True, board_conn=conn
        )
    assert result.ok is False
    assert any("absent board task: t_root" in e for e in result.errors)


def test_p000_14_board_task_not_in_required_contract_is_invalid(tmp_path, kanban_home):
    with kb.connect() as conn:
        kb.create_task(conn, title="root", idempotency_key="root")
        row = conn.execute("SELECT id FROM tasks LIMIT 1").fetchone()
    data = _project_json(tmp_path, root_task_id=row["id"])
    data["task_contracts"] = {}
    result = validate_project_schema(data, strict=True)
    assert result.ok is False
    assert any("task_contracts must not be empty" in e for e in result.errors)


def test_p000_15_contradictory_project_membership_fails_closed(
    tmp_path, projects_root, kanban_home
):
    task_id = "t_shared"
    with kb.connect() as conn:
        actual_id = kb.create_task(conn, title="shared", idempotency_key=task_id)

    # Use the real generated id so board existence is not the failure source.
    task_id = actual_id
    p1 = _project_json(tmp_path, root_task_id=task_id, slug="p1")
    p1["task_contracts"] = {task_id: _contract(tmp_path, task_id)}
    p2 = _project_json(tmp_path, root_task_id=task_id, slug="p2")
    p2["task_contracts"] = {task_id: _contract(tmp_path, task_id)}
    _write_project(projects_root / "p1", p1)
    _write_project(projects_root / "p2", p2)

    result = discover_project_for_task(task_id, "default")
    assert result.ok is False
    assert result.lifecycle_state is ProjectState.BROKEN_INVARIANT
    assert "multiple projects claim task" in result.errors[0]


def test_discover_project_for_task_returns_contract_for_unique_match(
    tmp_path, projects_root, kanban_home
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="schema", assignee="codex")
    data = _project_json(tmp_path, root_task_id=task_id)
    data["task_contracts"] = {task_id: _contract(tmp_path, task_id)}
    _write_project(projects_root / "proj", data)

    result = discover_project_for_task(task_id, "default")
    assert result.ok is True
    assert result.task_contract is not None
    assert result.task_contract.task_id == task_id
