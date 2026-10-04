# Scheduler jobs: `dureh slurm`

```
dureh slurm submit --wi <WI> --script <job.sh> [--sbatch-arg ARG ...] [--module <m> [--rev <rev>]] [--dry-run]
dureh slurm status --run <run id>
dureh slurm wait   --run <run id> [--timeout S] [--interval S]
dureh slurm cancel --run <run id>
```

Without `--module`, `submit` works as before: the job script is submitted as it is (job directory = the WI
target, logs under `logs/slurm/<WI>/` unless the script chooses its own, config `slurm.time` as the fallback
`--time`, the workday start check) and a `run` record is written.

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
- **Exports.** `DUREH_MODULE_ROOT=<tree>`, `DUREH_MODULE_REV=<full sha>` and, when config names one, the
  module's own variable: `modules[<m>].root_env`, for example `"root_env": "CAMRI_PROJECT"` on the camri
  entry. The harness never guesses it. Without it, only `DUREH_MODULE_ROOT` points at the tree (the submit
  warns). A job script that assigns the module root itself (`CAMRI_PROJECT=...`, `export <root_env>=...`, or
  any assignment pointing at `$DUREH_SITE/modules/<m>`) overrides the export; the submit warns with the line
  and never edits the script. Use `"${CAMRI_PROJECT}"` or `"${DUREH_MODULE_ROOT}"` in the script instead.
- **Wrapper.** The harness writes `<tree>.sbatch`: the script's `#SBATCH` lines, the exports and environment
  set-up, then the original script run as a child with its own interpreter (its shebang). That wrapper is what
  `sbatch` receives; the record keeps the original `script` and its sha256, plus `wrapper`.
- **Cleanup.** The wrapper's `trap ... EXIT` makes the tree writable again and removes the tree and
  `<tree>.venv` when the job ends. A failed `sbatch` (or a refused start check) removes them at once.
  `dureh purge --wi <WI>` removes any leftovers (`scratch/exec/<WI>-*`, for example after a node failure).
- **Record.** The run record gets `module: {name, rev, rev_override, tree, env_mode, manifest_pin, manifest,
  root_env}` and any `warnings`.

Site set-up for camri: add `"root_env": "CAMRI_PROJECT"` to the camri entry of `config.json` `modules`, and
change job scripts to read `CAMRI_PROJECT` instead of setting it.
