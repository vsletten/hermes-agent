"""Project Autopilot schema and task-contract helpers.

This module is intentionally read-only.  P0.0 defines the executable data
model used by later verifier/dispatcher slices, but it must not mutate the
Kanban DB or any project-home files.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


SUPPORTED_SCHEMA_VERSION = "project-autopilot/v1"
LEGACY_SCHEMA_PREFIXES = ("project-autopilot/failsafe-v1-bootstrap",)
PROJECT_JSON = "project.json"
VALID_WORKSPACE_KINDS = {"scratch", "dir", "worktree"}
DEFAULT_PROJECTS_ACTIVE_DIR = Path.home() / "Documents" / "hermes-projects" / "active"


class ProjectState(str, Enum):
    NEW = "NEW"
    BOOTSTRAPPED = "BOOTSTRAPPED"
    PLANNED = "PLANNED"
    READY = "READY"
    EXECUTING = "EXECUTING"
    REVIEWING = "REVIEWING"
    REMEDIATING = "REMEDIATING"
    INTEGRATING = "INTEGRATING"
    PR_OPEN = "PR_OPEN"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    DONE = "DONE"
    ARCHIVED = "ARCHIVED"
    BROKEN_INVARIANT = "BROKEN_INVARIANT"
    BLOCKED_PROCESS = "BLOCKED_PROCESS"
    BLOCKED_WORKER = "BLOCKED_WORKER"
    BLOCKED_HUMAN = "BLOCKED_HUMAN"
    PAUSED = "PAUSED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProjectFailureState:
    failure_class: str
    message: str
    owner: str = "process"
    fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "failure_class": self.failure_class,
            "message": self.message,
            "owner": self.owner,
        }
        if self.fingerprint:
            data["fingerprint"] = self.fingerprint
        return data


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    expected_outputs: list[str]
    workspace_kind: str = "scratch"
    workspace_path: str | None = None
    branch_name: str | None = None
    completion_contract: dict[str, Any] = field(default_factory=dict)
    assignee: str | None = None
    kind: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "expected_outputs": list(self.expected_outputs),
            "workspace_kind": self.workspace_kind,
            "workspace_path": self.workspace_path,
            "branch_name": self.branch_name,
            "completion_contract": dict(self.completion_contract),
            "assignee": self.assignee,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class ProjectVerificationResult:
    ok: bool
    lifecycle_state: ProjectState
    autopilot_confidence: str
    project_home: str | None = None
    project_slug: str | None = None
    board_slug: str | None = None
    project_json: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failure_state: ProjectFailureState | None = None
    task_contract: TaskContract | None = None
    next_legal_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "lifecycle_state": self.lifecycle_state.value,
            "autopilot_confidence": self.autopilot_confidence,
            "project_home": self.project_home,
            "project_slug": self.project_slug,
            "board_slug": self.board_slug,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "failure_state": self.failure_state.to_dict() if self.failure_state else None,
            "task_contract": self.task_contract.to_dict() if self.task_contract else None,
            "next_legal_action": self.next_legal_action,
        }


def _failure_result(
    state: ProjectState,
    message: str,
    *,
    project_home: str | None = None,
    project_slug: str | None = None,
    board_slug: str | None = None,
    failure_class: str = "project_schema",
    owner: str = "process",
    fingerprint: str | None = None,
    warnings: list[str] | None = None,
    next_legal_action: str | None = None,
) -> ProjectVerificationResult:
    return ProjectVerificationResult(
        ok=False,
        lifecycle_state=state,
        autopilot_confidence="stopped" if state != ProjectState.RECOVERY_REQUIRED else "degraded",
        project_home=project_home,
        project_slug=project_slug,
        board_slug=board_slug,
        errors=[message],
        warnings=warnings or [],
        failure_state=ProjectFailureState(failure_class, message, owner=owner, fingerprint=fingerprint),
        next_legal_action=next_legal_action,
    )


def _project_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("HERMES_PROJECTS_HOME", "").strip()
    if env:
        roots.append(Path(env).expanduser())
    roots.append(DEFAULT_PROJECTS_ACTIVE_DIR)
    seen: set[Path] = set()
    uniq: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        if resolved not in seen:
            seen.add(resolved)
            uniq.append(root)
    return uniq


def _candidate_project_home(project_home_or_slug: str | os.PathLike[str]) -> Path:
    raw = Path(project_home_or_slug).expanduser()
    text = str(project_home_or_slug)
    if raw.is_absolute() or any(sep in text for sep in (os.sep, "/")):
        return raw
    for root in _project_roots():
        candidate = root / text
        if candidate.exists():
            return candidate
    return DEFAULT_PROJECTS_ACTIVE_DIR / text


def _as_mapping(value: Any, field_name: str, errors: list[str]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    errors.append(f"{field_name} must be an object")
    return {}


def load_project_json(project_home: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and parse ``project.json`` from a project home.

    Raises ``FileNotFoundError`` for missing project homes/files and
    ``ValueError`` for invalid JSON or non-object JSON.  ``load_project`` wraps
    these exceptions into typed fail-closed results for CLI and gate callers.
    """
    home = Path(project_home).expanduser()
    if not home.exists():
        raise FileNotFoundError(f"project home does not exist: {home}")
    path = home / PROJECT_JSON
    if not path.exists():
        raise FileNotFoundError(f"missing {PROJECT_JSON}: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def validate_project_schema(
    project_json: Mapping[str, Any],
    *,
    strict: bool = True,
    project_home: str | os.PathLike[str] | None = None,
    board_conn: Any | None = None,
) -> ProjectVerificationResult:
    """Validate the P0.0 strict project schema.

    Compatibility mode accepts legacy script-owned project files as degraded
    only; it never reports them as operational.
    """
    data = dict(project_json)
    errors: list[str] = []
    warnings: list[str] = []
    slug = str(data.get("slug") or "") or None
    board_slug = str(data.get("board_slug") or "") or None
    home = str(project_home or data.get("project_home") or "") or None

    schema_version = data.get("schema_version")
    if not schema_version:
        if not strict and ("state" in data or "task_graph" in data):
            warnings.append("legacy project.json has no schema_version; migration required")
            return ProjectVerificationResult(
                ok=False,
                lifecycle_state=ProjectState.UNKNOWN,
                autopilot_confidence="degraded",
                project_home=home,
                project_slug=slug,
                board_slug=board_slug,
                project_json=data,
                warnings=warnings,
                next_legal_action="migrate project.json to project-autopilot/v1",
            )
        errors.append("missing required field: schema_version")
    elif schema_version != SUPPORTED_SCHEMA_VERSION:
        if not strict and schema_version in LEGACY_SCHEMA_PREFIXES:
            warnings.append(
                f"legacy schema_version {schema_version!r}; migration required"
            )
            return ProjectVerificationResult(
                ok=False,
                lifecycle_state=ProjectState.UNKNOWN,
                autopilot_confidence="degraded",
                project_home=home,
                project_slug=slug,
                board_slug=board_slug,
                project_json=data,
                warnings=warnings,
                next_legal_action="migrate project.json to project-autopilot/v1",
            )
        errors.append(f"unsupported schema_version: {schema_version}")

    required = [
        "schema_version",
        "slug",
        "board_slug",
        "root_task_id",
        "project_home",
        "lifecycle_state",
        "execution_policy",
        "worker_policy",
        "task_contracts",
        "failure_state",
    ]
    for field_name in required:
        if field_name not in data or data.get(field_name) in (None, ""):
            errors.append(f"missing required field: {field_name}")

    if "lifecycle_state" in data:
        try:
            ProjectState(str(data["lifecycle_state"]))
        except ValueError:
            errors.append(f"unsupported lifecycle_state: {data.get('lifecycle_state')}")

    execution_policy = _as_mapping(data.get("execution_policy", {}), "execution_policy", errors)
    _as_mapping(data.get("worker_policy", {}), "worker_policy", errors)
    task_contracts = _as_mapping(data.get("task_contracts", {}), "task_contracts", errors)
    _as_mapping(data.get("failure_state", {}), "failure_state", errors)

    requires_contracts = bool(execution_policy.get("requires_task_contracts", True))
    if requires_contracts and not task_contracts:
        errors.append("task_contracts must not be empty when contracts are required")

    for task_id in task_contracts:
        contract_result = validate_task_contract(parse_task_contract(str(task_id), data))
        errors.extend(f"task_contracts.{task_id}: {err}" for err in contract_result.errors)

    if board_conn is not None and task_contracts:
        missing = _missing_board_tasks(board_conn, task_contracts.keys())
        for task_id in missing:
            errors.append(f"task_contracts references absent board task: {task_id}")

    if errors:
        return ProjectVerificationResult(
            ok=False,
            lifecycle_state=ProjectState.BROKEN_INVARIANT,
            autopilot_confidence="stopped",
            project_home=home,
            project_slug=slug,
            board_slug=board_slug,
            project_json=data,
            errors=errors,
            warnings=warnings,
            failure_state=ProjectFailureState(
                "project_schema", "; ".join(errors), fingerprint="project_schema_invalid"
            ),
            next_legal_action="repair project.json schema before dispatch",
        )

    return ProjectVerificationResult(
        ok=True,
        lifecycle_state=ProjectState(str(data["lifecycle_state"])),
        autopilot_confidence="operational",
        project_home=home,
        project_slug=slug,
        board_slug=board_slug,
        project_json=data,
        warnings=warnings,
    )


