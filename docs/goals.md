# Goals: the Director's goal, its criteria, evidence and acceptance (release 98)

Measured 2026-11-09 at a company site (dureh-team#161): the Director's goal ("run it end to end, up to a report
comparing several methods") became a work item whose acceptance was only "runs complete, metrics in range". The
checker verified that acceptance, the item was promoted with one table and no report or figures, the desk reported
the goal done and nobody compared the result with the Director's words. Afterwards no next goal was proposed. The
harness had no record of the goal, no link from work items to it, no evidence requirement and no alert for a site
without a goal. This release adds them; the mechanism is the same at every site.

```
dureh goal new --text "<Director's words>" | --text-file <file> --criterion "<what must hold> => <deliverable>[; <deliverable>]" ...
               --source <board item> --by Director|DUREH [--title <short>] [--memo ...]
dureh goal list [--state open|closed|dropped] [--json]                    # read-only, allowed with --observe
dureh goal show --goal G-0001                                             # read-only, allowed with --observe
dureh goal check --goal G-0001 [--json]                                   # read-only, allowed with --observe
dureh goal evidence --goal G-0001 --criterion C1 [--deliverable <name|n>] --path <p> | --figure <p> | --link <url> [--note ...]
dureh goal evidence --goal G-0001 --withdraw <evidence id> --memo "<why>"
dureh goal close --goal G-0001 --approval <board item URL>                # or, in the Director's own terminal: --by Director --memo ...
dureh goal drop  --goal G-0001 --memo "<Director's words>" --approval <board item URL>
dureh wi new ... --goal-id G-0001 --acceptance "[C1] ..." | --no-goal "<reason>"
dureh wi amend --wi <WI> --goal-id G-0001 --acceptance "[C1] ..." --memo ... --by ...   # link a legacy item
```

## The goal record

- `goal new` (desk only) records `goal.created`: the id (`G-0001`, `G-0002`, ... per site), the Director's
  **text verbatim** (stored exactly as given: no whitespace or line change; `--text-file` for several lines), a short
  title (`--title`, at most 60 characters; default the text's first line), the criteria, `source` (the board item
  the goal comes from: an https URL, `owner/repo#N` or `#N`; never fetched), `by` (Director, or DUREH relaying the
  Director's words; required, never assumed) and `memo`.
- A criterion is `"<what must hold> => <deliverable>[; <deliverable>]"`: every criterion names at least one
  deliverable (for example `"each option is compared => comparison document; summary figure"`). Criteria get the ids
  `C1`, `C2`, ... in the order given. A criterion without a deliverable, or naming one twice, is refused.
- A second open goal from the same board item is refused. A goal is not amended: a changed goal is a new goal (drop
  the old one, record the new).
- Records: a goal is a projection of events (`goal.created`, `goal.evidence`, `goal.evidence-withdrawn`,
  `goal.closed`, `goal.dropped`), like a work item's status; no new record kind. `goal.*` events are reserved
  (`ledger add event` refuses them) and so are the decision types `goal-acceptance` and `goal-drop`.

## Work items name their goal; Gate 1 checks coverage

- `wi new` requires `--goal-id <G>` (an open goal) or `--no-goal "<reason>"` (work that serves none: an urgent repair,
  housekeeping; recorded as `no_goal`). An item created with `--parent` or `--fix-for` inherits that item's goal when
  it names an open one. Both flags together, an unknown or closed goal and an empty reason are refused.
- An acceptance line covers a criterion when it carries the criterion's tag: `--acceptance "[C2] the comparison
  document with its summary figure"`. Tags are checked at `wi new` and `wi amend`: a tag the goal does not have is
  refused. The tags travel in the brief (they are part of the acceptance), so the worker and the checker see which
  criterion each line delivers.
- **Gate 1** (`wi approve`, and the re-approval of an amended stopped item) of an item that names a goal builds the
  coverage table, criterion -> work items: the items of the goal that are not stopped or reverted and tag the
  criterion, this one included. While a criterion is covered by no item the approval is refused and **nothing is
  recorded**; the message carries the table (`-> NOT COVERED`) and the two ways out: tag the line that delivers it
  (`wi amend --acceptance`), or create the item that does (`wi new --goal-id <G> --acceptance "[C2] ..."`) first.
  So a goal split into several items is approved once all of them are written down (proposed). `wi approve` prints
  the table after the approval; the `gate1` decision records `binding: {goal, coverage: {C1: [WI...], ...}}` and
  `wi.approved` records `goal`.
- **Migration.** Items recorded before release 98 name neither (`goal_id`, `no_goal`): they are *legacy*. They are
  approved, dispatched and promoted as before; `wi approve` prints `Warning: <WI> names no goal ...` and
  `wi.approved` records `legacy_no_goal: true`. Link one to a goal with `wi amend --goal-id <G>` (with tagged
  acceptance lines); a goal once set never changes. `dureh status` lists open items that name no goal (legacy and
  `--no-goal`) on one `Goal:` line. Library callers (the package's bootstrap items at install) still record legacy
  items; only the CLI requires the choice.

## Evidence and closing (fail-closed)

- `goal evidence` (desk only) records one piece of evidence for one deliverable of one criterion
  (`--deliverable` by name or number; default the criterion's only deliverable):
  - `--path <p>`: a file or directory inside the site, bound by its sha256 (a directory by a manifest of every
    file's relative path, size and sha256, at most 20000 files; an empty directory is refused);
  - `--figure <p>`: an image or PDF file inside the site (`.png .jpg .jpeg .svg .pdf .gif .webp .tif .tiff`), bound
    the same way;
  - `--link <url>`: an https URL, for example the board item that carries the report for the Director.
  A path outside the site, a missing path and a path under a protected no-read path are refused. `--withdraw <id>
  --memo` removes an entry (for example after the file was replaced: withdraw, then record it again).
- `goal check` (read-only, allowed with `--observe`, exit 0 when ready or no longer open, 1 when not ready) prints
  the Director's words, each criterion with its work items and each deliverable with its evidence and state: `ok`,
  `missing`, `changed` (the file changed since it was recorded) or `refused` (now protected). The verdict is
  `Check: READY for the Director's acceptance` when every deliverable has evidence in state `ok` and no item of the
  goal is still open (proposed to verified), else `Check: NOT READY` with the list of problems. The upstream team
  runs it (with `--observe`) before relaying a company's "done"; the desk shows it before reporting a goal done.
- `goal close` (desk only) refuses unless the check is READY, and then needs the Director's acceptance, one of:
  - `--approval <board item URL | github.decision.changed id>`: the Decision recorded by `github watch` on the goal's
    source item or on a board item given as its evidence (`--link`), an Approve (1-3), not changed since (a later
    Decision on that item, for example Rejected, voids it), and **later than the goal's latest evidence**, so the
    Director decided on the evidence as it is now. The desk cannot write that record (`github.*` events are reserved;
    on a layer-3 item only the Director sets the Decision).
  - `--by Director --memo "<words>"` in the Director's own terminal (no `DUREH_ACTOR`, `CLAUDECODE` or
    `DUREH_AGENT_ID`), as for `site workday`.
  Anything else is refused (`--by desk`, `--by DUREH`, `--by Director` inside an agent session): the desk cannot
  close a goal. The close records a `goal-acceptance` decision (`by: Director`, `binding: {goal, digest, criteria,
  record, url, decision}` or `{terminal: true}`; `digest` is the sha256 over the evidence that was `ok`) and then
  `goal.closed` (refs: the goal and the decision). A closed goal takes no new items and no evidence.
- `goal drop --memo` drops an open goal the Director withdrew, with the same authority (no evidence needed); it
  records a `goal-drop` decision and `goal.dropped`.

## No open goal: status and the day plan

- `dureh status` (also `--observe status`) has stable lines that start with `Goal:`:
  - one per open goal: `Goal: G-0001 open: <title> (criteria 2, deliverables with evidence 1/3, work items 2)`, with
    `; not covered by any work item: C2` while a criterion has no item;
  - with no open goal: `Goal: none open`, and while the site is on duty (a day plan is open, or a session started
    today) `Goal: none open (on duty): propose the next goal to the Director; the day plan starts with it`. A
    heartbeat outside the site greps `^Goal: none open (on duty)`;
  - `Goal: <n> open work items name no goal (legacy or --no-goal): <WIs>` when there are any.
  `dureh status --json` carries the same under `goals` (`open`, `on_duty`, `closed`, `unlinked_open_items`).
- `day plan` with no open goal records the plan with a first line added by the harness, `0. Propose the next goal to
  the Director (...)`, and `no_open_goal: true` on `day.planned`, unless the plan's first line already speaks of the
  next goal. An addendum is not changed.
