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

Phase 1 code exists, but acceptance is not marked verified until the criteria above have recorded results. Full component and security checks remain not run.