def parse_task_contract(task_id: str, project_json: Mapping[str, Any]) -> TaskContract:
    contracts = project_json.get("task_contracts")
    raw: Any = {}
    if isinstance(contracts, Mapping):
        raw = contracts.get(task_id, {})
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raw = {"expected_outputs": raw}
    raw_dict = dict(raw)
    workspace = raw_dict.get("workspace")
    workspace_dict = dict(workspace) if isinstance(workspace, Mapping) else {}
    expected_raw = raw_dict.get("expected_outputs")
    expected_outputs = [str(v) for v in expected_raw] if isinstance(expected_raw, list) else []
    completion = raw_dict.get("completion_contract")
    return TaskContract(
        task_id=task_id,
        expected_outputs=expected_outputs,
        workspace_kind=str(raw_dict.get("workspace_kind") or workspace_dict.get("kind") or "scratch"),
        workspace_path=(
            str(raw_dict.get("workspace_path") or workspace_dict.get("path"))
            if raw_dict.get("workspace_path") or workspace_dict.get("path")
            else None
        ),
        branch_name=(
            str(raw_dict.get("branch_name") or workspace_dict.get("branch_name"))
            if raw_dict.get("branch_name") or workspace_dict.get("branch_name")
            else None
        ),
        completion_contract=dict(completion) if isinstance(completion, Mapping) else {},
        assignee=str(raw_dict["assignee"]) if raw_dict.get("assignee") else None,
        kind=str(raw_dict["kind"]) if raw_dict.get("kind") else None,
        raw=raw_dict,
    )


