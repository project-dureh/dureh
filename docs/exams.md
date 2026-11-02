# Model qualification exams per tier (release 90 candidate)

Director 2026-11-02 (dureh-team#116; proposal `model-qualification-exams-2026-11-02.md` sections 2-7a, section 8
answered with its Recommended line): a model tier runs on a model only with a live bootstrap waiver or a passing exam
of that exact model, and each tier moves to the cheapest model that passes. Tiers are mechanism; which roles map to
them stays the package's choice. The role-level scorer `verify --exam` (org/exams/) is unchanged.

## The gate

`models.<tier>.qualification` is either `waived-bootstrap` (unchanged: passes until `waiver_expires`) or
`run-<id>`. Since release 90 a run id is never taken on trust: `dispatch brief` (and `day handshake`, `status`)
accept it only when

- a `run` record with that id exists, `run_type: qualification`, `status: pass`;
- its `tier` is the tier being dispatched and its `model` is the **exact** `models.<tier>.model` string (a model
  change voids the qualification: exam first, switch second);
- its `expires` (qualified day + 90 days) has not passed.

Anything else refuses the dispatch with the reason (`qualification run-x names no run record`, `... qualified M, not
N: a model change needs a new exam`, `... expired on D`). `dureh status` warns and `day handshake` prints a
`qualifications` line: FAIL for a run id that does not hold, warn 7 days before one ends.

**Fallback (off by default).** `models.fallback_tiers` is an ordered tier path, lowest first, for example
`["recount", "worker", "pm"]`. When set, an unqualified tier on the path runs on the nearest qualified tier above it:
the brief states that tier's model, `dispatch.prepared` carries `fallback_tier: {from, to, reason}`, `dispatch brief`
prints a `# WARN` line and `day handshake` shows the tier as warn (not FAIL). Absent or empty: fail-closed, as before.

## Commands (desk; compare and plan are read-only)

| Command | What it does |
|---|---|
| `dureh exam build --tier T [--count 3-5]` | Freezes an exam set from promoted work items of tier T: build tasks from items whose dispatch ran on T (its own tier, `--tier`, routing, `writer-academic` for academic items) with declared checks, a passing verify run and a recorded base commit that still exists; check tasks (when T is the checker's tier or `models.routing.check`) from recorded verify runs at a staged head that still exists, expected verdict = the recorded one (failures included). Items touching a protected path are left out. Newest first; the newest that needed rework (or a known failed check) is always included. Every task is `split: holdout`. Writes `state/exams/<T>/set-<ts>/` (`set.json` + one `<task>.json`: WI, kind, role, target, commit, checks, outputs, expected verdict, frozen brief and its sha256; the set's sha256) and records `exam.built`. Fewer than 3 eligible items: the example-task fallback below, else refused as `not examinable` with the reason, nothing written. |
| `dureh exam run --tier T --model M [--set S]` | One detached git worktree per task under `scratch/exams/<run>/` at the frozen commit and one brief per task (`scratch/exams/<run>/<task>.brief.md`), headed `DUREH-EXAM <run> task=<id> tier=<T> model=<M>`. Records a `run` record `exam-run` (`run_type: exam`, `status: submitted`) with each brief's digest. The desk spawns each brief unchanged with `subagent_type` = the task's role and `model` = the exact id of M (see Exam spawns). One exam run per site at a time. |
| `dureh exam score --run R [--local]` | Runs every task's declared checks: one scheduler job (the `verify --wi` checks script per task, run in turn by one wrapper), or in this process with `--local` / `checks.runner: local`. Build task: pass = every check exits 0, declared outputs present, a change was made, no write outside the role's write scopes (a hard failure). Check task: pass = the verdict in `<task>.verdict.json` equals the recorded one and no file was changed (hard failure). Example tasks (fixture) are scored here by the role-level scorer. A failing deterministic task without a hard failure is first sent back once (Rerun); otherwise records one `run` record `run_type: qualification` and removes the workspaces; exit 1 on fail. `--no-rerun` scores now (a prepared rerun is dropped and its first attempt counts). |
| `dureh exam compare --tier T \| --all [--json]` | Table tier x model: score, pass, units per task, date (the latest record per model; `*` = incumbent) and the recommendation. |
| `dureh exam plan --tier T \| --all [--json]` | Per tier, the candidates cheapest first on the tier's newest exam set: passed, failed, `needs a run`, `needs a run (incumbent reference)`, or `not needed (a cheaper candidate passed)`. A tier without a set and without enough items or example tasks is `not examinable` with the reason (both counts). |

## Scoring and selection

- **Pass rule:** no hard failure and at least `ceil(0.8 x N)` tasks pass; a candidate replacing the incumbent must
  also score at least the incumbent's score on the same exam set minus one task.
- **Cost:** usage units (tokens x `model_weights`, the units of `dureh usage`) of the agent run records that name the
  exam run (`exam_run`); otherwise `unmeasured`.
- **Recommendation:** among passing models (not expired, no worse than the incumbent minus one task), the lowest units
  per task; when any is unmeasured, the order of `models.candidates` (cheapest first). Default candidates: the
  harness default model table's Haiku, Sonnet and Opus names. `keep` or `switch`.
