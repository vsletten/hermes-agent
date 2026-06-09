"""Project Autopilot schema and task-contract helpers.

This module is intentionally read-only.  P0.0 defines the executable data
model used by later verifier/dispatcher slices, but it must not mutate the
Kanban DB or any project-home files.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    truth_source: str | None = None
    truth_read_at: str | None = None
    board_snapshot: dict[str, Any] = field(default_factory=dict)
    project_graph_snapshot: dict[str, Any] = field(default_factory=dict)
    project_home_invariant: str | None = None
    active_task: dict[str, Any] | None = None
    board_event_watermark: dict[str, Any] = field(default_factory=dict)

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
            "truth_source": self.truth_source,
            "truth_read_at": self.truth_read_at,
            "board_snapshot": dict(self.board_snapshot),
            "project_graph_snapshot": dict(self.project_graph_snapshot),
            "project_home_invariant": self.project_home_invariant,
            "active_task": dict(self.active_task) if self.active_task else None,
            "board_event_watermark": dict(self.board_event_watermark),
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


def _format_truth_time(now: datetime | int | float | None) -> str:
    if now is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        dt = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    else:
        dt = datetime.fromtimestamp(float(now), tz=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bucket_task(row: Mapping[str, Any]) -> str:
    status = str(row.get("status") or "").lower()
    if status in {"done", "completed", "complete"} or row.get("completed_at"):
        return "done"
    if status == "blocked":
        return "blocked"
    if status in {"running", "in_progress", "claimed"}:
        return "active"
    if status in {"ready", "triage"}:
        return "ready"
    if status == "todo":
        return "todo"
    if row.get("current_run_id") or (row.get("started_at") and not row.get("completed_at")):
        return "active"
    return status or "unknown"


def _counts_from_rows(rows: list[Mapping[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter(_bucket_task(row) for row in rows)
    return {
        "total": len(rows),
        "done": counts.get("done", 0),
        "active": counts.get("active", 0),
        "ready": counts.get("ready", 0),
        "todo": counts.get("todo", 0),
        "blocked": counts.get("blocked", 0),
        "other": sum(v for k, v in counts.items() if k not in {"done", "active", "ready", "todo", "blocked"}),
    }


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _open_board_read_only(board_slug: str | None) -> tuple[sqlite3.Connection, Path]:
    from hermes_cli import kanban_db as kb

    path = kb.kanban_db_path(board=board_slug)
    if not path.exists():
        raise FileNotFoundError(f"cannot read board truth: board DB does not exist: {path}")
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn, path


def read_board_event_watermark(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS max_id, COUNT(*) AS count FROM task_events").fetchone()
    return {"max_event_id": int(row["max_id"] or 0), "event_count": int(row["count"] or 0)}


def compute_full_board_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, started_at, completed_at, current_run_id FROM tasks"
    ).fetchall()
    return _counts_from_rows([_row_dict(row) for row in rows])


def compute_project_graph(root_task_id: str, conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        WITH RECURSIVE graph(id) AS (
          SELECT ?
          UNION
          SELECT l.child_id
          FROM task_links l
          JOIN graph ON l.parent_id = graph.id
          UNION
          SELECT l.parent_id
          FROM task_links l
          JOIN graph ON l.child_id = graph.id
        )
        SELECT DISTINCT id FROM graph ORDER BY id ASC
        """,
        (root_task_id,),
    ).fetchall()
    ids = [str(row["id"]) for row in rows]
    if not ids:
        return {"root_task_id": root_task_id, "task_ids": [], "counts": _counts_from_rows([]), "tasks": []}
    placeholders = ",".join("?" for _ in ids)
    task_rows = conn.execute(
        f"""
        SELECT id, title, assignee, status, started_at, completed_at, current_run_id,
               last_failure_error, result, created_at
        FROM tasks
        WHERE id IN ({placeholders})
        ORDER BY created_at ASC, id ASC
        """,
        ids,
    ).fetchall()
    tasks = [_row_dict(row) for row in task_rows]
    present_ids = {str(task["id"]) for task in tasks}
    missing_ids = [task_id for task_id in ids if task_id not in present_ids]
    return {
        "root_task_id": root_task_id,
        "task_ids": ids,
        "missing_task_ids": missing_ids,
        "counts": _counts_from_rows(tasks),
        "tasks": tasks,
    }