def validate_task_contract(
    contract: TaskContract,
    task_row: Any | None = None,
    *,
    require_completion_contract: bool = True,
) -> ProjectVerificationResult:
    errors: list[str] = []
    if not contract.task_id:
        errors.append("task_id is required")
    if not contract.expected_outputs:
        errors.append("expected_outputs is required and must not be empty")
    else:
        for output in contract.expected_outputs:
            lowered = output.lower()
            if " or " in lowered:
                errors.append(f"ambiguous expected_outputs alternative: {output}")
            if not output.startswith("/") and "://" not in output:
                errors.append(f"expected_outputs must name exact absolute path or URI: {output}")

    if contract.workspace_kind not in VALID_WORKSPACE_KINDS:
        errors.append(f"unknown workspace_kind: {contract.workspace_kind}")
    if contract.workspace_kind in {"dir", "worktree"}:
        if not contract.workspace_path:
            errors.append(f"workspace_path is required for {contract.workspace_kind}")
        elif not Path(contract.workspace_path).expanduser().is_absolute():
            errors.append("workspace_path must be absolute for dir/worktree")
    if contract.workspace_kind == "worktree" and not contract.branch_name:
        errors.append("branch_name is required for worktree")
    if contract.branch_name and contract.workspace_kind != "worktree":
        errors.append("branch_name is only valid for worktree")

    if require_completion_contract:
        required_completion = {"status_report", "tests"}
        missing = sorted(required_completion - set(contract.completion_contract))
        for field_name in missing:
            errors.append(f"missing completion_contract field: {field_name}")

    if task_row is not None:
        row = task_row if isinstance(task_row, Mapping) else getattr(task_row, "__dict__", {})
        row_kind = row.get("workspace_kind") if isinstance(row, Mapping) else None
        row_path = row.get("workspace_path") if isinstance(row, Mapping) else None
        row_branch = row.get("branch_name") if isinstance(row, Mapping) else None
        if row_kind and row_kind != contract.workspace_kind:
            errors.append(f"workspace_kind disagrees with board task: {row_kind}")
        if row_path and contract.workspace_path and row_path != contract.workspace_path:
            errors.append("workspace_path disagrees with board task")
        if row_branch and contract.branch_name and row_branch != contract.branch_name:
            errors.append("branch_name disagrees with board task")

    if errors:
        return ProjectVerificationResult(
            ok=False,
            lifecycle_state=ProjectState.BLOCKED_PROCESS,
            autopilot_confidence="stopped",
            errors=errors,
            failure_state=ProjectFailureState(
                "task_contract", "; ".join(errors), fingerprint="task_contract_invalid"
            ),
            task_contract=contract,
            next_legal_action="repair task contract before dispatch",
        )
    return ProjectVerificationResult(
        ok=True,
        lifecycle_state=ProjectState.READY,
        autopilot_confidence="operational",
        task_contract=contract,
    )


