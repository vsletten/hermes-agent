from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.project import SUPPORTED_SCHEMA_VERSION, render_project_status, verify_project


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
        "branch_name": "project-autopilot-failsafe/status-test",
        "completion_contract": {
            "status_report": str(tmp_path / "status" / f"{task_id}.md"),
            "tests": ["python -m pytest tests/hermes_cli/test_project_status_renderer.py -q -o addopts="],
        },
    }


def _create_project(tmp_path: Path, project_home: Path, root: str, contracts: dict | None = None):
    project_home.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "slug": project_home.name,
        "board_slug": "default",
        "root_task_id": root,
        "project_home": str(project_home),
        "lifecycle_state": "READY",
        "execution_policy": {"requires_task_contracts": True},
        "worker_policy": {"allowed_profiles": ["codex"]},
        "task_contracts": contracts if contracts is not None else {root: _contract(tmp_path, root)},
        "failure_state": {},
    }
    (project_home / "project.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def _task(conn, title: str, status: str, *, assignee: str = "codex", parents=()) -> str:
    task_id = kb.create_task(conn, title=title, assignee=assignee, parents=parents, initial_status="running")
    conn.execute(
        "UPDATE tasks SET status = ?, current_run_id = CASE WHEN ? = 'running' THEN 123 ELSE NULL END WHERE id = ?",
        (status, status, task_id),
    )
    conn.commit()
    return task_id


def test_status_renderer_includes_required_truth_fields(tmp_path, isolated_kanban):
    with kb.connect() as conn:
        root = _task(conn, "root", "ready")
    home = tmp_path / "proj"
    _create_project(tmp_path, home, root)

    result = verify_project(home, now=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc))
    text = render_project_status(result)

    for label in [
        "State:",
        "Truth source:",
        "Truth read at:",
        "Board snapshot:",
        "Project graph snapshot:",
        "Project-home invariant:",
        "Active task:",
        "Next legal transition:",
        "Blocker:",
        "Autopilot confidence:",
    ]:
        assert label in text
    assert "Truth read at: 2026-01-02T03:04:00Z" in text
    assert "Project-home invariant: OK" in text


def test_status_renderer_does_not_replay_stale_zero_counts(tmp_path, isolated_kanban):
    with kb.connect() as conn:
        root = _task(conn, "root", "done")
        _task(conn, "outside", "ready")
    home = tmp_path / "proj"
    _create_project(tmp_path, home, root)
    (home / "STATUS.md").write_text("Board snapshot: total=0, done=0, active=0, ready=0, todo=0, blocked=0, other=0\n", encoding="utf-8")

    text = render_project_status(verify_project(home))

    assert "Board snapshot: total=0, done=0, active=0, ready=0, todo=0, blocked=0, other=0" not in text
    assert "ready=1" in text


def test_status_renderer_active_task_details(tmp_path, isolated_kanban):
    with kb.connect() as conn:
        root = _task(conn, "root", "done")
        child = _task(conn, "active child", "running", assignee="codex", parents=[root])
    contracts = {root: _contract(tmp_path, root), child: _contract(tmp_path, child)}
    home = tmp_path / "proj"
    _create_project(tmp_path, home, root, contracts=contracts)

    text = render_project_status(verify_project(home))

    assert f"Active task: {child} / active child / running / codex / run_id=123" in text
    assert "State: EXECUTING" in text


def test_status_renderer_has_exactly_one_next_legal_transition_line(tmp_path, isolated_kanban):
    with kb.connect() as conn:
        root = _task(conn, "root", "done")
        child = _task(conn, "blocked child", "blocked", parents=[root])
        _task(conn, "ready unrelated", "ready")
    contracts = {root: _contract(tmp_path, root), child: _contract(tmp_path, child)}
    home = tmp_path / "proj"
    _create_project(tmp_path, home, root, contracts=contracts)

    text = render_project_status(verify_project(home))
    transition_lines = [line for line in text.splitlines() if line.startswith("Next legal transition:")]

    assert len(transition_lines) == 1
    assert transition_lines[0].strip() != "Next legal transition:"


def test_status_renderer_board_read_failure_is_stopped_unknown(tmp_path, isolated_kanban, monkeypatch):
    with kb.connect() as conn:
        root = _task(conn, "root", "ready")
    home = tmp_path / "proj"
    _create_project(tmp_path, home, root)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "nope.db"))

    text = render_project_status(verify_project(home))

    assert "State: UNKNOWN" in text
    assert "Autopilot confidence: stopped" in text
    assert "cannot read board truth" in text