def compute_active_task_details(conn: sqlite3.Connection, task_ids: list[str] | None = None) -> dict[str, Any] | None:
    where = "WHERE status IN ('running', 'in_progress') OR current_run_id IS NOT NULL"
    params: list[Any] = []
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        where = f"WHERE id IN ({placeholders}) AND (status IN ('running', 'in_progress') OR current_run_id IS NOT NULL)"
        params = list(task_ids)
    row = conn.execute(
        f"""
        SELECT id, title, status, assignee, current_run_id
        FROM tasks
        {where}
        ORDER BY started_at DESC, created_at ASC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        return None
    data = _row_dict(row)
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "status": data.get("status"),
        "assignee": data.get("assignee"),
        "run_id": data.get("current_run_id"),
    }


def read_board_snapshot(board_slug: str | None) -> dict[str, Any]:
    conn, path = _open_board_read_only(board_slug)
    try:
        return {
            "board_slug": board_slug or "default",
            "db_path": str(path),
            "counts": compute_full_board_counts(conn),
            "event_watermark": read_board_event_watermark(conn),
        }
    finally:
        conn.close()


def _project_home_invariant(project_home: Path, project_json: Mapping[str, Any]) -> tuple[str, list[str]]:
    errors: list[str] = []
    if not project_home.exists():
        errors.append(f"project home does not exist: {project_home}")
    if not (project_home / PROJECT_JSON).exists():
        errors.append(f"missing {PROJECT_JSON}: {project_home / PROJECT_JSON}")
    declared = project_json.get("project_home")
    if declared:
        try:
            if Path(str(declared)).expanduser().resolve() != project_home.resolve():
                errors.append("project_home in project.json does not match loaded project home")
        except OSError:
            errors.append("project_home path could not be resolved")
    return ("BROKEN" if errors else "OK"), errors


def compute_next_legal_transition(result: ProjectVerificationResult) -> str:
    if result.failure_state:
        message = result.failure_state.message
        if "cannot read board truth" in message:
            return "repair board DB access, then rerun `hermes project verify --json`"
        if result.project_home_invariant == "BROKEN":
            return "repair project-home invariant before dispatch"
        return result.next_legal_action or f"resolve {result.failure_state.failure_class}: {message}"
    graph_counts = result.project_graph_snapshot.get("counts", {}) if result.project_graph_snapshot else {}
    active = result.active_task
    if active:
        return f"monitor or unblock active task {active.get('id')} ({active.get('title')})"
    if graph_counts.get("blocked", 0):
        return "resolve the first blocked project task before dispatching more work"
    if graph_counts.get("ready", 0):
        return "dispatch the next ready project task"
    if graph_counts.get("todo", 0):
        return "wait for dependencies or promote the next eligible todo task"
    if graph_counts.get("active", 0):
        return "monitor active project task until it completes or blocks"
    if graph_counts.get("total", 0) and graph_counts.get("done", 0) == graph_counts.get("total", 0):
        return "verify completion contracts, then transition project to DONE"
    return "repair project graph membership before dispatch"


def verify_project(
    project_home_or_slug: str | os.PathLike[str],
    *,
    board: str | None = None,
    now: datetime | int | float | None = None,
) -> ProjectVerificationResult:
    truth_read_at = _format_truth_time(now)
    loaded = load_project(project_home_or_slug, board_slug=board, strict=True)
    board_slug = board or loaded.board_slug
    if not loaded.ok:
        return ProjectVerificationResult(
            **{
                **loaded.to_dict(),
                "lifecycle_state": loaded.lifecycle_state,
                "failure_state": loaded.failure_state,
                "task_contract": loaded.task_contract,
                "truth_read_at": truth_read_at,
                "next_legal_action": loaded.next_legal_action or compute_next_legal_transition(loaded),
            }
        )

    try:
        conn, board_path = _open_board_read_only(board_slug)
    except Exception as exc:
        failure = ProjectFailureState(
            "board_read_failure",
            f"cannot read board truth: {exc}",
            owner="process",
            fingerprint="board_read_failure",
        )
        result = ProjectVerificationResult(
            ok=False,
            lifecycle_state=ProjectState.UNKNOWN,
            autopilot_confidence="stopped",
            project_home=loaded.project_home,
            project_slug=loaded.project_slug,
            board_slug=board_slug,
            project_json=loaded.project_json,
            errors=[failure.message],
            warnings=loaded.warnings,
            failure_state=failure,
            truth_read_at=truth_read_at,
            project_home_invariant="UNKNOWN",
        )
        return ProjectVerificationResult(
            **{**result.to_dict(), "lifecycle_state": result.lifecycle_state, "failure_state": failure, "next_legal_action": compute_next_legal_transition(result)}
        )

    try:
        board_counts = compute_full_board_counts(conn)
        watermark = read_board_event_watermark(conn)
        root_task_id = str((loaded.project_json or {}).get("root_task_id") or "")
        graph = compute_project_graph(root_task_id, conn)
        active_task = compute_active_task_details(conn, graph.get("task_ids") or None)
        schema_result = validate_project_schema(loaded.project_json or {}, strict=True, project_home=loaded.project_home, board_conn=conn)
    finally:
        conn.close()

    home = Path(str(loaded.project_home)).expanduser() if loaded.project_home else _candidate_project_home(project_home_or_slug)
    invariant, invariant_errors = _project_home_invariant(home, loaded.project_json or {})
    errors = list(schema_result.errors) + invariant_errors
    failure: ProjectFailureState | None = schema_result.failure_state
    state = schema_result.lifecycle_state if schema_result.ok else ProjectState.BROKEN_INVARIANT
    confidence = "operational" if schema_result.ok and invariant == "OK" else "stopped"
    ok = schema_result.ok and invariant == "OK"

    root_present = any(task.get("id") == root_task_id for task in graph.get("tasks", []))
    if not root_present:
        errors.append(f"root task {root_task_id} not found on board {board_slug}")
        failure = ProjectFailureState("project_graph", errors[-1], fingerprint="project_root_missing")
        state = ProjectState.BROKEN_INVARIANT
        confidence = "stopped"
        ok = False

    graph_counts = graph.get("counts", {})
    if ok:
        if graph_counts.get("active", 0):
            state = ProjectState.EXECUTING
        elif graph_counts.get("blocked", 0):
            state = ProjectState.BLOCKED_PROCESS
            confidence = "degraded"
        elif graph_counts.get("ready", 0) or graph_counts.get("todo", 0):
            state = ProjectState.READY
        elif graph_counts.get("total", 0) and graph_counts.get("done", 0) == graph_counts.get("total", 0):
            state = ProjectState.DONE

    if loaded.lifecycle_state is ProjectState.DONE and graph_counts.get("total", 0) != graph_counts.get("done", 0):
        msg = "project.json says DONE but board graph still has active/non-done tasks"
        errors.append(msg)
        failure = ProjectFailureState("project_board_disagreement", msg, fingerprint="project_done_disagreement")
        state = ProjectState.BROKEN_INVARIANT
        confidence = "stopped"
        ok = False

    result = ProjectVerificationResult(
        ok=ok,
        lifecycle_state=state,
        autopilot_confidence=confidence,
        project_home=loaded.project_home,
        project_slug=loaded.project_slug,
        board_slug=board_slug,
        project_json=loaded.project_json,
        errors=errors,
        warnings=loaded.warnings,
        failure_state=failure,
        task_contract=loaded.task_contract,
        truth_source=str(board_path),
        truth_read_at=truth_read_at,
        board_snapshot={"counts": board_counts, "db_path": str(board_path), "board_slug": board_slug},
        project_graph_snapshot={k: v for k, v in graph.items() if k != "tasks"},
        project_home_invariant=invariant,
        active_task=active_task,
        board_event_watermark=watermark,
    )
    return ProjectVerificationResult(
        **{**result.to_dict(), "lifecycle_state": result.lifecycle_state, "failure_state": failure, "task_contract": result.task_contract, "next_legal_action": compute_next_legal_transition(result)}
    )


def _fmt_counts(counts: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={int(counts.get(key, 0) or 0)}" for key in ("total", "done", "active", "ready", "todo", "blocked", "other"))


def render_project_status(result: ProjectVerificationResult) -> str:
    board_counts = (result.board_snapshot or {}).get("counts", {})
    graph_counts = (result.project_graph_snapshot or {}).get("counts", {})
    active = result.active_task
    if active:
        active_line = f"{active.get('id')} / {active.get('title')} / {active.get('status')} / {active.get('assignee')} / run_id={active.get('run_id')}"
    else:
        active_line = "none"
    blocker = "none" if not result.failure_state else json.dumps(result.failure_state.to_dict(), sort_keys=True)
    return "\n".join(
        [
            "# Project status",
            "",
            f"State: {result.lifecycle_state.value}",
            f"Truth source: {result.truth_source or 'unknown'}",
            f"Truth read at: {result.truth_read_at or 'unknown'}",
            f"Board snapshot: {_fmt_counts(board_counts)}",
            f"Project graph snapshot: {_fmt_counts(graph_counts)}",
            f"Project-home invariant: {result.project_home_invariant or 'UNKNOWN'}",
            f"Active task: {active_line}",
            f"Next legal transition: {result.next_legal_action or compute_next_legal_transition(result)}",
            f"Blocker: {blocker}",
            f"Autopilot confidence: {result.autopilot_confidence}",
            "",
        ]
    )


def cmd_project(args: argparse.Namespace) -> int:
    action = getattr(args, "project_action", None)
    if action == "verify":
        result = verify_project(
            args.project,
            board=getattr(args, "board", None),
        )
        if getattr(args, "json", False):
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            print(render_project_status(result))
        return 0 if result.ok else 1
    if action == "status":
        result = verify_project(
            args.project,
            board=getattr(args, "board", None),
        )
        status_text = render_project_status(result)
        if getattr(args, "write", False):
            home = Path(result.project_home or _candidate_project_home(args.project))
            home.mkdir(parents=True, exist_ok=True)
            (home / "STATUS.md").write_text(status_text, encoding="utf-8")
        print(status_text)
        return 0 if result.ok else 1
    raise SystemExit("missing project subcommand")


def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = parent_subparsers.add_parser(
        "project",
        help="Inspect Hermes Project Autopilot project truth",
        description="Read-only Project Autopilot schema, verifier, and status commands.",
    )
    sub = parser.add_subparsers(dest="project_action")
    verify = sub.add_parser("verify", help="Verify project truth from project.json and kanban board")
    verify.add_argument("project", help="Project slug under active projects/ or project home path")
    verify.add_argument("--board", help="Expected kanban board slug")
    verify.add_argument("--strict", action="store_true", help="Accepted for compatibility; verifier always requires strict project-autopilot/v1 schema")
    verify.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    status = sub.add_parser("status", help="Render truthful project STATUS.md text")
    status.add_argument("project", help="Project slug under active projects/ or project home path")
    status.add_argument("--board", help="Expected kanban board slug")
    status.add_argument("--write", action="store_true", help="Write STATUS.md in the project home")
    parser.set_defaults(func=cmd_project)
    return parser
