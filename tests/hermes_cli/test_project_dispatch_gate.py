from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.project import SUPPORTED_SCHEMA_VERSION
from hermes_cli.profile_health import CapabilityRequirements, ProfileHealthResult, HEALTHY, DEGRADED, UNHEALTHY


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    projects = tmp_path / "projects" / "active"
    projects.mkdir(parents=True)
    monkeypatch.setenv("HERMES_PROJECTS_HOME", str(projects))
    (home / "profiles" / "codex").mkdir(parents=True)
    (home / "profiles" / "codex" / "config.yaml").write_text(
        "model:\n  provider: openai\n  default: gpt-test\n", encoding="utf-8"
    )
    kb.init_db()
    return {"home": home, "projects": projects, "tmp": tmp_path}


def _spawn(task, workspace, board=None):
    return 4321


def _contract(tmp_path: Path, task_id: str, **overrides) -> dict:
    data = {
        "expected_outputs": [str(tmp_path / "status" / f"{task_id}.md")],
        "workspace_kind": "scratch",
        "completion_contract": {"status_report": str(tmp_path / "status" / f"{task_id}.md"), "tests": ["pytest"]},
    }
    data.update(overrides)
    return data


def _write_project(projects: Path, tmp_path: Path, task_id: str, **overrides) -> Path:
    slug = overrides.pop("slug", "proj")
    home = projects / slug
    home.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "slug": slug,
        "board_slug": "default",
        "root_task_id": task_id,
        "project_home": str(home),
        "lifecycle_state": overrides.pop("lifecycle_state", "READY"),
        "execution_policy": {"requires_task_contracts": True},
        "worker_policy": {"allowed_profiles": ["codex"], **overrides.pop("worker_policy", {})},
        "task_contracts": {task_id: overrides.pop("contract", _contract(tmp_path, task_id))},
        "failure_state": overrides.pop("failure_state", {}),
    }
    data.update(overrides)
    (home / "project.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return home


def _events(conn, task_id: str, kind: str):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"] or "{}") for row in rows]


def test_p003_01_non_project_task_dispatch_unchanged(env):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ordinary", assignee="codex")
        result = kb.dispatch_once(conn, spawn_fn=_spawn, dry_run=False)
        task = kb.get_task(conn, tid)
    assert result.spawned[0][0] == tid
    assert result.spawned[0][1] == "codex"
    assert result.spawned[0][2].endswith(f"/kanban/workspaces/{tid}")
    assert task.status == "running"


def test_p003_02_blocked_worker_project_state_denies_before_claim_and_emits_event(env):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="project", assignee="codex")
        _write_project(env["projects"], env["tmp"], tid, lifecycle_state="BLOCKED_WORKER")
        result = kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
        payloads = _events(conn, tid, "project_preflight_blocked")
    assert result.spawned == []
    assert tid in result.auto_blocked
    assert task.status == "blocked"
    assert task.current_run_id is None
    assert payloads[-1]["failure_class"] == "project_state_hold"
    assert payloads[-1]["failure_fingerprint"] == "project_state_blocked_worker"
    assert payloads[-1]["owner"] == "process"
    assert payloads[-1]["legal_next_action"]
    assert payloads[-1]["project_slug"] == "proj"
    assert payloads[-1]["project_home"]


def test_p003_03_missing_project_home_hint_denies_without_spawn(env):
    missing = env["projects"] / "missing"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="project", body=f"project_home: {missing}\n", assignee="codex"
        )
        result = kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
        payloads = _events(conn, tid, "project_preflight_blocked")
    assert result.spawned == []
    assert task.status == "blocked"
    assert task.current_run_id is None
    assert payloads[-1]["failure_fingerprint"] == "project_home_missing"


