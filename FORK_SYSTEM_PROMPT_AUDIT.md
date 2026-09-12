# Fork System Prompt Audit

## Decision

This fork no longer accepts upstream changes. The `upstream` remote has been removed from the installed repository, and both local update wrappers now fail closed. Future changes are deliberate local commits pushed only to Victor's fork.

## Executive finding

The regression is not evidence that the model lost the ability to perform simple work. The harness now surrounds the same model with several large, overlapping control layers that reward procedural activity over direct task completion.

The most damaging interaction was:

1. Mandatory skill loading told the model to retrieve any even partially relevant runbook.
2. Broad operational skills injected stale platform procedures before the task was understood.
3. Tool-use and execution-discipline blocks reinforced more discovery, more tools, and more verification.
4. Honcho hybrid recall injected a 41,931-character memory payload into the latest user turn.
5. Compaction preserved an exhaustive historical task ledger, including every tool action, error, file, and correction.
6. The model then optimized measurable proxy state such as jobs, PIDs, logs, and watcher counts instead of the user's actual outcome.

The harness did not force that failure. The model still failed to prioritize the explicit request. The harness made that failure mode much more likely and much harder to recover from.

## What reaches the model

The following are distinct transport fields, but all consume attention in the model's request context.

| Layer | Source | Current behavior | Risk |
|---|---|---|---|
| Base identity | `~/.hermes/SOUL.md`, loaded by `agent/prompt_builder.py` | Injects persona, autonomy, disagreement, accountability, public/private voice, and Hermes-specific guidance | Medium. Useful identity is mixed with operational rules that belong elsewhere. |
| Built-in guidance | `agent/prompt_builder.py` | Adds task-completion, parallel-tool, memory, environment, skill, coding, platform, tool-enforcement, and execution blocks | Critical. Several blocks overlap and reinforce procedural behavior. |
| Guidance gates | `agent/agent_init.py`, `agent/system_prompt.py`, `hermes_cli/config_defaults.py`, `~/.hermes/config.yaml` | Resolves `auto`, booleans, and model lists to decide which guidance blocks are inserted | Critical. `auto` previously targeted GPT/Codex models with extra tool and execution coaching. |
| Prompt assembly | `agent/system_prompt.py` | Concatenates identity, guidance, project context, memory, plugins, profile, skills, runtime, and ephemeral prompt sections | High. There is no global complexity budget or deduplication across semantically overlapping sections. |
| Skill index | `agent/prompt_builder.py`, `tools/skills_tool.py`, profile/plugin skill directories | Lists all available skills and formerly required loading any skill that was even partially relevant | Critical. This directly caused broad skill loading before a trivial task. |
| Persistent profile | `~/.hermes/MEMORY.md`, `~/.hermes/USER.md` | Adds global memory and user profile to every request | Medium. Small enough individually, but duplicated by Honcho conclusions and compaction. |
| Honcho recall | `~/.hermes/honcho.json`, `plugins/memory/honcho/` | In hybrid mode, injected derived memory into user turns and exposed six Honcho tools | Critical. The latest payload was 41,931 characters and contained a comprehensive historical summary unrelated to the immediate action path. |
| Project context | Context file discovery in `agent/prompt_builder.py` and `agent/system_prompt.py` | May inject `AGENTS.md`, `CLAUDE.md`, and other cwd-specific files | Medium to high. Correct for repository work, harmful when multiple instruction files overlap. |
| Platform and profile hints | `agent/prompt_builder.py`, `agent/system_prompt.py`, desktop/gateway initialization | Adds desktop rendering, files, widgets, profile location, runtime environment, model, date, and steering protocol | Low to medium. Mostly useful, but verbose and repeated. |
| Plugin prompt sections | Plugin engine hooks consumed by `agent/system_prompt.py` | Plugins can insert system-prompt sections at named positions | Variable. No enabled plugin section was identified as a major contributor in this audit, but this is an unrestricted extension point. |
| Direct tool schemas | `model_tools.py`, tool registry, individual tool modules | Sends names, descriptions, and JSON schemas for every active direct tool | Critical for fixed context size. The representative CLI request carried 30 tools totaling 47,494 JSON bytes after the first cuts. |
| Deferred tool catalog | `tools/tool_search.py`, `tools/tool_search_catalog.py` | Previously embedded names and descriptions for deferred connectors inside the `tool_search` schema | High. It advertises capabilities the model does not need and encourages discovery instead of execution. |
| Conversation history | Session store and gateway request assembly | Replays prior user, assistant, tool, and result messages | High in long sessions. This is necessary state, but verbose tool results amplify every earlier mistake. |
| Compaction handoff | `agent/context_compressor.py`, `agent/conversation_compression.py` | Replaces old history with a highly structured historical ledger | Critical. The current template explicitly preserves completed actions, active state, blockers, decisions, errors, resolved questions, files, exact commands, and a detailed session log. |
| Turn memory hook | Memory provider prefetch path | Appends `<memory-context>` to user input | Critical when hybrid recall is enabled. It is not a system message, but it has the same practical effect on reasoning. |
| Provider/runtime policy | API host and model provider | May add policy or transport-specific instructions outside the fork | Not auditable from this repository. No claim is made about hidden provider instructions. |

