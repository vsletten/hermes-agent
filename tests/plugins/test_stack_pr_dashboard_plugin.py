"""Tests for the Stack PR dashboard plugin backend.

The plugin mounts as /api/plugins/stack-pr/ in Hermes dashboard. These tests
attach its router to a bare FastAPI app and mock all local CLI interaction so
they do not require stack-pr to be installed.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "stack-pr" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        f"hermes_dashboard_plugin_stack_pr_test_{id(plugin_file)}",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def plugin_api():
    return _load_plugin_module()


@pytest.fixture
def client(plugin_api):
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/stack-pr")
    return TestClient(app)


def _completed(argv: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def test_plugin_api_imports_router(plugin_api):
    assert isinstance(plugin_api.router, APIRouter)


def test_stack_pr_dashboard_manifest_and_bundle_contract():
    repo_root = Path(__file__).resolve().parents[2]
    dashboard_dir = repo_root / "plugins" / "stack-pr" / "dashboard"
    manifest = json.loads((dashboard_dir / "manifest.json").read_text())
    bundle = (dashboard_dir / manifest["entry"]).read_text()

    assert manifest["name"] == "stack-pr"
    assert manifest["label"] == "Stack PR"
    assert manifest["entry"] == "dist/index.js"
    assert manifest["tab"]["path"] == "/stack-pr"
    assert manifest["tab"].get("hidden") is not True
    assert manifest.get("css") == "dist/style.css"

    assert "window.__HERMES_PLUGIN_SDK__" in bundle
    assert 'window.__HERMES_PLUGINS__.register("stack-pr"' in bundle
    for endpoint in (
        "/api/plugins/stack-pr/status",
        "/api/plugins/stack-pr/view",
        "/api/plugins/stack-pr/submit",
        "/api/plugins/stack-pr/land",
        "/api/plugins/stack-pr/abandon",
    ):
        assert endpoint in bundle

    assert "SDK.fetchJSON" in bundle
    assert "Confirm stack-pr submit" in bundle
    assert "Confirm stack-pr land" in bundle
    assert "Type abandon to confirm" in bundle
    assert "confirm: true" in bundle
    assert 'confirm_text: "abandon"' in bundle
    assert "fetch(" not in bundle
    assert "XMLHttpRequest" not in bundle
    assert "textarea" not in bundle
    assert "spr" not in bundle
    assert "Arbitrary command" not in bundle
    assert "Switch branch" not in bundle


def test_status_reports_tool_availability_and_repo_validation(client, plugin_api, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    paths = {"git": "/usr/bin/git", "gh": "/usr/bin/gh", "stack-pr": None}
    monkeypatch.setattr(plugin_api.shutil, "which", lambda name: paths[name])

    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs["shell"] is False
        assert argv == ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"]
        return _completed(argv, stdout="true\n")

    monkeypatch.setattr(plugin_api.subprocess, "run", fake_run)

    response = client.get("/api/plugins/stack-pr/status", params={"repo_path": str(repo)})

    assert response.status_code == 200
    data = response.json()
    assert data["tools"]["git"] == {"available": True, "path": "/usr/bin/git"}
    assert data["tools"]["gh"] == {"available": True, "path": "/usr/bin/gh"}
    assert data["tools"]["stack-pr"] == {"available": False, "path": None}
    assert data["repo"]["valid"] is True
    assert data["repo"]["path"] == str(repo)
    assert calls


def test_status_reports_invalid_repo_without_500(client, plugin_api, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_api.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        plugin_api.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("missing repo should not invoke subprocess"),
    )

    response = client.get(
        "/api/plugins/stack-pr/status",
        params={"repo_path": str(tmp_path / "missing")},
    )

    assert response.status_code == 200
    assert response.json()["repo"]["valid"] is False


def test_view_validates_repo_and_runs_fixed_argv_without_shell(client, plugin_api, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs["shell"] is False
        if argv == ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"]:
            return _completed(argv, stdout="true\n")
        if argv == ["stack-pr", "view"]:
            assert kwargs["cwd"] == str(repo)
            return _completed(argv, stdout="stack contents\n")
        raise AssertionError(f"unexpected argv: {argv!r}")

    monkeypatch.setattr(plugin_api.subprocess, "run", fake_run)

    response = client.post("/api/plugins/stack-pr/view", json={"repo_path": str(repo)})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["argv"] == ["stack-pr", "view"]
    assert data["stdout"] == "stack contents\n"
    assert data["stderr"] == ""
    assert data["exit_code"] == 0
    assert data["parsed_text"] == "stack contents"
    assert [call[0] for call in calls] == [
        ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
        ["stack-pr", "view"],
    ]


def test_view_returns_stack_pr_failure_as_structured_result(client, plugin_api, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(argv, **kwargs):
        assert kwargs["shell"] is False
        if argv == ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"]:
            return _completed(argv, stdout="true\n")
        if argv == ["stack-pr", "view"]:
            return _completed(argv, returncode=7, stderr="branch precondition failed\n")
        raise AssertionError(f"unexpected argv: {argv!r}")

    monkeypatch.setattr(plugin_api.subprocess, "run", fake_run)

    response = client.post("/api/plugins/stack-pr/view", json={"repo_path": str(repo)})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is False
    assert data["exit_code"] == 7
    assert data["stderr"] == "branch precondition failed\n"
    assert data["parsed_text"] == "branch precondition failed"


@pytest.mark.parametrize(
    ("endpoint", "payload", "detail"),
    [
        ("/submit", {"repo_path": "/tmp/repo"}, "confirm=true is required"),
        ("/land", {"repo_path": "/tmp/repo"}, "confirm=true is required"),
        ("/abandon", {"repo_path": "/tmp/repo"}, 'confirm_text="abandon" is required'),
    ],
)
def test_mutating_routes_require_confirmation_before_subprocess(
    client,
    plugin_api,
    monkeypatch,
    endpoint,
    payload,
    detail,
):
    monkeypatch.setattr(
        plugin_api.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("confirmation gate should run before subprocess"),
    )

    response = client.post(f"/api/plugins/stack-pr{endpoint}", json=payload)

    assert response.status_code == 400
    assert detail in response.json()["detail"]


@pytest.mark.parametrize(
    ("endpoint", "payload", "argv"),
    [
        ("/submit", {"confirm": True}, ["stack-pr", "submit"]),
        ("/land", {"confirm": True}, ["stack-pr", "land"]),
        ("/abandon", {"confirm_text": "abandon"}, ["stack-pr", "abandon"]),
    ],
)
def test_mutating_routes_run_only_fixed_stack_pr_argv(
    client,
    plugin_api,
    tmp_path,
    monkeypatch,
    endpoint,
    payload,
    argv,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(actual_argv, **kwargs):
        calls.append((actual_argv, kwargs))
        assert kwargs["shell"] is False
        if actual_argv == ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"]:
            return _completed(actual_argv, stdout="true\n")
        if actual_argv == argv:
            assert kwargs["cwd"] == str(repo)
            return _completed(actual_argv, stdout="ok\n")
        raise AssertionError(f"unexpected argv: {actual_argv!r}")

    monkeypatch.setattr(plugin_api.subprocess, "run", fake_run)

    response = client.post(
        f"/api/plugins/stack-pr{endpoint}",
        json={"repo_path": str(repo), **payload},
    )

    assert response.status_code == 200
    assert response.json()["argv"] == argv
    assert [call[0] for call in calls] == [
        ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
        argv,
    ]


@pytest.mark.parametrize("repo_path", ["", "relative/repo", "/"])
def test_repo_path_validation_rejects_unsafe_paths_before_subprocess(
    client,
    plugin_api,
    monkeypatch,
    repo_path,
):
    monkeypatch.setattr(
        plugin_api.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("unsafe repo path should not invoke subprocess"),
    )

    response = client.post("/api/plugins/stack-pr/view", json={"repo_path": repo_path})

    assert response.status_code == 400


def test_repo_path_validation_rejects_non_git_directory_before_stack_pr(
    client,
    plugin_api,
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(argv, **kwargs):
        assert kwargs["shell"] is False
        assert argv == ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"]
        return _completed(argv, returncode=128, stderr="not a git repository\n")

    monkeypatch.setattr(plugin_api.subprocess, "run", fake_run)

    response = client.post("/api/plugins/stack-pr/view", json={"repo_path": str(repo)})

    assert response.status_code == 400
    assert "not a git repository" in response.json()["detail"]
