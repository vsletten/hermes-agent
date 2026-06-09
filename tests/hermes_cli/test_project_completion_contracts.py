from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.project import SUPPORTED_SCHEMA_VERSION, validate_completion_contract


@pytest.fixture
def project_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    projects = tmp_path / "projects" / "active"
    projects.mkdir(parents=True)
    monkeypatch.setenv("HERMES_PROJECTS_HOME", str(projects))
    kb.init_db()
    return {"tmp": tmp_path, "projects": projects}


def _project_json(project_home: Path, tmp_path: Path, task_id: str, *, artifact: Path | None = None, status: Path | None = None) -> dict:
    artifact = artifact or (tmp_path / "artifacts" / f"{task_id}.md")
    status = status or (tmp_path / "status" / f"{task_id}.md")
    return {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "slug": "proj",
        "board_slug": "default",
        "root_task_id": task_id,
        "project_home": str(project_home),
        "lifecycle_state": "READY",
        "execution_policy": {"requires_task_contracts": True, "strict_completion_contracts": True},
        "worker_policy": {"allowed_profiles": ["codex"]},
        "task_contracts": {
            task_id: {
                "expected_outputs": [str(artifact), str(status)],
                "workspace_kind": "scratch",
                "completion_contract": {
                    "artifacts": [str(artifact)],
                    "status_report": str(status),
                    "tests": ["pytest"],
                    "require_task_id_in_status": True,
                    "require_artifacts_listed_in_status": True,
                    "require_blockers_field": True,
                    "require_next_safe_action": True,
                },
            }
        },
        "failure_state": {},
    }


def _write_project(projects: Path, tmp_path: Path, task_id: str, **kwargs) -> dict:
    project_home = projects / "proj"
    project_home.mkdir(parents=True, exist_ok=True)
    data = _project_json(project_home, tmp_path, task_id, **kwargs)
    (project_home / "project.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def _write_valid_outputs(task_id: str, artifact: Path, status: Path) -> None:
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(f"# Artifact for {task_id}\n\nUseful content.\n", encoding="utf-8")
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(
        "\n".join(
            [
                f"Task id: {task_id}",
                "Status: PASS",
                f"Artifacts: {artifact}",
                "Blockers: none",
                "Next safe action: review local diff",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _events(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"] or "{}") for row in rows]


def test_validate_completion_contract_accepts_valid_artifact_and_status(project_env):
    task_id = "t_contract"
    artifact = project_env["tmp"] / "artifacts" / "report.md"
    status = project_env["tmp"] / "status" / "t_contract.md"
    data = _project_json(project_env["projects"] / "proj", project_env["tmp"], task_id, artifact=artifact, status=status)
    _write_valid_outputs(task_id, artifact, status)

    decision = validate_completion_contract(
        task_id,
        run_id=12,
        metadata={"artifacts": [str(artifact)]},
        project_json=data,
        board_snapshot={},
    )

    assert decision.allowed is True
    assert decision.missing == []


def test_validate_completion_contract_rejects_missing_artifact(project_env):
    task_id = "t_missing_artifact"
    artifact = project_env["tmp"] / "artifacts" / "missing.md"
    status = project_env["tmp"] / "status" / "t_missing_artifact.md"
    data = _project_json(project_env["projects"] / "proj", project_env["tmp"], task_id, artifact=artifact, status=status)
    status.parent.mkdir(parents=True)
    status.write_text(f"Task id: {task_id}\nArtifacts: {artifact}\nBlockers: none\nNext safe action: repair\n", encoding="utf-8")

    decision = validate_completion_contract(task_id, run_id=None, metadata={"artifacts": [str(artifact)]}, project_json=data, board_snapshot={})

    assert decision.allowed is False
    assert "missing artifact" in decision.reason
    assert str(artifact) in decision.missing


def test_project_completion_without_required_artifact_blocks_task_and_child(project_env):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="codex")
        child = kb.create_task(conn, title="child", assignee="codex", parents=[parent])
        _write_project(project_env["projects"], project_env["tmp"], parent)
        kb.claim_task(conn, parent)
        ok = kb.complete_task(conn, parent, summary="done", metadata={"artifacts": []})
        parent_row = kb.get_task(conn, parent)
        child_row = kb.get_task(conn, child)
        payloads = _events(conn, parent, "project_completion_rejected")

    assert ok is False
    assert parent_row.status == "blocked"
    assert child_row.status == "todo"
    assert payloads[-1]["failure_fingerprint"] == "completion_contract_invalid"
    assert "missing artifact" in payloads[-1]["reason"]


def test_project_completion_with_valid_contract_promotes_child(project_env):
    artifact = project_env["tmp"] / "artifacts" / "report.md"
    status = project_env["tmp"] / "status" / "parent.md"
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="codex")
        child = kb.create_task(conn, title="child", assignee="codex", parents=[parent])
        _write_project(project_env["projects"], project_env["tmp"], parent, artifact=artifact, status=status)
        _write_valid_outputs(parent, artifact, status)
        kb.claim_task(conn, parent)
        ok = kb.complete_task(conn, parent, summary="done", metadata={"artifacts": [str(artifact)]})
        parent_row = kb.get_task(conn, parent)
        child_row = kb.get_task(conn, child)

    assert ok is True
    assert parent_row.status == "done"
    assert child_row.status == "ready"


def test_non_project_completion_remains_permissive(project_env):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ordinary", assignee="codex")
        kb.claim_task(conn, tid)
        ok = kb.complete_task(conn, tid, summary="done", metadata={})
        row = kb.get_task(conn, tid)

    assert ok is True
    assert row.status == "done"
