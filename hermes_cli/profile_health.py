"""Deterministic profile/worker health preflight helpers.

This module deliberately performs only cheap local checks.  It does not call an
LLM provider unless a future caller explicitly asks for a smoke test.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml


HEALTHY = "healthy"
DEGRADED = "degraded"
UNHEALTHY = "unhealthy"
DEFAULT_TTL_SECONDS = 300
CACHE_DIRNAME = ".cache"
CACHE_FILENAME = "profile_health.json"


@dataclass(frozen=True)
class CapabilityRequirements:
    """Tool/skill requirements for one dispatch capability tuple."""

    required_toolsets: tuple[str, ...] = ()
    required_skills: tuple[str, ...] = ()
    strict: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "required_toolsets",
            tuple(sorted(str(x) for x in self.required_toolsets if str(x).strip())),
        )
        object.__setattr__(
            self,
            "required_skills",
            tuple(sorted(str(x) for x in self.required_skills if str(x).strip())),
        )

    def capability_key(self) -> str:
        payload = {
            "required_toolsets": list(self.required_toolsets),
            "required_skills": list(self.required_skills),
            "strict": self.strict,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"cap:{digest}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_toolsets": list(self.required_toolsets),
            "required_skills": list(self.required_skills),
            "strict": self.strict,
            "capability_key": self.capability_key(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "CapabilityRequirements":
        if not data:
            return cls()
        return cls(
            required_toolsets=tuple(data.get("required_toolsets") or ()),
            required_skills=tuple(data.get("required_skills") or ()),
            strict=bool(data.get("strict", True)),
        )


@dataclass(frozen=True)
class ProfileHealthResult:
    profile: str
    status: str
    message: str
    fingerprint: str | None = None
    failure_class: str | None = None
    provider: str | None = None
    model: str | None = None
    profile_home: str | None = None
    capability_key: str = field(default_factory=lambda: CapabilityRequirements().capability_key())
    requirements: CapabilityRequirements = field(default_factory=CapabilityRequirements)
    missing_requirements: tuple[str, ...] = ()
    checked_at: float = field(default_factory=time.time)
    cache_hit: bool = False
    llm_smoke_ran: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {HEALTHY, DEGRADED}

    def with_cache_hit(self) -> "ProfileHealthResult":
        return ProfileHealthResult.from_dict({**self.to_dict(), "cache_hit": True})

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "status": self.status,
            "ok": self.ok,
            "message": self.message,
            "fingerprint": self.fingerprint,
            "failure_class": self.failure_class,
            "provider": self.provider,
            "model": self.model,
            "profile_home": self.profile_home,
            "capability_key": self.capability_key,
            "requirements": self.requirements.to_dict(),
            "missing_requirements": list(self.missing_requirements),
            "checked_at": self.checked_at,
            "cache_hit": self.cache_hit,
            "llm_smoke_ran": self.llm_smoke_ran,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProfileHealthResult":
        requirements = CapabilityRequirements.from_dict(data.get("requirements"))
        return cls(
            profile=str(data.get("profile") or ""),
            status=str(data.get("status") or UNHEALTHY),
            message=str(data.get("message") or ""),
            fingerprint=data.get("fingerprint"),
            failure_class=data.get("failure_class"),
            provider=data.get("provider"),
            model=data.get("model"),
            profile_home=data.get("profile_home"),
            capability_key=str(data.get("capability_key") or requirements.capability_key()),
            requirements=requirements,
            missing_requirements=tuple(data.get("missing_requirements") or ()),
            checked_at=float(data.get("checked_at") or 0.0),
            cache_hit=bool(data.get("cache_hit", False)),
            llm_smoke_ran=bool(data.get("llm_smoke_ran", False)),
            details=dict(data.get("details") or {}),
        )


def _hermes_root() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        home = Path(env).expanduser()
        # If called from inside a named profile, the profile root is the parent
        # of profiles/<name>.  This mirrors hermes_cli.profiles behavior.
        if home.parent.name == "profiles":
            return home.parent.parent
        return home
    return Path.home() / ".hermes"


def _profile_home(profile: str) -> Path:
    name = str(profile or "default")
    root = _hermes_root()
    if name == "default":
        return root
    return root / "profiles" / name


def _cache_path_for_profile(profile: str) -> Path:
    return _profile_home(profile) / CACHE_DIRNAME / CACHE_FILENAME


def _load_profile_config(profile_home: Path) -> dict[str, Any]:
    path = profile_home / "config.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml must contain a mapping")
    return data


def _model_provider(config: Mapping[str, Any]) -> tuple[str | None, str | None]:
    model_cfg = config.get("model")
    provider: str | None = None
    model: str | None = None
    if isinstance(model_cfg, Mapping):
        provider = str(model_cfg.get("provider") or "").strip() or None
        model = (
            str(
                model_cfg.get("default")
                or model_cfg.get("model")
                or model_cfg.get("name")
                or ""
            ).strip()
            or None
        )
    elif isinstance(model_cfg, str):
        model = model_cfg.strip() or None
    provider = provider or str(config.get("provider") or "").strip() or None
    model = model or str(config.get("default_model") or "").strip() or None
    return provider, model


def _available_toolsets() -> set[str]:
    try:
        from toolsets import get_all_toolsets

        return set(get_all_toolsets().keys())
    except Exception:
        return set()


def _skill_exists(profile_home: Path, skill: str) -> bool:
    skills_root = profile_home / "skills"
    candidates = [
        skills_root / skill / "SKILL.md",
        skills_root / f"{skill}.md",
        skills_root / skill / "skill.md",
    ]
    if any(path.exists() for path in candidates):
        return True
    try:
        for path in skills_root.rglob("SKILL.md"):
            if path.parent.name == skill or str(path.relative_to(skills_root).parent) == skill:
                return True
    except OSError:
        return False
    return False


def _path_access_failure(path: str | os.PathLike[str] | None) -> str | None:
    if path is None:
        return None
    p = Path(path).expanduser()
    try:
        if not p.exists():
            return f"path does not exist: {p}"
        if not os.access(p, os.R_OK | os.X_OK):
            return f"path is not accessible: {p}"
    except OSError as exc:
        return f"path access check failed for {p}: {exc}"
    return None


def normalize_worker_health_failure(
    error_text: str,
    profile: str,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str, str]:
    """Normalize deterministic worker/profile failures to failure class/fingerprint."""
    text = (error_text or "").lower()
    if "workspace-preflight" in text or "workspace" in text and "access" in text:
        return "worker_health", "worker_workspace_access_denied"
    if "token_expired" in text or "token expired" in text or "oauth" in text and "expired" in text:
        return "worker_health", "worker_auth_expired"
    if "401" in text or "unauthorized" in text or "forbidden" in text or "403" in text:
        return "worker_health", "worker_auth_expired"
    if "missing" in text and ("api_key" in text or "api key" in text or "_key" in text):
        return "worker_health", "worker_model_auth_failed"
    if "authentication" in text or "invalid api key" in text or "auth" in text and "failed" in text:
        return "worker_health", "worker_model_auth_failed"
    if "model" in text and ("not found" in text or "not configured" in text):
        return "worker_health", "profile_model_missing"
    return "worker_health", "worker_health_unknown"


def read_health_cache(
    profile: str,
    capability_key: str,
    *,
    ttl_seconds: int | None = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> ProfileHealthResult | None:
    path = _cache_path_for_profile(profile)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return None
    raw = entries.get(capability_key)
    if not isinstance(raw, dict):
        return None
    result = ProfileHealthResult.from_dict(raw)
    current = time.time() if now is None else now
    if ttl_seconds is not None and ttl_seconds >= 0:
        if current - result.checked_at > ttl_seconds:
            return None
    return result.with_cache_hit()


def write_health_cache(result: ProfileHealthResult) -> None:
    path = _cache_path_for_profile(result.profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"entries": {}}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict) and isinstance(existing.get("entries"), dict):
            payload = existing
    except (OSError, json.JSONDecodeError):
        pass
    payload.setdefault("entries", {})[result.capability_key] = result.to_dict()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def invalidate_health_cache(profile: str, fingerprint: str | None = None) -> None:
    path = _cache_path_for_profile(profile)
    if fingerprint is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return
    for key, raw in list(entries.items()):
        if not isinstance(raw, dict):
            continue
        result = ProfileHealthResult.from_dict(raw)
        entries[key] = ProfileHealthResult(
            profile=result.profile,
            status=UNHEALTHY,
            message=f"cached health invalidated by {fingerprint}",
            fingerprint=fingerprint,
            failure_class="worker_health",
            provider=result.provider,
            model=result.model,
            profile_home=result.profile_home,
            capability_key=result.capability_key,
            requirements=result.requirements,
            missing_requirements=result.missing_requirements,
            checked_at=time.time(),
            details={**result.details, "invalidated": True},
        ).to_dict()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _run_llm_smoke(*args: Any, **kwargs: Any) -> None:
    """Placeholder for an explicit future smoke test; not called by default."""
    return None


def check_profile_health(
    profile: str,
    requirements: CapabilityRequirements | None = None,
    workspace_path: str | os.PathLike[str] | None = None,
    project_home: str | os.PathLike[str] | None = None,
    ttl_seconds: int | None = DEFAULT_TTL_SECONDS,
    use_cache: bool = True,
    *,
    now: float | None = None,
    run_llm_smoke: bool = False,
) -> ProfileHealthResult:
    req = requirements or CapabilityRequirements()
    capability_key = req.capability_key()
    current = time.time() if now is None else now
    if use_cache:
        cached = read_health_cache(profile, capability_key, ttl_seconds=ttl_seconds, now=current)
        if cached is not None:
            return cached

    home = _profile_home(profile)
    if not home.exists() or not home.is_dir():
        result = ProfileHealthResult(
            profile=profile,
            status=UNHEALTHY,
            message=f"profile does not exist: {profile}",
            fingerprint="profile_missing",
            failure_class="worker_health",
            profile_home=str(home),
            capability_key=capability_key,
            requirements=req,
            checked_at=current,
        )
        return result

    try:
        config = _load_profile_config(home)
    except Exception as exc:
        result = ProfileHealthResult(
            profile=profile,
            status=UNHEALTHY,
            message=f"config load failed: {exc}",
            fingerprint="profile_config_load_failed",
            failure_class="worker_health",
            profile_home=str(home),
            capability_key=capability_key,
            requirements=req,
            checked_at=current,
        )
        if use_cache:
            write_health_cache(result)
        return result

    provider, model = _model_provider(config)
    if not provider or not model:
        result = ProfileHealthResult(
            profile=profile,
            status=UNHEALTHY,
            message="profile model/provider configuration is missing",
            fingerprint="profile_model_missing",
            failure_class="worker_health",
            provider=provider,
            model=model,
            profile_home=str(home),
            capability_key=capability_key,
            requirements=req,
            checked_at=current,
        )
        if use_cache:
            write_health_cache(result)
        return result

    for checked_path in (workspace_path, project_home):
        access_failure = _path_access_failure(checked_path)
        if access_failure:
            result = ProfileHealthResult(
                profile=profile,
                status=UNHEALTHY,
                message=access_failure,
                fingerprint="worker_workspace_access_denied",
                failure_class="worker_health",
                provider=provider,
                model=model,
                profile_home=str(home),
                capability_key=capability_key,
                requirements=req,
                checked_at=current,
            )
            if use_cache:
                write_health_cache(result)
            return result

    missing: list[str] = []
    available_toolsets = _available_toolsets()
    for toolset in req.required_toolsets:
        if toolset not in available_toolsets:
            missing.append(f"toolset:{toolset}")
    for skill in req.required_skills:
        if not _skill_exists(home, skill):
            missing.append(f"skill:{skill}")

    if missing:
        result = ProfileHealthResult(
            profile=profile,
            status=UNHEALTHY if req.strict else DEGRADED,
            message="missing required profile capabilities",
            fingerprint="profile_capability_missing",
            failure_class="worker_health",
            provider=provider,
            model=model,
            profile_home=str(home),
            capability_key=capability_key,
            requirements=req,
            missing_requirements=tuple(missing),
            checked_at=current,
        )
        if use_cache:
            write_health_cache(result)
        return result

    llm_smoke_ran = False
    if run_llm_smoke:
        _run_llm_smoke(profile=profile, provider=provider, model=model)
        llm_smoke_ran = True

    result = ProfileHealthResult(
        profile=profile,
        status=HEALTHY,
        message="deterministic profile health checks passed",
        provider=provider,
        model=model,
        profile_home=str(home),
        capability_key=capability_key,
        requirements=req,
        checked_at=current,
        llm_smoke_ran=llm_smoke_ran,
    )
    if use_cache:
        write_health_cache(result)
    return result
