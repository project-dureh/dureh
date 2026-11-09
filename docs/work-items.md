# Work items: requirements files, re-opening, titles

Work-item options added in release 45. The lifecycle itself (proposed, approved, dispatched, staged, verified,
promoted; rework and stopped) is a projection of events (`dureh_core/records.py`).

## Requirements file: `--requirements <file>` (6g)

Measured 2026-10-08 at a company site: an acceptance list of 23 requirements did not fit the 2048-byte worker
brief (an amendment was refused at 2293 B), so the acceptance had to point at a note by hand.

```
dureh wi new ... --acceptance "R1-R23 of the requirements file are met" --requirements notes/<file>.md
dureh wi amend --wi <WI> --memo "<Director's words>" --by Director|DUREH|desk --requirements notes/<file>.md   # re-pin after a change
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

## Staging needs a commit; re-staging at a new head (release 81, UR-20261023-01)

Measured 2026-10-23 at a company site: `wi stage` accepted two items whose branch head was still the dispatch base
(nothing committed yet), and the worker's later commit could not be staged (`staged -> staged` was refused), so
the stage record named the base while verify measured the commit.

- `wi stage` refuses a WI worktree whose `HEAD` equals its dispatch base (`dispatch.prepared` base, or the base
  of a later `wi amend --rebase`): "commit the work on the WI branch first". Nothing is recorded.
- A staged item whose head moved since its last `wi.staged` is staged again at the new head (`staged -> staged`,
  the new `wi.staged` carries `restaged_from`); the same head is refused ("already staged"). A verify job
  submitted at the old head is superseded as before (release 61).

## `wi amend --rebase`: the harness rebases, `wi stage` guards (release 97, UR-20261109-01)

Measured 2026-11-06 at a company site: after `wi amend --rebase`, the re-dispatched worker skipped the brief's
"run `git rebase <base>`" line and staged a head on the old base; only the desk's own check caught it before
verify. Now the step is not left to the worker:

- **Dispatch rebases.** When `dispatch brief` prepares the worktree of a worker's item whose latest
  `wi.amended` has a `rebase_onto` and whose branch head does not contain it (`git merge-base --is-ancestor`), it
  runs `git rebase <rebase_onto>` in the WI worktree before the brief is written. This holds for any later
  dispatch too (also one after a worker skipped the step), not only the first after the amendment.
  - It went through: the brief says "the harness rebased your branch onto <base> (head was <old>)" and gives no
    rebase step; `dispatch.prepared` records `rebase: {onto, from, status: "done", to}`.
  - It did not (a conflict, uncommitted changes in the worktree, or a rebase already in progress there, which is
    never touched): the rebase is aborted (`git rebase --abort`, only for the rebase the harness started) and the
    branch is left as it was. The brief keeps the "First: the base moved. In your worktree run `git rebase
    <base>`" line, and a block outside the budget names the conflicted paths (or git's reason). The dispatch is
    not refused: resolving a conflict is the worker's job (the Gate 2 recovery above depends on it).
    `dispatch.prepared` records `rebase: {onto, from, status: "conflict", files, error, head_after_abort}`.
  - A branch that already contains the base is left alone, with no line and no `rebase` field.
- **Stage guards.** `wi stage` refuses a WI worktree whose head does not contain the amended base, naming the
  command: `git -C <worktree> rebase <base>` (resolve any conflict, `git rebase --continue`), then stage again.
  Nothing is recorded. When git gives no verdict (an unknown revision) the check is skipped. Items never amended
  with `--rebase` are unchanged.
- **Recorded with any flags; `--by` required** (a second site's UR-20261109-01, finding e785c91f). `--rebase` is
  recorded as `rebase_onto` whatever other changes come with it (`--requirements`, `--check`, ...; pinned by
  tests). `wi amend --by Director|DUREH|desk` is required, as for `wi approve` (release 78): without it the CLI
  refuses and records nothing; before release 97 every amendment was recorded as the Director's. The approver is
  recorded on `wi.amended`, on the re-approval of a stopped item and on `wi.reopened`.

## The spawn prompt is the printed brief (release 97, option 2a of a second site's UR-20261109-01)

Measured 2026-11-09 at a company site: leaders retyped briefs with errors, and the spawn hook checked only the
brief id in the header. Now `dispatch brief` records `prompt_sha256` on `dispatch.prepared` (sha256 of the brief
exactly as printed, trailing whitespace aside), and the PreToolUse hook refuses an Agent spawn whose prompt hashes
differently, even with the right id: "pass the output of `dispatch brief --wi <WI>` unchanged as the whole prompt".
Nothing is recorded on a refusal. Text added before or after the brief counts as a change; to change the work,
amend the WI and prepare a new brief. A brief prepared before release 97 (no `prompt_sha256`), and a late binding
from the subagent's transcript after a spawn hook that never ran (UR-20261015-01), keep the id check.

## The worktree's scratch directory (release 93, UR-20261106-02)

Release 92 told file-writing workers to write a multi-line script to a file and run `python3 -I <file>` (a heredoc
into an interpreter is refused), but named no place their scope lets them write; a company's writer drew 8 guard
refusals in one morning probing `$TMPDIR` and the worktree root. Now every WI worktree has `.dureh-scratch/` at its
top level:

- the agent bound to the WI, of a role with write scopes and worktree isolation, may write there whatever its
  scope (managers, roles without write scopes and the desk may not; another WI's agent may not);
- it is git-ignored through the repository's own exclude file (`git rev-parse --git-path info/exclude`, written
  when `dispatch brief` creates or reuses the worktree), never a tracked `.gitignore`;
- verify's ownership check does not list it (it is exempt like interpreter caches), and `wi stage` removes it;
- the brief's script-file line names `<worktree>/.dureh-scratch/`; a role without a worktree keeps the line that
  points at its scope or scratch space (`$TMPDIR`, written out as an absolute path).

## A read-only sweep is staged without a dispatch (release 81, UR-20261023-02)

An item owned by a role with the verify capability (the checker) that has no worktree, for example a read-only
readiness sweep, has no worker and no worker output. Before release 81 it reached `staged` only through
`dispatched`, which only a member run records, so each sweep (and each rework after a FAIL) cost a no-op codex
job. Now `wi stage --wi <WI>` takes such an item from `approved` and from `rework` (the ledger allows
`approved -> staged` and `rework -> staged` for it only; any other item still needs a dispatch). The desk runs
`wi stage`, then `dispatch brief --wi <WI> --role checker` and the checker run once. Re-staging a staged sweep is
refused (it has no head to move).

## Gate 2 and a merge conflict: `wi.promote-failed` (release 60, UR-20261013-01)

Before release 60 a `wi promote` whose merge conflicted left the target's main tree mid-merge (unmerged files,
`MERGE_HEAD`) and recorded nothing; the desk had to `git merge --abort` by hand. Now, for a module or project WI
with a worktree:

- **Pre-check.** After the Gate 2 checks (verified, head unchanged, clean main tree) and before the merge,
  `wi promote` runs `git merge-tree --write-tree --name-only <target HEAD> <WI branch>` (git >= 2.38; the merged
  tree goes to the object store only, nothing in the index, HEAD or files changes). A conflict refuses the
  promotion: the item stays verified, one `wi.promote-failed` event records `{target, base (merge base of the
  target HEAD and the WI head), head (WI head), target_head, conflicts: [paths], stage: "pre-check"}`, no Gate 2
  decision is written, and the message lists the paths and the next step. If this git gives no verdict (older
  git, a ref it cannot merge), the pre-check is skipped and the merge decides.
- **Abort fallback.** If the merge itself still fails (a conflict the pre-check could not see, or git refusing to
  start, for example an untracked file in the main tree that the merge would overwrite), `wi promote` runs
  `git merge --abort` in the target root (its own failure is ignored when there is no merge to abort), records the
  same event with `stage: "merge"`, the unmerged paths (`git diff --name-only --diff-filter=U`, taken before the
  abort; empty when git refused before starting) and `error` (git's message), and fails with the same guidance.
  The main tree is back at its HEAD with a clean status either way.
- **Recovery.** `dureh wi amend --wi <WI> --reopen --rebase <target HEAD> --memo "<Director's words>" --by Director|DUREH|desk` (the
  verification is void, the item goes back to approved with its base moved to the current main), then dispatch
  the WI again: the harness tries the rebase onto that base (release 97, below); it conflicts here, so it is
  aborted and the worker rebases and resolves the conflict, the checker verifies, and the desk promotes. `wi.promote-failed` changes no status (it is not in the lifecycle projection); the item is verified
  until it is re-opened.

## Gate 2 and the main tree's environment (release 81)

Measured 2026-10-23 at a company site: a module's main `.venv` no longer matched the module after a promotion,
twice in one day, and the desk rebuilt it by hand (`worktree repair --target`). Now, after a successful merge
into a module or project whose config entry has `env_rebuild` and whose main tree has a `.venv`, `wi promote`
checks that environment as `worktree repair --target` does: the interpreter, and `uv sync --check` when
env_rebuild is one `uv sync` (release 51). A stale environment is rebuilt from scratch with env_rebuild (one
`env.repaired` with `after_promote: true`); a matching one is left alone. When env_rebuild is not a `uv sync`
(nothing to ask), the environment is rebuilt only if the merge changed a top-level dependency file
(`pyproject.toml`, `uv.lock`, `setup.py`, `setup.cfg`, `requirements*.txt`). The result is in the `wi.promoted`
payload as `env` (`ok`, `rebuilt` or `error`, with `deps_changed`, `stale_lock`, `lock_check`) and on the
`main env:` line `wi promote` prints. A failed rebuild does not undo the promotion: `env: error` names the
repair (`dureh worktree repair --target <target>`). Without env_rebuild, or without a main `.venv` (none is
created here), nothing runs.

## Gate 2 on a public module (release 82, UR-20261026-01)

A module whose config entry has `public: true` is published under its owner's own name: its history must not
name a WI, the company or the harness. For such a module:

- `wi promote --wi <WI> --memo ... [--public-summary "<type>: <summary>"]` writes the merge commit message as
  `<type>: <short English summary>`, from `--public-summary` or else the WI's `--label`; a missing or malformed
  summary, or one that names a WI id or the site, is refused before anything merges. When the WI branch is one
  commit on top of the target's current head, the target is fast-forwarded to it instead (no merge commit). The
  `wi.promoted` record keeps the WI id and decision as always and adds `public: {message, fast_forward}`; the
  compensation is `git revert <sha>` for a fast-forward. `--public-summary` on another target is refused.
- Worker briefs carry the line "Commit messages: English, no WI ids, no company name, no Co-Authored-By /
  Claude-Session trailers."
- `verify` reads the branch's commit messages (`<base>..<head>`) and fails the item when one names a WI id
  (`WI-NNNN`) or the site (its configured names and folder), carries a `Co-Authored-By` or `Claude-Session`
  trailer, or uses letters outside the Latin script; the verify run lists them as `commit_problems`. The worker
  rewords its own branch (it is not yet published) and stages again.

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

## The approver is named: `wi approve --by` (release 78, improvement plan v0.4 bundle 1)

`wi approve --wi <WI> --by Director|DUREH|desk --memo "<the approver's words>"`: `--by` is required and is refused
when missing or outside those three. Before, a missing `--by` recorded the Director as the approver, so a desk's own
Gate 1 approval read as the Director's in the ledger. The same holds for the gate approvals `push --approve`,
`purge --project ... --approve` and `day approve` (Director or DUREH). The re-approval of an amended stopped item names
the approver of its last Gate 1. Old records keep their `by` and load as before.

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
not in the digest). `dispatch.prepared`, `wi.dispatched` and `checker.dispatched` record `kind` next to `model`,
and since release 59 (D4 follow-up) `kind_reason` as well, the rule that chose the kind: `check`, `role in
routing_doc_roles`, `lane desk` or `default`; `kind` itself is unchanged.

The desk passes the printed model as the Agent tool's `model` (which takes precedence over the agent file). The
spawn hook refuses a spawn whose `model` names another model than the brief's (the `provider:id`, the id or the
family alias `opus`/`sonnet`/`haiku` match). A spawn without `model` is always allowed, as before; when the
brief's model is not the role's own tier model (routing, academic or rework escalation) that spawn runs the agent
file's model, so the hook warns (`brief model <id> not passed; pass model=<id>`, on its stderr and as the hook's
system message) and the spawn event records `model_unstated: true`.

## Items of a local-runtime role need declared outputs (release 65, UR-20261015-03)

A role with `runtime: local` writes only the item's declared `outputs` that lie inside its write scopes
(docs/member-runtimes.md). `wi new` refuses an item for such a role without one (`--output <path>`, exact files, no
globs), and `wi approve` refuses one recorded earlier, with the reason `member run` gives ("the item declares no output
inside the role's write scopes, so the local member would have no file to write"); declare them with
`wi amend --wi <WI> --memo ... --by Director|DUREH|desk --output <path>`. Other roles are not affected.

## Files in a project cache: `purge --project` (release 77, project-dureh/dureh#40)

Before, `purge` reached only WI worktrees, so a Director-approved delete of old container images in a project cache
had to be done by hand.

```
dureh purge --project <name> --cache <cache> --path <file> [--path ...] --dry-run                 # list, sizes, sha256
dureh purge --project <name> --cache <cache> --path <file> ... --approve --by Director --memo '<where approved>'   # --by required (release 78)
dureh purge --project <name> --cache <cache> --path <file> ... --approval <decision>
```

- Desk only. Each path is a file in `projects/<name>/cache/<cache>/` (relative to it, or absolute inside it). Refused:
  `..`, a symlink, a directory, a missing file, anything that resolves outside that directory, a protected path, and
  a **pinned** file: its file name or its sha256 appears in the site's `config.json` or the project manifest
  (config `project.manifest`).
- Data deletion is fail-closed: `--approval` is required (no default, no config switch). `--approve` records the
  Director's approval as a `purge-approval` decision bound to the exact files and their sha256 (like `push
  --approve`); the purge refuses a decision of another type, one that binds other files or content, and one already
  used.
- The purge records one `cache.purged` event (refs: the decision) with `files` (path, size, sha256), `bytes` and
  `removed`.
- Untracked project files (release 85, dureh-team#91): without `--cache`, each `--path` is a file or a directory in
  `projects/<name>/` that git does not track (untracked or ignored), e.g. run outputs or checkpoints a WI left in a
  linked project. A directory stands for every regular file under it (each bound by path and sha256) and is removed
  once empty. Refused besides the cache rules: a tracked file (`git ls-files`), any symlink inside a directory
  (annexed content), `.git/` and `.datalad/`. Same approval, same `cache.purged` event (`cache` null).

## Stray untracked files in an active WI: `purge --path` (release 72, project-dureh/dureh#23)

`dureh purge --wi <WI>` removes a finished WI whole (promoted, stopped or reverted). A dispatched or staged WI whose
worktree holds stray untracked or ignored files (a test that wrote logs under `runs/`, which then fail verify's
ownership check) is cleaned without replacing the WI:
`dureh purge --wi <WI> --path <P> [--path <P2> ...] [--dry-run]`, desk only. Each path is relative to the WI's
worktree and is a file or a directory. Refused, before anything is removed: a WI that is not dispatched or staged; an
absolute path, one with `..`, the worktree itself or `.git`; a missing path; a path whose directory or target
resolves outside the worktree (symlinks are resolved); and any path that is a tracked file or holds one (tracked
files change through the WI's own commits). A symlink inside the worktree is removed as a link, its target never
followed. `--dry-run` lists what would be removed and changes nothing. A removal records one `purged` event with
`paths` (relative to the worktree) and `removed` (absolute paths); the whole-WI purge is unchanged.