def _missing_board_tasks(board_conn: Any, task_ids: Any) -> list[str]:
    ids = [str(t) for t in task_ids]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = board_conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})", ids
    ).fetchall()
    present = {row["id"] if hasattr(row, "keys") else row[0] for row in rows}
    return [task_id for task_id in ids if task_id not in present]


def load_project(
    project_home_or_slug: str | os.PathLike[str],
    board_slug: str | None = None,
    *,
    strict: bool = False,
) -> ProjectVerificationResult:
    home = _candidate_project_home(project_home_or_slug)
    if not home.exists():
        return _failure_result(
            ProjectState.RECOVERY_REQUIRED,
            f"project home does not exist: {home}",
            project_home=str(home),
            board_slug=board_slug,
            failure_class="project_home_missing",
            fingerprint="project_home_missing",
            next_legal_action="create or reconstruct project home before dispatch",
        )
    try:
        data = load_project_json(home)
    except FileNotFoundError as exc:
        return _failure_result(
            ProjectState.BROKEN_INVARIANT,
            str(exc),
            project_home=str(home),
            board_slug=board_slug,
            failure_class="project_json_missing",
            fingerprint="project_json_missing",
            next_legal_action="create or reconstruct project.json before dispatch",
        )
    except ValueError as exc:
        return _failure_result(
            ProjectState.BROKEN_INVARIANT,
            str(exc),
            project_home=str(home),
            board_slug=board_slug,
            failure_class="project_json_invalid",
            fingerprint="project_json_invalid",
            next_legal_action="repair project.json JSON before dispatch",
        )
    result = validate_project_schema(data, strict=strict, project_home=home)
    if board_slug and result.board_slug and result.board_slug != board_slug:
        return _failure_result(
            ProjectState.BROKEN_INVARIANT,
            f"project board_slug {result.board_slug!r} does not match requested board {board_slug!r}",
            project_home=str(home),
            project_slug=result.project_slug,
            board_slug=result.board_slug,
            failure_class="project_board_mismatch",
            fingerprint="project_board_mismatch",
            next_legal_action="repair project.json board_slug before dispatch",
        )
    return result


def _project_json_candidates() -> list[Path]:
    candidates: list[Path] = []
    for root in _project_roots():
        if not root.exists():
            continue
        try:
            for path in root.glob(f"*/{PROJECT_JSON}"):
                candidates.append(path)
        except OSError:
            continue
    return candidates


def _task_in_project_json(task_id: str, data: Mapping[str, Any]) -> bool:
    if data.get("root_task_id") == task_id:
        return True
    contracts = data.get("task_contracts")
    if isinstance(contracts, Mapping) and task_id in contracts:
        return True
    graph = data.get("task_graph")
    if isinstance(graph, Mapping) and task_id in {str(v) for v in graph.values()}:
        return True
    return False


def _board_has_task(board_slug: str, task_id: str) -> bool | None:
    try:
        from hermes_cli import kanban_db as kb

        with kb.connect(board=board_slug) as conn:
            row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return row is not None
    except Exception:
        return None


