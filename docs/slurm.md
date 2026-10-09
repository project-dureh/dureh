# Scheduler jobs: `dureh slurm`

```
dureh slurm submit --wi <WI> --script <job.sh> [--sbatch-arg ARG ...] [--module <m> [--rev <rev>]] [--dry-run]
                   [--each <items> | --resubmit-failed <run> | --single --reason '<why>']
                   [--after <run>[,<run>...]] [--max-parallel N]
dureh slurm submit --wi <WI> --via '<module command ...>' [--record <path>] [--via-module <m>]
                   [--after <run>[,<run>...]] [--dry-run]            # a module's own submit command (release 42)
dureh slurm submit --wi <WI> --resubmit-failed <via run>             # re-runs the module command over the failed items
dureh slurm status --run <run id>
dureh slurm wait   --run <run id> [--timeout S] [--interval S]
dureh slurm watch  [--interval S] [--once]          # desk only
dureh slurm cancel --run <run id>
```

## Scheduler logs never land in the site root or a project tree (release 32)

Director, 2026-10-05: scheduler stdout/stderr belong in the site's log dir only. Evidence: 8
`dureh-WI-0058-<job>.out/.err` files in a company site root from 2026-10-03, written by a script's
`#SBATCH --output=dureh-%x-%j.out` (relative, so it resolved against the job's directory, the site root).

- **Config `slurm.log_dir`**: default `logs/slurm`, relative to the site root; an absolute path is allowed only
  inside the site (the site root itself, `..` escapes and paths outside are refused). Every scheduler log of a
  WI goes to `<log_dir>/<WI>/`.
- **Always on the command line.** `slurm submit` and the checks job of `verify --wi`
  pass `--output` and `--error` on the `sbatch` command line, after any `--sbatch-arg` / `checks.sbatch_args`:
  command-line options override `#SBATCH` directives, so a script cannot move them.
- **Redirect rule.** Without a name: `%x-%j.out|.err` (`%x-%A_%a` for an array). When the script (`#SBATCH -o/-e`)
  or an sbatch argument names a file: an absolute path whose directory is inside the site's log dir is kept as
  it is; any other path (relative, absolute elsewhere) keeps only its basename pattern (`%x`, `%j`, `%A`, `%a`
  ...) inside `<log_dir>/<WI>/`, and a `warning:` line on stderr (also in the record's `warnings`) says so. A job
  that names only `--output` keeps stderr in the same file (no `--error` is passed), as sbatch does.
- **Arrays** (`--each`, or `--array` in the script): a name without `%a` gets `_%a` before its extension and
  its `%j` becomes `%A` (a task's `%j` is its own job id, which no record holds), with a warning.
- **Records and hints.** The run record's `logs`, `log_out`, `log_err` name the redirected paths; `slurm status`
  and `slurm wait` print `logs: {log_out, log_err}` with the job id filled in (not recorded again).
- **Purge and old records.** `purge` finds a WI's logs from the recorded `log_out`/`log_err`, the `--each` item
  list and wrapper in the configured `<log_dir>/<WI>/` or the old `logs/slurm/<WI>/`, and for records from before
  release 19 the old candidates (job chdir, target root, site root, the script's own -o/-e directory). Files
  already in the site root from earlier releases are removed by `dureh purge --wi <WI>` when their record names
  them (inside the site only).
- **Agents.** The generated desk instructions and those of every role with the `submit_jobs` capability say: "Do not set --output/--error in job
  scripts; the harness writes scheduler logs to <log_dir>/<WI>/."
- The session job keeps its own logs under `logs/session/` (absolute `#SBATCH` lines in the generated script),
  with its tmux pane log `<NAME>.pane.log` and, while closing, the close marker `<NAME>.close` that makes the job
  end itself (`dureh day close`, `dureh site session release`; docs/workday.md "Closing the session job").

## Job names (release 44)

- **`slurm submit`** names the job `<prefix>-<WI>`, the prefix being config `slurm.job_prefix`, else the site's
  session name (the `<name>` of its session job `cc-<name>`: config `session.name`, else the site folder name).
  When the script carries its own `#SBATCH -J/--job-name` (or an `--sbatch-arg -J/--job-name` is given), the
  harness passes no `--job-name`, so the script's name stands (a command-line option would override it). The run
  record's `job_name` holds the name the job got; `slurm status|wait` and `purge` fill `%x` from it.
- **`verify --wi`** names its checks job `<prefix>-verify-<WI>`.
- A prefix holds letters, digits, `.`, `_`, `+` and `-` and starts with a letter or digit; anything else is refused.
- Records written before release 44 named every job `dureh-<WI>`; their log names are still found from that.
- The scheduler logs keep the rule above whatever the name: always under `<log_dir>/<WI>/`, never the site root.

## Jobs end outside agent turns: `dureh slurm watch` (release 30, provisional names)

Measured 2026-10-04 at a company site: agents cannot wait for a job inside their session (background waits,
`&`, `until squeue` loops and `run_in_background` are refused; a long `slurm wait` holds the session), so a
worker ends its turn while its job runs. Its item then shows as dispatched with an ended agent, the desk
noticed late and re-adopted by hand, and such items held the two lanes all day. Director's rule: workers never
wait for jobs; the desk watches the scheduler and re-dispatches.

- **`dureh slurm watch`** (the desk; other actors are refused) polls `sacct` every `--interval` seconds
  (default 300, i.e. 5 minutes) — ONE batched call per round (`sacct -n -P -X -j id1,id2,... --format=JobID,State,ExitCode,Elapsed`,
  200 ids per call), never one call per job — for every tracked run — a `run` record of `slurm submit`, single jobs and `--each` arrays,
  with a job id — whose final state is not recorded yet. When a job is terminal (COMPLETED, FAILED, CANCELLED,
  TIMEOUT, OUT_OF_MEMORY, NODE_FAIL, PREEMPTED, BOOT_FAIL, DEADLINE; an array once every task is), it records
  a `run.status` event (`final: true, watched: true`; per-item states for an array, recorded once as for
  `slurm wait`) and prints ONE line:

  ```
  2026-10-05T14:02:11Z run=run-20261005T...-ab12 job=3761206 wi=WI-0117 role=runner agent=a221b499:ended state=COMPLETED items=COMPLETED:3 next=dispatch brief --wi WI-0117 (bundle WI-0110)
  ```

  `agent` is the latest agent bound to the WI and its state (running, ended, starting, not started; `none`
  when no agent ever bound). `next` is `dispatch brief --wi <WI>` when the WI is dispatched or in rework and
  its agent is gone, `agent still running; nothing to do` while it runs, or `none (WI <status>)`.
- **Idempotent.** What is reported is derived from records: a run whose final state is recorded (by `slurm
  wait`, `slurm status` or an earlier `watch`, on any node) is never reported again, so a restarted watch
  prints nothing twice. No state file. Under `--observe` nothing is recorded, and a later run reports the same
  jobs again. `--once` makes one pass and exits (also a cheap way to refresh job states before a dispatch).
- **Stale runs.** A tracked run submitted more than `watch_stale_days` ago (org/tiers.yaml limits, default 7;
  a site overrides it in `config.json` `limits`) with no recorded final state for which `sacct` returns no state
  at all (no line, UNKNOWN, purged from accounting; an array: no task known) is recorded once as `run.status`
  `{scheduler_state: UNKNOWN, final: true, stale: true, watched: true}` (not under `--observe`) and reported
  with `next=check by hand (no scheduler state after N days)`. It is never probed again and holds no lane. A job
  `sacct` reports as PENDING, RUNNING, REQUEUED, SUSPENDED ... is never stale: it keeps its lane and is probed
  however old.
- **Desk routine.** Run `dureh slurm watch` as a Monitor for the whole company day. When a line arrives,
  re-dispatch the owning WI with `dureh dispatch brief --wi <WI>` (for an item of a bundle whose manager has
  ended, re-dispatch the bundle; its manager re-dispatches the item). The re-dispatched worker reads the final
  state with `dureh slurm status --run <run id>` and continues (stage, resubmit failed items, ...). A dispatched
  item whose agent ended while its job runs is the normal state, not an incident.
- **Workers.** After `slurm submit`, report the run id(s) and end the turn. The generated role files say so,
  and a refused wait form (background command, `&`, command substitution, a heredoc into an interpreter)
  carries the hint "do not wait for jobs in the session: end your turn; the desk re-dispatches you when `dureh
  slurm watch` reports the job". `slurm wait` stays for jobs expected to end within minutes.

### External jobs (release 45)

A job the site user started outside the harness (by hand, by a module's own framework, a leftover) holds
cluster resources that no record, lane or end report shows. `dureh status` asks `squeue -h -u $USER -o
%i|%j|%T|%M` once and lists the jobs no harness record owns:

```
External jobs (2, no harness record): 1001 manual-test RUNNING 0:12:01; 1002_2 framework-step RUNNING 0:05
```

- **Owned** (never listed): every job id a `run` record names (`job_id`, each `jobs[].job_id`, a verify run's job;
  array tasks by their array id), every `session.started` (and, from records before release 46, `container.built`)
  job, and the calling job (`$SLURM_JOB_ID`: the session itself).
- **Release 46: an include list.** Only jobs whose name matches config `slurm.external_match` (a list of name
  patterns, e.g. `["<prefix>-*"]` for a module framework's own job names) are listed; empty or absent lists none,
  and `squeue` is not asked at all. The old `slurm.external_ignore` was ignored with a warning in releases 46-48; from
  release 49 it is refused (`adapter sync` fails, `verify --self` fails, `status` warns): remove the key.
- At most 10 are listed, the rest counted (`... and N more`). When `squeue` is missing or fails, nothing is
  listed and nothing fails. Config `slurm.external_jobs: false` never asks.
- `dureh day close` warns about them (result `warning`, `external_jobs` in `day.closed`) and does not refuse.
- The harness never cancels such a job: it is not the harness's to end.

### Package scheduler tools (release 46, Director 2026-10-09)

A package may declare `scheduler_tools:` (docs/config.md): the company framework's own commands that submit its
jobs. Any agent may run a declared command; `exclude` globs are refused for everyone; `sbatch`/`srun` and container
runtimes stay denied. `squeue` jobs whose name matches a tool's `job_match` are listed by `status` as

```
Package jobs (1, scheduler tools): 700001 exflow-step-a RUNNING 0:10 [exflow]
```

separately from external jobs (which, without `slurm.external_match` in the site config, default to the same
union and so stay empty). The harness writes no `run` record for them; `day close` warns while any is queued or
running.

### Watch modes: config `slurm.watch` (release 45)

Measured 2026-10-08 at a company site: a standing watch Monitor expires and must be re-armed; about ten re-arms
brought one useful line in a day. `slurm.watch` sets what the generated desk text (CLAUDE.md items 1, 8 and 9), the
role files and the refused-wait hint tell agents about job ends. The commands themselves (`slurm watch [--once]`,
`slurm wait`, `slurm status`) work the same in every mode, and lanes, records and `day close` do not change.

| mode | desk text | worker/checker hint |
|---|---|---|
| `monitor` (default for sites without the key; release 46: `install` writes `on-demand` for a new site) | keep `dureh slurm watch` running as a Monitor; re-dispatch when a line arrives | "the desk re-dispatches you when `dureh slurm watch` reports the job" |
| `on-demand` | no standing Monitor: a job expected within minutes is waited for with `slurm wait --run <id>`; a longer one is checked with `slurm status --run <id>` (or one `slurm watch --once` pass) at the next turn or when the Director asks | "the desk re-dispatches you once the job has ended" |
| `off` | job ends are not watched; a job's state is read with `slurm status --run <id>` | as `on-demand` |

In every mode an ended job is followed by the same re-dispatch of the owning WI, and an ended verify job by the
checker's `verify --wi <WI> --collect`. Another value fails generation (`adapter sync`) with the allowed values.
Switching: set the key in `config.json` (a key that changes running behaviour: merge window or the desk's OK), run
`dureh adapter sync`, and stop a running watch Monitor if the new mode has none.

### Lanes count live work only (release 30)

`max_concurrent_lanes` (default 2) counts top-level work items (not items inside a bundle) that are **active
and alive**: status dispatched, staged, rework or verified **and** either a live agent (`agent.started` without
`agent.finished` after the latest dispatch; a deferred start of the role counts as starting) or a scheduler job
not known to have ended, on the item or on an item of its bundle. A job counts as ended when a terminal state
is recorded, when `sacct` reports one at the time of the check (one batched call for all runs, as in `slurm
watch`), or when the run is stale (older than `watch_stale_days` and `sacct` has no state for it); a run the scheduler cannot report
counts as live until its state is recorded. Staged, rework and verified items waiting for the desk, and orphaned bundles
(manager ended, no child agent or job alive), hold no lane. `dureh status` lists each holder (`Lane: WI-0110
coordinator [dispatched] held by runner agent running on WI-0117; job 3761206 RUNNING on WI-0117
(run-...)`).

Every refusal is recorded as a `lane.refused` event on the refused WI (actor: the dispatcher; payload `count`,
`limit`, `lane`, `holders: [{wi, lane, by}]`) and counted in status (`Lane refusals (24h): n; last WI-... by
... at ...`). Query: `dureh ledger query "SELECT ts, wi, actor, payload FROM records WHERE type='lane.refused'"`.

### A dispatch whose agent never started does not block (release 30)

Desk finding `fnd-20261004T214606784656Z-a4f231c9`: WI-0117 was dispatched by its manager at 21:25 UTC, no
agent ever started, and every later `dispatch brief` was refused with "dispatched and its agent has not
finished"; only the desk could clear it (`dispatch stop` + `wi amend` + re-approval). Now `dispatch brief` on
a dispatched item allows the new dispatch when the latest dispatch's agent finished, **or** when no agent
started for it within `dispatch_start_grace_seconds` (org/tiers.yaml limits, default 120; a site overrides it
in `config.json` `limits`). The new `dispatch.prepared` record carries `supersedes: {dispatched: <event id>,
agent: ended | not started, age_seconds}`. Inside the grace period the refusal says how long to wait; while an
agent runs or a deferred start of the role may still bind, the refusal is unchanged (stop it first).

Without `--module`, `submit` works as before: the job script is submitted as it is (job directory = the WI
target, logs under `<slurm.log_dir>/<WI>/` (see above; a script's own name is kept only as a basename there), config `slurm.time` as the fallback
`--time`, the workday start check) and a `run` record is written. The start check refuses a job that would end
after the day's end time, except a job for a plan item or a fix for one (finish-plan rule, Director 2026-10-06):
that one is submitted with `start_check.past_end: true`, the overtime recorded by the harness and a warning, unless
a stop (usage share, urgent, wall time) holds; `workday.finish_plan: false` refuses it as before.

## Declared checks as one scheduler job: `dureh verify --wi` (release 27)

Director's rule (2026-10-04): checks and module test suites are scheduler work, never session work. Evidence:
the company's session job reached MaxRSS 16.8 GB of 16 GB from about 20 module test runs inside the session.

```
dureh verify --wi <WI> [--local]
```

- **Runner.** Config `checks.runner`: `slurm` or `local`. Without it: `slurm` when the site has a scheduler
  (a `cluster` profile or any `slurm` setting) and `sbatch` is on the site's PATH, else `local` (tests and
  installs without a cluster keep working). `--local` runs the checks in the calling process whatever the
  config says (escape hatch; recorded as `runner_override: true`).
- **Job.** ONE job per verification: `sbatch --parsable --job-name=<slurm.job_prefix, else the session name>-verify-<WI> --time=<checks.time, else
  slurm.time, else 01:00:00> --chdir=<work root> --export=ALL [--account/--partition from slurm]
  [checks.sbatch_args...] --output=<log_dir>/<WI>/verify-<utc ts>.out --error=... <log_dir>/<WI>/verify-<utc ts>.sh`. The work root is the WI worktree (the target root
  when the role has none), the environment the site's (`site.env()`, `PYTHONDONTWRITEBYTECODE=1`,
  `DUREH_WORK_ROOT`, `DUREH_TARGET_ROOT`, `DUREH_WI`, `DUREH_SITE`), as the in-session run had. The generated
  script runs each declared check through `sh -c` and writes `verify-<ts>.check<i>.{exit,ms,out}` next to the job
  log `verify-<ts>.out|.err`. No workday start check: the checker gate already governs checking.
- **Wait.** The command polls `sacct` like `slurm wait` (`checks.interval` seconds, default 15) until the job is
  terminal or `checks.wait` seconds passed (default: the time limit plus 15 minutes); then it cancels the job
  (`scancel`) and records the state as not finished.
- **Record.** The verify `run` record and the printed result carry `runner` and `job: {job_id, state, raw,
  script, log_out, log_err, command, time, waited_seconds}`; each check has `exit` (an integer, or `not-run`
  when the job ended without running it, for example OUT_OF_MEMORY or TIMEOUT), `seconds` and `tail`. The note
  reads `n/m declared checks passed (scheduler job <id>: <state>)`.
- **Refusals.** A failed `sbatch` (or no `sbatch` at all under `checks.runner: slurm`) records nothing and says so;
  fix the config or use `--local` on a machine without a scheduler. Without declared checks nothing is submitted.
- **Migration.** A site with `slurm.account`/`partition` and `sbatch` on its PATH needs no config change; add
  `"checks": {"runner": "slurm", "time": "00:30:00", "sbatch_args": ["--mem=8G"]}` to pin memory or time.

### Per-item local runner: `--checks-runner local` and `checks.local_allow` (release 45)

Measured 2026-10-08 at a company site: a 0.04 s `grep` check cost one scheduler job and two checker spawns.

```
dureh wi new ... --check 'grep -q "## Usage" README.md' --checks-runner local
dureh wi amend --wi <WI> --memo "..." --checks-runner local|site
```

- **Config.** `checks.local_allow` lists globs (fnmatch over the whole command; `*` matches anything) of checks
  that may run in the verify process, for example `["grep -q *", "test -f *", "test -s *"]`. A check that holds a
  shell operator (`;`, `&`, `|`, `<`, `>`, a backquote, `$(`) never matches, so a pattern cannot admit a chain.
  Absent or empty (the default): `--checks-runner local` is refused and nothing changes for any item.
  `checks.local_timeout` (default 60) is the per-check timeout in seconds on this path.
- **Choice.** `wi new|amend --checks-runner local` is accepted only when every declared check matches; it is recorded
  in the item (`checks_runner: local`, an amendment like any other). Amending the checks of such an item checks them
  again. On a staged item the runner alone may be amended (not while a verify job is waiting to be collected).
  `--checks-runner site` returns to config `checks.runner`.
- **Verify.** The item's checks run locally, synchronously (`verify --submit` verifies at once), and the record has
  `runner: local, runner_source: wi`. If the checks no longer match (the config changed), they run on the site
  runner with `runner_source: site` and a `runner_note`. The site runner records `runner_source: site`, `--local`
  `runner_source: override`. The checker's brief follows the item's runner.

Agents: the generated role files say that test suites (`pytest`, `unittest discover`) never run in the session;
the checker's `verify --wi` runs the declared checks as this job and other computation goes through
`slurm submit`.

### Asynchronous verify: `--submit` / `--collect` (release 37, UR-20261005-02)

Evidence: a checker ran `verify --wi` in the background twice; the ~15-minute checks job outlived the agent, its
end-of-agent cleanup killed the command and no verification was recorded.

```
dureh verify --wi <WI> --submit          # checker: submit the checks job, print the verify run id, end the turn
dureh verify --wi <WI> --collect [RUN]   # checker, re-dispatched: record the verification from the ended job
```

- **Submit** writes the same job script and logs as the synchronous path and records a pending verify `run`
  (`run_type: verify`, `status: submitted`, `job_id`, `head`, `checks`, `script`, `prefix`, `log_out`, `log_err`,
  `command`, `time`); it returns at once. One pending verify job per WI. With nothing to submit (a bundle, no
  declared checks, or `checks.runner: local`) it verifies synchronously as before.
- **Watch.** `slurm watch` tracks pending verify jobs with the other jobs; when one ends it records the final
  state and prints `next=dispatch brief --wi <WI> --role checker (checker collects verify run <id>)` (for a bundle
  item while its manager runs: the manager dispatches the checker; release 45: when the manager has ended, the
  same `dispatch brief --wi <item> --role checker` by the desk, with no manager re-dispatch in between).
- **Bundles without their manager (release 45, 6h).** A bundle whose manager ended and whose every item is
  staged, verified, promoted or stopped is not reported as orphaned: `dureh status` prints `Bundle to close
  (manager not needed): <bundle> ... -> dispatch the checker for <staged item> ...; then close the bundle: wi
  stage --wi <bundle>, verify --wi <bundle>` (the desk may stage a bundle and run its recount). It holds no lane
  and does not keep a merge window or `day close` from seeing the site idle. The manager is re-dispatched only
  when an item needs rework or dispatch.
- **Collect** reads each check's `.exit/.ms/.out` files and records the verification exactly as the synchronous
  path (`verification.recorded`, then `wi.verified` or `rework.requested`; the record adds `collected_from`). It is
  refused, recording nothing, while the job is pending or running, when the WI head or its declared checks changed
  since submission, or when the run was already collected.
- **Lanes.** A pending verify job is a running scheduler job: it holds its item's lane until its end is recorded.
- **Rework briefs.** `dispatch brief` for an item with rework > 0 appends a "Last verification" block from the
  latest failed verify run: failed check ids and commands, exit codes, the last 20 output lines of each, per-check
  log paths and the job logs, violations and missing outputs. The block is outside the 2048-byte brief budget
  (which covers the desk-written part) and capped at 4 KB.
- **Checks environment (release 46).** A new WI worktree has no `.venv` (it is untracked). `verify --wi` picks
  the environment itself (`workflow.checks_env_plan`, recorded as `checks_env` on the verify run): the worktree's
  own `.venv` (`own`); else, when the worktree's `uv.lock` is byte-identical to the target main tree's (or neither
  has one and `pyproject.toml` is identical) and the main tree's `.venv` is not stale, that environment activated
  (`shared-venv`: `VIRTUAL_ENV`, `PATH`, `UV_PROJECT_ENVIRONMENT`, `UV_NO_SYNC=1` so a check never syncs or changes
  it, and the worktree's package directory first on `PYTHONPATH`, as `slurm submit --module` does); else, when the
  target has `env_rebuild`, the worktree's own `.venv` built first (`rebuild`: inside the checks job for the slurm
  runner, exit 95 when it fails; in the verify process for the local runner); else nothing (`none`, as before). No
  desk repair is needed for a new worktree; a stale `.venv` is still refused with the `worktree repair` hint.
  Since release 48 (company finding d381ae6b) `own` activates that `.venv` the same way (`VIRTUAL_ENV`, `PATH`,
  `UV_PROJECT_ENVIRONMENT`, `UV_NO_SYNC=1`; no `PYTHONPATH` change), for the local runner and in the checks job
  script, so a check calling a bare `python` runs the work root's environment, not the system python. This holds
  for the WI worktree's own `.venv` (including the shared form) and for a target main tree's `.venv`; a work root
  with no `.venv` at all still activates nothing.
- **Worktree environment at dispatch (release 47, UR-20261009-01).** The worker's own commands need an environment
  too, so `dispatch brief` gives a new (or reused, `.venv`-less) WI worktree of a module or project target that
  has `env_rebuild` its `.venv` before the brief returns (`workflow.provision_worktree_env`); a target without
  `env_rebuild` gets nothing, as before.
  - **Shared form** when the worktree's `uv.lock` is byte-identical to the main tree's (the `shared-venv` rule
    above) and the main tree's `.venv` exists and is not stale: `<worktree>/.venv` is a small virtualenv of the same
    base interpreter (`python -m venv --without-pip`, no network, about a second) whose site-packages holds one
    `_dureh_shared.pth`: the worktree's package directory first, then the main tree's site-packages (its own
    `.pth` files included, so editable installs of other modules resolve). `.venv/bin/python`, `source
    .venv/bin/activate` and `uv run --no-sync` (uv finds the project `.venv`) import the worktree's code with the
    main tree's dependencies; a plain `uv run` syncs first, which installs the full lock into the worktree's
    `.venv` (network and time; the brief says to use `--no-sync`). Nothing in it points at the main environment as a target: `sys.prefix` is the worktree's
    `.venv`, so a `uv sync`/`uv add`/`pip install` in the worktree installs into the worktree's own `.venv` and
    never writes into the shared one (it only reads it). A `DUREH-SHARED` file in the `.venv` names the shared
    environment and the worktree it was made for.
  - **Own environment** otherwise (the lock differs, or the main env is missing or stale): `env_rebuild` runs in
    the worktree (without the caller's `VIRTUAL_ENV`/`UV_PROJECT_ENVIRONMENT`), bounded by config
    `env.rebuild_timeout` (default 900 s). On failure or timeout the brief is refused with the error and the
    `dureh worktree repair --wi <WI>` hint, a half-built `.venv` is moved to `.venv.stale-<time>-partial`, and the
    failure is recorded as `worktree.env-failed`.
  - The dispatch output prints `# worktree env: <venv> shares <main env>` (or `built with ... in N s`) above the
    spawn line, `dispatch.prepared` records `worktree_env`, and the brief carries one `Python env:` line (outside
    the brief budget, as the verification block). A worktree that already has a `.venv` keeps it.
  - Why not a `.venv` symlink to the main env: uv and pip follow it, so a `uv sync` in the worktree would write
    into the main tree's environment. Why not env hints only: an agent's shell does not keep exported variables
    between commands, so every command would need them; a `.venv` is what the worker's tools already look for.
  - Checks: `verify --wi` treats the shared form as no own environment: `shared-venv` (main env activated,
    `UV_NO_SYNC=1`, worktree code first) while the locks match; once the worker changed `uv.lock`, `rebuild`
    replaces the shared form with the worktree's own `.venv` (`rm -rf` of it in the checks job, before
    `env_rebuild`). The shared form is stale when its main env is stale or gone or the worktree moved; `verify`
    refuses it with the `worktree repair` hint, and `worktree repair --wi`/`--all` provisions it again (shared
    when the locks match and the main env is usable, else `env_rebuild`), recorded with `env_mode`.
  - **No environment (release 47).** A module or project target with `env_rebuild` whose checks would run in
    its main tree without a `.venv` (no worktree) is refused: `worktree env missing: run worktree repair`,
    nothing recorded, instead of running a bare python.
  - Trade-off as for `--module` shared-venv: the worktree sees the main env live; a dependency change in the
    main tree reaches it, and package metadata reports the main tree's install.
- The synchronous `verify --wi` stays for the local runner and small checks. Never run verify in the background.

## One array per step: `--each`, `--after`, `--resubmit-failed` (release 26; release 46: package labels)

The unit of submission is one array per step with one task per item, all items submitted at once, and Slurm
schedules them (no concurrency cap, no default `%N`). The next step starts only after every task of the previous
one has finished (`--after`); group steps go `--after` the array as single jobs. Never loop `slurm submit` by hand.
Release 46 removed the harness's own item walker (the old dataset walker and its `--level/--suffix/--datatype/--match
/--sub/--ses` flags): items are listed by the submitter (`--each`) or by a module's own submit command (`--via`).

- **`--each <items>`** submits ONE Slurm array `--array=0-(N-1)`, one task per item. Items are a comma or space
  list (`--each a,b` or `--each a b`) or `@file` (one item per line; blank lines and `#` comment lines are ignored;
  the file must lie inside the site). Each item must match `^[A-Za-z0-9][A-Za-z0-9._-]*$`; other items, duplicates
  and an empty list are refused. A script or argument that sets `--array` itself is refused. `--max-parallel N`
  adds `%N`; there is no default cap.
- **Levels and fields come from the package** (`labels.item_grammar` in startup.yaml, `{<level>: <regex>}`). An
  item's level is the first level whose regular expression matches the whole item; the regex's named groups are
  the item's fields. An item no regex matches is `plain` with no fields; a package without a grammar makes every
  item plain (the harness parses no grammar itself). One level per array: mixing is refused. Recorded as
  `array.level`. Example (the neutral example package): `sample: '^sample-(?P<sample>[A-Za-z0-9]+)$'` and
  `batch: '^sample-(?P<sample>[A-Za-z0-9]+)_batch-(?P<batch>[A-Za-z0-9]+)$'` make `sample-01` a `sample` item with
  field `sample=01`.
- **Item mapping and names.** The job script runs once per task with the item, the array level (`by`), its 0-based
  index and each grammar field exported (a field the item lacks is exported empty). The names are the site's:
  config `slurm.each_env` = `{"item": NAME, "index": NAME, "by": NAME, <field>: NAME ...}` (`by` and the fields
  optional; only the configured names are exported; another key is refused). Without it the defaults are
  `DUREH_ITER`, `DUREH_ITER_BY`, `DUREH_ITER_INDEX` and `DUREH_ITER_<FIELD>` for each field. The record keeps the
  names as `array.env`. The harness writes the list to `logs/slurm/<WI>/each-<utc ts>.items` (fixed for the run)
  and a wrapper `each-<utc ts>.sbatch` next to it that carries the script's `#SBATCH` lines, maps
  `$SLURM_ARRAY_TASK_ID` to the item (exit 98 when a task has no item) and runs the script with its own
  interpreter. With `--module` the module wrapper does the same mapping.
- **The loop rule** (config `slurm.each_level`: one of the package's `labels.levels` or `any`; default: the
  package's finest level, else `any`). While it is on, a job without `--each` is refused unless it is a group step
  (`--after` an array) or `--single --reason '<why one job>'`; the refusal is "a job without --each is refused while
  the loop rule is on (config slurm.each_level <level>): ..." followed by the package's `policies.loop_rule.refuse`
  text when it declares one. The desk and the roles with `submit_jobs` read the package's `policies.loop_rule.text`
  in their generated instructions. `--via` runs are not refused (the module owns the loop): the module's declared
  level is recorded and a missing one is warned. The run record says `level` (an item level, `plain`, `group` or
  `single`), `reason` and `single`.
- **Logs.** `<slurm.log_dir>/<WI>/%x-%A_%a.out|.err` (one pair per task: `<job name>-<array job>_<task>.out`); a
  script's own `-o/-e` name is kept as a basename there, with `_%a` added (and `%j` turned into `%A`) when it has no `%a`.
- **Record.** `array: {job_id, items, count, items_file, wrapper, env, level}`, plus `each_file` (the resolved `@file`),
  `max_parallel` and `sbatch_args` (reused by `--resubmit-failed`).
- **`--after <run>[,<run>...]`** (with or without `--each`, repeatable) adds `--dependency=afterok:<job>[:<job>]`
  with each referenced run's job id; for an array that waits for every task. Unknown runs, runs without a
  submitted job and runs already known failed or cancelled (their latest recorded state) are refused, because a
  dependent job would never start. Recorded as `after: [{run, job_id}]`. Note: with `afterok`, a dependent job of
  an array with a failed task stays pending (`DependencyNeverSatisfied`): cancel it with `slurm cancel`, rerun
  the failed items, then submit it again `--after` the resubmission.
- **`--resubmit-failed <run>`** (an `--each` run) reads
  `sacct -j <job> -X -P -n --format=JobID,State,ExitCode` (`<job>_<i>|STATE|x:y`) and submits a new array over
  the items whose task is not COMPLETED: the same script, sbatch arguments, `--max-parallel` and module revision
  unless `--script`, `--sbatch-arg`, `--module/--rev` or `--max-parallel` are given. It is refused while any task
  is pending or running, and when every item completed. Recorded `resubmit_of` and `resubmit_states`.
- **`slurm status` / `slurm wait`** on an array report every item's state from sacct and an overall state:
  COMPLETED only if all tasks completed; FAILED if any task ended otherwise and none is pending, running or
  unknown; else RUNNING or PENDING. `wait` returns when every task has ended. Once all are terminal the record
  gets `item_states` (and `failed_items`), recorded once: a later `status` returns the same record.
- **Workday start check.** One `sbatch --test-only` for the whole array; the expected end is its first task's
  expected start plus the per-task `--time` (precedence as for single jobs: sbatch argument, then the script's
  `#SBATCH --time`, then config `slurm.time`). The record says `per_task: true`. Tasks that wait for resources
  start later than the check assumes.
- **Purge.** `dureh purge --wi <WI>` removes the task logs (`%A_%a` resolved for every task), the item list and
  the wrapper.

Example (the example package's names, `each_env` unset): two steps over samples and a group step.

```
S=bin/dureh
$S slurm submit --wi WI-0120 --script checkpoints/step-a/step1.sh --module exmod --each @checkpoints/step-a/samples.txt
#   -> run-A (array 5 tasks)
$S slurm submit --wi WI-0120 --script checkpoints/step-a/step2.sh --module exmod --each @checkpoints/step-a/samples.txt --after run-A
$S slurm submit --wi WI-0120 --script checkpoints/step-a/group.sh --module exmod --after run-B
$S slurm wait --run run-B                       # per-item states, overall state
$S slurm submit --wi WI-0120 --resubmit-failed run-A --script checkpoints/step-a/step1-fixed.sh
```

A job script for `--each` reads its item from the environment:

```
#!/bin/bash
#SBATCH --time=02:00:00
#SBATCH --cpus-per-task=4
set -euo pipefail
: "${DUREH_ITER_SAMPLE:?submit with --each}"
uv run --frozen --project "${DUREH_MODULE_ROOT}" exmod run step1 --sample "${DUREH_ITER_SAMPLE}" ...
```

## A module's own submit command: `--via` (release 42, proposals/module-submit-contract.md)

Director, 2026-10-06: every CLI a company module ships is free of dureh; item levels, item filters and the module's own
environment names belong to the module. The harness then does two things only: it runs the module's submit command
inside the WI context and ingests the record that command writes. `--script` (with `--each`) stays the generic path
for plain scripts and is unchanged.

```
dureh slurm submit --wi WI-0120 --via 'exmod run pipe step1 --workflow pipe.default --level sample --submit --detach'
```

- **Who, where, how.** The same guards as a plain submit: a role with the `submit_jobs` capability (or the desk), a
  dispatched WI of that role. The command (one quoted string, split like a shell) runs with cwd = the WI's job root (the target project
  or module, where `--chdir` points for a plain submit) and the site environment (`site.env()`: `DUREH_SITE`,
  `env.path_prepend`, `env.vars`); nothing DUREH-specific is required of the module. stdout/stderr are captured;
  config `slurm.via_timeout` (default 600 s) bounds the command. `--dry-run` records `status: not-run` with the
  command the harness would run and runs nothing.
- **What the harness may add to the command** comes only from config, never from a module name in code:
  `modules[<m>].submit: {record_arg, log_arg, after_arg, items_arg}` (each optional, each one option name of the
  module's CLI). The entry is the `modules[]` item whose `cli` (or `id`) is the command's first word (basename), or the
  one named with `--via-module <m>` (needed for wrapped commands such as `uv run exmod ...`). `record_arg <path>`
  tells the module where to write its submission record (`<slurm.log_dir>/<WI>/via-<utc ts>.json`, or the `--record`
  path), `log_arg <slurm.log_dir>/<WI>/` where its logs go, `after_arg <job id>` once per job id of each `--after`
  run, `items_arg @<file>` the items to rerun (`--resubmit-failed`). Without a declared option nothing is passed:
  the module decides and the record says where things are. Example:
  `"submit": {"record_arg": "--submission-record", "log_arg": "--log-dir", "after_arg": "--after", "items_arg": "--items"}`.
- **`--after` with `--via`** needs `after_arg`; without it the submit is refused (put the dependency in the module
  command yourself). A dependency on a via run waits for every job of that submission (`after[].job_ids`), for a
  plain submit too (`--dependency=afterok:<id>:<id>`).
- **Failure.** Exit code other than 0, a command not found, or no exit within `via_timeout`: nothing is submitted
  by the harness, no `run` record is written; one `slurm.via-failed` event (command, exit, stderr/stdout tails) is
  recorded and the error shows the tail. A command that exits 0 but names no record is refused the same way (no
  event: nothing ran that the scheduler knows of).
- **Ingest contract (decision 1).** The record path is `--record <path>` (relative to the job root), else the path
  the harness passed through `record_arg`, else the last stdout line matching `submission-record: <path>`. The file
  is JSON in the minimal schema below; a module's own schema (e.g. `exmod.submission.v1`) is a superset, unknown
  top-level keys are kept under `via.extra`.

  ```
  { "schema_version": str,                       # or "schema"
    "tool": str?, "tool_version": str?, "submitted_at": iso8601?, "cwd": path?,
    "jobs": [ { "job_id": str, "kind": "array"|"single", "count": int?, "max_parallel": int?, "name": str?,
                "script": path?, "logs": {"stdout": pattern, "stderr": pattern}?  (or "log_out"/"log_err"),
                "dependency": [job id]?, "items": [...]? } ],
    "items": [ str | {"index": int, "item": str, "level": str?, "sub": str?, "ses": str?, "path": str?} ]?,
    "level": str?, "manifest": {"path", "sha256"}?, "selection": {"level": str, ...}?, "records": object? }
  ```

  Required: `schema_version`, `jobs[].job_id`, `jobs[].kind`. An array job needs items (its own `items`, else the
  record's); each item matches `^[A-Za-z0-9][A-Za-z0-9._-]*$`, no duplicates, `count` (when given) equals their
  number. The level is what the record declares (`selection.level`, `level`, or the items' `level`), else
  the package's item grammar when every item fits one level, else `plain` (a module's own item names are not parsed).
  Relative paths resolve against the record's `cwd`, else the job root. Log patterns keep `%A_%a`/`%j`; `name` is the
  job name `%x` stands for (status/wait print the resolved paths; purge resolves them per task).
- **Record (decision 3).** The usual `run` of type `slurm` with `status: submitted`, so everything downstream reads one
  shape: `job_id` = the first job, `jobs[]` (all, normalised as above), `script`/`script_sha256`/`log_out`/`log_err` from
  the first job, `chdir`, `logs`, `after`, `array: {job_id, items, count, level}` for the FIRST array job, `max_parallel`,
  and `via: {command, text, module, submit, added, record, schema_version, tool, tool_version, submitted_at, cwd,
  manifest, selection, records, extra, stdout_tail}`. `--module` with `--via` (the module's jobs on a pinned tree) is
  phase 2 and refused for now (decision 2): the module's jobs run its live checkout.
- **status / wait / watch.** One batched `sacct` call over every job of the submission. The state is the worst:
  COMPLETED only when every task of every job completed; FAILED when all ended and any did not; else RUNNING or
  PENDING. `wait` returns when every job ended. The `run.status` event carries `jobs: {job id: state}`, the first
  array's `items`/`counts`, and when final `item_states`, `failed_items`, `failed_jobs` (recorded once). `slurm cancel` cancels every job. `watch` reports the submission as one line
  (`job=` the first job, `items=` the first array's counts).
- **Workday start check (decision 4), warn-only.** The job exists before the harness can check it. In an on-duty
  workday the harness reads `#SBATCH --time` from the first job's recorded script (else config `slurm.time`) and the
  expected start from `squeue --start`; when the expected end is past the day's end the record says
  `start_check: {late: true, ...}` and the submit prints a warning (cancel with `slurm cancel` or let it run). Not
  checkable: `"unavailable"`; no `--time`: `"skipped: ..."`.
- **`--resubmit-failed <via run>`.** Refused while any job is pending or running. The items of the array job(s)
  whose task is not COMPLETED are written to `<slurm.log_dir>/<WI>/via-failed-<utc ts>.items`; with a configured
  `items_arg` the recorded module command runs again with `items_arg @<file>` (the module's item option must take
  such a list and replace its own item walk; filters may still apply), recorded `resubmit_of`, `resubmit_states`,
  `resubmit_items_file`. Without `items_arg` the refusal lists the failed items, the file and the command to rerun
  by hand. `--script`, `--sbatch-arg`, `--module`, `--max-parallel` do not apply to a via run.
- **Purge.** `dureh purge --wi <WI>` removes, for every recorded job, its logs (patterns resolved per task) and the
  module's rendered scripts when they lie in `<slurm.log_dir>/<WI>/` (what `log_arg` points the module at); inside
  the site only, as for every other log.
- **Desk guidance** (generated item 8): a module with its own submit command is submitted `--via`, one submission per
  step, the next step `--after <run>`; the module makes the array and the item list, the harness records it; plain
  scripts use `--each`; group steps go `--after` the array; never loop submits by hand. The hook lets
  `dureh slurm submit --via '... --submit ...'` through (the quoted command is one word to the guard) and still denies
  a bare `sbatch`.

## Pinned module trees: `--module <m>` (UR-20261004-04)

A job script that points at the live checkout (`EXMOD_ROOT=$DUREH_SITE/modules/exmod`) runs whatever that
checkout holds when Python imports it; a promotion during the job changes code under a running job (measured at a
company site: a job started on one module revision while the checkout moved two revisions on). With `--module <m>`
the job runs against a frozen copy instead:

- **Revision.** `--rev`, else the pin in the WI target project's manifest (config `project.manifest`):
  `provenance.tool_versions.<m>`, written as `"<version>+<sha>"` (for example `"2.0.0a1+2396359"`; a bare sha
  or `+g<sha>` also count). It is resolved to the full sha in `modules/<m>`. A `--rev` that differs from the
  pin is allowed and recorded as `rev_override: true`. With neither (a module WI, or no pin) the submit is
  refused.
- **Tree.** At submit time `git -C modules/<m> archive <rev> | tar -x` into
  `scratch/exec/<WI>-<m>-<rev7>-<utc ts>/` under the site, then `chmod -R a-w`. No `git worktree`: nothing is
  written into the module's `.git`. `export-ignore` paths are absent.
- **Environment.**
  - `shared-venv` when the tree's `uv.lock` is byte-identical to `modules/<m>/uv.lock` and
    `modules/<m>/.venv` exists (and its interpreters are present): the job activates `modules/<m>/.venv`
    (`VIRTUAL_ENV`, `PATH`, `UV_PROJECT_ENVIRONMENT`) and prepends the tree's package directory (`src/` for a
    src layout) to `PYTHONPATH`, so imports come from the tree, not the editable install of the live checkout.
  - `rebuild` otherwise: the job first runs the module's `env_rebuild` (config `modules[<m>].env_rebuild`,
    else `env.rebuild`) in the tree with `UV_PROJECT_ENVIRONMENT=<tree>.venv` (a writable sibling), and fails
    with exit 97 when that fails. No `env_rebuild` configured is refused at submit time.
  - Both set `UV_NO_SYNC=1`, so `uv run --project "$EXMOD_ROOT" ...` in the job uses that environment and
    never syncs (never changes) it.
  - Trade-off: `shared-venv` starts at once and needs no network or cache, but shares the live `.venv`; a
    dependency change in the live checkout during the job (an `env repair` or `uv sync` there) still reaches
    the job, and package metadata (`importlib.metadata.version`) reports the live install. The lock check
    only guarantees the two locks matched at submit time. `rebuild` gives the job its own environment matching
    the pinned lock, at the cost of time and disk per job and a uv cache (use `--offline` in `env_rebuild`
    where nodes have no network). Rule of thumb: the same lock is the common case and the cheap one.
- **Exports.** The module's own names from config (Director 2026-10-04: variables a company's job scripts read
  carry the company's names): `modules[<m>].root_env=<tree>`, for example `"root_env": "EXMOD_ROOT"`, and,
  only when the entry also sets `rev_env` (for example `"EXMOD_REV"`), `<rev_env>=<full sha>`. Only when no
  root_env is configured does the wrapper export the fallbacks `DUREH_MODULE_ROOT=<tree>` and
  `DUREH_MODULE_REV=<full sha>` (the submit warns). `rev_env` without `root_env` is refused. The record keeps the
  names as `module.env: {root, rev}`. (Release 25 exported `DUREH_MODULE_ROOT`/`DUREH_MODULE_REV` always, plus
  root_env; a script that reads `DUREH_MODULE_ROOT` on a site with root_env set must switch to the root_env name.) A job script that assigns the module root itself (`EXMOD_ROOT=...`, `export <root_env>=...`, or
  any assignment pointing at `$DUREH_SITE/modules/<m>`) overrides the export; the submit warns with the line
  and never edits the script. Use `"${EXMOD_ROOT}"` or `"${DUREH_MODULE_ROOT}"` in the script instead.
- **Wrapper.** The harness writes `<tree>.sbatch`: the script's `#SBATCH` lines, the exports and environment
  set-up, then the original script run as a child with its own interpreter (its shebang). That wrapper is what
  `sbatch` receives; the record keeps the original `script` and its sha256, plus `wrapper`.
- **Cleanup.** The wrapper's `trap ... EXIT` makes the tree writable again and removes the tree and
  `<tree>.venv` when the job ends. For an `--each` array all tasks share the tree: a task removes only its own
  rebuilt environment (`<tree>.venv-<task>`), and `slurm wait` / `slurm status` remove the tree once every task
  has ended. A failed `sbatch` (or a refused start check) removes them at once.
  `dureh purge --wi <WI>` removes any leftovers (`scratch/exec/<WI>-*`, for example after a node failure).
- **Record.** The run record gets `module: {name, rev, rev_override, tree, env_mode, manifest_pin, manifest,
  root_env}` and any `warnings`.

Site set-up for a module: add `"root_env": "EXMOD_ROOT", "rev_env": "EXMOD_REV"` (the module's own names) to its
entry of `config.json` `modules`, and change job scripts to read `EXMOD_ROOT` instead of setting it (or reading
`DUREH_MODULE_ROOT`).
