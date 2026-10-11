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

## WI titles for the Director (UR-20261008-02)

Director, 2026-10-08: whenever a WI is named to the Director, a short title goes with it, for example
`WI-0138 (framework design doc)`.

- **Short title.** `wi new --label "<text>"` (1-40 characters) sets it; without a label it is the title cut to 40
  characters (`...` marks the cut). `wi show --summary` prints it as `label`, and `wi new` prints the new item that way.
- **Harness output.** `dureh status` (gate lines, lanes, lane refusals, orphaned items, bundles to close, the
  upstream and workday lines; the per-status lists already carry the full title), `day status`, Slack posts
  (`slack post`, `report`, the day-plan `slack ask`), the session text `report` returns (`text`), and `upstream
  message` (also what `upstream send` delivers) render each bare WI id as `WI-xxxx (short title)`. Ids inside
  backquotes (commands to copy), ids already followed by a parenthesis, and unknown ids stay as they are.
  `day plan` and `day end` record a `titles` map ({WI: short title}) for the WIs they name (plan items,
  proposed overtime, the day's job runs, items finished past the end time).
- **Desk.** The generated CLAUDE.md says: "When you name a WI to the Director, add its short title".

## Kind and model of a brief (release 54, D3)

Every brief is a work unit of one kind, derived in `dispatch brief` in this order: `check` (a checker dispatch,
`--role checker`), `doc` (the role is in `models.routing_doc_roles`, default `doc-writer`), `triage` (the work
item's lane is `desk`), else `build` (writers, runners, managers). The command prints both on its first line:

```
# spawn subagent_type=doc-writer model=anthropic:claude-haiku-4-5-20251001 kind=doc; pass everything below as the prompt ...
```

The model is the routed tier's model when `models.routing` maps the kind to a tier (docs/config.md), otherwise
the role's own tier, as before release 54. The academic escalation (`academic: true` -> tier `writer-academic`) and
the rework escalation (a worker at the tier's `rework_limit` -> tier `pm`) apply after routing and win. Without
`models.routing` no model choice changes; the brief body never changes with routing (the kind is on the spawn line,
not in the digest). `dispatch.prepared`, `wi.dispatched` and `checker.dispatched` record `kind` next to `model`.

The desk passes the printed model as the Agent tool's `model` (which takes precedence over the agent file). The
spawn hook refuses a spawn whose `model` names another model than the brief's (the `provider:id`, the id or the
family alias `opus`/`sonnet`/`haiku` match). A spawn without `model` is always allowed, as before; when the
brief's model is not the role's own tier model (routing, academic or rework escalation) that spawn runs the agent
file's model, so the hook warns (`brief model <id> not passed; pass model=<id>`, on its stderr and as the hook's
system message) and the spawn event records `model_unstated: true`.
