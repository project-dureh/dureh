# Scheduler jobs: `dureh slurm`

```
dureh slurm submit --wi <WI> --script <job.sh> [--sbatch-arg ARG ...] [--module <m> [--rev <rev>]] [--dry-run]
                   [--each <items> | --each-from-bids <dataset> [--level file|subject|session] [--suffix S] [--datatype D]
                                                                 [--match GLOB] [--sub L ...] [--ses L ...]
                    | --resubmit-failed <run> | --single --reason '<why>']
                   [--after <run>[,<run>...]] [--max-parallel N] [--reason '<why a coarser level>']
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
`dureh-WI-0058-<job>.out/.err` files in the camri-org root from 2026-10-03, written by a script's
`#SBATCH --output=dureh-%x-%j.out` (relative, so it resolved against the job's directory, the site root).

- **Config `slurm.log_dir`**: default `logs/slurm`, relative to the site root; an absolute path is allowed only
  inside the site (the site root itself, `..` escapes and paths outside are refused). Every scheduler log of a
  WI goes to `<log_dir>/<WI>/`.
- **Always on the command line.** `slurm submit` (and so `container build`) and the checks job of `verify --wi`
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
- **Agents.** The generated desk and pipeline-runner instructions say: "Do not set --output/--error in job
  scripts; the harness writes scheduler logs to <log_dir>/<WI>/."
- The session job keeps its own logs under `logs/session/` (absolute `#SBATCH` lines in the generated script),
  with its tmux pane log `<NAME>.pane.log` and, while closing, the close marker `<NAME>.close` that makes the job
  end itself (`dureh day close`, `dureh site session release`; docs/workday.md "Closing the session job").

## Jobs end outside agent turns: `dureh slurm watch` (release 30, provisional names)

