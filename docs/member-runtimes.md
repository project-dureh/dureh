# Member runtimes (release 64)

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
| `dureh member collect --wi W --discard --reason R` | the dispatcher or the desk | local worker: restores the member's changes, records `member.discarded` |
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
   submitted and the brief stays unused, so the desk can choose the fallback at once.
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
  `verification.recorded`, then `wi.verified` or `rework.requested`, exactly as `verify --wi`. If the item's head moved
  after the review, nothing is verified (`member.collected` with `superseded`); prepare a new checker brief.
- **local worker**: the member never commits. Its changes stay in the worktree for the dispatcher's review
  (`member.collected` lists them, the result shows `git diff --stat`). `--stage --summary` commits exactly the changed
  files (all must be on the allowlist) with a message naming the run, and records `wi.staged` as the member
  (`reviewed_by`, `member_run`); the checker verifies it as usual. `--discard --reason` restores them; the item stays
  dispatched for a new brief.

## Codex runtime (checker)

`codex exec --ephemeral --ignore-user-config --ignore-rules --skip-git-repo-check --json -C <evidence>` with `-c`
overrides: `default_permissions="<profile>"`, `permissions.<profile>.filesystem` = `:minimal` read, the evidence
bundle read and the sandbox helper's own paths read (no write entry, so nothing is writable),
`permissions.<profile>.network={enabled=false}`, `project_doc_max_bytes=0`, `web_search="disabled"`,
`allow_login_shell=false`, `model_reasoning_effort` from config, `-m <model>` when configured. The generic
`--sandbox read-only` flag is never used (it overrode a named profile and widened reads in an earlier team's canary).

- **Evidence bundle** (`logs/members/<WI>/<run>/evidence/`): `brief.md`, `acceptance.md` (goal, acceptance,
  constraints, outputs, checks), `diff.patch` (base..head), `changed.txt`, `checks.json` (the declared checks, run by
  the job before the review) and `files/` (copies of untracked changed files).
- The reviewer ends with `VERDICT: PASS` or `VERDICT: FAIL`. NOT_RUN: past the wait limit (`members.codex.wait_seconds`),
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

No shell, no git. Options: the profile's `PARAMETER` lines, then `members.local.options`; `think` off unless
`members.local.think`. Stall handling: a stream quiet for `quiet_seconds` earns a grace window only while the model
is still loaded (`/api/ps`), at most `grace_windows`; `wall_seconds` bounds the run; streamed text repeating one
short unit stops it; `max_turns` bounds the loop; a harness error (connection refused or reset, a broken line) is
retried once (`retries`). The run records the profile's name and sha256 and refuses (NOT_RUN) a profile that changed
since office hours started. Usage: the server's `prompt_eval_count`, `eval_count` and durations, summed over attempts,
with `gpu_seconds` from `total_duration`.

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
| `member.canary` | the desk, or the checker's job | `runtime`, `host`, `version`, `result`, `rows` |
| `office-hours.started` / `office-hours.stopped` | the desk | `job_id`, `wall`, `profiles` / `reason`, `scancel` |

`member.*` and `office-hours.*` are reserved: `ledger add event` and `event add` refuse them.

Not in this release: Claude headless members, member memory, style records.
