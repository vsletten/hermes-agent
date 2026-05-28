"""Deterministic filesystem-backed Project Autopilot V0.

V0 is intentionally boring:
- file-backed project homes
- kanban board remains execution truth
- project.json owns lifecycle/continuity metadata
- only project_mode=stacked-slices-one-pr is supported
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "project-autopilot/v0"
PROJECT_MODE_V0 = "stacked-slices-one-pr"
SUPPORTED_PROJECT_MODES = {PROJECT_MODE_V0}
PROJECTS_ROOT = Path.home() / "Documents" / "hermes-projects"
ACTIVE_PROJECTS_ROOT = PROJECTS_ROOT / "active"

REQUIRED_FILES = (
    "PROJECT.md",
    "STATUS.md",
    "SESSION-HANDOFF.md",
    "SESSION-LOG.md",
    "PARKING-LOT.md",
    "TASKS.md",
    "project.json",
)
REQUIRED_DIRS = ("status", "refs", "scratch", "artifacts")
TERMINAL_TASK_STATES = {"done", "archived", "superseded", "canceled"}
DEPENDENCY_SATISFYING_STATES = {"done"}

_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-_]{0,63}$")


class ProjectAutopilotError(RuntimeError):
    """Base error for deterministic Project Autopilot failures."""


class InvariantError(ProjectAutopilotError):
    """Raised when a project lifecycle invariant fails."""


def now_ts() -> int:
    return int(time.time())


def branch_to_worktree_path(*, worktree_namespace: Path, branch_name: str) -> Path:
    branch = branch_name.strip().strip("/")
    if not branch or branch.startswith("-") or ".." in branch.split("/"):
        raise ProjectAutopilotError(
            f"invalid branch name for worktree path: {branch_name!r}"
        )
    if not _BRANCH_RE.match(branch):
        raise ProjectAutopilotError(
            f"invalid branch name for worktree path: {branch_name!r}"
        )
    return worktree_namespace / Path(*branch.split("/"))


def _canonical_worktree_namespace(repo_org: str, repo_name: str) -> Path:
    return Path("/Users/vsletten/src") / repo_org / repo_name


def normalize_project_doc(
    *,
    slug: str,
    title: str,
    goal: str,
    board_slug: str,
    root_task_id: str,
    project_home: Path,
    repo_org: str,
    repo_name: str,
    canonical_checkout: Path,
    final_branch: str,
    default_worker: str = "codexapp",
) -> dict[str, Any]:
    if not _SLUG_RE.match(slug):
        raise ProjectAutopilotError(f"invalid project slug: {slug!r}")
    worktree_namespace = _canonical_worktree_namespace(repo_org, repo_name)
    final_worktree_path = branch_to_worktree_path(
        worktree_namespace=worktree_namespace,
        branch_name=final_branch,
    )
    ts = now_ts()
    return {
        "schema_version": SCHEMA_VERSION,
        "slug": slug,
        "title": title,
        "goal": goal,
        "project_type": "coding",
        "project_mode": PROJECT_MODE_V0,
        "state": "BOOTSTRAPPED",
        "board_slug": board_slug,
        "root_task_id": root_task_id,
        "project_home": str(project_home),
        "default_worker": default_worker,
        "repo": {
            "org": repo_org,
            "name": repo_name,
            "canonical_checkout": str(canonical_checkout),
            "worktree_namespace": str(worktree_namespace),
            "remote_name": "origin",
            "remote_url": None,
            "default_branch": "main",
            "pr_base_ref": "main",
            "provider": "github",
        },
        "branch_strategy": {
            "mode": PROJECT_MODE_V0,
            "base_ref": "origin/main",
            "final_branch": final_branch,
        },
        "final_worktree_path": str(final_worktree_path),
        "pr_url": None,
        "pr_requirement": {
            "required": True,
            "waived": False,
            "waived_by": None,
            "waived_at": None,
            "reason": None,
            "approval_evidence": None,
        },
        "execution_policy": {"parallelism": "none", "max_active_workers": 1},
        "cleanup_policy": {
            "mode": "manual-approval-required",
            "keep_final_pr_worktree": True,
            "remove_intermediate_worktrees_after_pr_open": False,
        },
        "cleanup": {
            "state": "not_started",
            "inventory_path": None,
            "approved_by": None,
            "approved_at": None,
            "completed_at": None,
            "targets": [],
        },
        "completion": {
            "done_by": None,
            "done_at": None,
            "done_basis": None,
            "evidence": [],
        },
        "created_at": ts,
        "updated_at": ts,
        "last_verified_at": None,
        "last_transition": None,
        "transition_log": [],
        "invariant_failures": [],
        "workspace_contracts": {},
        "task_graph": {"nodes": [], "edges": []},
    }


def validate_project_doc(doc: dict[str, Any]) -> None:
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ProjectAutopilotError("unsupported schema_version")
    if doc.get("project_mode") not in SUPPORTED_PROJECT_MODES:
        raise ProjectAutopilotError(
            f"unsupported project_mode: {doc.get('project_mode')!r}"
        )
    branch_strategy = doc.get("branch_strategy") or {}
    if branch_strategy.get("mode") not in SUPPORTED_PROJECT_MODES:
        raise ProjectAutopilotError(
            f"unsupported branch_strategy.mode: {branch_strategy.get('mode')!r}"
        )
    if "final_branch" in doc:
        raise ProjectAutopilotError(
            "top-level final_branch is not authoritative in V0"
        )

    required_top = [
        "slug",
        "title",
        "goal",
        "state",
        "board_slug",
        "root_task_id",
        "project_home",
        "repo",
        "branch_strategy",
        "final_worktree_path",
        "pr_requirement",
        "execution_policy",
        "cleanup",
        "transition_log",
    ]
    missing = [key for key in required_top if key not in doc]
    if missing:
        raise ProjectAutopilotError(f"missing project fields: {', '.join(missing)}")

    repo = doc["repo"]
    for key in (
        "org",
        "name",
        "canonical_checkout",
        "worktree_namespace",
        "remote_name",
        "default_branch",
        "pr_base_ref",
        "provider",
    ):
        if not repo.get(key):
            raise ProjectAutopilotError(f"missing repo.{key}")

    expected_policy = {"parallelism": "none", "max_active_workers": 1}
    if doc["execution_policy"] != expected_policy:
        raise ProjectAutopilotError(
            "V0 requires execution_policy parallelism=none max_active_workers=1"
        )
