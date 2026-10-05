# DUREH

DUREH is a desk-led, unbundled, reversibility-aware, evidence-accumulating harness for AI-agent organizations. This repository contains the executable harness. Company values and domain experience belong in a separate startup package. Persona, memory, records, projects, and configuration belong to a site.

## Quick start

```sh
./install.sh --startup ../camri-org --site /path/to/new-site --preset presets/example.json
cd /path/to/new-site && claude          # the desk session; hooks and agents are generated
bin/dureh status                        # the same tools the agents use
bin/dureh verify --self                 # harness unit tests plus site consistency checks
```

Installation requires an absent or empty destination and a startup package with `startup.yaml` and no state. It copies the package without following symlinks, links read-only references, asks or reads persona answers, creates fresh site state (a git repository, or a DataLad dataset when configured), records the package's bootstrap proposals as work items awaiting Gate 1, generates the Claude Code adapter, runs the self-test, and writes a private `deployment.json` that pins harness, package, and state revisions.

## Organization and runtime

`org/roles.yaml` is the single, runtime-neutral definition: the desk, three managers, nine worker types, their dispatch authority, tiers, and write scopes. `org/tiers.yaml` holds provisional limits. The Claude Code adapter (`dureh_core/runtimes/claude_code.py`) generates `CLAUDE.md`, `.claude/settings.json`, `.claude/agents/*.md`, and `.claude/skills/*` in the site, and implements the dispatch contract with hooks:

| Contract | Claude Code mechanism |
|---|---|
| spawn | The desk or a manager runs `dureh dispatch brief --wi <WI>` and passes the recorded brief to the Agent tool. The PreToolUse hook admits only a recorded, unconsumed brief from an authorized caller and appends `wi.dispatched`. Depth is desk -> manager -> worker. |
| identity | Hook input carries the subagent's `agent_type`; the main session is the desk. The hook injects `DUREH_ACTOR` into `dureh` commands and refuses impersonation. |
| writes | Write/Edit are checked against the role's scopes, WI worktrees, read-only references, append-only records, and generated files. Bash is screened for writes, background work, scheduler bypass, pushes, and recursive deletes. |
| status | SubagentStart/Stop record `agent.started`, `agent.finished`, and an agent run with transcript token usage. |
| session | SessionStart injects persona, handoff, status, and checks; SessionEnd records the session and saves state. |

Hooks and the path checks are workflow controls. They are not an operating-system sandbox: a program started from an allowed command can still write where the user account can write. For agents the guard also refuses whole-filesystem scans (`find`, `grep -r`, `rg`, `du`, `ls -R`, `tree` rooted outside the site, the WI worktree, scratch space or the harness; the denial names the allowed roots), marks every command an agent runs with `DUREH_AGENT_ID` in its environment (an `updatedInput` rewrite without a permission decision: the rewritten command still goes through Claude Code's own permission rules and auto-mode classifier; measured in labs/hook-test, 2026-10-05), and when the runtime reports the agent finished (SubagentStop) kills the processes that still carry that marker in the session's own scheduler job (never the session process, never another agent's or a submitted job's processes), recording them as `agent.cleaned` (release 27; `dureh_core/procs.py`). Verification at promotion (`dureh verify --wi`) re-checks every changed, untracked, and ignored path in the WI worktree against the owner role and runs the declared checks as one scheduler job (config `checks.runner`, docs/slurm.md; `--local` for a machine without a scheduler).

## Tools

`wi`, `dispatch`, `verify`, `ledger`, `status`, `slurm` (scheduler jobs, optionally on a pinned read-only module tree with `--module`, docs/slurm.md), `container` (image builds as scheduler jobs, docs/containers.md), `path-refs`, `purge`, `push`, `usage`, `slack` (desk only: one-way reports to the company's Slack thread, docs/slack.md) (plus `install`, `adapter sync`, `hook`). Records are one JSON file per record under `state/records/<kind>/`, validated against `schemas/`, never rewritten; work-item status is a projection of events. The SQLite index is node-local and rebuildable.

## Status

| Capability | Status |
|---|---|
| Phase 0 decisions and Phase 1 acceptance plan | Documented in `PHASE-0.md` |
| Install, records, index, WI lifecycle, role-scoped writes | Implemented; unit and acceptance results in `acceptance/` |
| Claude Code adapter and hooks (dispatch, identity, guards, session) | Implemented; live-runtime results in `acceptance/` |
| Slurm, push, purge, path-refs, usage, exam scorer | Implemented; Slurm tested with a fake scheduler only |
| DataLad state backend | Implemented; RIA sibling and two-host sync not tested |
| Console, OOD/Apptainer/Docker deployment, recovery from a snapshot, second runtime | Proposed / not implemented |

Read `AGENTS.md`, `PHASE-0.md`, then the pinned design documents in `DESIGN-BASELINE.md` before extending this implementation.
