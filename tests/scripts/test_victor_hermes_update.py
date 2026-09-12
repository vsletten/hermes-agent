"""The fork update entrypoint is permanently disabled."""

from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/victor-hermes-update.sh"


def test_updater_fails_closed_for_every_invocation():
    for args in ([], ["--dry-run"], ["--apply"], ["--apply", "--no-push"]):
        result = subprocess.run(
            ["bash", str(SCRIPT), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 1
        assert "upstream updates are disabled" in result.stderr
        assert result.stdout == ""
