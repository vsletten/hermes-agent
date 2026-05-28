---
sidebar_position: 16
title: "Project Autopilot"
description: "Filesystem-backed project lifecycle automation for Kanban work"
---

# Project Autopilot

Project Autopilot V0 gives long-running Hermes engineering projects a durable
project home on disk. The project home stores lifecycle state, continuity docs,
task status reports, and dry-run cleanup inventories. Kanban remains the execution truth for task dispatch and worker handoff.

V0 is intentionally narrow. It supports only `stacked-slices-one-pr`, one active
executable task at a time, deterministic worktree contracts, and a final
completion gate that requires a draft or open PR unless the project owner
explicitly waives it.

## Project Home

Each project lives under:

```text
~/Documents/hermes-projects/active/<slug>/
```

The home contains the files workers and reviewers use to resume safely:

```text
PROJECT.md
STATUS.md
SESSION-HANDOFF.md
SESSION-LOG.md
PARKING-LOT.md
TASKS.md
project.json
status/
refs/
scratch/
artifacts/
```

Code worktrees do not live in the project home. A task worktree must use the
deterministic branch-to-path rule:

```text
/Users/vsletten/src/<org>/<repo>/<branch-name-as-path>
```

For example, branch `feat/project-autopilot-v0` in `vsletten/hermes-agent`
maps to:

```text
/Users/vsletten/src/vsletten/hermes-agent/feat/project-autopilot-v0
```

## Commands

### Initialize a Project

```bash
hermes project init \
  --slug project-autopilot \
  --title "Project Autopilot V0" \
  --goal "Ship deterministic project lifecycle automation" \
  --board project-autopilot \
  --root-task t_root \
  --project-home ~/Documents/hermes-projects/active/project-autopilot \
  --repo-org vsletten \
  --repo-name hermes-agent \
  --canonical-repo ~/src/vsletten/hermes-agent/main \
  --final-branch feat/project-autopilot-v0 \
  --source-plan ~/Documents/hermes-projects/active/project-autopilot/refs/project-autopilot-implementation-plan-v0.md
```

`init` creates the project home, writes `project.json`, and seeds the continuity
docs. It does not create or switch git branches.

### Verify Invariants

```bash
hermes project verify <project_home>
```

Use `verify` before dispatching or reviewing work. It validates the project
schema, required files, supported mode, PR requirement, worktree path policy, and
task lifecycle invariants.

### Sync From Kanban

```bash
hermes project sync <project_home>
```

`sync` refreshes `TASKS.md`, `STATUS.md`, `SESSION-HANDOFF.md`, and the cached
task graph from the Kanban board. The board remains canonical for task status,
parent links, comments, runs, and worker handoffs.

If you need to read from a specific board database:

```bash
hermes project sync <project_home> --kanban-db ~/.hermes/kanban/boards/<slug>/kanban.db
```

### Prepare Cleanup Inventory

```bash
hermes project cleanup-inventory <project_home>
```

This writes an auditable inventory under `artifacts/cleanup/` and updates
`project.json` to `cleanup.state=inventory_ready`. No cleanup command deletes files.
Destructive cleanup remains a separate human-approved operation outside
Project Autopilot V0.

## Workflow

1. Create or update the project home with `hermes project init`.
2. Create planned Kanban tasks with explicit workspace contracts.
3. Let the dispatcher run the next ready task.
4. Require every worker to write a report under `status/<task_id>.md`.
5. Run `hermes project sync <project_home>` after task completion.
6. Run `hermes project verify <project_home>` before promoting the next task or
   reviewing completion.
7. Before marking the project done, confirm the final branch has a draft or open
   PR, unless the owner recorded an explicit waiver in `project.json`.

## Guardrails

- Do not use Project Autopilot V0 for independent multi-PR projects.
- Do not create code worktrees under `~/Documents/hermes-projects/...`.
- Do not switch an existing local worktree branch.
- Do not treat `project.json` as the execution source of truth. Kanban owns
  dispatch, task state, comments, dependencies, and worker runs.
- Do not delete files from cleanup inventory output.
