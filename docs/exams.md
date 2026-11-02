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
| `dureh exam build --tier T [--count 3-5]` | Freezes an exam set from promoted work items of tier T: build tasks from items whose dispatch ran on T (its own tier, `--tier`, routing, `writer-academic` for academic items) with declared checks, a passing verify run and a recorded base commit that still exists; check tasks (when T is the checker's tier or `models.routing.check`) from recorded verify runs at a staged head that still exists, expected verdict = the recorded one (failures included). Items touching a protected path are left out. Newest first; the newest that needed rework (or a known failed check) is always included. Every task is `split: holdout`. Writes `state/exams/<T>/set-<ts>/` (`set.json` + one `<task>.json`: WI, kind, role, target, commit, checks, outputs, expected verdict, frozen brief and its sha256; the set's sha256) and records `exam.built`. Fewer than 3 eligible items: refused, nothing written. |
| `dureh exam run --tier T --model M [--set S]` | One detached git worktree per task under `scratch/exams/<run>/` at the frozen commit and one brief per task (`scratch/exams/<run>/<task>.brief.md`), headed `DUREH-EXAM <run> task=<id> tier=<T> model=<M>`. Records a `run` record `exam-run` (`run_type: exam`, `status: submitted`). The desk spawns each brief with model M. One exam run per site at a time. |
| `dureh exam score --run R [--local]` | Runs every task's declared checks: one scheduler job (the `verify --wi` checks script per task, run in turn by one wrapper), or in this process with `--local` / `checks.runner: local`. Build task: pass = every check exits 0, declared outputs present, a change was made, no write outside the role's write scopes (a hard failure). Check task: pass = the verdict in `<task>.verdict.json` equals the recorded one and no file was changed (hard failure). Records one `run` record `run_type: qualification` and removes the worktrees; exit 1 on fail. |
| `dureh exam compare --tier T \| --all [--json]` | Table tier x model: score, pass, units per task, date (the latest record per model; `*` = incumbent) and the recommendation. |
| `dureh exam plan --tier T \| --all [--json]` | Per tier, the candidates cheapest first on the tier's newest exam set: passed, failed, `needs a run`, `needs a run (incumbent reference)`, or `not needed (a cheaper candidate passed)`. |

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

## Not in this release (phase 2)

- Exam spawns through the spawn hook: the hook admits only `DUREH-BRIEF` spawns bound to a work item, so an exam brief
  is refused there and its writes would be refused; phase 2 binds `DUREH-EXAM` briefs to the exam run (model check,
  writes limited to the task workspace, agent usage recorded with `exam_run`).
- One rerun of a failing task; harness and package tasks for a site with fewer than 3 eligible items; pm, desk and
  planner task kinds (draft validation, typed decisions, plan JSON); spread over lanes and targets.
- `models.<tier>` in the permit catalog; a finding on a failed exam; a `newer model available` handshake line.
