from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.project import (
    SUPPORTED_SCHEMA_VERSION,
    ProjectState,
    compute_next_legal_transition,
    read_board_snapshot,
    verify_project,
)


@pytest.fixture
def isolated_kanban(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _contract(tmp_path: Path, task_id: str) -> dict:
    return {
        "expected_outputs": [str(tmp_path / "status" / f"{task_id}.md")],
        "workspace_kind": "worktree",
        "workspace_path": str(tmp_path / "worktree"),
        "branch_name": "project-autopilot-failsafe/test",
        "completion_contract": {
            "status_report": str(tmp_path / "status" / f"{task_id}.md"),
            "tests": ["python -m pytest tests/hermes_cli/test_project_verify.py -q -o addopts="],
        },
    }


def _write_project(project_home: Path, tmp_path: Path, root_task_id: str, *, lifecycle_state: str = "READY", contracts: dict | None = None) -> Path:
    project_home.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "slug": project_home.name,
        "board_slug": "default",
        "root_task_id": root_task_id,
        "project_home": str(project_home),
        "lifecycle_state": lifecycle_state,
        "execution_policy": {"requires_task_contracts": True},
        "worker_policy": {"allowed_profiles": ["codex"]},
        "task_contracts": contracts if contracts is not None else {root_task_id: _contract(tmp_path, root_task_id)},
        "failure_state": {},
    }
    (project_home / "project.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return project_home


def _create_task(conn, *, title: str, status: str = "ready", assignee: str = "codex", parents=()) -> str:
    task_id = kb.create_task(conn, title=title, assignee=assignee, parents=parents, initial_status="running")
    conn.execute("UPDATE tasks SET status = ?, current_run_id = CASE WHEN ? = 'running' THEN 99 ELSE NULL END WHERE id = ?", (status, status, task_id))
    conn.commit()
    return task_id


def test_verify_healthy_strict_project_includes_truth_fields(tmp_path, isolated_kanban):
    with kb.connect() as conn:
        root = _create_task(conn, title="root", status="done")
    home = _write_project(tmp_path / "proj", tmp_path, root)

    result = verify_project(home, now=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc))

    assert result.ok is True
    assert result.lifecycle_state is ProjectState.DONE
    assert result.truth_source and result.truth_source.endswith("kanban.db")
    assert result.truth_read_at == "2026-01-02T03:04:00Z"
    assert result.board_snapshot["counts"]["total"] == 1
    assert result.project_graph_snapshot["counts"]["done"] == 1
    assert result.project_home_invariant == "OK"
    assert isinstance(compute_next_legal_transition(result), str)


def test_board_read_failure_returns_unknown_stopped(tmp_path, isolated_kanban, monkeypatch):
    with kb.connect() as conn:
        root = _create_task(conn, title="root", status="ready")
    home = _write_project(tmp_path / "proj", tmp_path, root)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "missing" / "kanban.db"))

    result = verify_project(home)

    assert result.ok is False
    assert result.lifecycle_state is ProjectState.UNKNOWN
    assert result.autopilot_confidence == "stopped"
    assert result.failure_state is not None
    assert "cannot read board truth" in result.failure_state.message
    assert result.next_legal_action == "repair board DB access, then rerun `hermes project verify --json`"


def test_stale_status_false_confidence_reads_full_board_counts(tmp_path, isolated_kanban):
    with kb.connect() as conn:
        root = _create_task(conn, title="root", status="done")
        outside_ready = _create_task(conn, title="outside ready", status="ready")
        outside_todo = _create_task(conn, title="outside todo", status="todo")
    home = _write_project(tmp_path / "proj", tmp_path, root)
    (home / "STATUS.md").write_text("Child task summary: 0 done, 0 active, 0 ready, 0 todo, 0 blocked.\n", encoding="utf-8")

    result = verify_project(home)

    assert result.board_snapshot["counts"]["ready"] >= 1
    assert result.board_snapshot["counts"]["todo"] >= 1
    assert outside_ready and outside_todo
    assert result.board_snapshot["counts"] != {"total": 0, "done": 0, "active": 0, "ready": 0, "todo": 0, "blocked": 0, "other": 0}


def test_root_done_with_active_child_is_not_done(tmp_path, isolated_kanban):
    with kb.connect() as conn:
        root = _create_task(conn, title="root", status="done")
        child = _create_task(conn, title="child", status="running", parents=[root])
    contracts = {root: _contract(tmp_path, root), child: _contract(tmp_path, child)}
    home = _write_project(tmp_path / "proj", tmp_path, root, contracts=contracts)

    result = verify_project(home)

    assert result.lifecycle_state is ProjectState.EXECUTING
    assert result.active_task is not None
    assert result.active_task["id"] == child
    assert result.project_graph_snapshot["counts"]["active"] == 1


def test_project_home_invariant_broken_on_path_mismatch(tmp_path, isolated_kanban):
    with kb.connect() as conn:
        root = _create_task(conn, title="root", status="ready")
    home = _write_project(tmp_path / "proj", tmp_path, root)
    data = json.loads((home / "project.json").read_text(encoding="utf-8"))
    data["project_home"] = str(tmp_path / "elsewhere")
    (home / "project.json").write_text(json.dumps(data), encoding="utf-8")

    result = verify_project(home)

    assert result.ok is False
    assert result.project_home_invariant == "BROKEN"
    assert any("project_home" in error for error in result.errors)


def test_active_task_details_include_identity_assignee_and_run(tmp_path, isolated_kanban):
    with kb.connect() as conn:
        root = _create_task(conn, title="root active", status="running", assignee="codex")
    home = _write_project(tmp_path / "proj", tmp_path, root)

    result = verify_project(home)

    assert result.active_task == {
        "id": root,
        "title": "root active",
        "status": "running",
        "assignee": "codex",
        "run_id": 99,
    }


def test_read_board_snapshot_uses_full_board_not_project_graph(tmp_path, isolated_kanban):
    with kb.connect() as conn:
        _create_task(conn, title="a", status="ready")
        _create_task(conn, title="b", status="blocked")

    snapshot = read_board_snapshot("default")

    assert snapshot["counts"]["ready"] == 1
    assert snapshot["counts"]["blocked"] == 1
    assert "event_watermark" in snapshot