## Measured prompt budget

Representative `hermes prompt-size --platform cli --json` readings:

| State | System prompt | Skill index | Tool schemas |
|---|---:|---:|---:|
| Before live cuts | 20,673 chars | 4,350 chars | 48,142 JSON bytes |
| After live guidance/catalog cuts | 15,968 chars | 4,350 chars | 47,494 JSON bytes |
| Fork branch with prompt text cuts | 15,711 chars | 4,093 chars | 47,494 JSON bytes |

The Honcho payload is not counted in those numbers. Removing hybrid recall eliminates another observed 41,931 characters from the latest user turn. The first live cuts therefore remove at least 46,636 characters of automatic context before counting the larger deferred catalog savings in the real desktop toolset.

The remaining 47,494 bytes of direct tool schemas are now the largest fixed request component in the representative inspection.

## Evidence of recent churn

The installed fork's current head is merge commit `a035c884910639fb68bc25dcb5ac36927c34a57c`, created on 2026-09-10 with subject `Merge upstream main into fork`. That merge touched 1,971 files, adding 97,882 lines and deleting 36,800 lines.

Compared with commit `590d547b40f36a6b2286fcd781a05545d199409c` from 2026-08-10, the current tree added 1,192,239 lines and deleted 696,887 lines across 7,129 files.

This proves that the harness changed drastically during the reported regression window. It does not prove that every upstream change was harmful or identify a single causal commit. The observed prompt composition is sufficient to explain the failure without relying on that stronger claim.

Notable prompt-history evidence:

- The mandatory skills language existed before August but was strengthened on 2026-09-02.
- The compaction updater's `PRESERVE all existing information` instruction was introduced on 2026-09-02.
- The compaction `Errors & Fixes` ledger was added on 2026-08-15.
- The 2026-09-10 upstream merge introduced or replaced large parts of tool, plugin, profile, gateway, and model infrastructure.

## Cuts already applied to the live profile

1. Removed the installed repository's `upstream` remote.
2. Replaced `~/.hermes/scripts/victor-hermes-update.sh` with a fail-closed kill switch.
3. Replaced `~/.hermes/scripts/update-hermes-preserve-local-patches.sh` with a fail-closed kill switch.
4. Replaced the fork's tracked `scripts/victor-hermes-update.sh` with the same fail-closed policy and pinned it with a regression test.
5. Set Honcho recall mode from `hybrid` to `tools`, stopping automatic memory prefetch while preserving explicit Honcho lookup tools.
6. Set `agent.tool_use_enforcement` to `false`.
7. Set `agent.execution_guidance` to `false`.
8. Set `tools.tool_search.listing` to `off`.

## Durable fork changes on `fix/prompt-amputation`

1. Skills are optional. The prompt now says to act directly on simple or urgent tasks and to load one best-matching skill only when specialized procedures are actually needed.
2. The generic tool-use and execution-discipline blocks default to `false` rather than `auto`.
3. Deferred tool catalog listing defaults to `off`.
4. Memory guidance is shorter and no longer declares that skills always come first.
5. Tests pin the new direct-work behavior and lean defaults.

Validation result: `472 passed, 1 skipped, 1 deselected`. The deselected Anthropic interrupt test requires the optional `anthropic` package missing from the installed environment and is unrelated to these changes.

## Next cuts, in order

### 1. Replace the compaction ledger

The current compaction template is historical scar tissue. Replace it with a short state handoff containing only:

- Current user objective
- Explicit constraints and stop/reversal instructions
- Verified state-changing actions
- Unresolved blockers
- Exact identifiers needed to continue
- Latest user messages verbatim

Remove the exhaustive completed-action log, repeated errors, resolved questions, detailed session log, broad file inventory, and narrative critical context. Add a hard summary budget.

### 2. Narrow default direct tools

Keep only the core filesystem, shell, web, and explicit interaction tools loaded by default. Defer browser, vault, delegation, TTS, reaction, connectors, and specialized memory tools until the request needs them.

### 3. Separate identity from operating procedures

Keep `SOUL.md` short: identity, voice, and irreversible-action boundary. Move tool mechanics, verification protocol, skill policy, and coding workflow into code/config gates rather than repeating them in persona text.

### 4. Remove duplicate memory layers

Use one automatic standing profile source. Keep Honcho as explicit lookup or replace `MEMORY.md` and `USER.md`, but do not inject both plus a derived Honcho summary plus compaction history.

### 5. Prune the skill catalog

Keep a small set of high-value, class-level runbooks. Delete narrow incident fossils and overlapping workflow skills. Skill content must not be part of the default prompt until explicitly loaded.

### 6. Add a prompt budget regression test

Fail the fork's test suite when default fixed context exceeds explicit limits for:

- system-prompt characters
- skill-index characters
- direct tool schema bytes
- deferred catalog bytes
- automatic memory-prefetch characters

A functional regression test should also assert that a simple request produces the direct action path without skill loading, delegation, or orchestration setup.
