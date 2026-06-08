"""Stack PR dashboard plugin backend.

Mounted at /api/plugins/stack-pr/ by the dashboard plugin system.

The plugin intentionally wraps only the fixed v1 ``stack-pr`` actions. It does
not expose arbitrary CLI passthrough, custom subcommands, or branch switching.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

router = APIRouter()

_STACK_PR_TIMEOUT_SECONDS = 120
_GIT_TIMEOUT_SECONDS = 15


class RepoRequest(BaseModel):
    repo_path: str


class ConfirmedRepoRequest(RepoRequest):
    confirm: bool = False


class AbandonRequest(RepoRequest):
    confirm_text: str = ""


def _tool_availability(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    return {"available": bool(path), "path": path}


def _raise_bad_request(message: str) -> None:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


def _validate_repo_path(repo_path: str) -> Path:
    raw_path = (repo_path or "").strip()
    if not raw_path:
        _raise_bad_request("repo_path is required")

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        _raise_bad_request("repo_path must be an absolute path")

    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        _raise_bad_request(f"repo_path could not be resolved: {exc}")

    if resolved == Path(resolved.anchor):
        _raise_bad_request("repo_path cannot be a filesystem root")
    if not resolved.exists():
        _raise_bad_request(f"repo_path does not exist: {resolved}")
    if not resolved.is_dir():
        _raise_bad_request(f"repo_path is not a directory: {resolved}")

    argv = ["git", "-C", str(resolved), "rev-parse", "--is-inside-work-tree"]
    try:
        probe = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            shell=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="git executable was not found on PATH",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="git repo validation timed out",
        ) from exc

    if probe.returncode != 0 or probe.stdout.strip().lower() != "true":
        detail = (probe.stderr or probe.stdout or "not a git repository").strip()
        _raise_bad_request(detail)

    return resolved


def _repo_status(repo_path: Optional[str]) -> Optional[dict[str, Any]]:
    if repo_path is None or repo_path.strip() == "":
        return None
    try:
        resolved = _validate_repo_path(repo_path)
    except HTTPException as exc:
        return {
            "valid": False,
            "path": repo_path,
            "error": str(exc.detail),
            "status_code": exc.status_code,
        }
    return {"valid": True, "path": str(resolved), "error": None}


def _parsed_text(stdout: str, stderr: str, command: str, exit_code: Optional[int]) -> str:
    text = stdout.strip() or stderr.strip()
    if text:
        return text
    if exit_code is None:
        return f"stack-pr {command} produced no output"
    return f"stack-pr {command} exited with code {exit_code}"


def _command_result(
    *,
    argv: list[str],
    repo_path: Path,
    stdout: str,
    stderr: str,
    exit_code: Optional[int],
) -> dict[str, Any]:
    command = argv[-1]
    return {
        "ok": exit_code == 0,
        "argv": argv,
        "repo_path": str(repo_path),
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "parsed_text": _parsed_text(stdout, stderr, command, exit_code),
    }


def _run_stack_pr(repo_path: Path, command: str) -> dict[str, Any]:
    argv = ["stack-pr", command]
    try:
        completed = subprocess.run(
            argv,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            shell=False,
            timeout=_STACK_PR_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="stack-pr executable was not found on PATH",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        stdout = _timeout_output(exc.stdout)
        stderr = _timeout_output(exc.stderr) or f"stack-pr {command} timed out"
        return _command_result(
            argv=argv,
            repo_path=repo_path,
            stdout=stdout,
            stderr=stderr,
            exit_code=None,
        )

    return _command_result(
        argv=argv,
        repo_path=repo_path,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        exit_code=completed.returncode,
    )


def _timeout_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _run_validated_command(repo_path: str, command: str) -> dict[str, Any]:
    resolved = _validate_repo_path(repo_path)
    return _run_stack_pr(resolved, command)


@router.get("/status")
async def get_status(repo_path: Optional[str] = Query(default=None)):
    return {
        "tools": {
            "git": _tool_availability("git"),
            "gh": _tool_availability("gh"),
            "stack-pr": _tool_availability("stack-pr"),
        },
        "repo": _repo_status(repo_path),
    }


@router.post("/view")
async def post_view(body: RepoRequest):
    return _run_validated_command(body.repo_path, "view")


@router.post("/submit")
async def post_submit(body: ConfirmedRepoRequest):
    if body.confirm is not True:
        _raise_bad_request("confirm=true is required for stack-pr submit")
    return _run_validated_command(body.repo_path, "submit")


@router.post("/land")
async def post_land(body: ConfirmedRepoRequest):
    if body.confirm is not True:
        _raise_bad_request("confirm=true is required for stack-pr land")
    return _run_validated_command(body.repo_path, "land")


@router.post("/abandon")
async def post_abandon(body: AbandonRequest):
    if body.confirm_text != "abandon":
        _raise_bad_request('confirm_text="abandon" is required for stack-pr abandon')
    return _run_validated_command(body.repo_path, "abandon")
