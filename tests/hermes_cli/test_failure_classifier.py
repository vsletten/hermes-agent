from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.failure_classifier import FailureClassifierInput, classify_worker_or_task_failure
from hermes_cli.project import SUPPORTED_SCHEMA_VERSION


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


def _contract(tmp_path: Path, task_id: str) -> dict:
    status = tmp_path / "status" / f"{task_id}.md"
    return {
        "expected_outputs": [str(status)],
        "workspace_kind": "scratch",
        "completion_contract": {"status_report": str(status), "tests": ["pytest"]},
    }


def _write_project(projects: Path, tmp_path: Path, task_id: str, *, slug: str = "proj") -> Path:
    project_home = projects / slug
    project_home.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "slug": slug,
        "board_slug": "default",
        "root_task_id": task_id,
        "project_home": str(project_home),
        "lifecycle_state": "READY",
        "execution_policy": {"requires_task_contracts": True},
        "worker_policy": {"allowed_profiles": ["codex"]},
        "task_contracts": {task_id: _contract(tmp_path, task_id)},
        "failure_state": {},
    }
    (project_home / "project.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return project_home


def _event_payloads(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"] or "{}") for row in rows]


def test_http_401_token_expired_normalizes_to_worker_auth_expired():
    output = classify_worker_or_task_failure(
        FailureClassifierInput(
            task_id="t_auth",
            profile="codexapp",
            outcome="spawn_failed",
            error_text="OpenAI HTTP 401: token_expired while refreshing OAuth token",
        )
    )
    assert output.failure_class == "worker_health"
    assert output.normalized_fingerprint == "worker_auth_expired"
    assert output.scope == "profile"
    assert output.owner == "worker"
    assert output.retry_budget_key == "t_auth:codexapp:worker_health:worker_auth_expired"
    assert "auth" in output.legal_next_action.lower()


@pytest.mark.parametrize(
    "text, fingerprint",
    [
        ("Missing required environment variable OPENAI_API_KEY", "worker_auth_missing_api_key"),
        ("model provider rejected credentials: unauthorized", "worker_model_auth_failed"),
        ("workspace-preflight: declared worktree path does not exist", "workspace_preflight_failed"),
        ("project-completion-contract: missing artifact /tmp/report.md", "completion_artifact_missing"),
        ("worker exited cleanly (rc=0) without calling kanban_complete or kanban_block", "worker_protocol_violation_before_work"),
        ("task timed out after 3600s", "task_timeout"),
    ],
)
def test_known_failure_surfaces_get_stable_fingerprints(text, fingerprint):
    output = classify_worker_or_task_failure(
        FailureClassifierInput(task_id="t", profile="codex", outcome="crashed", error_text=text)
    )
    assert output.normalized_fingerprint == fingerprint
    assert output.confidence >= 0.7


def test_manual_operator_kill_does_not_poison_worker_auth():
    output = classify_worker_or_task_failure(
        FailureClassifierInput(
            task_id="t",
            profile="codex",
            outcome="blocked",
            error_text="manual operator kill: stopped worker process during deploy",
        )
    )
    assert output.normalized_fingerprint == "manual_operator_interrupt"
    assert output.failure_class == "operator_interrupt"
    assert output.scope == "task"
    assert output.owner == "operator"


def test_project_spawn_auth_failure_is_classified_and_blocks_same_profile(project_env, all_assignees_spawnable):
    def bad_spawn(task, workspace, board=None):
        raise RuntimeError("OpenAI HTTP 401 token_expired")

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="project auth", assignee="codex")
        _write_project(project_env["projects"], project_env["tmp"], tid)
        result = kb.dispatch_once(conn, spawn_fn=bad_spawn, failure_limit=5)
        task = kb.get_task(conn, tid)
        payloads = _event_payloads(conn, tid, "project_failure_classified")

    assert tid in result.auto_blocked
    assert task.status == "blocked"
    assert task.consecutive_failures == 1
    assert payloads[-1]["failure_class"] == "worker_health"
    assert payloads[-1]["normalized_fingerprint"] == "worker_auth_expired"
    assert payloads[-1]["retry_budget_key"].endswith(":worker_health:worker_auth_expired")


def test_non_project_spawn_failure_keeps_existing_retry_behavior(project_env, all_assignees_spawnable):
    def bad_spawn(task, workspace, board=None):
        raise RuntimeError("OpenAI HTTP 401 token_expired")

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ordinary", assignee="codex")
        result = kb.dispatch_once(conn, spawn_fn=bad_spawn, failure_limit=5)
        task = kb.get_task(conn, tid)
        payloads = _event_payloads(conn, tid, "project_failure_classified")

    assert tid not in result.auto_blocked
    assert task.status == "ready"
    assert task.consecutive_failures == 1
    assert payloads == []
