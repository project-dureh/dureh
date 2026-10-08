# Work items: requirements files, re-opening, titles

Work-item options added in release 45. The lifecycle itself (proposed, approved, dispatched, staged, verified,
promoted; rework and stopped) is a projection of events (`dureh_core/records.py`).

## Requirements file: `--requirements <file>` (6g)

Measured 2026-10-08 at a company site: an acceptance list of 23 requirements did not fit the 2048-byte worker
brief (an amendment was refused at 2293 B), so the acceptance had to point at a note by hand.

```
dureh wi new ... --acceptance "R1-R23 of the requirements file are met" --requirements notes/<file>.md
dureh wi amend --wi <WI> --memo "<Director's words>" --requirements notes/<file>.md   # re-pin after a change
```

- The file must be inside the site (a relative path is taken from the site root; `notes/` or a target's main
  tree) and at most `limits.requirements_bytes` (default 65536). The item records `requirements: {path
  (site-relative), sha256, bytes}`.
- The brief carries one line: `Requirements (read first; part of the acceptance): <absolute path> (sha256
  <12 hex>, <n> bytes; do not edit it)`. Only that line counts toward the brief limit (`worker_brief_bytes`,
  `manager_brief_bytes`); the file's text does not.
- **A changed file refuses.** `dispatch brief` (any role, the checker too) and `verify --wi` (synchronous,
  `--submit` and `--collect`) compare the file's sha256 with the record and refuse, recording nothing, when the
  file changed or is missing. Restore it, or pin the new content with `wi amend --requirements` (Director's words,
  like any amendment; allowed alone on a staged item unless a verify job waits to be collected), then dispatch or
  verify again. Refusing rather than recording a failed verification keeps a rework brief from pinning the old hash.

## Re-opening a verified item: `wi amend --reopen` (6f)

See docs/workday.md ("What the plan approval covers"): a verified item that is not promoted goes back to
`approved` with a `wi.reopened` event; promoted items never change.
