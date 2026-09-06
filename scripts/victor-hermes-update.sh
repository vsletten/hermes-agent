#!/usr/bin/env bash
# Fork-first updates preserve committed install customizations through merge history.
# Default is a read-only dry run. Failed and --no-push candidates remain reviewable.
# Successful updates keep the install on its current branch. Resolve conflicts
# in the retained candidate worktree, then validate again before publishing.
set -Eeuo pipefail

REPO="${HERMES_REPO:-$HOME/.hermes/hermes-agent}"
UPSTREAM_REMOTE="${HERMES_UPSTREAM_REMOTE:-upstream}"
ORIGIN_REMOTE="${HERMES_ORIGIN_REMOTE:-origin}"
BRANCH="${HERMES_BRANCH:-main}"
TMP_ROOT="${HERMES_UPDATE_WORKTREE_ROOT:-$HOME/.hermes/update-worktrees}"
# Override with HERMES_UPDATE_TEST_COMMAND='scripts/run_tests.sh' for the full suite.
DEFAULT_TEST_COMMAND='scripts/run_tests.sh tests/scripts/test_victor_hermes_update.py tests/agent/test_credential_pool_oauth_writethrough.py tests/agent/transports/test_codex_app_server_runtime.py tests/agent/transports/test_codex_app_server_session.py tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_overlay.py tests/hermes_cli/test_overlay_cli.py tests/hermes_cli/test_voice_wrapper.py tests/run_agent/test_codex_app_server_integration.py tests/tools/test_delegate_credentials.py tests/tools/test_voice_cli_integration.py tests/tools/test_voice_stop_phrase.py tests/cron/test_execution_ledger.py tests/cron/test_cron_workdir.py tests/cron/test_cronjob_schema.py tests/cron/test_inflight_stale_guard.py tests/tools/test_cronjob_run_background.py tests/tools/test_cronjob_list_execution_state.py tests/hermes_cli/test_setup_agent_settings.py tests/hermes_cli/test_kanban_review_lifecycle.py'
TEST_COMMAND="${HERMES_UPDATE_TEST_COMMAND:-$DEFAULT_TEST_COMMAND}"
APPLY=0
SKIP_TESTS=0
NO_PUSH=0
TMP_WORKTREE=""
TMP_BRANCH=""
SUCCESS=0

usage() {
  printf '%s\n' \
    'Usage: victor-hermes-update.sh [--dry-run | --apply] [--skip-tests] [--no-push]' \
    '  --dry-run     Inspect and print the plan; default, no writes.' \
    '  --apply       Merge, validate, publish, then update the current install branch.' \
    '  --skip-tests  Explicitly skip candidate and overlay verification.' \
    '  --no-push     Validate and retain the candidate; do not publish or install.' \
    'Environment: HERMES_REPO, HERMES_ORIGIN_REMOTE, HERMES_UPSTREAM_REMOTE,' \
    '  HERMES_BRANCH (fork/upstream target, default main),' \
    '  HERMES_UPDATE_WORKTREE_ROOT, HERMES_UPDATE_TEST_COMMAND.' \
    'The test command runs under bash in the candidate worktree before publication.' \
    'Enabled overlays are reapplied after updating; archive obsolete overlays first.'
}
log() { printf '→ %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run() { printf '+'; printf ' %q' "$@"; printf '\n'; "$@"; }
cleanup() {
  local status=$?
  if [[ -n "$TMP_WORKTREE" && -d "$TMP_WORKTREE" ]]; then
    if [[ "$SUCCESS" == 1 ]]; then
      # A dirty candidate is retained, never force-removed.
      git -C "$REPO" worktree remove "$TMP_WORKTREE" || true
    else
      printf 'Candidate retained: %s (branch %s)\n' "$TMP_WORKTREE" "$TMP_BRANCH" >&2
      printf 'Inspect git status there; after resolving a merge, use git merge --continue and rerun validation before publishing.\n' >&2
    fi
  fi
  return "$status"
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply|--yes|-y) APPLY=1 ;;
    --skip-tests|--no-tests) SKIP_TESTS=1 ;;
    --no-push) NO_PUSH=1 ;;
    --discard-failed-worktree) die 'Failed worktrees are now always preserved; remove a reviewed worktree manually.' ;;
    --help|-h) usage; exit 0 ;;
    *) usage; die "Unknown argument: $1" ;;
  esac
  shift
done

git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || die "Not a Git repository: $REPO"
git check-ref-format "refs/heads/$BRANCH" >/dev/null || die "Invalid target branch: $BRANCH"
current_branch="$(git -C "$REPO" symbolic-ref --quiet --short HEAD)" || die 'Install is detached; attach it explicitly before updating.'
install_head="$(git -C "$REPO" rev-parse HEAD)"
assert_install_unchanged() {
  [[ "$(git -C "$REPO" symbolic-ref --quiet --short HEAD)" == "$current_branch" ]] || die 'Install branch changed during validation.'
  [[ "$(git -C "$REPO" rev-parse HEAD)" == "$install_head" ]] || die 'Install HEAD changed during validation.'
  [[ -z "$(git -C "$REPO" status --porcelain)" ]] || die 'Install has uncommitted changes; commit them before updating.'
}
assert_install_unchanged
git -C "$REPO" remote get-url "$ORIGIN_REMOTE" >/dev/null || die "Missing remote: $ORIGIN_REMOTE"
git -C "$REPO" remote get-url "$UPSTREAM_REMOTE" >/dev/null || die "Missing remote: $UPSTREAM_REMOTE"
if [[ "$ORIGIN_REMOTE" != origin ]]; then
  [[ "$(git -C "$REPO" remote get-url origin)" == "$(git -C "$REPO" remote get-url "$ORIGIN_REMOTE")" ]] ||
    die 'Hermes updater fetches origin; configured publication remote must resolve to that same URL.'
