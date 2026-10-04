# Container builds

Images are built only as scheduler jobs, from a tracked recipe and a pinned module revision
(UR-20261004-02). No role runs `apptainer build` in a session, and no role writes the container cache.

```text
dureh container build --wi <WI> --recipe projects/<p>/code/containers/<name>.def --module <m> --rev <rev>
                      [--version V] [--name N] [--time HH:MM:SS] [--mem 16G] [--dry-run]
dureh slurm wait --run <run id>
dureh container status --wi <WI>
```

- Who: pipeline-runner on its own dispatched WI (or the desk). The WI's target must be the recipe's project.
- Recipe: under `projects/*/code/containers/`, tracked with exactly this content at the WI base or the
  project's HEAD: a git blob, or a git-annex file (locked symlink or unlocked pointer) whose SHA256 key matches
  the local content. That content is copied into the job's directory at submit time; the provenance keeps the
  blob, the annex key and the recipe's sha256.
- Context: `git -C modules/<m> archive <rev> | tar -x` plus the recipe; `<rev>` is recorded in full.
- Version: the module's `pyproject.toml` `[project] version` at `<rev>`, unless `--version`.
- Target: `<cache>/<name>_<version>-<rev7>.sif` (name defaults to the recipe stem). The build is refused
  when the target or its provenance file exists: images are never overwritten.
- Cache: config `containers.cache`, else `env.vars.CAMRI_CONTAINER_CACHE`. The hook refuses every role's
  writes there; only the build job writes it.
- Job: the harness writes `build.sh` under `projects/<p>/checkpoints/<WI>-container-<target>/` and submits it
  with `slurm submit` (run record, logs under `logs/slurm/<WI>/`, workday start check). The job uses node-local
  `$TMPDIR` as `APPTAINER_TMPDIR`, keeps the site module's `APPTAINER_CACHEDIR` (or `containers.apptainer_cachedir`),
  runs `apptainer build <containers.build_args, default --fakeroot> <cache>/.<target>.partial-<job> <recipe>`,
  hashes it, writes `<target>.provenance.json` (recipe, recipe blob, module, rev, version, job id, apptainer
  version, built_at, host, sha256, size), then `mv -n` to the target. If the target appeared meanwhile the
  partial is left and the job fails.
- Record: after the job ended, `container status` re-hashes the image, compares it with the provenance and
  appends one `container.built` event (refs the run). The data-steward then updates the manifest digest.
- Musl bases (alpine) need `"build_args": ["--fakeroot", "--ignore-fakeroot-command"]`; glibc bases build
  with `--fakeroot` on Longleaf (probe 2026-10-04, apptainer 1.5.0, root-mapped namespace).
- Other config keys under `containers`: `time` (02:00:00), `mem` (16G), `cpus` (2), `module` (`apptainer`,
  loaded only when apptainer is not on PATH; empty to skip).
