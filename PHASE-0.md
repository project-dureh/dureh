# Phase 0 decisions and Phase 1 acceptance plan

This is the initial implementation decision record. Choices marked provisional must be revisited before site data, agent execution, or external actions are enabled.

## Decisions

1. **Ownership:** harness code lives here; company experience and templates live in a startup package; each installation's configuration, persona, memory, records, and projects live in its site directory. The package contains no private state gitlink or recovery revision.
2. **Install:** `install.sh --startup PATH --site PATH` accepts only an absent or empty destination. It validates the package manifest, copies package files without following symlinks, creates fresh state directories, and writes a site-local private deployment manifest. Existing nonempty sites are not overwritten.
3. **Records:** one JSON file per event under `site/state/records/events/`; create-only writes, UUID event IDs, UTC timestamps, and query by work-item ID. The files are authoritative. Indexes are deferred until profiling justifies one.
4. **Authority:** harness-mediated writes require a role and a relative path matching that role's declared scope. Absolute paths, traversal, and symlinked path components are rejected. The desk owns organizational and state files. This check is not an OS sandbox and does not constrain a worker process that bypasses the CLI.
5. **Runtime:** no runtime adapter or agent dispatch is enabled in Phase 1. Before execution is added, a capability-enforced worker boundary must be selected; prompts and hooks alone do not qualify.
6. **Compatibility:** startup package identity and revision are recorded in the private site manifest. State schema begins at version 1. Console compatibility and migration rules remain open until Phase 2.
7. **Privacy:** startup packages may contain reviewed, anonymized lessons and reusable templates, but no persona, memory, records, project names, research data, credentials, or site recovery pointer.

## Phase 1 acceptance criteria

Acceptance must be run in a clean temporary directory and recorded with exact command, result, duration, and skipped checks.

1. Install a valid startup package into an empty site; verify package identity, private deployment manifest, and empty state record directories.
2. Attempt install with a missing `startup.yaml` and with a nonempty destination; each must fail without changing the destination.
3. Append a `work-item.created` event, query by its WI ID, and verify the returned JSON matches the stored event.
4. Reject a missing required event input and malformed JSON payload with a nonzero result and a useful error.
5. Permit a role-scoped path and reject an out-of-scope write before any file is created. Include traversal and symlink attempts.
6. Verify event creation does not overwrite an existing event and malformed stored records cause query to fail visibly.

## Deferred decisions

- Formal event schemas and the complete state-transition machine.
- A process-level filesystem/network capability backend and runtime-specific dispatch.
- Concurrent event writes, index rebuild strategy, DataLad/RIA recovery, and rollback after interrupted install.
- Console API identity, authorization, CSRF protection, and OOD/Apptainer deployment.
- Qualification exam design and calibrated promotion thresholds.

## Implementation decisions (2026-09-30, Phase 1 build)

8. **Runtime adapter:** Claude Code (measured with 2.1.283) passes `agent_type` and `agent_id` in hook input for subagent tool calls, and a subagent can spawn a subagent. The adapter therefore hosts the full depth-2 organization and identifies roles from hook input. Hooks declared in agent frontmatter did not fire in the probe, so all policy runs from one project-level hook. Generated files live in the site (`CLAUDE.md`, `.claude/`, `bin/dureh`) with a hash manifest; hand edits are reported as drift.
9. **Configuration format:** site configuration and presets are JSON (`config.json`) so the harness stays standard-library only. Package and org files use a strict YAML subset parser that fails on unsupported syntax and is checked against PyYAML when available.
10. **Records:** six kinds (`work-item`, `run`, `decision`, `session`, `finding`, `event`); `kind` is the record kind and `type` the event type. Work-item status is projected from lifecycle events with an explicit transition table (`proposed -> approved -> dispatched -> staged -> verified -> promoted`, plus `rework`, `stopped`, `reverted`). A correction is a `record.voided` event.
11. **Dispatch:** a spawn is admitted only for a single-use brief recorded by `dispatch brief`, prepared by the caller that spawns. Managers dispatch only children of their own dispatched bundle. At most two top-level lanes and one reorganization lane. A tier without a qualification record needs a dated bootstrap waiver; an expired waiver blocks dispatch.
12. **Isolation:** workers write git-tracked targets only inside a `wi/<WI>` worktree under `site/worktrees/`. `verify --wi` compares the branch with its recorded base and inventories untracked and ignored files (interpreter caches excepted). Promotion requires the verified head to be unchanged.
13. **State backend:** a git repository by default; DataLad when `state.backend` is `datalad` and git-annex is on the configured path. Session end saves state. RIA push runs only when configured and is reported, never assumed.
14. **Console:** not implemented. `compat.json` declares record schema 1 with no compatible console revision, so no console pin is accepted yet.

Acceptance results for Phase 1 are recorded in `acceptance/`. Items not exercised there are `not run`.
