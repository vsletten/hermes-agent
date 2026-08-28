"""Regression tests for the 2026-08-23 fire-claim retry storm.

Incident shape: a 30m-interval job's fire ran ~2h. The winning claimant
heartbeated its durable ``fire_claim`` the whole time, but a sibling
scheduler process could not see that the fire slot was consumed:

* the due scan had no recurring-job fire-claim guard, and
* the persisted stale-error recovery (``_job_is_stale_error_recurring``)
  only checks the *in-process* running set, so it re-armed ``next_run_at``
  to now on every tick,

so every ticker minute dispatched a doomed claim attempt whose loss was
durably closed as a failed run — 122 "Fire claim lost" rows in 2h.

The fixes under test:
1. the due scan skips a recurring job while a live ``fire_claim`` holds
   its fire slot (TTL-bounded; manual runs exempt);
2. the stale-error recovery treats a live ``fire_claim`` as "slow, not
   wedged";
3. an attempt that loses the claim CAS is discarded from the execution
   ledger (debug log only), never durably logged as a failed run;
4. (already-existing behavior, pinned here per the incident review) the
   claim winner's attempt is durably visible as ``claimed``/``running``
   from claim time and finalized on completion.

E2E-over-mocks: real stores against a temp HERMES_HOME.
"""

from datetime import timedelta

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so jobs.json / executions.db don't touch the
    real store."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def _make_recurring_job(name="storm", schedule="every 30m"):
    from cron.jobs import create_job

    return create_job(prompt="x", schedule=schedule, name=name)


def _mutate_job(job_id, **fields):
    """Directly edit persisted job fields (simulating another process's
    stamps: fire_claim, next_run_at, last_status, ...)."""
    from cron.jobs import load_jobs, save_jobs

    jobs = load_jobs()
    for job in jobs:
        if job["id"] == job_id:
            job.update(fields)
    save_jobs(jobs)


def _fresh_foreign_claim(now, age_seconds=30):
    return {
        "at": (now - timedelta(seconds=age_seconds)).isoformat(),
        "by": "other-host:9999:deadbeef",
    }


def test_due_scan_skips_recurring_job_with_live_fire_claim(temp_home):
    """(fix 1 / regression a) A due recurring job whose fire slot is held
    by a live foreign claim is NOT returned as due — the ticker does not
    re-attempt the fire while the winner's run is in flight."""
    from cron.jobs import _hermes_now, get_due_jobs, get_job

    now = _hermes_now()
    job = _make_recurring_job()
    past = (now - timedelta(seconds=90)).isoformat()
    _mutate_job(
        job["id"],
        next_run_at=past,
        fire_claim=_fresh_foreign_claim(now),
    )

    due_ids = {j["id"] for j in get_due_jobs()}
    assert job["id"] not in due_ids
    # And the scan left the job's schedule state alone (no re-arm churn).
    assert get_job(job["id"])["next_run_at"] == past


def test_stale_fire_claim_does_not_block_due(temp_home):
    """TTL backstop preserved: a claim past FIRE_CLAIM_TTL_SECONDS (dead
    claimant stopped heartbeating) no longer consumes the fire slot."""
    from cron.jobs import (
        FIRE_CLAIM_TTL_SECONDS,
        _hermes_now,
        get_due_jobs,
    )

    now = _hermes_now()
    job = _make_recurring_job(name="stale-claim")
    _mutate_job(
        job["id"],
        next_run_at=(now - timedelta(seconds=90)).isoformat(),
        fire_claim=_fresh_foreign_claim(
            now, age_seconds=FIRE_CLAIM_TTL_SECONDS + 100
        ),
    )

    due_ids = {j["id"] for j in get_due_jobs()}
    assert job["id"] in due_ids


def test_manual_run_bypasses_fire_claim_guard(temp_home):
    """A pending manual run ("run now") still reaches dispatch; the claim
    CAS in claim_job_for_fire stays the arbiter for it."""
    from cron.jobs import _hermes_now, get_due_jobs

    now = _hermes_now()
    job = _make_recurring_job(name="manual")
    manual_at = (now - timedelta(seconds=30)).isoformat()
    _mutate_job(
        job["id"],
        next_run_at=manual_at,
        manual_run_at=manual_at,
        fire_claim=_fresh_foreign_claim(now),
    )

    due_ids = {j["id"] for j in get_due_jobs()}
    assert job["id"] in due_ids


