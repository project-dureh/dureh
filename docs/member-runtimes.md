# Member runtimes (release 64; release 66 changes noted)

A role runs on one of three runtimes, chosen in the startup package:

| `runtime` | who runs it | for |
|---|---|---|
| `claude-code` (default) | the caller spawns a Claude subagent with the Agent tool (as before) | any role |
| `codex` | the harness, through `dureh member run` | a read-only verifier: only a role with the `verify` capability (the checker may set it) |
| `local` | the harness, through `dureh member run`, on the site's office-hours model server | a worker without `verify`, `isolation: worktree`, with write scopes and a `profile` |

The non-Claude runtimes keep the harness invariants: one desk; a member run consumes a recorded, unused brief exactly
like a spawn (and is refused by the same workday gate and worktree checks); the run's job binds `agent.started` to that
dispatch; records stay append-only; Gate 2 stays the desk's; a checker's verdict is recorded by the same code path as
`verify --wi`, bound to the head it reviewed.

## Package interface

```yaml
roles:
  coder-local:
    kind: worker
    tier: worker
    runtime: local                       # claude-code | codex | local
    runtime_fallback: claude-code        # claude-code (default) | none
    profile: profiles/coder.Modelfile    # local only: a package file, the model profile (system prompt, parameters)
    write_scopes: [modules/*/src/**, modules/*/tests/**]
  checker:                               # the checker re-skin may set runtime and runtime_fallback
    runtime: codex
```

Validation (install, `adapter sync`, `verify --self`): `codex` only for a verify role (read-only: a verify role has no
write scopes); `local` only for a worker without `verify`, with `isolation: worktree`, write scopes and a `profile`; a
`profile` only with `local`, and it must be a file inside the package; managers always run as Claude subagents.

The generated agent file of a non-Claude role says the harness runs it (`dureh member run`) and that it is spawned
only after a recorded fallback. The Agent-tool spawn hook refuses spawning such a role unless `member run --fallback`
was recorded for that exact brief; the refusal names `member run`.

## Commands

| command | who | what |
|---|---|---|
| `dureh member run --wi W` | the dispatcher (desk or manager), after `dispatch brief --wi W [--role <checker>]` | records the dispatch and submits ONE job; returns at once with the run id and job id |
| `dureh member run --wi W --fallback --reason R` | the dispatcher | records `member.fallback` for the unused brief; then spawn the role as a Claude subagent with that same brief |
| `dureh member exec --run R` | the job only | runs the member and records it (refused from an agent or outside its own job) |
| `dureh member status --wi W` | anyone (also `--observe`) | the item's member runs: job state, run status, outcome, verdict, collection |
| `dureh member collect --wi W [--run R]` | the dispatcher or the desk | records the outcome of an ended run |
| `dureh member collect --wi W --stage --summary S` | the dispatcher or the desk, after review | local worker: commits the member's changes on the WI branch and records `wi.staged` |
| `dureh member collect --wi W --stage --unchanged --reason R` | the dispatcher or the desk | local worker (release 66): re-stages the same, already staged head for re-verification when the rework rightly changed nothing |
| `dureh member collect --wi W --discard --reason R` | the dispatcher or the desk | local worker: restores the member's changes, records `member.discarded` |
| `dureh member collect --wi W --rework [--reason R]` | the dispatcher or the desk | codex checker (release 66): chooses rework for a held verdict FAIL |
| `dureh member canary --runtime codex` | the desk | proves on this host that the codex profile refuses reads outside the bundle and every write |
| `dureh site office-hours plan\|start\|stop\|status` | the desk | the model server job of the local runtime |

### `member run`

1. The caller's latest brief for the item (`dispatch.prepared` by the caller) must be unused. `dispatch brief` keeps the
   full text of a member role's brief under `logs/members/<WI>/briefs/<digest>.md`; the job reads it and checks its
   digest. The brief says `Runtime: <runtime>` and, for a local worker, its exact write allowlist.
