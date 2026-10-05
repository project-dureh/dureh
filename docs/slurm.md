# Scheduler jobs: `dureh slurm`

```
dureh slurm submit --wi <WI> --script <job.sh> [--sbatch-arg ARG ...] [--module <m> [--rev <rev>]] [--dry-run]
                   [--each <items> | --each-from-bids <dataset> [--level subject|session] | --resubmit-failed <run>]
                   [--after <run>[,<run>...]] [--max-parallel N]
dureh slurm status --run <run id>
dureh slurm wait   --run <run id> [--timeout S] [--interval S]
dureh slurm cancel --run <run id>
```

Without `--module`, `submit` works as before: the job script is submitted as it is (job directory = the WI
target, logs under `logs/slurm/<WI>/` unless the script chooses its own, config `slurm.time` as the fallback
`--time`, the workday start check) and a `run` record is written.

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
  [checks.sbatch_args...] logs/slurm/<WI>/verify-<utc ts>.sh`. The work root is the WI worktree (the target root
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

## One job per subject: `--each`, `--after`, `--resubmit-failed` (release 26)

Director's rule (2026-10-04): the unit of submission is one job per subject, all subjects submitted at once, and
Slurm schedules them (no concurrency cap, no default `%N`). During development each step is submitted per
subject and the next step starts only after every subject job of that step has finished; a workflow that chains
several steps is also one job per subject; group-level steps start after all subject jobs finish. Never loop
`slurm submit` by hand.

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
- **`--each-from-bids <dataset> [--level subject|session]`** takes the items from the directory names of a BIDS
  dataset inside the site: `sub-*/` (default) or `sub-*/ses-*/` as `sub-X_ses-Y`. Only the listing is read; it produces subject or session items only.
  Recorded as `each_from_bids: {dataset, level}`.
- **Item mapping and names.** The job script runs once per task with five variables exported: the item, the
  array level (`by`: `subject|session|plain`, as `array.level`), its 0-based index in this run's list, and its
  subject and session labels without `sub-`/`ses-` (empty when the item has none). The names are the company's:
  config `slurm.each_env` = `{"item": NAME, "by": NAME, "index": NAME, "sub": NAME, "ses": NAME}` (`by`, `sub`,
  `ses` optional; only the configured names are exported). Without it the defaults are `DUREH_ITER`,
  `DUREH_ITER_BY`, `DUREH_ITER_INDEX`, `DUREH_ITER_SUB`, `DUREH_ITER_SES`. The record keeps the names as
  `array.env`. For camri-org: `"each_env": {"item": "CAMRI_ITER", "by": "CAMRI_ITER_BY", "index":
  "CAMRI_ITER_INDEX", "sub": "CAMRI_SUBJECT", "ses": "CAMRI_SESSION"}`. The harness writes the list to
  `logs/slurm/<WI>/each-<utc ts>.items` (fixed for the run) and a wrapper `each-<utc ts>.sbatch` next to it that
  carries the script's `#SBATCH` lines, maps `$SLURM_ARRAY_TASK_ID` to the item (exit 98 when a task has no item)
  and runs the script with its own interpreter. With `--module` the module wrapper does the same mapping.
- **Logs.** `logs/slurm/<WI>/%x-%A_%a.out|.err` (one pair per task: `dureh-<WI>-<array job>_<task>.out`), unless
  the script chooses its own `-o/-e` (then keep `%a` or `%j` in the name; the submit warns otherwise).
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
