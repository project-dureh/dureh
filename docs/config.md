# Site configuration keys (`config.json`), the startup package (`startup.yaml`) and `config_defaults`

`config.json` is written by `dureh install` from the harness defaults, the startup package's `config_defaults` and the
install preset (in that order of precedence: the preset wins, then the package, then the harness). It is read-only for
agents; the desk edits it in a merge window or with the Director's OK (upstream edits a company's config as harness
work, rule 1 of the workspace). This page lists the keys that matter to `slurm`, `push`, the session job and
the project guidance, and the startup package interface (schema 2, release 46); the Slack and workday keys are in
docs/slack.md and docs/workday.md.

## Rule: no company or domain names in the harness (UP-0069, Director 2026-10-06; widened in release 46)

A harness string that names a company's module, manifest or variable is a config value or a package value. Since
release 46 `tests/test_no_company_names.py` scans `dureh_core/`, `org/`, `docs/`, the README and the tests for
registered company names, domain data-standard and layout words, and the roles release 46 removed, outside a minimal
allow-list. Harness defaults are generic; a startup package supplies its own values (roles, labels, `config_defaults`).
Tests prove the mechanism with the neutral example package `tests/fixtures/example-org`.

## Keys

| key | default | read by |
|---|---|---|
| `startup.source` | the install's `--startup` path | the package source tree `adapter sync` re-copies `startup/` from (release 46, "Single package source" below) |
| `startup.rev` | written by install and `adapter sync` | the package revision installed in `startup/`: a git commit, or `sha256:<hex>` of the tree for a source without git; `deployment.json` pins the same |
| `project.manifest` | `manifest.yaml` | the one project manifest the harness reads: the `--module` revision pin (`provenance.tool_versions.<m>`, exectree). `null` turns it off (`--module` then needs `--rev`). Release 46: `project.pipeline_version_key` and `project.derivative_entry` are unused (a warning names them) |
| `containers.cache` | unset | the container cache directory (relative: under the site); no role writes it (write guard). Release 46 removed `dureh container build/status/retire` and `containers.manifest_globs`, `module`, `build_args`, `time`, `mem` |
| `containers.cache_env` | `DUREH_CONTAINER_CACHE` | env var (config `env.vars` or the process) that names the cache when `containers.cache` is unset; `null` = no env lookup |
| `slurm.account`, `slurm.partition`, `slurm.time` | unset | sbatch defaults (a script or argument `--time` wins) |
| `slurm.log_dir` | `logs/slurm` | where scheduler logs go (docs/slurm.md) |
| `slurm.job_prefix` | the session name (`session.name`, else the site folder name) | prefix of the job names the harness gives: `<prefix>-<WI>` for `slurm submit`, `<prefix>-verify-<WI>` for the checks job of `verify --wi`. A script's own `#SBATCH -J/--job-name` (or `--sbatch-arg -J`) wins for `slurm submit` (docs/slurm.md "Job names") |
| `slurm.each_level` | the package's finest `labels.levels`, else `any` | the loop rule: one of the package's levels or `any` (off). While on, a job without `--each` needs `--after` (a group step) or `--single --reason` (docs/slurm.md "One array per step") |
| `slurm.each_env` | DUREH_ITER names | the variable names an `--each` task gets its item, level, index and item-grammar fields under |
| `slurm.watch` | `monitor` for a site without the key; `on-demand` written by `install` for a new site (release 46) | how the desk learns that a job ended (release 45; docs/slurm.md "Watch modes"): `monitor` keeps a standing `slurm watch` Monitor, `on-demand` keeps none (`slurm wait --run` for short jobs, `slurm status` at a later turn for long ones), `off` names only `slurm status`. Only generated text (CLAUDE.md, role files, hints) follows it; another value fails `adapter sync` |
| `slurm.external_jobs` | `true` | with `slurm.external_match` set, `dureh status` asks `squeue -u $USER` for jobs no harness record owns and lists the matching ones as `External jobs` (release 45; docs/slurm.md); `false` never asks |
| `slurm.external_match` | absent: the union of the package's `scheduler_tools[].job_match` (none without tools) | release 46: name patterns of the external jobs to list (an include list, e.g. a module framework's job-name prefix); empty or absent lists none and asks `squeue` nothing. Replaces `slurm.external_ignore` (ignored with a warning in 46-48; refused from release 49: `adapter sync` and `verify --self` fail, `status` warns) |
| `slurm.via_timeout` | `600` | seconds a `--via` module command may take to submit and exit |
| `checks.local_allow` | `[]` | globs of declared checks an item may run in the verify process with `wi new/amend --checks-runner local` (release 45; docs/slurm.md "Per-item local runner"); empty refuses the per-item choice |
| `checks.local_timeout` | `60` | seconds per check on the per-item local path |
| `limits.requirements_bytes` | `65536` | the largest requirements file `wi new/amend --requirements` pins (release 45; docs/work-items.md) |
| `upstream.watch` | `true` | with `upstream.exchange` set, `false` drops the `upstream inbox --watch` Monitor from the desk text (release 45; docs/upstream-protocol.md) |
| `push.allowed_remotes` | `[]` | remote URLs or names `dureh push` may push to (desk only, with a Director decision bound to the exact revision, remote and ref); empty refuses every push |
| `push.standing_ff` | `false` | release 58 (Director, 2026-10-12): `true` lets the desk run `dureh push --repo <path> --remote <remote> --ref <branch>` without `--approval` when the remote is allowlisted, the branch exists on the remote and its tip is an ancestor of the pushed revision (checked with `git ls-remote`, a fetch into FETCH_HEAD when needed, and `merge-base --is-ancestor`); never forced. The event carries `approval: "standing-ff"` and `fast_forward: true`; a non-fast-forward (or `false`, the default) is refused naming the approval path. `--approval <decision>` works as before. Must be a boolean (`adapter sync` and `verify --self` refuse another value, `status` warns) |
| `push.standing_ff_new_branches` | `false` | release 58: with `push.standing_ff`, `true` also lets the standing push create a branch that does not yet exist on the remote (the event carries `new_branch: true`); `false` refuses it naming the approval path. Must be a boolean |
| `session.name` | the site folder name | the site's session name: session job `cc-<name>[-<purpose>]`, pane log, and the default `slurm.job_prefix` (docs/workday.md) |
| `session.runtime` | `claude-code` | the session runtime the session job starts (`site session plan` and `start`); an unknown name is refused, listing the available ones |
| `session.handoff` | `state/memory/handoff.md` | the desk's handoff file (relative: under the site) that `day close` requires to exist and to be newer than today's start |
| `modules[].id`, `path`, `source`, `rev`, `branch`, `env_rebuild` | written by `site link-module` (and `site new-module`); removed by `site retire-module` | the site's modules |
| (`site retire-module --id <id> [--dry-run] [--force]`) | desk only, release 55 (UR-20261011-02) | retires a module, in this order: the id must be registered; refused while a non-final WI (not promoted, stopped or reverted) targets `modules/<id>`, a lane is held on it or the checkout has a worktree no final WI owns; refused while the checkout has uncommitted changes or a HEAD not reachable from `source` at `branch` (an unreachable source refuses too; `--force` never overrides this); `path-refs` is run and structural references (other modules' configuration files, project manifests, `config.json` outside the module's own entry) refuse unless `--force`, history text is only listed; then the final WIs' worktrees and branches, the entry and `modules/<id>` are removed, in that order, one `site.module-retired` event records `{id, path, source, branch, head, rev, removed, compensation}` with what was actually removed, and the adapter files are regenerated. Release 57 (UR-20261012-01): the event is recorded only after every removal step succeeded; a failure mid-way records `site.module-retire-failed` `{id, path, step, error, removed, compensation}` (`step` is `worktree <WI>`, `config-entry` or `directory`; `removed` lists what was removed so far) and the command fails naming that step, with the module still registered since the entry and the directory go last, so the command can simply be run again; a worktree whose `git worktree remove --force` fails (git deregisters it anyway, for example an untracked `.venv` on NFS with a file held open) is removed by the harness itself (symlinks removed as links, their targets never followed, read-only trees made writable) followed by `git worktree prune`, and a directory git no longer registers is removed the same way; `purge` shares this removal. `--dry-run` lists every step with its paths and the first refusal it would hit (the `record` step after `remove`); the compensation is `site link-module --id <id> --source <source> --rev <head>` |
| `env.rebuild` | unset | the site-wide `env_rebuild` for a module or project entry without its own |
| `env.rebuild_timeout` | `900` | seconds the `env_rebuild` that `dispatch brief` (and `worktree repair` re-provisioning a worktree) runs for a new WI worktree may take; on timeout the brief is refused with the `worktree repair` hint (release 47; docs/slurm.md "Worktree environment at dispatch") |
| `modules[].root_env`, `modules[].rev_env` | unset | names a `--module` job exports the pinned tree and revision under |
| `modules[].cli` | the id | the command word that selects this entry for `slurm submit --via` |
| `models.<tier>` | written by `install` (bootstrap waivers) | per tier (`org/tiers.yaml`: desk, pm, writer-academic, worker, recount): `{model: provider:id, qualification: run-... | waived-bootstrap, waiver_expires, note}`; `dispatch brief` refuses a tier whose waiver expired or that has no qualification; the agent file carries the role's tier model |
| `models.routing` | absent (no routing) | release 54 (D3): kind -> tier name or `null`, kinds `check`, `doc`, `triage`, `build` (docs/work-items.md "Kind and model of a brief"). `null` or a missing kind keeps the role's own tier; an unknown kind or a tier not defined under `models` is refused (`adapter sync` fails, `verify --self` lists it, `status` warns, `dispatch brief` refuses). The academic and rework escalations keep precedence |
| `models.routing_doc_roles` | `["doc-writer"]` | release 54: the roles whose dispatch is kind `doc` |
| `modules[].submit` | `{}` | `{record_arg, log_arg, after_arg, items_arg}`: the options of the module's own submit command the harness may add (docs/slurm.md "via") |
| `members.slurm` | `{cpus: 2, mem: 8G}` | release 64 (docs/member-runtimes.md): `#SBATCH` keys of the member-run job (`partition`, `account`, `qos`, `cpus`, `mem`, `gres`, `time`) over config `slurm`; without `time` the job gets the runtime's own limit plus 15 minutes |
| `members.record_wait_seconds` | `300` | how long the member job waits for `member run` to record its run before it gives up |
| `members.codex.bin` | `codex` | the codex command (on the site's PATH, config `env.path_prepend` included) |
| `members.codex.model` | unset (codex's default) | the model passed with `-m`; recorded as requested |
| `members.codex.reasoning_effort` | `medium` | `model_reasoning_effort` |
| `members.codex.wait_seconds` | `1800` | the review's wait limit; past it the run is NOT_RUN |
| `members.codex.checks_timeout` | `1800` | seconds per declared check the job runs before the review |
| `members.codex.permission_profile` | `dureh_member` | the name of the read-only permission profile passed with `-c` |
| `members.codex.sandbox_read` | detected | extra read-only paths the codex sandbox helper needs (default: `$CODEX_HOME/tmp/arg0` and the native binary's directory) |
| `members.codex.env_pass` | proxy and certificate variables, `CODEX_HOME` | environment variables passed to codex besides the minimal set (HOME, USER, LOGNAME, LANG, TMPDIR, PATH) |
| `members.local.wall_seconds`, `quiet_seconds`, `grace_windows`, `max_turns` | `3600`, `300`, `4`, `24` | the local member's wall clock, quiet timeout, grace windows while the model stays loaded, and tool-loop turns |
| `members.local.write_cap` | `6` | writes per allowlisted file |
| `members.local.test_timeout` | `900` | seconds per declared check `run_tests` runs |
| `members.local.retries` | `1` | retries after a harness error (connection refused or reset) |
| `members.local.think`, `options`, `repetition` | `false`, `{}`, `{tail_chars: 600, max_period: 80, min_repeats: 6}` | the request's `think`; options over the profile's `PARAMETER` lines; the repetition detector |
| `office_hours.enabled` | `false` | `site session start` also starts office hours (release 64) |
| `office_hours.partition`, `account`, `qos`, `gres`, `cpus`, `mem` | config `slurm` for partition and account | the GPU job's `#SBATCH` keys |
| `office_hours.time` | unset | an upper bound for the job's wall time, which otherwise follows the workday clock (the day's end plus the overtime allowance, within the partition's limit) |
| `office_hours.server` | `{kind: ollama, bin: ollama, models_dir: cache/office-hours/models, port: 11434, start_timeout: 180}` | the model server the job starts; `models_dir` relative to the site |
| `office_hours.profiles` | `[]` | `[{name, file, base}]`: each model the server creates (`name`) from a package file (`file`, a role's `profile`), pulling `base` when missing. Unknown keys, a missing file, a duplicate name or a bad port are refused (`adapter sync`, `verify --self`; `status` warns) |

## The startup package: `startup.yaml` schema 2 (release 46)

A company package declares what is about one company or domain; the harness knows roles only by kind (desk,
manager, worker) and by capability. Validated at install, `adapter sync` and `verify --self`
(`install.validate_package`, `package.check`, `Org.load`); errors refuse, warnings are printed (and listed by
`status` and `verify --self`).

```yaml
schema_version: 2
package: {id, name, version, harness_min: 46}      # harness_min: the oldest harness release the package needs
desk:                                              # additive to the harness desk
  may_dispatch: [<worker roles the desk dispatches directly>]
  write_scopes: [projects/*/README.md]             # logical scopes only; site:* scopes are harness-owned
roles:
  <name>:                                          # ^[a-z][a-z0-9-]{1,30}$; desk reserved; checker re-skin only
    kind: manager | worker
    tier: <tier in org/tiers.yaml>
    description | output | never: <text>
    isolation: worktree | scratch                  # scratch: no WI worktree; writes its scope in the target main tree
    write_scopes: [<logical glob under modules/, projects/, package/, harness/>]
    may_dispatch: [<workers>]                      # managers only
    capabilities: [submit_jobs, verify, network]
    project: true                                  # also a project-level agent file (projectgen)
    skill: skills/<x>/SKILL.md                     # the role's skill (brief); must exist in the package
    instructions: [<extra agent-file lines>]
    runtime: claude-code | codex | local           # release 64; codex: verify roles only; local: worktree workers
    runtime_fallback: claude-code | none           # what `member run --fallback` runs instead (default claude-code)
    profile: profiles/<x>.Modelfile                # local only: the model profile, a file in the package
values: [{id, title, skill, manager}]              # manager: the role a bootstrap item of that value goes to
policies:
  loop_rule: {text: <desk/agent text>, refuse: <added to the loop-rule refusal>}
protected:
  - {path: <site-relative glob>, level: no-read | read-only | none, why: <text>}
labels:
  levels: [<finest>, ..., <coarsest>]              # slurm.each_level is one of these (or any)
  layout: {staging: <dir>, final: <dir>, reports: <dir>}   # defaults: checkpoints, (none), reports
  item_grammar: {<level>: <regex with named groups>}       # --each item levels and fields
templates: {project: templates/project}            # holds CLAUDE.md.template for linked projects
guards: {scheduler_only_commands: [...]}           # additive only
scheduler_tools:                                   # the company's own scheduling commands (compute route B)
  - {name: <word>, commands: [<Bash command glob>, ...], exclude: [<glob>, ...], job_match: [<job-name glob>, ...],
     description: <text>}
config_defaults: {project, containers, slurm, checks, env, limits, guards, modules, index}
lessons | bootstrap | dependencies | state: as before
```

- **Capabilities** (closed set): `submit_jobs` (`slurm submit/status/wait/resubmit-failed`, `run.status`,
  `run.completed`, `slurm.via-failed`), `verify`
  (`verify --wi`, `verification.recorded`, `verification.superseded`, `wi.verified`), `network` (WebFetch,
  WebSearch). A role's tools come from its kind, capabilities and scopes; the events it may record from its kind's
  list plus its capabilities' lists
  (`org/roles.yaml` `event_authority`, `capability_authority`). The desk holds `submit_jobs`, `verify`, `network`.
- **No `promote` capability** (Director, 2026-10-09): release 46 removed the `dureh promote` data path (run
  promotion, `--metadata`, `--kind report`); landing results in a final area is the company's own tooling. `dureh wi
  promote` (Gate 2, `contrib/` branches) is unrelated and stays. `labels.layout.final` is only a label (`path-refs`
  skips it).
- **Scheduler tools** (Director, 2026-10-09, compute route B). A company whose own framework schedules its own
  compute declares it in `scheduler_tools:`. There is no capability gate: any agent may run a declared command (the
  framework's workflows submit to Slurm themselves and must keep working). The guard matches the parsed command
  segment it already sees (after `&&`/`;`, `NAME=value` prefixes and wrappers such as `timeout`); a matching
  command is not held by the package's `scheduler_only_commands`. `exclude` globs (internal entry points) win over
  `commands` and are refused for every agent (`guard.denied`). Direct `sbatch`/`srun`/`salloc`/`scancel` and
  container runtimes stay denied; a `commands` glob may not start with one of them (or with `dureh`), so a
  container-runtime launcher in front of the tool is refused at validation. Each `job_match` is non-empty. The
  tools' jobs are the package's: `status` lists them as `Package jobs` (name, state, time; the harness writes no
  `run` record), `day close` warns while any is queued or running, and `slurm.external_match` defaults to the union
  of `job_match`. The desk, worker and project texts name the tools instead of "only through `dureh slurm submit`";
  without tools the texts are as before.
- **Runtimes** (release 64, docs/member-runtimes.md): a role's `runtime` is `claude-code` (a subagent, the default),
  `codex` (only a role with `verify`; the checker re-skin may set it with `runtime_fallback`) or `local` (a worker
  without `verify`, `isolation: worktree`, write scopes and a `profile` file in the package). Non-Claude roles run
  through `dureh member run`; the spawn hook refuses spawning them without a recorded `member run --fallback`.
- **Default org** (`org/roles.yaml`): `desk`, a generic `manager` (dispatches `worker`, `checker`), a generic
  `worker` (module `src/`, `tests/`, `docs/`), and `checker` (`verify`). A package's `roles` add roles or replace
  `manager`/`worker`; `checker` may only be re-skinned (skill, instructions, texts, and since release 64 `runtime`, `runtime_fallback`) and keeps `verify` and no write
  scopes. Every manager and every verify role is dispatched by the desk. A package without `roles` runs on the four.
- **Validation** (3.4): unknown top-level keys refuse (since release 49; they warned in 46-48); `harness_min` above this release
  refuses; roles: kind, tier, `may_dispatch` only on managers and only to workers (depth desk -> manager -> worker),
  known targets, every dispatching manager can dispatch a verify role, at least one verify role, a verify role has no
  write scopes; write scopes are segment-aware globs under `modules/`, `projects/`, `package/`, `harness/` and none
  covers a protected path; capabilities from the closed set; skills and the
  project template exist inside the package; no symlinks; protected levels are `no-read`, `read-only` or `none`
  (a `none` rule that overlaps no stricter rule of the package warns).
- **Schema 1 is refused** (release 49; it loaded with a warning in release 46 only): a `startup.yaml` without
  `schema_version: 2` fails install, `adapter sync` and `verify --self` with a pointer to this section. The
  `organization` enable list (`managers`/`workers`) is refused too: the package's `roles` is authoritative. A
  `values[].manager` must name a role of the organization.

### Protected paths

`protected` rules bind every agent, the desk included: `read-only` refuses Write/Edit/NotebookEdit and Bash writes
through the write guard (before any role scope); `no-read` also refuses content reads: Read and Grep with a path
inside the area and Bash content readers (`cat`, `head`, `grep`, `python <file>`, `cp` from it, `< file` ...), a
best-effort word scan (a script that opens the file is not caught). Listing names (Glob, `ls`) stays allowed. The
PreToolUse matcher gains `Read|Grep` only when a `no-read` rule exists. Every refusal is recorded as `guard.denied`.
Harness commands (scheduler jobs) are not agent reads or writes; `path-refs` does not read inside a
no-read area. `dureh protected list [--json]` (allowed with `--observe`) prints each rule with its level and source
(package id, version, revision) and the resolved symlink targets below, or "none declared: interim default
(workspace rule 2)": the harness applies no default of its own.

- **`level: none` and precedence** (Director, 2026-10-09). The most specific matching rule decides, compared
  segment by segment from the left (a literal segment beats a partial wildcard, which beats `*`, which beats `**`;
  at an equal prefix the longer glob wins); on a tie the later rule wins. `none` lifts the package's own rules for
  that path (example: `projects/*/published/** read-only` + `projects/p-test/** none` leaves the test project to
  the role scopes). It never lifts a harness write zone (records, generated files, the installed package, config,
  references, the container cache, worktrees of other items: section 5 invariants), which are checked first.
- **Symlink targets** (Director, 2026-10-09). At install and `adapter sync` the harness walks each protected area
  (rules other than `none`) by names only (bounded depth, entries and link count; nothing is opened) and records
  every symlink in it, the area itself included, with its resolved real path and the rule's level in the generated
  file `.claude/dureh-protected-links.json` (a link a more specific `none` rule lifts, or one whose target is inside
  the site, is not stored). The guard (Read, Grep, Write/Edit through the write guard, and the Bash word scan)
  checks the path as given, its resolved real path against the site-relative rules, and the real path against the
  stored target prefixes. New links are seen at the next `adapter sync`.

### Single package source (release 46, option A)

A site runs a copy of one committed package revision. `install` and `adapter sync [--package-rev REV]` export the
source (`config.json` `startup.source`) with `git archive <rev>` (default: its committed HEAD) into `startup.new/`,
refuse a source with uncommitted or untracked changes, validate the copy, swap it in for `startup/`, and record
`package.synced {from, to, changed}` when the revision or the files changed; `startup.rev` (and `startup.version`)
follow, and `deployment.json` pins the same revision (no more `+dirty`). A source without git is copied and its
revision is a content hash. When `startup.source` is unset or missing, `startup/` is kept as installed. Everything
else (`config_defaults`, skills, roles, templates, the deployment pin) is read from `startup/` only. A failed
validation leaves the installed package untouched. `adapter sync` runs only where it already ran: install, merge
windows and the desk's switch to a landed release.

## `config_defaults` in a startup package

`startup.yaml` may carry a `config_defaults` block with any of `project`, `containers`, `slurm`, `checks`, `env`,
`limits`, `guards`, `index`, `members`, `office_hours` (mappings; the last two since release 64) and `modules` (a list
of `{id, ...}` entries). It never carries the site
identity, harness root, models or state backend. `validate_package` refuses other keys or shapes.

- **Install.** `install._config` merges it below the preset: a key the preset sets stands; a mapping is merged key
  by key; `modules` entries are matched by id (a later `site link-module <id>` keeps the seeded keys, for example
  `submit`); lists are not merged.
- **Existing sites.** `dureh adapter sync` re-copies the package first (above), then reads the block from the
  installed `startup/` and adds the keys the site's `config.json` lacks, once; a value the site has is never
  changed. It prints `config.json: added from the package's config_defaults: ...` when it did.

Example (neutral names):

```yaml
config_defaults:
  project:
    manifest: example-manifest.yaml
  containers:
    cache_env: EXAMPLE_IMAGE_CACHE
  slurm:
    external_match: ["exmod-*"]
  modules:
    - id: exmod
      submit:
        record_arg: --submission-record
        log_arg: --log-dir
        after_arg: --after
        items_arg: --items
```
