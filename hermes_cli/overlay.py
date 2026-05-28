"""Local overlay manager for preserving Hermes customizations across updates.

An overlay is a small, explicit customization bundle stored outside the Hermes
source tree under ``$HERMES_HOME/local-overlays/<name>``. It can contain a git
patch for tracked repo changes plus full-file snapshots for vendor/venv files
that normal repo stash/update logic does not protect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import shutil
import subprocess
from typing import Any

from hermes_constants import get_hermes_home


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


class OverlayError(RuntimeError):
    """Raised when an overlay operation cannot be completed safely."""


@dataclass(frozen=True)
class OverlayRecord:
    name: str
    path: Path
    repo: Path | None = None
    reason: str | None = None
    tests: list[str] | None = None


@dataclass(frozen=True)
class ApplyResult:
    name: str
    applied: bool
    repo_applied: bool = False
    files_restored: list[Path] | None = None
    tests_run: list[str] | None = None


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = False, **kwargs: Any) -> subprocess.CompletedProcess:
    """Thin subprocess wrapper to keep tests monkeypatchable."""

    return subprocess.run(cmd, cwd=cwd, check=check, **kwargs)


def overlays_root() -> Path:
    return get_hermes_home() / "local-overlays"


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise OverlayError(
            "invalid overlay name; use letters, numbers, dot, underscore, or dash"
        )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest(overlay_dir: Path) -> dict[str, Any]:
    manifest_path = overlay_dir / "manifest.json"
    if not manifest_path.exists():
        raise OverlayError(f"missing overlay manifest: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _record_from_manifest(overlay_dir: Path, manifest: dict[str, Any]) -> OverlayRecord:
    repo = Path(manifest["repo"]) if manifest.get("repo") else None
    return OverlayRecord(
        name=manifest["name"],
        path=overlay_dir,
        repo=repo,
        reason=manifest.get("reason"),
        tests=list(manifest.get("tests") or []),
    )


def list_overlays() -> list[OverlayRecord]:
    root = overlays_root()
    if not root.exists():
        return []
    records: list[OverlayRecord] = []
    for overlay_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        manifest_path = overlay_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        records.append(_record_from_manifest(overlay_dir, _load_manifest(overlay_dir)))
    return records


def get_overlay(name: str) -> OverlayRecord:
    _validate_name(name)
    overlay_dir = overlays_root() / name
    manifest = _load_manifest(overlay_dir)
    return _record_from_manifest(overlay_dir, manifest)


def _capture_repo_patch(repo: Path) -> str:
    if not repo.exists():
        raise OverlayError(f"repo does not exist: {repo}")
    result = run(
        ["git", "diff", "HEAD", "--binary", "--no-ext-diff"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def capture_overlay(
    name: str,
    *,
    repo: Path | str | None = None,
    files: list[Path | str] | None = None,
    reason: str | None = None,
    tests: list[str] | None = None,
    overwrite: bool = False,
) -> OverlayRecord:
    """Capture a repo diff and/or external files as a named overlay."""

    _validate_name(name)
    root = overlays_root()
    overlay_dir = root / name
    if overlay_dir.exists():
        if not overwrite:
            raise OverlayError(f"overlay already exists: {name}")
        shutil.rmtree(overlay_dir)
    overlay_dir.mkdir(parents=True, exist_ok=True)

    repo_path = Path(repo).expanduser().resolve() if repo is not None else None
    file_paths = [Path(p).expanduser().resolve() for p in (files or [])]
    repo_patch_name: str | None = None
    captured_files: list[dict[str, Any]] = []

    if repo_path is not None:
        patch_text = _capture_repo_patch(repo_path)
        if not patch_text.strip() and not file_paths:
            shutil.rmtree(overlay_dir)
            raise OverlayError("no repo diff to capture")
        if patch_text.strip():
            repo_patch_name = "repo.patch"
            (overlay_dir / repo_patch_name).write_text(patch_text, encoding="utf-8")

    files_dir = overlay_dir / "files"
    for source in file_paths:
        if not source.is_file():
            raise OverlayError(f"file does not exist: {source}")
        files_dir.mkdir(exist_ok=True)
        safe_name = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
        dest_name = f"{safe_name}-{source.name}"
        shutil.copy2(source, files_dir / dest_name)
        captured_files.append(
            {
                "path": str(source),
                "snapshot": f"files/{dest_name}",
                "sha256": _sha256(source),
            }
        )

    if repo_path is None and not captured_files:
        shutil.rmtree(overlay_dir)
        raise OverlayError("nothing to capture")

    manifest = {
        "schema_version": "hermes-overlay/v0",
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(repo_path) if repo_path is not None else None,
        "reason": reason,
        "repo_patch": repo_patch_name,
        "files": captured_files,
        "tests": list(tests or []),
    }
    (overlay_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return _record_from_manifest(overlay_dir, manifest)


def _apply_repo_patch(repo: Path, patch_path: Path) -> bool:
    if not patch_path.exists() or not patch_path.read_text(encoding="utf-8").strip():
        return False
    already_applied = run(
        ["git", "apply", "--reverse", "--check", str(patch_path)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if already_applied.returncode == 0:
        return False
    result = run(
        ["git", "apply", "--3way", str(patch_path)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "git apply failed").strip()
        raise OverlayError(f"failed to apply repo patch: {details}")
    return True


def _restore_file(snapshot: Path, target: Path) -> Path:
    if not snapshot.is_file():
        raise OverlayError(f"missing file snapshot: {snapshot}")
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.pre-overlay-{stamp}.bak")
        shutil.copy2(target, backup)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snapshot, target)
    return target


def apply_overlay(name: str, *, run_tests: bool = True) -> ApplyResult:
    """Apply a named overlay and run its verification commands."""

    record = get_overlay(name)
    manifest = _load_manifest(record.path)
    repo_applied = False
    if manifest.get("repo") and manifest.get("repo_patch"):
        repo_applied = _apply_repo_patch(
            Path(manifest["repo"]), record.path / manifest["repo_patch"]
        )

    restored: list[Path] = []
    for file_entry in manifest.get("files") or []:
        restored.append(
            _restore_file(record.path / file_entry["snapshot"], Path(file_entry["path"]))
        )

    tests_run: list[str] = []
    if run_tests:
        for command in manifest.get("tests") or []:
            result = subprocess.run(
                command,
                cwd=Path(manifest["repo"]) if manifest.get("repo") else None,
                shell=True,
                text=True,
            )
            tests_run.append(command)
            if result.returncode != 0:
                raise OverlayError(f"overlay test failed: {command}")

    return ApplyResult(
        name=name,
        applied=repo_applied or bool(restored),
        repo_applied=repo_applied,
        files_restored=restored,
        tests_run=tests_run,
    )


def apply_all_overlays(*, run_tests: bool = True) -> list[ApplyResult]:
    return [apply_overlay(record.name, run_tests=run_tests) for record in list_overlays()]
