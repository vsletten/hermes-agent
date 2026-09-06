"""Local-only updater contract tests: real Git histories, fake Hermes installer."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/victor-hermes-update.sh"


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-update-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        self.upstream = self.root / "upstream.git"
        self.repo = self.root / "install"
        self.run_cmd("git", "init", "--bare", str(self.origin))
        self.run_cmd("git", "init", "--bare", str(self.upstream))
        self.run_cmd("git", "init", "-b", "main", str(self.repo))
        self.git("config", "user.name", "Updater Test")
        self.git("config", "user.email", "updater@example.invalid")
        (self.repo / ".gitignore").write_text("venv/\n")
        self.commit("base", "base")
        self.git("remote", "add", "origin", str(self.origin))
        self.git("remote", "add", "upstream", str(self.upstream))
        self.git("push", "origin", "main")
        self.git("push", "upstream", "main")
        self.git("switch", "-c", "wip/local-customizations")
        self.commit("custom", "local customization")
        self.original = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "main")
        self.commit("upstream", "upstream feature")
        self.git("push", "upstream", "main")
        self.upstream_head = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "wip/local-customizations")
        self.logfile = self.root / "installer.log"
        python = self.repo / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(
            "#!/bin/bash\nset -e\n"
            'printf "%s\\n" "$*" >> "$TEST_INSTALL_LOG"\n'
            'if [[ "$3" == update ]]; then\n'
            '  git fetch origin "$6"\n'
            '  git merge --ff-only "origin/$6"\n'
            'fi\n'
        )
        python.chmod(0o755)
        self.env = dict(os.environ, HERMES_REPO=str(self.repo),
                        HERMES_UPDATE_WORKTREE_ROOT=str(self.root / "candidates"),
                        HERMES_UPDATE_TEST_COMMAND="git merge-base --is-ancestor " + self.original + " HEAD",
                        TEST_INSTALL_LOG=str(self.logfile))

    def run_cmd(self, *args, check=True, **kwargs):
        return subprocess.run(args, text=True, capture_output=True, check=check, **kwargs)

    def git(self, *args):
        return self.run_cmd("git", "-C", str(self.repo), *args).stdout

    def commit(self, filename, content):
        (self.repo / filename).write_text(content)
        self.git("add", ".")
        self.git("commit", "-m", content)

    def update(self, *args):
        return self.run_cmd("bash", str(SCRIPT), *args, env=self.env, check=False)

    def test_dry_run_has_no_remote_or_install_writes(self):
        before = self.git("show-ref")
        result = self.update("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("show-ref"), before)
        self.assertFalse(self.logfile.exists())

    def test_current_commits_survive_and_branch_does_not_switch(self):
        result = self.update("--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        candidate = self.git("rev-parse", "HEAD").strip()
        self.git("merge-base", "--is-ancestor", self.original, candidate)
        self.git("merge-base", "--is-ancestor", self.upstream_head, candidate)
        self.assertEqual(self.git("branch", "--show-current").strip(), "wip/local-customizations")
        for branch in ("main", "wip/local-customizations"):
            self.assertIn(candidate, self.git("ls-remote", "origin", "refs/heads/" + branch))
        self.assertIn(self.original, self.git("ls-remote", "origin", "refs/heads/codex/hermes-pre-update-*"))
        self.assertIn("update --yes --branch wip/local-customizations", self.logfile.read_text())
        self.assertIn("overlay apply all", self.logfile.read_text())

    def test_no_push_validates_but_does_not_publish_or_install(self):
        before = self.git("ls-remote", "origin")
        result = self.update("--apply", "--no-push")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("ls-remote", "origin"), before)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.original)
        self.assertFalse(self.logfile.exists())
        self.assertEqual(len(list((self.root / "candidates").iterdir())), 1)

    def test_narrow_origin_fetch_refspec_updates_current_tracking_ref(self):
        self.git("config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
        self.git("push", "origin", "HEAD:refs/heads/wip/local-customizations")
        self.git("update-ref", "refs/remotes/origin/wip/local-customizations", self.original)
        result = self.update("--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        candidate = self.git("rev-parse", "HEAD").strip()
        self.assertNotEqual(candidate, self.original)
        self.assertEqual(self.git("rev-parse", "origin/wip/local-customizations").strip(), candidate)
        self.git("merge-base", "--is-ancestor", self.original, candidate)
        self.git("merge-base", "--is-ancestor", self.upstream_head, candidate)
        self.assertIn(candidate, self.git("ls-remote", "origin", "refs/heads/wip/local-customizations"))

    def test_failed_validation_never_publishes_candidate_or_installs(self):
        before = self.git("ls-remote", "origin", "refs/heads/main")
        self.env["HERMES_UPDATE_TEST_COMMAND"] = "exit 42"
        result = self.update("--apply")
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/main"), before)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.original)
        self.assertFalse(self.logfile.exists())
        self.assertEqual(len(list((self.root / "candidates").iterdir())), 1)

    def test_existing_remote_current_branch_commits_are_preserved(self):
        self.git("switch", "-c", "remote-only")
        self.commit("remote", "another worker's change")
        self.git("push", "origin", "HEAD:refs/heads/wip/local-customizations")
        remote_head = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "wip/local-customizations")
        # Both sides have unique commits, so preserving only local HEAD is insufficient.
        self.commit("second-local", "another local customization")
        local_head = self.git("rev-parse", "HEAD").strip()
        result = self.update("--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for ancestor in (remote_head, local_head, self.upstream_head):
            self.git("merge-base", "--is-ancestor", ancestor, "HEAD")
        candidate = self.git("rev-parse", "HEAD").strip()
        for branch in ("main", "wip/local-customizations"):
            self.assertIn(candidate, self.git("ls-remote", "origin", "refs/heads/" + branch))

    def test_atomic_rejection_preserves_both_remote_branches_and_install(self):
        # A writer advances the remote only after this updater captured its inputs.
        self.git("switch", "-c", "remote-only")
        self.commit("remote", "concurrent writer's change")
        remote_head = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "wip/local-customizations")
        self.git("push", "origin", "HEAD:refs/heads/wip/local-customizations")
        main_before = self.git("ls-remote", "origin", "refs/heads/main")
        self.env["HERMES_UPDATE_TEST_COMMAND"] = (
            "git push origin " + remote_head + ":refs/heads/wip/local-customizations"
        )
        result = self.update("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/main"), main_before)
        self.assertIn(remote_head, self.git("ls-remote", "origin", "refs/heads/wip/local-customizations"))
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.original)
        self.assertFalse(self.logfile.exists())

    def test_remote_current_lookup_failure_is_not_treated_as_missing(self):
        before = self.git("ls-remote", "origin")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        git_proxy = bin_dir / "git"
        git_proxy.write_text(
            '#!/bin/bash\nif [[ " $* " == *" ls-remote --exit-code "* ]]; then exit 128; fi\n'
            'exec ' + shlex.quote(shutil.which("git")) + ' "$@"\n'
        )
        git_proxy.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]
        result = self.update("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not inspect remote install branch", result.stderr)
        self.assertEqual(self.git("ls-remote", "origin"), before)
        self.assertFalse(self.logfile.exists())

    def test_dirty_install_refuses_before_any_remote_writes(self):
        before = self.git("ls-remote", "origin")
        (self.repo / "custom").write_text("uncommitted")
        result = self.update("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("ls-remote", "origin"), before)
        self.assertFalse(self.logfile.exists())

    def test_merge_conflict_is_retained_without_installing(self):
        self.git("switch", "main")
        self.commit("custom", "conflicting upstream customization")
        self.git("push", "upstream", "main")
        self.git("switch", "wip/local-customizations")
        before = self.git("ls-remote", "origin")
        result = self.update("--apply", "--no-push")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("ls-remote", "origin"), before)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.original)
        candidates = list((self.root / "candidates").iterdir())
        self.assertEqual(len(candidates), 1)
        unmerged = self.run_cmd("git", "-C", str(candidates[0]), "ls-files", "--unmerged").stdout
        self.assertIn("custom", unmerged)
        self.assertFalse(self.logfile.exists())


if __name__ == "__main__":
    unittest.main()
