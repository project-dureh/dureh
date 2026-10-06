# Site configuration keys (`config.json`) and package `config_defaults`

`config.json` is written by `dureh install` from the harness defaults, the startup package's `config_defaults` and the
install preset (in that order of precedence: the preset wins, then the package, then the harness). It is read-only for
agents; the desk edits it in a merge window or with the Director's OK (upstream edits a company's config as harness
work, rule 1 of the workspace). This page lists the keys that matter to `slurm`, `container` and the project guidance;
the Slack and workday keys are in docs/slack.md and docs/workday.md.

## Rule: no company names in harness code (UP-0069, Director 2026-10-06)

A harness string that names a company's module, manifest or variable is a config value or an example labelled as
such; `tests/test_no_company_names.py` greps `dureh_core/` for `camri` outside a minimal allow-list of help-text
examples. Harness defaults are generic; a startup package supplies its own values through `config_defaults`.

## Keys

| key | default | read by |
|---|---|---|
| `project.manifest` | `manifest.yaml` | the one project manifest the harness reads: `--module` revision pin (`provenance.tool_versions.<m>`, exectree), the generated project CLAUDE.md (projectgen), the first file `container retire` scans. `null` turns these off (`--module` then needs `--rev`). |
| `project.pipeline_version_key` | `version` | key of a manifest `pipelines[]` entry that names the pipeline's software version (projectgen) |
| `project.derivative_entry` | `{family}_{species}_{suffix}` | `str.format` of a `pipelines[]` entry giving the derivative it produces (projectgen "produced by"); unknown fields become `?` |
| `containers.cache` | unset | the container cache directory (relative: under the site) |
| `containers.cache_env` | `DUREH_CONTAINER_CACHE` | env var (config `env.vars` or the process) that names the cache when `containers.cache` is unset; `null` = no env lookup |
| `containers.manifest_globs` | `["*manifest*.yaml", "*manifest*.yml", "*manifest*.json"]` | top-level project files `container retire` scans for a pinned sha256, after `project.manifest` |
| `containers.module`, `containers.build_args`, `containers.time`, `containers.mem` | see docs/containers.md | container build jobs |
| `slurm.account`, `slurm.partition`, `slurm.time` | unset | sbatch defaults (a script or argument `--time` wins) |
| `slurm.log_dir` | `logs/slurm` | where scheduler logs go (docs/slurm.md) |
| `slurm.each_env` | DUREH_ITER names | the variable names an `--each` task gets its item under |
| `slurm.via_timeout` | `600` | seconds a `--via` module command may take to submit and exit |
| `modules[].id`, `path`, `source`, `rev`, `branch`, `env_rebuild` | written by `site link-module` | the site's modules |
| `modules[].root_env`, `modules[].rev_env` | unset | names a `--module` job exports the pinned tree and revision under |
| `modules[].cli` | the id | the command word that selects this entry for `slurm submit --via` |
| `modules[].submit` | `{}` | `{record_arg, log_arg, after_arg, items_arg}`: the options of the module's own submit command the harness may add (docs/slurm.md "via") |

## `config_defaults` in a startup package

`startup.yaml` may carry a `config_defaults` block with any of `project`, `containers`, `slurm`, `checks`, `env`,
`limits`, `guards`, `index` (mappings) and `modules` (a list of `{id, ...}` entries). It never carries the site
identity, harness root, models or state backend. `validate_package` refuses other keys or shapes.

- **Install.** `install._config` merges it below the preset: a key the preset sets stands; a mapping is merged key
  by key; `modules` entries are matched by id (a later `site link-module <id>` keeps the seeded keys, for example
  `submit`); lists are not merged.
- **Existing sites.** `dureh adapter sync` reads the block from the package source tree (`startup.source`, else the
  site's `startup/` copy) and adds the keys the site's `config.json` lacks, once; a value the site has is never
  changed. It prints `config.json: added from the package's config_defaults: ...` when it did.

The block the camri-org package (`startup-packages/camri-org/startup.yaml`) needs, so that today's behaviour is kept:

```yaml
config_defaults:
  project:
    manifest: camri-manifest.yaml
    pipeline_version_key: camri_version
    derivative_entry: "{family}_camri_{species}_{suffix}"
  containers:
    cache_env: CAMRI_CONTAINER_CACHE
    manifest_globs: ["*manifest*.yaml", "*manifest*.yml", "*manifest*.json"]
  modules:
    - id: camri
      submit:
        record_arg: --submission-record
        log_arg: --log-dir
        after_arg: --after
        items_arg: --items
```

Until the package carries it and `adapter sync` has run at the site, the same values go into the site's
`config.json` by hand (`project`, `containers.cache_env`, `containers.manifest_globs`, `modules[camri].submit`).
