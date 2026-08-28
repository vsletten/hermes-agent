# Upstream-ready patch: fire-claim retry storm (per-tick "Fire claim lost" rows)

Status: prepared for upstream contribution to NousResearch/hermes-agent, not yet
sent. The patch is the commit titled
`fix(cron): stop the per-tick fire-claim retry storm during long in-flight runs`
(branch `fix/cron-claim-retry` in the live install). It is self-contained and
general — no deployment-specific behavior.

## Suggested PR title

fix(cron): stop the per-tick fire-claim retry storm during long in-flight runs

## Suggested PR description

### Symptom

A recurring job (`every 30m`) fired at 15:14 and its execution legitimately ran
~2 hours. For the entire duration, the 60s scheduler ticker durably logged one
**failed** execution row per minute:

```
Fire claim lost; execution was not started.
```

122 rows in 2 hours, every one of them noise, all rendered by `hermes cron runs`
as failures of a perfectly healthy job.

### Root cause

The winning claimant heartbeats its durable `fire_claim` (~60s cadence), so a
healthy long run keeps the claim fresh for hours. But a **sibling scheduler
process** (two gateways sharing one HERMES_HOME, or gateway + desktop ticker)
had no way to see that the fire slot was consumed:

1. `_get_due_jobs_locked` had a cross-process claim guard for one-shots
   (`run_claim`, #59229) but **none for recurring jobs** — a recurring job
   whose `next_run_at` had lapsed was returned as due regardless of a live
   `fire_claim`.
2. Worse, the persisted stale-error recovery (`_job_is_stale_error_recurring`)
   only consults the *in-process* running set. With `last_status == "error"`
   from a previous fire and `last_run_at` older than one cadence (inevitable —
   the current run hadn't completed yet), the sibling's every tick concluded
   "wedged", re-armed `next_run_at` back to *now*, and dispatched.
3. Each doomed dispatch pre-created its execution-ledger row, lost the claim
   CAS in `claim_job_for_fire`, and was durably closed as a **failed** run —
   one alarming row per ticker minute.

### Fix (three parts, mirroring the three failure layers)

1. **Due scan: consume the fire slot.** `_get_due_jobs_locked` now skips a
   cron/interval job while a live `fire_claim` holds its slot — the recurring
   mirror of the existing one-shot `run_claim` guard. The claim TTL
   (`FIRE_CLAIM_TTL_SECONDS`, now a named constant used by every liveness
   judgment) remains the backstop: a claimant that dies stops heartbeating and
   the job becomes due and reclaimable again within 300s. A pending manual run
   is exempt, so "run now" semantics are unchanged (the claim CAS stays the
   arbiter).
2. **Stale-error recovery: live claim ⇒ slow, not wedged.**
   `_job_is_stale_error_recurring` returns False when a live `fire_claim`
   exists. The in-process check cannot see a run owned by a sibling process;
   the durable claim can. The recovery still re-arms genuinely wedged jobs
   (no claim, or claim lapsed) — covered by a negative-control test.
3. **Claim-CAS losers are not "failed runs".** The dispatch path that loses
   `claim_job_for_fire` now discards its never-started ledger row
   (`discard_execution`, deletes only rows still in `status='claimed'`) and
   logs at debug level, instead of durably closing the attempt as failed. The
   winner's own row documents the fire. Attempts that reached `running` are
   never discarded — their only exits remain `finish_execution` and dead-owner
   recovery. (A new `skipped` terminal status was considered and rejected: the
   executions table has a `CHECK(status IN (...))` constraint baked into
   existing deployments' SQLite files, so a new status value would require a
   table rebuild for zero operator value.)

Winner visibility (row exists as `claimed`/`running` from claim time, finalized
at completion, dead claimant leaves an evident non-terminal row) already exists
via the execution ledger + `recover_interrupted_executions`; the new tests pin
it as part of this incident's contract.

### Behavior contracts preserved

- Interval-from-completion drift (`mark_job_run` re-anchors) — untouched.
- Stale-claim TTL backstop — untouched (and now uniformly 300s via the
  constant).
- `hermes cron runs` — schema unchanged; old rows render as before.
- Manual "run now" against an in-flight job — unchanged (reaches the CAS).

### Tests

`tests/cron/test_fire_claim_retry_storm.py` (8 tests, E2E against a temp
HERMES_HOME, no mocks except one CAS-race `monkeypatch`):

- due scan skips a recurring job under a live claim; leaves `next_run_at`
  untouched
- lapsed claim (TTL backstop) restores due-ness
- pending manual run bypasses the guard
- stale-error recovery ignores live-claim jobs; still recovers claim-less
  wedged jobs (negative control)
- three full `tick()`s over a held fire slot leave **zero** durable rows
- a CAS-race loser's row is discarded, not failed
- `discard_execution` deletes only never-started (`claimed`) rows
- winner attempt visible `claimed` → `running` → `completed` on one row

Full cron suite: 1025 passed, 1 skipped.

## Deployment notes (local, not for the PR)

- Live install: /home/vsletten/.hermes/hermes-agent, applied on branch
  `fix/cron-claim-retry`.
- **Rollback ref (pre-patch): `5cc47c994beb243407bb4c8ba47d2ab421cda9cf`
  (branch `main`)** — rollback is
  `git checkout main` in the install + gateway restart.
- Gateway processes import at start; a restart is required for the patch (and
  was required anyway — the 2026-08-23 incident gateway, PIDs 3335/3336
  started 2026-08-22, was still running pre-08-27 code).
- **Verification note (2026-08-27):** after activation, ophir-queue-drain's
  21:06 fire naturally ran 1h54m (21:06:36→23:00:16) — a live replay of the
  incident scenario. Its fire claim was held and heartbeated the whole time
  while three of its 30m slots lapsed and both patched tickers (gateway
  2385926, desktop backend 2502575) kept ticking: **zero "Fire claim lost"
  rows, zero failed rows** in the window (vs. 122 in the 2026-08-23
  incident), the run durably visible as `running` throughout and finalized
  `completed` on the same row, and victor-gates-notify completed normally at
  21:33/22:03/22:33. A synthetic every-3m/250s-sleep job earlier showed the
  same: clean cycles once all tickers ran patched code, and the ledger's
  `unknown` dead-owner marking working when the old desktop backend was
  terminated mid-claim.
- Live-verified 2026-08-27: the sibling ticker on the main cron store turned
  out to be the **Hermes desktop app's backend** (`hermes_cli.main serve`,
  child of the Electron app) — profile gateways use separate per-profile
  stores. During verification the old-code desktop backend reproduced exactly
  one "Fire claim lost" failed row against a patched-gateway in-flight run
  (writer attributed via the ledger's `pid` column); after terminating it the
  desktop auto-respawned it onto the patched tree. Activation therefore
  means: restart `hermes-gateway.service` + `hermes-gateway-ivy.service`
  (systemd user units) AND kill the desktop `serve` child (auto-respawns).
