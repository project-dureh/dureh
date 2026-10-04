# Container builds

Images are built only as scheduler jobs, from a tracked recipe and a pinned module revision
(UR-20261004-02, UR-20261004-03). No role runs `apptainer build` in a session, and no role writes the container
cache.

```text
dureh container build --wi <WI> --module <m> --rev <rev> [--name N]                 # module-owned recipe (canonical)
dureh container build --wi <WI> --module <m> --rev <rev> --recipe projects/<p>/code/containers/<x>.def
                      [--version V] [--name N] [--time HH:MM:SS] [--mem 16G] [--dry-run]
dureh slurm wait --run <run id>
dureh container status --wi <WI>
dureh container retire --sif <file name> --reason R [--dry-run] [--wi <WI>]
```

## Recipes

- **Module-owned (canonical).** A module keeps its recipes at `modules/<m>/containers/<name>.def`, written by
  py-writer on a module WI (`modules/*/containers/**` is in its scope) and promoted like any module change.
  Without `--recipe`, the build reads `containers/<m>.def` (or `containers/<N>.def` with `--name N`) from inside
  `git archive <rev>` of the module, the same archive that is the build context, and builds it from the archive root,
  so `%files` paths are module-relative (`src /opt/<m>/src`). A rev without that file is refused; commit the recipe
  in the module first.
- **Project-local (optional).** `--recipe projects/<p>/code/containers/<x>.def` keeps release-23 behaviour for
  images that belong to one project: the recipe must be tracked with exactly this content at the WI base or the
  project's HEAD (a git blob, or a git-annex file, locked symlink or unlocked pointer, whose SHA256 key matches the
  local content). It is copied into the context root. Project copies of module recipes are no longer needed.

## Who and which WI

- pipeline-runner on its own dispatched WI (or the desk).
- Module recipe: the WI may target `modules/<m>` (the module being built) or any project; the target is recorded
  as `wi_target`. A project WI keeps the job directory in `projects/<p>/checkpoints/<WI>-container-<target>/`; a
  module WI keeps it under `logs/containers/<WI>-container-<target>/`, so the module tree stays clean.
- Project recipe: the WI must target the recipe's project.

## Build

- Context: `git -C modules/<m> archive <rev> | tar -x`; `<rev>` is recorded in full.
- Version: the module's `pyproject.toml` `[project] version` at `<rev>`, unless `--version`.
- Target: `<cache>/<name>_<version>-<rev7>.sif` (name defaults to the module id for module recipes, the recipe
  stem for project recipes). The build is refused when the target or its provenance file exists: images are never
  overwritten.
- Cache: config `containers.cache`, else `env.vars.CAMRI_CONTAINER_CACHE`. The hook refuses every role's
  writes there; only the build job (and `container retire`) changes it.
- Job: the harness writes `build.sh` in the job directory and submits it with `slurm submit` (run record, logs
  under `logs/slurm/<WI>/`, workday start check). The job uses node-local `$TMPDIR` as `APPTAINER_TMPDIR`, keeps
  the site module's `APPTAINER_CACHEDIR` (or `containers.apptainer_cachedir`), runs
  `apptainer build <containers.build_args, default --fakeroot> <cache>/.<target>.partial-<job> <recipe>`, hashes it,
  writes `<target>.provenance.json`, then `mv -n` to the target. If the target appeared meanwhile the partial is
  left and the job fails.
- Provenance: `recipe_source` (`module` or `project`), `recipe_path` (`modules/<m>/containers/<name>.def` or
  `projects/<p>/code/containers/<x>.def`), `recipe_rev` (the full source rev for module recipes; the project
  commit for project recipes), `recipe` (= `recipe_path`), `recipe_blob`, `recipe_annex_key`, `recipe_sha256`,
  `wi_target`, `project`, module, rev, version, job id, apptainer version, built_at, host, sha256, size.
- Record: after the job ended, `container status` re-hashes the image, compares it with the provenance and
  appends one `container.built` event (refs the run). The data-steward then pins the digest in the manifest
  (`sha256:<hex>`).
- Musl bases (alpine) need `"build_args": ["--fakeroot", "--ignore-fakeroot-command"]`; glibc bases build
  with `--fakeroot` on Longleaf (probe 2026-10-04, apptainer 1.5.0, root-mapped namespace).
- Other config keys under `containers`: `time` (02:00:00), `mem` (16G), `cpus` (2), `module` (`apptainer`,
  loaded only when apptainer is not on PATH; empty to skip).

## Retire

`dureh container retire --sif <file name> --reason R` (desk or data-steward; `--wi` records it on the
data-steward's dispatched WI) removes an image nothing pins:

- The name is a file name in the cache (no slashes, ending in `.sif`). Unknown names are refused.
- The sha256 is measured from the image and also read from its provenance. Every `projects/<p>/camri-manifest.yaml`
  and any other top-level `projects/<p>/*manifest*.{yaml,yml,json}` is searched for the hex digest (bare or
  `sha256:<hex>`, any case).
- Pinned: refused, listing the pinning manifests. `--dry-run` lists them (or what would be removed) and changes
  nothing.
- Not pinned: the provenance moves to `<cache>/retired/<name>.provenance.json` with `retired_at`, `reason` and
  `by` added (never overwritten), the SIF is deleted, and one `container.retired` event is recorded (refs the
  `container.built` event when there is one). Nothing outside the cache directory is touched.