def test_stale_error_recovery_treats_live_claim_as_running(temp_home):
    """(fix 2) The persisted stale-error recovery must not re-arm a job
    whose fire slot is held by a live (heartbeated) claim — that job is
    slow, not wedged. This was the per-minute re-arm engine of the
    incident."""
    from cron.jobs import (
        _hermes_now,
        _job_is_stale_error_recurring,
        get_due_jobs,
        get_job,
    )

    now = _hermes_now()
    job = _make_recurring_job(name="err-inflight")
    future = (now + timedelta(minutes=20)).isoformat()
    wedged_fields = dict(
        next_run_at=future,
        last_status="error",
        last_run_at=(now - timedelta(hours=3)).isoformat(),
    )

    # With a live claim: helper says "not wedged", scan leaves it alone.
    _mutate_job(job["id"], fire_claim=_fresh_foreign_claim(now), **wedged_fields)
    claimed_job = get_job(job["id"])
    assert not _job_is_stale_error_recurring(
        claimed_job, claimed_job["schedule"], now
    )
    assert job["id"] not in {j["id"] for j in get_due_jobs()}
    assert get_job(job["id"])["next_run_at"] == future

    # Negative control — same wedged state with NO claim must still be
    # recovered (the 2026-08-14 incident fix keeps working).
    _mutate_job(job["id"], fire_claim=None, **wedged_fields)
    wedged_job = get_job(job["id"])
    assert _job_is_stale_error_recurring(wedged_job, wedged_job["schedule"], now)
    assert job["id"] in {j["id"] for j in get_due_jobs()}


def test_tick_with_held_fire_slot_writes_no_durable_rows(temp_home):
    """(regression a, end-to-end) A full scheduler tick over a due job
    whose fire slot is held by a live foreign claim starts nothing and
    leaves ZERO durable execution rows — the incident wrote one failed
    row per tick."""
    from cron.executions import list_executions
    from cron.jobs import _hermes_now
    from cron.scheduler import tick

    now = _hermes_now()
    job = _make_recurring_job(name="tick-held")
    _mutate_job(
        job["id"],
        next_run_at=(now - timedelta(seconds=90)).isoformat(),
        fire_claim=_fresh_foreign_claim(now),
    )

    for _ in range(3):  # three "ticker minutes"
        tick(verbose=False)

    assert list_executions(job_id=job["id"]) == []


def test_claim_cas_loss_discards_attempt_instead_of_failing_it(
    temp_home, monkeypatch
):
    """(fix 3) When an attempt genuinely races and loses the claim CAS,
    its pre-dispatch ledger row is discarded — not closed as a failed
    run."""
    from cron.executions import list_executions
    from cron.jobs import _hermes_now
    from cron.scheduler import tick

    now = _hermes_now()
    job = _make_recurring_job(name="cas-race")
    _mutate_job(job["id"], next_run_at=(now - timedelta(seconds=30)).isoformat())

    # Simulate "another process claimed between our due scan and our CAS".
    monkeypatch.setattr(
        "cron.scheduler.claim_job_for_fire", lambda *a, **k: False
    )
    tick(verbose=False)

    assert list_executions(job_id=job["id"]) == []


def test_discard_execution_only_removes_never_started_attempts(temp_home):
    """discard_execution deletes a 'claimed' row exactly once and never
    touches an attempt that reached 'running'."""
    from cron.executions import (
        create_execution,
        discard_execution,
        list_executions,
        mark_execution_running,
    )

    never_started = create_execution("job-a", source="builtin")
    assert discard_execution(never_started["id"]) is True
    assert discard_execution(never_started["id"]) is False
    assert list_executions(job_id="job-a") == []

    started = create_execution("job-b", source="builtin")
    mark_execution_running(started["id"])
    assert discard_execution(started["id"]) is False
    [row] = list_executions(job_id="job-b")
    assert row["status"] == "running"


def test_winner_attempt_is_visible_from_claim_through_completion(temp_home):
    """(regressions b + c) The claim winner's attempt is durably visible
    immediately after claim ('claimed', then 'running'), and completion
    finalizes the same row — a claimant that dies mid-run leaves an
    evident non-terminal row instead of vanishing."""
    from cron.executions import (
        create_execution,
        finish_execution,
        list_executions,
        mark_execution_running,
    )

    execution = create_execution("winner-job", source="builtin")
    [row] = list_executions(job_id="winner-job")
    assert row["status"] == "claimed"

    assert mark_execution_running(execution["id"])["status"] == "running"
    [row] = list_executions(job_id="winner-job")
    assert row["status"] == "running"
    assert row["started_at"]

    finished = finish_execution(execution["id"], success=True)
    assert finished["status"] == "completed"
    [row] = list_executions(job_id="winner-job")
    assert row["status"] == "completed"
    assert row["finished_at"]