def discover_project_for_task(task_id: str, board_slug: str | None = None) -> ProjectVerificationResult:
    """Find the unique project whose project.json claims ``task_id``.

    Discovery is conservative: zero matches is a recovery result, and multiple
    matches is a broken invariant.  Later dispatcher gates can use this without
    silently choosing a project membership when evidence conflicts.
    """
    matches: list[tuple[Path, dict[str, Any]]] = []
    board_slug = board_slug or os.environ.get("HERMES_KANBAN_BOARD") or None
    for path in _project_json_candidates():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or not _task_in_project_json(task_id, data):
            continue
        if board_slug and data.get("board_slug") and data.get("board_slug") != board_slug:
            continue
        matches.append((path.parent, data))

    if not matches:
        return _failure_result(
            ProjectState.RECOVERY_REQUIRED,
            f"no project claims task {task_id}",
            board_slug=board_slug,
            failure_class="project_membership_missing",
            fingerprint="project_membership_missing",
            next_legal_action="add task to a single project task_contracts entry or task_graph",
        )
    if len(matches) > 1:
        homes = ", ".join(str(home) for home, _data in matches)
        return _failure_result(
            ProjectState.BROKEN_INVARIANT,
            f"multiple projects claim task {task_id}: {homes}",
            board_slug=board_slug,
            failure_class="project_membership_ambiguous",
            fingerprint="project_membership_ambiguous",
            next_legal_action="repair project membership so exactly one project claims the task",
        )

    home, data = matches[0]
    result = validate_project_schema(data, strict=False, project_home=home)
    if not result.ok:
        return result
    if board_slug:
        has_task = _board_has_task(board_slug, task_id)
        if has_task is False:
            return _failure_result(
                ProjectState.BROKEN_INVARIANT,
                f"project claims absent board task: {task_id}",
                project_home=str(home),
                project_slug=result.project_slug,
                board_slug=board_slug,
                failure_class="project_board_task_missing",
                fingerprint="project_board_task_missing",
                next_legal_action="create/link the board task or remove it from project.json",
            )
    contract = parse_task_contract(task_id, data)
    contract_result = validate_task_contract(contract)
    if not contract_result.ok:
        return ProjectVerificationResult(
            ok=False,
            lifecycle_state=contract_result.lifecycle_state,
            autopilot_confidence="stopped",
            project_home=str(home),
            project_slug=result.project_slug,
            board_slug=result.board_slug,
            project_json=data,
            errors=contract_result.errors,
            warnings=result.warnings,
            failure_state=contract_result.failure_state,
            task_contract=contract,
            next_legal_action=contract_result.next_legal_action,
        )
    return ProjectVerificationResult(
        ok=True,
        lifecycle_state=result.lifecycle_state,
        autopilot_confidence="operational",
        project_home=str(home),
        project_slug=result.project_slug,
        board_slug=result.board_slug,
        project_json=data,
        warnings=result.warnings,
        task_contract=contract,
    )


def cmd_project(args: argparse.Namespace) -> int:
    action = getattr(args, "project_action", None)
    if action == "verify":
        result = load_project(
            args.project,
            board_slug=getattr(args, "board", None),
            strict=getattr(args, "strict", False),
        )
        if getattr(args, "json", False):
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            status = "OK" if result.ok else result.lifecycle_state.value
            print(f"{status}: {result.project_home or args.project}")
            for err in result.errors:
                print(f"error: {err}")
            for warning in result.warnings:
                print(f"warning: {warning}")
        return 0 if result.ok else 1
    raise SystemExit("missing project subcommand")


def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = parent_subparsers.add_parser(
        "project",
        help="Inspect Hermes Project Autopilot project schemas",
        description="Read-only Project Autopilot schema and contract commands.",
    )
    sub = parser.add_subparsers(dest="project_action")
    verify = sub.add_parser("verify", help="Validate a project.json schema")
    verify.add_argument("project", help="Project slug under active projects/ or project home path")
    verify.add_argument("--board", help="Expected kanban board slug")
    verify.add_argument("--strict", action="store_true", help="Require strict project-autopilot/v1 schema")
    verify.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(func=cmd_project)
    return parser
