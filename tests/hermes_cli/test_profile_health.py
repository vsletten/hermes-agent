from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from hermes_cli.profile_health import (
    CapabilityRequirements,
    check_profile_health,
    invalidate_health_cache,
    normalize_worker_health_failure,
    read_health_cache,
    write_health_cache,
)


def _profile(root: Path, name: str, config: dict | str | None = None) -> Path:
    if name == "default":
        home = root
    else:
        home = root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "SOUL.md").write_text("profile soul\n", encoding="utf-8")
    if config is None:
        config = {"model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4"}}
    if isinstance(config, str):
        (home / "config.yaml").write_text(config, encoding="utf-8")
    else:
        (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return home


def _skill(profile_home: Path, name: str) -> None:
    d = profile_home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n", encoding="utf-8")


def test_missing_profile_is_unhealthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = check_profile_health("ghost")

    assert result.status == "unhealthy"
    assert result.fingerprint == "profile_missing"
    assert result.profile == "ghost"
    assert result.ok is False
    assert (tmp_path / "profiles" / "ghost").exists() is False


def test_config_load_failure_is_unhealthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _profile(tmp_path, "bad", config="model: [unterminated\n")

    result = check_profile_health("bad")

    assert result.status == "unhealthy"
    assert result.fingerprint == "profile_config_load_failed"
    assert "config" in result.message.lower()


def test_missing_provider_or_model_is_unhealthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _profile(tmp_path, "nomodel", config={"model": {"provider": "", "default": ""}})

    result = check_profile_health("nomodel")

    assert result.status == "unhealthy"
    assert result.fingerprint == "profile_model_missing"


def test_required_toolset_and_skill_missing_have_capability_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = _profile(tmp_path, "worker")
    _skill(home, "present-skill")
    req = CapabilityRequirements(
        required_toolsets=("terminal", "definitely_missing_toolset"),
        required_skills=("present-skill", "missing-skill"),
        strict=True,
    )

    result = check_profile_health("worker", requirements=req)

    assert result.status == "unhealthy"
    assert result.capability_key != CapabilityRequirements().capability_key()
    assert "toolset:definitely_missing_toolset" in result.missing_requirements
    assert "skill:missing-skill" in result.missing_requirements
    assert result.fingerprint == "profile_capability_missing"


def test_optional_missing_capability_is_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _profile(tmp_path, "worker")
    req = CapabilityRequirements(required_skills=("missing-skill",), strict=False)

    result = check_profile_health("worker", requirements=req)

    assert result.status == "degraded"
    assert result.ok is True
    assert result.fingerprint == "profile_capability_missing"


def test_workspace_or_project_home_missing_is_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _profile(tmp_path, "worker")

    result = check_profile_health("worker", workspace_path=tmp_path / "missing-workspace")

    assert result.status == "unhealthy"
    assert result.fingerprint == "worker_workspace_access_denied"


def test_token_and_missing_key_errors_normalize() -> None:
    expired = normalize_worker_health_failure("HTTP 401 token_expired", "codexapp")
    missing = normalize_worker_health_failure("Missing OPENROUTER_API_KEY", "worker", provider="openrouter")

    assert expired[0] == "worker_health"
    assert expired[1] == "worker_auth_expired"
    assert missing[0] == "worker_health"
    assert missing[1] == "worker_model_auth_failed"


def test_health_cache_ttl_respected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _profile(tmp_path, "worker")
    old = check_profile_health("worker", use_cache=False, now=100.0)
    write_health_cache(old)

    cached = read_health_cache("worker", old.capability_key, ttl_seconds=60, now=130.0)
    expired = read_health_cache("worker", old.capability_key, ttl_seconds=60, now=200.0)

    assert cached is not None
    assert cached.checked_at == old.checked_at
    assert expired is None


def test_cache_invalidation_can_target_fingerprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _profile(tmp_path, "worker")
    healthy = check_profile_health("worker", use_cache=False, now=100.0)
    write_health_cache(healthy)

    invalidate_health_cache("worker", fingerprint="worker_auth_expired")
    cached = read_health_cache("worker", healthy.capability_key, ttl_seconds=600, now=110.0)

    assert cached is not None
    assert cached.status == "unhealthy"
    assert cached.fingerprint == "worker_auth_expired"


def test_capability_tuple_granularity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _profile(tmp_path, "worker")
    ok_req = CapabilityRequirements(required_toolsets=("terminal",))
    bad_req = CapabilityRequirements(required_toolsets=("definitely_missing_toolset",))

    ok = check_profile_health("worker", requirements=ok_req, use_cache=False, now=100.0)
    bad = check_profile_health("worker", requirements=bad_req, use_cache=False, now=101.0)

    assert ok.capability_key != bad.capability_key
    assert ok.status == "healthy"
    assert bad.status == "unhealthy"


def test_no_llm_smoke_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _profile(tmp_path, "worker")

    def explode(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("LLM smoke must not run by default")

    monkeypatch.setattr("hermes_cli.profile_health._run_llm_smoke", explode)
    result = check_profile_health("worker")

    assert result.status == "healthy"


def test_profile_health_cli_json(tmp_path: Path) -> None:
    _profile(tmp_path, "worker")
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "profile", "health", "worker", "--json"],
        cwd=Path(__file__).parents[2],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["profile"] == "worker"
    assert payload["status"] == "healthy"