def test_p003_04_invalid_task_contract_denies_without_claim(env):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="project", assignee="codex")
        bad = _contract(env["tmp"], tid)
        bad.pop("expected_outputs")
        _write_project(env["projects"], env["tmp"], tid, contract=bad)
        result = kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
        payloads = _events(conn, tid, "project_preflight_blocked")
    assert result.spawned == []
    assert task.status == "blocked"
    assert task.current_run_id is None
    assert payloads[-1]["failure_class"] == "task_contract"
    assert payloads[-1]["failure_fingerprint"] == "task_contract_invalid"


def test_p003_05_unhealthy_worker_health_required_denies_by_policy(env, monkeypatch):
    def fake_health(*args, **kwargs):
        return ProfileHealthResult(profile="codex", status=UNHEALTHY, message="token expired", fingerprint="worker_auth_expired", failure_class="worker_health")

    monkeypatch.setattr("hermes_cli.profile_health.check_profile_health", fake_health)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="project", assignee="codex")
        _write_project(env["projects"], env["tmp"], tid, worker_policy={"dispatch_requires_worker_health": True})
        result = kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
        payloads = _events(conn, tid, "project_preflight_blocked")
    assert result.spawned == []
    assert task.status == "blocked"
    assert payloads[-1]["failure_class"] == "worker_health"
    assert payloads[-1]["failure_fingerprint"] == "worker_auth_expired"
    assert payloads[-1]["owner"] == "worker"


def test_p003_07_degraded_worker_allowed_by_policy_can_dispatch(env, monkeypatch):
    def fake_health(*args, **kwargs):
        return ProfileHealthResult(profile="codex", status=DEGRADED, message="optional missing", fingerprint="profile_capability_missing", failure_class="worker_health")

    monkeypatch.setattr("hermes_cli.profile_health.check_profile_health", fake_health)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="project", assignee="codex")
        _write_project(
            env["projects"], env["tmp"], tid,
            worker_policy={"dispatch_requires_worker_health": True, "allow_degraded_worker_health": True},
        )
        result = kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
    assert result.spawned and result.spawned[0][0] == tid
    assert task.status == "running"


@pytest.mark.parametrize("state", ["BROKEN_INVARIANT", "BLOCKED_PROCESS", "PAUSED", "RECOVERY_REQUIRED", "BLOCKED_HUMAN"])
def test_p003_08_hold_states_deny_dispatch(env, state):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title=f"project {state}", assignee="codex")
        _write_project(env["projects"], env["tmp"], tid, lifecycle_state=state)
        result = kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
    assert result.spawned == []
    assert task.status == "blocked"
    assert task.current_run_id is None


def test_p003_09_repair_task_exception_in_blocked_process_can_dispatch(env):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="repair", assignee="codex")
        _write_project(
            env["projects"], env["tmp"], tid, lifecycle_state="BLOCKED_PROCESS",
            contract=_contract(env["tmp"], tid, kind="repair"),
        )
        result = kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
    assert result.spawned and result.spawned[0][0] == tid
    assert task.status == "running"


def test_p003_11_review_column_uses_project_gate_before_claim(env):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="codex")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        _write_project(env["projects"], env["tmp"], tid, lifecycle_state="BLOCKED_WORKER")
        result = kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
        payloads = _events(conn, tid, "project_preflight_blocked")
    assert result.spawned == []
    assert task.status == "blocked"
    assert task.current_run_id is None
    assert payloads[-1]["failure_fingerprint"] == "project_state_blocked_worker"


def test_p003_14_stale_health_cache_recomputed_before_claim(env, monkeypatch):
    calls = {"n": 0}

    def fake_health(*args, **kwargs):
        calls["n"] += 1
        assert kwargs.get("now") is None or isinstance(kwargs.get("now"), float)
        return ProfileHealthResult(profile="codex", status=HEALTHY, message="fresh")

    monkeypatch.setattr("hermes_cli.profile_health.check_profile_health", fake_health)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="project", assignee="codex")
        _write_project(env["projects"], env["tmp"], tid, worker_policy={"dispatch_requires_worker_health": True})
        result = kb.dispatch_once(conn, spawn_fn=_spawn)
    assert calls["n"] == 1
    assert result.spawned and result.spawned[0][0] == tid
