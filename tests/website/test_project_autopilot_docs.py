from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_AUTOPILOT_DOC = (
    REPO_ROOT / "website" / "docs" / "user-guide" / "features" / "project-autopilot.md"
)
CLI_COMMANDS_DOC = REPO_ROOT / "website" / "docs" / "reference" / "cli-commands.md"
SIDEBARS = REPO_ROOT / "website" / "sidebars.ts"


def test_project_autopilot_feature_doc_covers_v0_workflow():
    body = PROJECT_AUTOPILOT_DOC.read_text(encoding="utf-8")

    for command in (
        "hermes project init",
        "hermes project verify <project_home>",
        "hermes project sync <project_home>",
        "hermes project cleanup-inventory <project_home>",
    ):
        assert command in body

    for required_phrase in (
        "stacked-slices-one-pr",
        "Kanban remains the execution truth",
        "/Users/vsletten/src/<org>/<repo>/<branch-name-as-path>",
        "draft or open PR",
        "No cleanup command deletes files",
    ):
        assert required_phrase in body


def test_project_autopilot_doc_is_discoverable_from_docs_nav_and_cli_reference():
    cli_reference = CLI_COMMANDS_DOC.read_text(encoding="utf-8")
    sidebar = SIDEBARS.read_text(encoding="utf-8")

    assert "| `hermes project` |" in cli_reference
    assert "## `hermes project`" in cli_reference
    assert "user-guide/features/project-autopilot" in sidebar