Measured 2026-10-04 at UNC_Lee_CAMRI-ORG: agents cannot wait for a job inside their session (background waits,
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
  2026-10-05T14:02:11Z run=run-20261005T...-ab12 job=3761206 wi=WI-0117 role=pipeline-runner agent=a221b499:ended state=COMPLETED items=COMPLETED:3 next=dispatch brief --wi WI-0117 (bundle WI-0110)
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

### Lanes count live work only (release 30)

`max_concurrent_lanes` (default 2) counts top-level work items (not items inside a bundle) that are **active
and alive**: status dispatched, staged, rework or verified **and** either a live agent (`agent.started` without
`agent.finished` after the latest dispatch; a deferred start of the role counts as starting) or a scheduler job
not known to have ended, on the item or on an item of its bundle. A job counts as ended when a terminal state
is recorded, when `sacct` reports one at the time of the check (one batched call for all runs, as in `slurm
watch`), or when the run is stale (older than `watch_stale_days` and `sacct` has no state for it); a run the scheduler cannot report
counts as live until its state is recorded. Staged, rework and verified items waiting for the desk, and orphaned bundles
(manager ended, no child agent or job alive), hold no lane. `dureh status` lists each holder (`Lane: WI-0110
pm-projects [dispatched] held by pipeline-runner agent running on WI-0117; job 3761206 RUNNING on WI-0117
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
- **Job.** ONE job per verification: `sbatch --parsable --job-name=dureh-verify-<WI> --time=<checks.time, else
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
  item: its manager dispatches the checker).
- **Collect** reads each check's `.exit/.ms/.out` files and records the verification exactly as the synchronous
  path (`verification.recorded`, then `wi.verified` or `rework.requested`; the record adds `collected_from`). It is
  refused, recording nothing, while the job is pending or running, when the WI head or its declared checks changed
  since submission, or when the run was already collected.
- **Lanes.** A pending verify job is a running scheduler job: it holds its item's lane until its end is recorded.
- **Rework briefs.** `dispatch brief` for an item with rework > 0 appends a "Last verification" block from the
  latest failed verify run: failed check ids and commands, exit codes, the last 20 output lines of each, per-check
  log paths and the job logs, violations and missing outputs. The block is outside the 2048-byte brief budget
  (which covers the desk-written part) and capped at 4 KB.
- The synchronous `verify --wi` stays for the local runner and small checks. Never run verify in the background.

## Workflows loop at file level: `--each-from-bids --level file` (release 42, Director 2026-10-06)

Director, 2026-10-06: "회사가 아직 slurm의 병렬실행을 충분히 활용 못하고 있는 것 같습니다. 모든 workflow는 file 기준으로 loop를
돌리라고 하고". Every workflow step is submitted as one array with one task per file, all files at once:

```
dureh slurm submit --wi <WI> --script <step.sh> --each-from-bids <dataset> --level file --suffix bold [--datatype func]
                   [--match 'sub-01/*'] [--sub 01 02] [--ses 1] [--dry-run]
```

- **Enumeration is by name only.** The harness walks the dataset's `sub-*` trees (skipping `derivatives/`,
  `sourcedata/`, `code/`, `stimuli/`, `phenotype/` and dot directories) and lists the files whose BIDS suffix (the
  last `_` part of the stem, the name before its first `.`) is `--suffix`. Nothing is opened or fetched: a
  git-annex file that is not fetched is a dangling symlink, which is a name like any other. Filters: `--datatype`
  (the file's directory: `func`, `anat`, `dwi`...), `--match GLOB` on the path relative to the dataset, `--sub`
  and `--ses` labels (with or without `sub-`/`ses-`). One item per stem: a sidecar (`.json`, `.tsv`, `.bval`,
  `.bvec`) never stands for the data file beside it.
- **Items** are the file stems (`sub-01_ses-1_task-rest_run-01_bold`), `array.level` = `file`. The task gets, under
  the configured names (`slurm.each_env`; defaults `DUREH_ITER`, `DUREH_ITER_BY`, `DUREH_ITER_INDEX`,
  `DUREH_ITER_SUB`, `DUREH_ITER_SES`), the stem, `file`, its index and the labels parsed from the stem, plus two
  names for file-level arrays: `path` (default `DUREH_ITER_PATH`, the file's path relative to the dataset) and
  `root` (default `DUREH_ITER_ROOT`, the absolute dataset root). A configured `each_env` exports `path`/`root`
  only when it maps them.
- **Files.** The item list `each-<ts>.items` and the table `each-<ts>.tsv` (`index item sub ses path`) are written
  next to the wrapper in `logs/slurm/<WI>/`; `--dry-run` prints the table (stderr) and writes nothing. The record
  keeps `array: {..., level: file, files, table, root}` and `each_from_bids: {dataset, level, suffix, datatype?,
  match?, sub?, ses?, files}`.
- **The loop rule** (config `slurm.each_level`, default `file`). A `slurm submit` for a step that is not
  file-level is refused unless justified: a bare single job (no `--each*`, no `--after`) is refused with
  "workflows loop at file level (Director 2026-10-06): use --each-from-bids <dataset> --level file --suffix <s>, or
  --single --reason '<why one job>'"; `--level subject|session` needs `--reason '<why>'`; a group step `--after`
  an array is allowed as a single job; a hand-picked `--each <items>` list is allowed. `--via` runs are not
  refused (the module owns the loop): the module's declared level is recorded and a missing one is warned.
  Container builds are single jobs by nature (recorded with their reason). The run record says `level`
  (`file|session|subject|plain|group|single`), `reason` and `single`. `slurm.each_level: subject` makes subject
  the norm (file stays allowed); `any` switches the rule off (the test suites' plumbing jobs).

## One job per subject: `--each`, `--after`, `--resubmit-failed` (release 26)

Director's rule (2026-10-04): the unit of submission is one job per subject, all subjects submitted at once, and
Slurm schedules them (no concurrency cap, no default `%N`). During development each step is submitted per
subject and the next step starts only after every subject job of that step has finished; a workflow that chains
several steps is also one job per subject; group-level steps start after all subject jobs finish. Never loop
`slurm submit` by hand. Since release 42 the unit is the file (above); subject and session arrays stay available
with `--reason`.

- **`--each <items>`** submits ONE Slurm array `--array=0-(N-1)`, one task per item. Items are a comma or space
  list (`--each sub-01,sub-02` or `--each sub-01 sub-02`) or `@file` (one item per line; blank lines and `#`
  comment lines are ignored; the file must lie inside the site). Each item must match
  `^[A-Za-z0-9][A-Za-z0-9._-]*$`; other items, duplicates and an empty list are refused. A script or argument
  that sets `--array` itself is refused. `--max-parallel N` adds `%N`; there is no default cap.
- **Levels (provisional).** An item starting with `sub-` must be exactly `sub-<label>` (a subject-level job) or
  `sub-<label>_ses-<label>` (a session-level job), labels `[A-Za-z0-9]+`; any other `sub-` item is refused
  (`sub-01_run-1`, `sub-01_ses-1_acq-x`). Every other item is plain and not parsed (`dim-20`, `SIGMA`,
  `JDRS001LNA`); its subject and session variables are exported empty. One level per array: mixing is refused.
  Recorded as `array.level` = `subject|session|plain`.
- **`--each-from-bids <dataset> --level subject|session`** takes the items from the directory names of a BIDS
  dataset inside the site: `sub-*/` or `sub-*/ses-*/` as `sub-X_ses-Y`. Only the listing is read. The default
  level is config `slurm.each_level` (`file`, above); these two need `--reason`. Recorded as
  `each_from_bids: {dataset, level}`.
- **Item mapping and names.** The job script runs once per task with five variables exported: the item, the
  array level (`by`: `subject|session|plain`, as `array.level`), its 0-based index in this run's list, and its
  subject and session labels without `sub-`/`ses-` (empty when the item has none). The names are the company's:
  config `slurm.each_env` = `{"item": NAME, "by": NAME, "index": NAME, "sub": NAME, "ses": NAME, "path": NAME,
  "root": NAME}` (`by`, `sub`, `ses`, `path`, `root` optional; only the configured names are exported; `path` and
  `root` are set for file-level arrays). Without it the defaults are `DUREH_ITER`, `DUREH_ITER_BY`,
  `DUREH_ITER_INDEX`, `DUREH_ITER_SUB`, `DUREH_ITER_SES`, `DUREH_ITER_PATH`, `DUREH_ITER_ROOT`. The record keeps the names as
  `array.env`. For camri-org: `"each_env": {"item": "CAMRI_ITER", "by": "CAMRI_ITER_BY", "index":
  "CAMRI_ITER_INDEX", "sub": "CAMRI_SUBJECT", "ses": "CAMRI_SESSION"}`. The harness writes the list to
  `logs/slurm/<WI>/each-<utc ts>.items` (fixed for the run) and a wrapper `each-<utc ts>.sbatch` next to it that
  carries the script's `#SBATCH` lines, maps `$SLURM_ARRAY_TASK_ID` to the item (exit 98 when a task has no item)
  and runs the script with its own interpreter. With `--module` the module wrapper does the same mapping.
- **Logs.** `<slurm.log_dir>/<WI>/%x-%A_%a.out|.err` (one pair per task: `dureh-<WI>-<array job>_<task>.out`); a
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
  gets `item_states` (and `failed_items`), recorded once: a later `status` returns the same record. An overall
  COMPLETED is what `dureh promote --run` needs.
- **Workday start check.** One `sbatch --test-only` for the whole array; the expected end is its first task's
  expected start plus the per-task `--time` (precedence as for single jobs: sbatch argument, then the script's
  `#SBATCH --time`, then config `slurm.time`). The record says `per_task: true`. Tasks that wait for resources
  start later than the check assumes.
- **Purge.** `dureh purge --wi <WI>` removes the task logs (`%A_%a` resolved for every task), the item list and
  the wrapper.

Example (camri-org names configured as above): two per-subject steps and a group step during development.

```
S=bin/dureh
$S slurm submit --wi WI-0120 --script checkpoints/phase-d/step1.sh --module camri --each @checkpoints/phase-d/subjects.txt
#   -> run-A (array 5 tasks)
$S slurm submit --wi WI-0120 --script checkpoints/phase-d/step2.sh --module camri --each @checkpoints/phase-d/subjects.txt --after run-A
$S slurm submit --wi WI-0120 --script checkpoints/phase-d/group.sh --module camri --after run-B
$S slurm wait --run run-B                       # per-item states, overall state
$S slurm submit --wi WI-0120 --resubmit-failed run-A --script checkpoints/phase-d/step1-fixed.sh
```

A job script for `--each` reads its subject from the environment:

```
#!/bin/bash
#SBATCH --time=02:00:00
#SBATCH --cpus-per-task=4
set -euo pipefail
: "${CAMRI_SUBJECT:?submit with --each}"
uv run --frozen --project "${CAMRI_PROJECT}" camri run fmriprep --root . --subjects "${CAMRI_SUBJECT}" ...
```

A chained workflow (several steps in one camri call) is the same: one `--each` array over subjects.

## A module's own submit command: `--via` (release 42, proposals/module-submit-contract.md)

Director, 2026-10-06: every CLI a company module ships is free of dureh; item levels, BIDS filters and the module's own
environment names belong to the module. The harness then does two things only: it runs the module's submit command
inside the WI context and ingests the record that command writes. `--script` (with `--each`) stays the generic path
for plain scripts and is unchanged.

```
dureh slurm submit --wi WI-0120 --via 'camri run fmriprep step1 --workflow fmriprep.default --level subject --submit --detach'
```

- **Who, where, how.** The same guards as a plain submit: pipeline-runner (or the desk), a dispatched WI of that
  role. The command (one quoted string, split like a shell) runs with cwd = the WI's job root (the target project
  or module, where `--chdir` points for a plain submit) and the site environment (`site.env()`: `DUREH_SITE`,
  `env.path_prepend`, `env.vars`); nothing DUREH-specific is required of the module. stdout/stderr are captured;
  config `slurm.via_timeout` (default 600 s) bounds the command. `--dry-run` records `status: not-run` with the
  command the harness would run and runs nothing.
- **What the harness may add to the command** comes only from config, never from a module name in code:
  `modules[<m>].submit: {record_arg, log_arg, after_arg, items_arg}` (each optional, each one option name of the
  module's CLI). The entry is the `modules[]` item whose `cli` (or `id`) is the command's first word (basename), or the
  one named with `--via-module <m>` (needed for wrapped commands such as `uv run camri ...`). `record_arg <path>`
  tells the module where to write its submission record (`<slurm.log_dir>/<WI>/via-<utc ts>.json`, or the `--record`
  path), `log_arg <slurm.log_dir>/<WI>/` where its logs go, `after_arg <job id>` once per job id of each `--after`
  run, `items_arg @<file>` the items to rerun (`--resubmit-failed`). Without a declared option nothing is passed:
  the module decides and the record says where things are. camri-org:
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
  is JSON in the minimal schema below; a module's own schema (camri: `camri.submission.v1`) is a superset, unknown
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
  subject/session/plain by the `--each` grammar, else `plain` (a module's file-level stems are not parsed).
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
  array's `items`/`counts`, and when final `item_states`, `failed_items`, `failed_jobs` (recorded once). `promote
  --run` needs the overall COMPLETED. `slurm cancel` cancels every job. `watch` reports the submission as one line
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

A job script that points at the live checkout (`CAMRI_PROJECT=$DUREH_SITE/modules/camri`) runs whatever that
checkout holds when Python imports it; a promotion during the job changes code under a running job (measured:
job 3748401 started on camri 1eeab74 while modules/camri moved to 8333b22 and 69f83b3). With `--module <m>`
the job runs against a frozen copy instead:

- **Revision.** `--rev`, else the pin in the WI target project's `camri-manifest.yaml`:
  `provenance.tool_versions.<m>`, written as `"<version>+<sha>"` (for example `"2.0.0a1+2396359"`; a bare sha
  or `+g<sha>` also count). It is resolved to the full sha in `modules/<m>`. A `--rev` that differs from the
  pin is allowed and recorded as `rev_override: true`. With neither (a module WI, or no pin) the submit is
  refused.
- **Tree.** At submit time `git -C modules/<m> archive <rev> | tar -x` into
  `scratch/exec/<WI>-<m>-<rev7>-<utc ts>/` under the site, then `chmod -R a-w`. No `git worktree`: nothing is
  written into the module's `.git`. `export-ignore` paths are absent, as in container builds.
- **Environment.**
  - `shared-venv` when the tree's `uv.lock` is byte-identical to `modules/<m>/uv.lock` and
    `modules/<m>/.venv` exists (and its interpreters are present): the job activates `modules/<m>/.venv`
    (`VIRTUAL_ENV`, `PATH`, `UV_PROJECT_ENVIRONMENT`) and prepends the tree's package directory (`src/` for a
    src layout) to `PYTHONPATH`, so imports come from the tree, not the editable install of the live checkout.
  - `rebuild` otherwise: the job first runs the module's `env_rebuild` (config `modules[<m>].env_rebuild`,
    else `env.rebuild`) in the tree with `UV_PROJECT_ENVIRONMENT=<tree>.venv` (a writable sibling), and fails
    with exit 97 when that fails. No `env_rebuild` configured is refused at submit time.
  - Both set `UV_NO_SYNC=1`, so `uv run --project "$CAMRI_PROJECT" ...` in the job uses that environment and
    never syncs (never changes) it.
  - Trade-off: `shared-venv` starts at once and needs no network or cache, but shares the live `.venv`; a
    dependency change in the live checkout during the job (an `env repair` or `uv sync` there) still reaches
    the job, and package metadata (`importlib.metadata.version`) reports the live install. The lock check
    only guarantees the two locks matched at submit time. `rebuild` gives the job its own environment matching
    the pinned lock, at the cost of time and disk per job and a uv cache (use `--offline` in `env_rebuild`
    where nodes have no network). Rule of thumb: the same lock is the common case and the cheap one.
- **Exports.** The module's own names from config (Director 2026-10-04: variables a company's job scripts read
  carry the company's names): `modules[<m>].root_env=<tree>`, for example `"root_env": "CAMRI_PROJECT"`, and,
  only when the entry also sets `rev_env` (for example `"CAMRI_REV"`), `<rev_env>=<full sha>`. Only when no
  root_env is configured does the wrapper export the fallbacks `DUREH_MODULE_ROOT=<tree>` and
  `DUREH_MODULE_REV=<full sha>` (the submit warns). `rev_env` without `root_env` is refused. The record keeps the
  names as `module.env: {root, rev}`. (Release 25 exported `DUREH_MODULE_ROOT`/`DUREH_MODULE_REV` always, plus
  root_env; a script that reads `DUREH_MODULE_ROOT` on a site with root_env set must switch to the root_env name.) A job script that assigns the module root itself (`CAMRI_PROJECT=...`, `export <root_env>=...`, or
  any assignment pointing at `$DUREH_SITE/modules/<m>`) overrides the export; the submit warns with the line
  and never edits the script. Use `"${CAMRI_PROJECT}"` or `"${DUREH_MODULE_ROOT}"` in the script instead.
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

Site set-up for camri: add `"root_env": "CAMRI_PROJECT", "rev_env": "CAMRI_REV"` to the camri entry of
`config.json` `modules`, and change job scripts to read `CAMRI_PROJECT` instead of setting it (or reading
`DUREH_MODULE_ROOT`).
