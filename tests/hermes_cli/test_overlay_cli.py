import subprocess
import sys


def test_overlay_command_is_registered():
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "overlay", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "capture" in result.stdout
    assert "apply" in result.stdout
    assert "update" in result.stdout


def test_overlay_capture_help_exposes_repo_and_file_options():
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "overlay", "capture", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--repo" in result.stdout
    assert "--file" in result.stdout
    assert "--test" in result.stdout
