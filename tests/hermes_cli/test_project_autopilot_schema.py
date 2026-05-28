from pathlib import Path

import pytest

from hermes_cli.project_autopilot import (
    PROJECT_MODE_V0,
    SUPPORTED_PROJECT_MODES,
    ProjectAutopilotError,
    branch_to_worktree_path,
    normalize_project_doc,
    validate_project_doc,
)


def test_v0_rejects_unsupported_project_modes(tmp_path):
    doc = normalize_project_doc(
        slug="demo",
        title="Demo",
        goal="Demo goal",
        board_slug="demo",
        root_task_id="t_root",
        project_home=tmp_path / "demo",
        repo_org="summation",
        repo_name="Code",
        canonical_checkout=Path("/Users/vsletten/src/summation/Code/main"),
        final_branch="feat/demo-pr",
    )
    doc["project_mode"] = "many-independent-prs"

    with pytest.raises(ProjectAutopilotError, match="unsupported project_mode"):
        validate_project_doc(doc)


def test_v0_schema_has_required_repo_and_policy_fields(tmp_path):
    doc = normalize_project_doc(
        slug="demo",
        title="Demo",
        goal="Demo goal",
        board_slug="demo",
        root_task_id="t_root",
        project_home=tmp_path / "demo",
        repo_org="summation",
        repo_name="Code",
        canonical_checkout=Path("/Users/vsletten/src/summation/Code/main"),
        final_branch="feat/demo-pr",
    )

    validate_project_doc(doc)

    assert doc["schema_version"] == "project-autopilot/v0"
    assert doc["project_mode"] == PROJECT_MODE_V0
    assert SUPPORTED_PROJECT_MODES == {PROJECT_MODE_V0}
    assert doc["repo"]["worktree_namespace"] == "/Users/vsletten/src/summation/Code"
    assert doc["branch_strategy"]["final_branch"] == "feat/demo-pr"
    assert "final_branch" not in doc
    assert doc["execution_policy"] == {"parallelism": "none", "max_active_workers": 1}
    assert doc["pr_requirement"]["required"] is True
    assert doc["cleanup"]["state"] == "not_started"
    assert isinstance(doc["transition_log"], list)


def test_branch_to_worktree_path_uses_victor_convention():
    assert branch_to_worktree_path(
        worktree_namespace=Path("/Users/vsletten/src/summation/Code"),
        branch_name="kanban/fix-this-shit",
    ) == Path("/Users/vsletten/src/summation/Code/kanban/fix-this-shit")
