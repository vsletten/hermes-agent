import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import overlay


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    overlay.run(["git", "init"], cwd=repo, check=True)
    overlay.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    overlay.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    overlay.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    overlay.run(["git", "commit", "-m", "base"], cwd=repo, check=True)
    return repo


def test_capture_repo_overlay_writes_manifest_and_patch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(overlay, "get_hermes_home", lambda: home)
    repo = _init_repo(tmp_path)
    (repo / "tracked.txt").write_text("base\ncustom\n", encoding="utf-8")

    record = overlay.capture_overlay(
        "my-fix",
        repo=repo,
        reason="preserve local fix",
        tests=["pytest tests/example.py"],
    )

    overlay_dir = home / "local-overlays" / "my-fix"
    assert record.name == "my-fix"
    assert (overlay_dir / "repo.patch").read_text(encoding="utf-8")
    manifest = json.loads((overlay_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "my-fix"
    assert manifest["repo"] == str(repo)
    assert manifest["reason"] == "preserve local fix"
    assert manifest["tests"] == ["pytest tests/example.py"]
    assert manifest["repo_patch"] == "repo.patch"


def test_capture_repo_overlay_fails_when_no_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(overlay, "get_hermes_home", lambda: tmp_path / "home")
    repo = _init_repo(tmp_path)

    with pytest.raises(overlay.OverlayError, match="no repo diff"):
        overlay.capture_overlay("empty", repo=repo)


def test_apply_repo_overlay_uses_three_way_and_runs_tests(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(overlay, "get_hermes_home", lambda: home)
    repo = _init_repo(tmp_path)
    (repo / "tracked.txt").write_text("base\ncustom\n", encoding="utf-8")
    test_command = f"{sys.executable} -c 'print(1)'"
    overlay.capture_overlay("my-fix", repo=repo, tests=[test_command])
    overlay.run(["git", "checkout", "--", "tracked.txt"], cwd=repo, check=True)

    result = overlay.apply_overlay("my-fix")

    assert result.applied is True
    assert "custom" in (repo / "tracked.txt").read_text(encoding="utf-8")
    assert result.tests_run == [test_command]


def test_apply_repo_overlay_is_idempotent_when_patch_already_applied(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(overlay, "get_hermes_home", lambda: home)
    repo = _init_repo(tmp_path)
    (repo / "tracked.txt").write_text("base\ncustom\n", encoding="utf-8")
    overlay.capture_overlay("my-fix", repo=repo)
    overlay.run(["git", "checkout", "--", "tracked.txt"], cwd=repo, check=True)

    first = overlay.apply_overlay("my-fix")
    second = overlay.apply_overlay("my-fix")

    assert first.repo_applied is True
    assert second.applied is False
    assert second.repo_applied is False
    assert "custom" in (repo / "tracked.txt").read_text(encoding="utf-8")


def test_file_overlay_preserves_external_file_with_backup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(overlay, "get_hermes_home", lambda: home)
    target = tmp_path / "venv" / "sdk.py"
    target.parent.mkdir()
    target.write_text("patched sdk\n", encoding="utf-8")

    overlay.capture_overlay("sdk-hotfix", files=[target], reason="vendor hotfix")
    target.write_text("upstream sdk\n", encoding="utf-8")

    result = overlay.apply_overlay("sdk-hotfix")

    assert target.read_text(encoding="utf-8") == "patched sdk\n"
    assert result.files_restored == [target]
    backups = list(target.parent.glob("sdk.py.pre-overlay-*.bak"))
    assert backups
    assert backups[0].read_text(encoding="utf-8") == "upstream sdk\n"


def test_list_overlays_reads_manifests(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(overlay, "get_hermes_home", lambda: home)
    repo = _init_repo(tmp_path)
    (repo / "tracked.txt").write_text("base\ncustom\n", encoding="utf-8")
    overlay.capture_overlay("my-fix", repo=repo)

    records = overlay.list_overlays()

    assert [record.name for record in records] == ["my-fix"]