2. The workday gate, the worktree check and one live member run per item, as for a spawn.
3. Preflight: codex needs the command on the site's PATH and a passing canary; a local worker needs declared outputs in
   its write scopes (its allowlist), an `office_hours.profiles` entry for its profile, and a running server that serves
   it. A preflight problem is recorded as `member.not-run` (`stage: submit`, the reason) and refused: nothing is
   submitted and the brief stays unused, so the desk can choose the fallback at once. Release 65 (UR-20261015-03):
   `wi new` already refuses an item of a `local` role that declares no `--output` inside the role's write scopes, with
   the same reason, and `wi approve` (also the re-approval of an amended stopped item) refuses one recorded earlier;
   `wi amend --output` declares them. The preflight keeps the check for items recorded before release 65.
4. One job (`#SBATCH` from config `members.slurm` over `slurm`; job name `<prefix>-member-<WI>`; logs and script in
   `logs/members/<WI>/<run>/dispatch/`) runs `bin/dureh member exec --run <run>`. Then `wi.dispatched` (or
   `checker.dispatched`) is recorded with `runtime`, `member_run`, `job_id` and `model` (the runtime's model: the
   requested codex model or the profile name; the brief's model is kept as `brief_model`), and a `run` record
   `run_type: member`, `status: submitted`.

The caller ends its turn. The run holds the item's lane (it is a tracked job) and the item's agent state is
`starting` until the member starts; a new `dispatch brief` does not supersede it. `slurm watch` reports the job's end
with `next=member collect --wi W`; `dispatch stop` cancels a live member job (`member_runs_cancelled`).

### `member exec` (job side)

Records `agent.started` (actor: the member's role, `bound_by: member-run`, refs the dispatch), runs the runtime,
writes `dispatch/result.json` and an atomic `dispatch/run-status.json` (`submitted`, `running`, `done` with the
outcome), records `agent.finished` (`outcome`, `reason`) and one usage row (`run` type `agent`, `runtime`, `usage`
with `coverage: runtime`; `dureh usage` counts it and lists `per_runtime`). Outcomes: `completed`, or `not_run` with
the reason (a harness error included). Members get a minimal environment (an allowlist, never the job's environment).

### `member collect`

Records the outcome once (`member.collected`). A job that ended without a result (cancelled, out of time, node
failure) is recorded as NOT_RUN and its `agent.finished` written. NOT_RUN names the next step: fix and run again, or
the role's fallback with a new brief.

- **codex checker**: the job's declared check results plus the reviewer's row (`exit` 0 for `VERDICT: PASS`) go
  through `workflow._record_verification` with ownership and declared outputs from the current tree: a `verify` run,
  `verification.recorded`, then `wi.verified` on a pass, exactly as `verify --wi`. If the item's head moved after the
  review, nothing is verified (`member.collected` with `superseded`); prepare a new checker brief.
- **held FAIL** (release 66, UR-20261015-07): a collected codex FAIL is recorded like any FAIL (one `verify` run and
  `verification.recorded`, bound to the reviewed head), but the item does NOT go to rework at once: the checker
  records `member.held` (`run`, `head`, `member_run`, the note, violations, missing outputs) and the item stays
  `staged` ("verdict FAIL, held"; `member status` shows it). The dispatcher (the run's submitter, or the desk) chooses:
  - **rework**: `member collect --wi W --rework [--reason R]` records `member.choice {choice: rework}` and
    `rework.requested` from the held verification (its run, note, violations and missing outputs; no new verification);
    the owner's rework brief carries that verification as usual.
  - **the Claude checker on the same head**: a new checker brief (`dispatch brief --wi W --role <checker>`: the item
    is still staged), then `member run --wi W --fallback --reason R` records `member.fallback` (with `held`) and
    `member.choice {choice: fallback}`; the spawned checker's `verify --wi` records a second verification of that head,
    which verifies the item or sends it to rework.
  While a hold is open (no choice yet) a new codex `member run` of the checker is refused and names the two choices.
  `--rework` stays possible after a fallback whose checker has not verified yet. A hold ends when the item leaves
  `staged` or a later verification is recorded.
- **local worker**: the member never commits. Its changes stay in the worktree for the dispatcher's review
  (`member.collected` lists them, the result shows `git diff --stat`). `--stage --summary` commits exactly the changed
  files (all must be on the allowlist) with a message naming the run, and records `wi.staged` as the member
  (`reviewed_by`, `member_run`); the checker verifies it as usual. `--discard --reason` restores them; the item stays
  dispatched for a new brief.
- **unchanged re-stage** (release 66, UR-20261015-08): a local rework that correctly changed nothing (the FAIL was not
  in the worker's files) is staged again with `--stage --unchanged --reason R`: `wi.staged` as the member with
  `unchanged: true`, `reason`, `restaged_from` (the earlier `wi.staged` of the same head), `files: []`; nothing is
  committed. Refused when the worktree has changes (stage or discard them) or when the head was never staged before.
  The checker then verifies the same head again.

## Codex runtime (checker)

`codex exec --ephemeral --ignore-user-config --ignore-rules --skip-git-repo-check --json -C <evidence>` with `-c`
overrides: `default_permissions="<profile>"`, `permissions.<profile>.filesystem` = `:minimal` read, the evidence
bundle read and the sandbox helper's own paths read (no write entry, so nothing is writable),
`permissions.<profile>.network={enabled=false}`, `project_doc_max_bytes=0`, `web_search="disabled"`,
`allow_login_shell=false`, `model_reasoning_effort` from config, `-m <model>` when configured. The generic
`--sandbox read-only` flag is never used (it overrode a named profile and widened reads in an earlier team's canary).

- **Evidence bundle** (`logs/members/<WI>/<run>/evidence/`): `brief.md` (the brief the checker was given, checked
  against its recorded digest), `acceptance.md` (goal, acceptance, constraints, outputs, checks, base and head, and
  release 66: the requirements file and the snapshot, or why they are not included), `diff.patch`
  (`git diff <base>..<head>`), `changed.txt`, `checks.json` (the declared checks, run by the job before the review).
  Release 66 (UR-20261015-06):
  - `requirements/<name>`: the item's pinned `--requirements` file, copied; its sha256 is checked against the record
    before and after the copy (a mismatch is NOT_RUN: the review never judges against other requirements than the
    work was briefed with). A file the package declares no-read is listed, not copied.
  - `tree/`: a read-only snapshot of the target at the reviewed head (`git archive <head>` of the WI worktree,
    regular files only, mode 0444), unchanged files included, so a constraint about a module the change imports or
    must leave alone can be judged. Left out and listed in the manifest: the package's no-read paths, symlinks and
    submodules. When the snapshot's files exceed config `members.codex.tree_max_bytes` (default 20 MiB; 0: never),
    no `tree/` is written and the manifest and `acceptance.md` say it was not included and why.
  - `manifest.json`: base, head, the brief's digest, the requirements entry (`path`, `sha256`, `included`, `file` or
    `reason`) and the tree entry (`included`, `files`, `bytes`, `cap_bytes`, `left_out`, `reason`). The job's
    result carries `evidence: {requirements, tree, tree_files}`.

  Release 65 (UR-20261015-05): `changed.txt` and the diff are the item's tracked changes only, `git diff --name-only <base>..<verified head>`, captured BEFORE the declared checks
  run; untracked and ignored files (build output such as `*.egg-info/` that a check's own build creates) are never
  listed, and no `files/` copies are made (a staged item's work is committed). The harness's own ownership check at
  `member collect` uses the `changed_paths` list the job took before its checks too (as `verify --wi` does), carried
  in the result as `owned_paths` and used while the head is unchanged.
- **Review instructions**: the reviewer is told what each bundle file holds (release 66: `requirements/` for
  acceptance that refers to the requirements, `tree/` for unchanged files, `manifest.json` for what was left out; a
  criterion that needs what was left out is `not checked`); scope is judged on `changed.txt` alone; a claim about how
  the work was done that the diff cannot show ("read X first") is reported as `not checked`, never as not met, and
  does not fail the item on its own.
  The reviewer ends with `VERDICT: PASS` or `VERDICT: FAIL`. NOT_RUN: past the wait limit (`members.codex.wait_seconds`),
  a login or quota failure, a non-zero exit, no verdict line, or a failed canary.
- Usage: `input_tokens`, `cached_input_tokens`, `output_tokens` from `turn.completed`; the model as requested.
- **Sandbox helper paths**: under a narrow profile, bubblewrap must be able to exec the codex sandbox helper. The
  harness adds `$CODEX_HOME/tmp/arg0` and the native binary's directory (for an npm install, the platform package's
  `vendor/*/bin`); `members.codex.sandbox_read` replaces the detection. Measured with codex-cli 0.162 on a Linux
  cluster node: a denied parent wins over a readable child, so the profile names no deny entry; paths outside it are
  simply not mounted.
- **Canary**: `member canary --runtime codex` runs `codex sandbox` (no model, no quota) with the same profile in a
  throwaway directory: reading `evidence/allowed.txt` must succeed; reading `../outside/secret.txt` (relative, and
  absolute through `..`) and writing inside or outside the bundle must fail, and no file may appear. It records
  `member.canary {runtime, host, version, result, rows}`. `member run` needs a passing canary; the job needs one for
  its own host and codex version and runs it itself when missing, refusing (NOT_RUN) when it fails.

## Local runtime and office hours

`dureh site office-hours start` (also `site session start` when `office_hours.enabled`) submits ONE GPU job from config
`office_hours`. The job starts the server (kind `ollama`) bound to its node (`OLLAMA_HOST=<host>:<port>`, models in
`server.models_dir`), pulls each profile's `base` when missing, runs `create <name> -f <package file>` for each
profile, then writes `logs/office-hours/server.json` `{kind, host, port, models, profiles: {name: {file, sha256,
base}}, started, job}`; it removes the file when it ends. The wall time is the day's end plus the overtime allowance
(or the working day plus the allowance when no day runs), capped by `office_hours.time` and the partition's limit.
`day close` and `site session release` stop it (`office-hours.stopped`); `stop` and `status` are the desk's commands.
`plan` prints the script and submits nothing.

The local member (`member exec` of a `local` role) is a streaming `/api/chat` tool loop with four tools:

| tool | rule |
|---|---|
| `list_files(path)` | names under a directory of the WI worktree (no `.git`, `.venv`, caches; at most 200) |
| `read_file(path)` | a file inside the WI worktree; the package's no-read paths refused; symlinks out refused |
| `write_file(path, content)` | only the brief's exact allowlist (the item's declared `outputs` inside the role's write scopes), at most `write_cap` writes per file |
| `run_tests()` | the item's declared checks, run by the harness inside this job (cwd the worktree, the checks environment) |

No shell, no git. Release 65 (UR-20261015-01): a tool call's arguments are checked before any path or allowlist
rule. A call with a required argument missing or empty (`path`, `content`), a non-string one, or arguments that are not
valid JSON is answered `ERROR: <tool> call incomplete: missing path; the output was probably cut by the token limit;
write smaller pieces ...` and logged in `tools.json` as `malformed` (never `denied`, which stays for a well-formed call
the rules refuse); `tools.json` also has `counts` per result. A `list_files` call without `path` lists the work root.
There is no edit or patch tool: `write_file` replaces the whole file.

Options: the profile's `PARAMETER` lines, then `members.local.options` (config wins per key); they are sent with every
request, so `num_ctx` and `num_predict` from the profile or config are the effective limits. `think` off unless
`members.local.think`. Stall handling: a stream quiet for `quiet_seconds` earns a grace window only while the model
is still loaded (`/api/ps`), at most `grace_windows`; `wall_seconds` bounds the run; streamed text repeating one
short unit stops it; `max_turns` bounds the loop; a harness error (connection refused or reset, a broken line) is
retried once (`retries`). The run records the profile's name and sha256 and refuses (NOT_RUN) a profile that changed
since office hours started. Usage: the server's `prompt_eval_count`, `eval_count` and durations, summed over every
request of every attempt (so `input_tokens` is not the size of one context), with `gpu_seconds` from `total_duration`.

Context limits (release 65, UR-20261015-02): the run records `max_prompt_eval_count` (the largest single request's
prompt), the effective `num_ctx` and `num_predict` (null: the server's default) in `usage` and in the result's
`context`, with `context_full_turns` (a request whose `prompt_eval_count` reached 95% of `num_ctx`: the server probably
truncated the prompt) and `output_cut_turns` (a response that stopped with `done_reason: length`), each as
`{attempt, turn}`. The result's `notes` lists `context_full` and/or `output_cut`; `member collect` and `member status`
show them. After a cut response the model is told: each tool result of that turn starts with `NOTE: your previous
response stopped at the output limit (num_predict N): it was cut ...`; a cut response without a tool call gets that
note once as a user message and the loop goes on (a second cut answer in a row ends the run as usual).

## Events and records

| record | by | payload |
|---|---|---|
| `wi.dispatched` / `checker.dispatched` | the dispatcher | `runtime`, `member_run`, `job_id`, `model`, `brief_model`; a Claude spawn now carries `runtime: claude-code` and, after a fallback, `runtime_fallback {from, to, reason, event}` |
| run `member` | the dispatcher | `status: submitted`, `runtime`, `role`, `brief`, `job_id`, `script`, `run_dir`, `dispatch` |
| `agent.started` / `agent.finished` | the member's role | `bound_by: member-run`, `run`, `host`; `outcome`, `reason` |
| run `agent` | the member's role | `runtime`, `member_run`, `outcome`, `usage` (`coverage: runtime`) |
| `member.not-run` | the dispatcher | `brief`, `stage: submit`, `reason` |
| `member.fallback` | the dispatcher | `brief`, `runtime`, `fallback`, `reason` |
| `member.collected` / `member.discarded` | the dispatcher or the desk | `run`, `outcome`, `verdict`, `changed`, `superseded` |
| `member.held` (release 66) | the verify role, at collect | `run` (the verify run), `head`, `member_run`, `runtime`, `verdict`, `reason`, `violations`, `missing_outputs`, `collected_by`, `dispatcher` |
| `member.choice` (release 66) | the dispatcher or the desk | `held`, `choice` (`rework` or `fallback`), `run`, `member_run`, `head`, `reason`, `fallback` (the `member.fallback` event) |
| `wi.staged` with `unchanged` (release 66) | the member's role | `unchanged: true`, `reason`, `restaged_from`, `member_run`, `reviewed_by`, `files: []` |
| `member.canary` | the desk, or the checker's job | `runtime`, `host`, `version`, `result`, `rows` |
| `office-hours.started` / `office-hours.stopped` | the desk | `job_id`, `wall`, `profiles` / `reason`, `scancel` |

`member.*` and `office-hours.*` are reserved: `ledger add event` and `event add` refuse them.

Not in this release: Claude headless members, member memory, style records.

## Run directory contents (local runtime, release 65)

Each attempt keeps its whole conversation in `logs/members/<WI>/<run>/dispatch/transcript-<n>.json` (model,
profile, options, tool specs, every message: the prompt, each turn, tool calls and tool results) and the member's
final version of every file it wrote in `final-files.json`. They stay in the run directory, never in the records:
they are the raw material for training examples a manager proposes and the Director approves. `stream-<n>.jsonl`
and `tools.json` are unchanged.
