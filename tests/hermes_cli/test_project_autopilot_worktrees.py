import pytest

from hermes_cli.project_autopilot import (
    ProjectAutopilotError,
    validate_workspace_contract,
)


def test_workspace_contract_rejects_wrong_path():
    contract = {
        "task_id": "t_1",
        "workspace_path": "/tmp/demo",
        "branch_name": "kanban/fix-this-shit",
        "base_ref": "origin/main",
    }
    repo = {"worktree_namespace": "/Users/vsletten/src/summation/Code"}

    with pytest.raises(ProjectAutopilotError, match="branch-derived path"):
        validate_workspace_contract(contract, repo=repo)


def test_workspace_contract_accepts_branch_derived_path():
    contract = {
        "task_id": "t_1",
        "workspace_path": "/Users/vsletten/src/summation/Code/kanban/fix-this-shit",
        "branch_name": "kanban/fix-this-shit",
        "base_ref": "origin/main",
    }
    repo = {"worktree_namespace": "/Users/vsletten/src/summation/Code"}

    validate_workspace_contract(contract, repo=repo)


def test_workspace_contract_requires_metadata_fields():
    contract = {
        "task_id": "t_1",
        "workspace_path": "/Users/vsletten/src/summation/Code/kanban/fix-this-shit",
        "branch_name": "kanban/fix-this-shit",
    }
    repo = {"worktree_namespace": "/Users/vsletten/src/summation/Code"}

    with pytest.raises(ProjectAutopilotError, match="workspace contract missing base_ref"):
        validate_workspace_contract(contract, repo=repo)