fi
[[ -x "$REPO/venv/bin/python" ]] || die "Missing install Python: $REPO/venv/bin/python"

log "Install: $REPO ($current_branch @ $install_head)"
log "Merge $ORIGIN_REMOTE/$BRANCH and $UPSTREAM_REMOTE/$BRANCH into the committed install HEAD."
[[ "$current_branch" == "$BRANCH" ]] || log "Include $ORIGIN_REMOTE/$current_branch when that branch exists."
log "Validate with: $TEST_COMMAND"
if [[ "$NO_PUSH" == 1 ]]; then
  log 'Retain candidate after validation; no remote writes or installation.'
else
  log "Back up install HEAD remotely, then publish candidate atomically to $BRANCH and $current_branch."
  log "Run install Python: -m hermes_cli.main update --yes --branch $current_branch"
  log 'Verify installed HEAD, then apply enabled overlays (obsolete overlays must be archived).'
fi
[[ "$APPLY" == 1 ]] || { log 'Dry run complete; no changes made.'; exit 0; }

run git -C "$REPO" fetch "$ORIGIN_REMOTE" "refs/heads/$BRANCH:refs/remotes/$ORIGIN_REMOTE/$BRANCH"
run git -C "$REPO" fetch "$UPSTREAM_REMOTE" "refs/heads/$BRANCH:refs/remotes/$UPSTREAM_REMOTE/$BRANCH"
remote_current_exists=0
if [[ "$current_branch" != "$BRANCH" ]]; then
  if git -C "$REPO" ls-remote --exit-code --heads "$ORIGIN_REMOTE" "refs/heads/$current_branch" >/dev/null; then
    run git -C "$REPO" fetch "$ORIGIN_REMOTE" "refs/heads/$current_branch:refs/remotes/$ORIGIN_REMOTE/$current_branch"
    remote_current_exists=1
  else
    lookup_status=$?
    [[ "$lookup_status" == 2 ]] || die "Could not inspect remote install branch (git exit $lookup_status)."
  fi
fi
stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
backup_branch="codex/hermes-pre-update-$stamp"
run git -C "$REPO" update-ref "refs/heads/$backup_branch" "$install_head"
if [[ "$NO_PUSH" != 1 ]]; then
  run git -C "$REPO" push "$ORIGIN_REMOTE" "$install_head:refs/heads/$backup_branch"
fi

mkdir -p "$TMP_ROOT"
TMP_WORKTREE="$(mktemp -d "$TMP_ROOT/victor-hermes-update.XXXXXX")"
TMP_BRANCH="codex/hermes-update-$stamp"
run git -C "$REPO" worktree add -b "$TMP_BRANCH" "$TMP_WORKTREE" "$install_head"
run git -C "$TMP_WORKTREE" merge --no-edit "$ORIGIN_REMOTE/$BRANCH"
if [[ "$remote_current_exists" == 1 ]]; then
  run git -C "$TMP_WORKTREE" merge --no-edit "$ORIGIN_REMOTE/$current_branch"
fi
run git -C "$TMP_WORKTREE" merge --no-edit "$UPSTREAM_REMOTE/$BRANCH"
candidate="$(git -C "$TMP_WORKTREE" rev-parse HEAD)"
if [[ "$SKIP_TESTS" == 1 ]]; then
  log 'Candidate verification explicitly skipped.'
else
  (cd "$TMP_WORKTREE" && run bash -c "$TEST_COMMAND")
fi
[[ "$(git -C "$TMP_WORKTREE" rev-parse HEAD)" == "$candidate" ]] || die 'Tests changed candidate HEAD; revalidate the new commit.'
[[ -z "$(git -C "$TMP_WORKTREE" status --porcelain)" ]] || die 'Tests left candidate changes; inspect and commit before publishing.'
assert_install_unchanged

if [[ "$NO_PUSH" == 1 ]]; then
  log "Validated candidate $candidate retained; --no-push prevents publication and installation."
  exit 0
fi

push_refs=("$candidate:refs/heads/$BRANCH")
if [[ "$current_branch" != "$BRANCH" ]]; then
  push_refs+=("$candidate:refs/heads/$current_branch")
fi
# No force fallback: any non-fast-forward rejection leaves the install untouched.
run git -C "$TMP_WORKTREE" push --atomic "$ORIGIN_REMOTE" "${push_refs[@]}"
assert_install_unchanged
(cd "$REPO" && run "$REPO/venv/bin/python" -m hermes_cli.main update --yes --branch "$current_branch")
[[ "$(git -C "$REPO" symbolic-ref --quiet --short HEAD)" == "$current_branch" ]] || die 'Updater unexpectedly changed the install branch.'
[[ "$(git -C "$REPO" rev-parse HEAD)" == "$candidate" ]] || die 'Updater did not install the validated candidate.'
overlay_args=(overlay apply all)
[[ "$SKIP_TESTS" != 1 ]] || overlay_args+=(--no-tests)
(cd "$REPO" && run "$REPO/venv/bin/python" -m hermes_cli.main "${overlay_args[@]}")
SUCCESS=1
log "Installed validated candidate $candidate; backup: $ORIGIN_REMOTE/$backup_branch"