- **The record:** `{run_type: qualification, status, tier, model, exam_run, harness_release, exam_set {id, path,
  sha256, source}, tasks [...], score "k/N", passed, total, pass_rule, incumbent, runner, units, units_per_task,
  reasons, qualified_on, expires}`; refs the exam run.

## Applying a pass

`exam score` never writes config. On a pass it prints `config_entry` (`{model, qualification: run-<id>,
qualified_on, expires, note}`) and the class: the same model re-qualified (waiver -> run id) is class B, the desk's;
a new model for a tier is class C (one board Decision per company round). `models.<tier>` is not yet a `site permit`
catalog key (release 90 TODO in `permits.py`); until then the entry is applied under the Director's config rule.

## Exam spawns (phase 2: the Claude Code hooks)

An exam brief starts with `DUREH-EXAM <run> task=<id> tier=<T> model=<M>` (plus ` attempt=2` on a rerun). The spawn
hook admits it only when

- the desk spawns it (an exam is the desk's work: the desk may spawn the task's role even when it does not dispatch
  that role directly; any other spawner is refused);
- the run is an unscored exam run of that tier and model, the task exists (attempt 2 only after `exam score` sent
  that task back), its role is the `subagent_type`, and the prompt is the brief file's text unchanged (digest);
- the Agent tool's `model` is exactly the run's model (`claude-sonnet-5` or `anthropic:claude-sonnet-5`); a missing
  model or a family alias (`sonnet`) is refused, because the exam qualifies that exact model;
- that task and attempt was not spawned before, and the workday allows work (`day` gate).

It records `exam.spawned` (refs the exam run). The agent's first tool call binds it by the header in its own prompt
(`exam.agent.started`); it is never bound to a work item, not even by order. A bound exam agent

- writes (Write/Edit and Bash write targets) only inside its task workspace `scratch/exams/<run>/<task>/` (not its
  `.git`) and its verdict file; everything else, including `state/`, other tasks and the module, is refused;
- runs no `dureh` command (no ledger writes: the desk records through `exam` commands), runs git reads only (no commit,
  no ref change, no push) and spawns no agents;
- at its end (SubagentStop) leaves an `agent` run record with `exam_run`, `exam_task`, `exam_attempt` and its
  transcript usage, and `wi` null.

`exam score` sums those records: `units` (total) and `units` per task row, and `units_per_task`; it is `unmeasured`
with `units_why` when a task has no agent record or an agent's usage was not measured.

## Rerun (phase 2)

Proposal section 2: a task that fails is rerun once to separate noise from inability. On the first `exam score`, each
failing task of a deterministic kind (build, check, example; scored by exit codes and exact matches) without a hard
failure is reset to its frozen commit (or a fresh fixture copy, verdict removed) and gets
`scratch/exams/<run>/<task>.attempt-2.brief.md`; `exam.rerun` records the first scores. The command returns
`status: rerun` (exit 0) and nothing qualifies yet. The desk spawns the attempt-2 briefs and runs `exam score` again:
those tasks are scored once more, the record keeps both (`rerun: true`, `first: {...}`, `reruns: [...]`). A hard
failure (a write outside the scopes, an edit in a checking task, a forbidden write) is final.

## Example-task fallback and `not examinable` (phase 2)

A tier with fewer than 3 eligible promoted items is built from the example tasks of its roles instead: the harness
tasks `org/exams/<role>/<task>/` and the package's `lessons.evals` directory (`<evals>/<role>/<task>/`), in the
`verify --exam` format (`task.yaml` with `id`, `prompt`, `required_outputs`, `forbidden_writes` and at least one
`checks` command, plus `fixture/`). With at least 3 such tasks the set is `source: fixture` (each task keeps its declared
split and a sha256 of its directory, checked at `exam run`); the workspace is a copy of the fixture and scoring is the
role-level scorer (checks exit 0, required outputs present; a forbidden write is a hard failure), run in the session
(example tasks are small). The config note says `fixture set`; rebuild from replay at re-qualification. With fewer
than 3 example tasks too, `exam build` refuses and `exam plan` marks the tier `not examinable` with both counts.

## Newer model available (phase 2)

`day handshake` has a `newer models` line: `skip` when `models.candidates` is not set; else `warn` per tier whose model
family (same provider and family, for example `sonnet`) has a higher version in `models.candidates`
(`newer model available for tier worker: anthropic:claude-sonnet-5-5 (incumbent anthropic:claude-sonnet-5)`), else
`ok`. Versions compare numerically; a date suffix is not part of the version. The desk runs the candidate exam that
week and asks for the switch with the scores; the harness never switches by itself.

## Not yet (after phase 2)

- pm, desk and planner task kinds (draft validation, typed decisions, plan JSON); spread over lanes and targets.
- `models.<tier>` in the permit catalog; a finding on a failed exam.
- Example tasks run in the session, not in the scheduler job; replay and example tasks are not mixed in one set.
- Exam spawns are admitted for the Claude Code runtime only (member runtimes are not examined).
