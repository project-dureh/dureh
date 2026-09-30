# DUREH Technical Specification and Implementation Appendix

*D*esk-led, *U*nbundled, *R*eversibility-aware, *E*vidence-accumulating *H*arness

RP-0010 · Companion to [the public white paper](DUREH-public-white-paper.md) · 2026-09-30  
**Status:** Proposed design extracted and reorganized from the uploaded `RP-0010-camri-org.md.html`. No DUREH implementation or acceptance result was supplied. Paths, commands, limits, and component names below are source proposals, not verified behavior.

## 1. Scope and ownership

This document holds the implementation material separated from the public argument: repository layout, records, hooks, adapters, DataLad/RIA, OOD/Apptainer/Docker, Slurm, bootstrap, phase gates, and measurement recipes. It deliberately excludes internal decision chronology, reviewer scores, and naming deliberations. The uploaded source remains read-only.

The three ownership layers must be kept distinct:

| Layer | Owner and purpose | Distribution and recovery |
|---|---|---|
| Harness | Shared organization machinery: roles, tools, adapters, schemas, tests, console compatibility. | Versioned and intended for reuse. |
| Startup package | Company values, skills, lessons, evaluations, templates, bootstrap WI. | Transfer only after content review. A private state submodule *reference* may exist here for recovery of this installation. |
| Site | Configuration, linked projects, caches, persona, memory, records, and raw data. | Site-bound. A private versioned snapshot may restore this same installation; it is not a default payload for new installations. |

The source describes `state/` both under the package as a submodule and under the site as a checkout. **Resolution (proposed, from external review):** the package must not contain a Git submodule pointing at the private state dataset. A shared, contributable package that carries a gitlink (URL + commit) to private state conflicts with the privacy scan in the contribution loop, makes every fork inherit a pointer to someone else's state, and cannot be cloned with `--recurse-submodules` when the URL is a `ria+` DataLad address (plain Git does not understand it; DataLad's `datalad-url` submodule property would be required). Instead, a **private deployment manifest** — `deployment.yaml`, kept in the site or in a private evidence repository, never in the package — pins the three revisions together:

```yaml
# deployment.yaml (private; example structure)
harness:  {url: "<harness-remote>",  rev: "<sha>"}
package:  {url: "<package-remote>",  rev: "<sha>"}
state:    {url: "<private-dataset-remote or ria+ url>", rev: "<sha>", schema_version: 3}
console:  {rev: "<console tag compatible with schema_version>"}   # from compat.yaml
recorded: "<UTC timestamp>"
```

Recovery of the *same* installation clones the three revisions named by a manifest; ordinary installation of the package at a new site creates empty state. The package therefore stays free of site-specific pointers, and the manifest — not the package — is the "snapshot pin". `install.sh --recover <deployment.yaml>` is the proposed entry point. The command sequence that keeps the site checkout and the private remote synchronized (session-end save/push, session-start update/merge) is specified in Section 4 and remains to be proven by a two-host test.

## 2. Proposed repository and site layout

The following is a design layout, not a claim that these paths exist now. The Longleaf path and remote in the source are deployment examples and must be parameters.

```text
<harness>/
  README.md                 install.sh
  presets/                  example site presets (e.g., longleaf, VM)
  org/                      runtime-neutral roles, tiers, briefing templates,
    hooks/                  guards, session start/end, promotion checks
    exams/                  role-specific task inputs and scorers
    skills/package-from-source/
  runtimes/                 claude-code/ first; other adapters proposed
  tools/                    wi, dispatch, verify, ledger, status, slurm,
                            path-refs, purge, push, usage (proposed limit: 10)
  schemas/                  work-item, run, decision, session, finding, event
  apps/console              separately versioned submodule
  compat.yaml               state schema version <-> console revision
  .claude/                  generated adapter output; not source of truth

<startup-package>/
  startup.yaml              company manifest
  CORE-VALUES.md            one-page values
  skills/                   company/domain skills
  lessons/LESSONS.jsonl     evidence-backed, shareable lessons
  lessons/evals/            regression and qualification cases
  templates/project/        project skeleton and AI instruction source
  bootstrap/                first WI and installation questions
  state/                    private site-state dataset submodule reference

<site>/                     outside the harness clone
  config.yaml               site paths, runtime/model mapping, RIA, Slurm, OOD
  state/                    operating checkout of private state dataset
    persona/                desk identity preferences
    memory/                 desk context and handoff
    records/{work-items,runs,decisions,sessions,findings,events}/
  index.sqlite              rebuildable node-local cache, never authority
  projects/  inbox/  reference/  cache/
```

The source proposes a DataLad superdataset for a domain project:

```text
<project>/
  sourcedata/  rawbids/  derivatives/<pipeline>/   subdatasets
  code/  reports/                                tracked in superdataset
  camri-manifest.yaml  dataset_description.json
  CLAUDE.md  .claude/                             generated from template
  checkpoints/  cache/                            untracked working data
```

Project layout and BIDS-related fields are CAMRI package material, not universal harness requirements. The proposed `camri project init` and `sync-agent` commands are not supplied implementations.

## 3. Configuration and compatibility contract

The source calls for one site `config.yaml` containing paths, remote allowlists, RIA transport, scheduler settings, console launch settings, runtime adapter, and `tier -> provider:model` mapping. It does not provide a final schema or complete defaults. A minimal *illustrative field inventory* is:

```yaml
# Example structure only; keys and validation schema are not finalized.
site: {root: "<site-path>"}
harness: {root: "<harness-path>"}
startup: {source: "<package-path-or-url>", revision: "<pin>"}
references: []             # read-only paths or URLs
projects: []               # site-bound paths or links
ria: {url: "<ria+file-or-ria+ssh-url>"}
index: {path: "<node-local-path>"}
runtime: {adapter: "claude-code"}
models: {}                 # tier -> provider:model, qualification record
slurm: {}                  # account, partition, limits, output paths
console: {}                # mode, bind path, access endpoint
push: {allowed_remotes: []}
```

An implementation must define types, required fields, environment overrides, secret references, and migration rules before `install.sh` uses this file. Configuration must not contain secret values. The source's fixed tool count, 2 KB worker briefing, 10 KB manager briefing, 30-line report, span/size thresholds, retry limit of two, and reorganization finding budget of three are provisional defaults, not validated limits.

`compat.yaml` must map state record schema versions to console revisions. `verify` and `install.sh` should reject incompatible pins. A schema-changing WI and console-changing WI should promote as one bundle. Recovery must choose a console revision compatible with the pinned state. The shape of `compat.yaml` and migration directionality remain open.

## 4. Record model and lifecycle

The proposed source of truth is one schema-validated JSON file per record at `state/records/<kind>/<id>.json`, with large logs and attachments in annex. The six kinds are `work-item`, `run`, `decision`, `session`, `finding`, and `event`. Proposed common fields are `id`, `kind`, `ts`, `actor`, `wi`, `status`, `refs[]`, and `schema_version`. Some fields may be null or inapplicable by kind; that rule is not specified in the source and needs a formal schema. The proposed ID form is `<kind>-<UTC>-<host>-<random4>`; collision probability and host naming/privacy implications require validation.

The WI status path in the source is:

```text
APPROVED -> STAGED -> VALIDATED -> PROMOTED
             |            |
          finding / rework / stop (events, not silent overwrites)
```

Gate 1 approves the plan and acceptance criteria. `dispatch` creates the worktree and assignment event. The worker stages output. A checker records verification; `verify` owns any repeatable test execution. Gate 2 permits desk-controlled merge and tag. The source does not specify every invalid transition, failure state, or concurrent update rule; these must be explicit in the implementation state machine. Findings are not automatically new WIs. Append-only history is corrected by a new event instead of deleting the original record.

The source proposes `ledger add/update/index/query/migrate`. Since it also describes records as one-write files, `update` must be resolved. **Proposed resolution:** records are immutable after creation; the current status of a work item is a *projection* computed by the index from its events (`dispatched`, `staged`, `verified`, `promoted`, `reverted`, `stopped`). `ledger update` is therefore removed; anything that would have changed a record is instead a new event that references the record. The single exception is `ledger migrate`, which rewrites records only to move them between schema versions and records a `migrated` event per file. This choice keeps the one-write-per-file property that makes NFS-hosted records safe. The index is rebuildable from records, runs on node-local storage, and is not backed up as authority. An interrupted write, malformed record, stale index, and crash before `datalad save` each need a recovery test. It is unverified that full rebuild will take seconds at target size.

### Concurrent hosts and RIA

The desk is the single writer to the state dataset's `main` branch. A remote console or other host is proposed to write `decisions/<host>` and push that branch to a RIA sibling. The desk fetches and merges on session start or periodic sync. Unique files reduce content conflicts but do not eliminate Git history conflicts, schema conflicts, or simultaneous branch updates. Non-fast-forward push must fetch and retry within a bounded policy; failed merge or migration must stop promotion and surface an incident. One host losing connectivity must not silently overwrite the other's decision.

The source proposes `datalad save` and `datalad push --to <ria>` at session end, with an initial save of unsaved changes on next session start after abnormal exit. This creates a durability window between a record write and a successful remote push. Define a recovery point objective and test it before production use. RIA may be `ria+file://` on a directly mounted path or `ria+ssh://` on a remote path. The source cautions against mounting remote storage with sshfs and then treating it as a local RIA target. Exact concurrency and annex behavior require a two-host test.

## 5. Roles, capabilities, and hooks

The desk is the sole persistent agent with persona and memory. Proposed managers are modules, projects, and harness. Proposed workers are `py-writer`, `web-writer`, `tool-writer`, `data-steward`, `pipeline-runner`, `interpreter`, `checker`, `researcher`, and `doc-writer`. These names and ownership rules came from the CAMRI design; another package may require different worker types.

| Worker | Proposed write scope | Important exclusion |
|---|---|---|
| `py-writer` | `modules/*/src/**`, `modules/*/tests/**` | Data and verification. |
| `web-writer` | `apps/console/src/**`, `apps/console/test/**` | Hooks and tools. |
| `tool-writer` | `tools/**`, `org/hooks/**`, `org/skills/**`, `schemas/**`, `install.sh`, `runtimes/**` | Console and project data. |
| `data-steward` | Project manifest, metadata, subdataset registration | Raw data content and pipeline execution. |
| `pipeline-runner` | Project `derivatives/**` on WI branch, checkpoints; run records via tool | Code changes and interpretation. |
| `interpreter` | `reports/interpretation/**`, its evaluation cases | Data writes and pipeline execution. |
| `doc-writer` | Documentation, manuscript, devnotes, package proposals | Data interpretation and code. |
| `researcher` | Finding record through ledger | Direct code/document writes. |
| `checker` | Verification/run event through tool | Repairing the artifact it checks. |

The desk writes the remaining organization, package, persona, memory, and site configuration paths. Two audits apply to the desk itself, because its writes bypass the worker gates: `state/memory/` has a size cap set in `config.yaml` and every change to it is recorded as a `memory-changed` event with a diff summary, so that policy cannot silently re-grow inside memory; and desk commits to the harness or package default branch are listed in the session record.

### Dispatch contract (adapter API)

A shell tool cannot spawn or stop an agent inside a runtime's session, and a separate background process would violate the "no hidden session background work" invariant. `dispatch` is therefore a *contract* that each runtime adapter implements, not a single script:

| Operation | Semantics | Claude Code adapter (proposed) |
|---|---|---|
| `spawn(role, wi, brief, model_tier)` | Start one agent for one WI with the generated briefing; returns an agent handle; records a `dispatched` event | Runtime subagent API invoked by the calling agent (desk or manager); the adapter writes the event through `ledger` |
| `stop(handle, reason)` | Terminate the agent; records `stopped`; the WI worktree is left for inspection or discard | Runtime stop/cancel API |
| `status(handle)` | running / finished / failed, tokens so far | Runtime task status; usage from the runtime's accounting when available (see evidence case on incomparable usage fields) |
| `wait(handle, timeout)` | Block until finished or timeout; timeout produces an event, never an unbounded loop | Runtime notification with a bounded deadline |

Authority: the desk dispatches managers, researchers and document workers; a **manager may dispatch and re-dispatch specialists for its own bundle only** (depth 1 below it), and the adapter refuses a spawn whose depth would exceed two. A runtime without these primitives cannot host managers; it may still host single-level dispatch from the desk.

### Ownership enforcement at promotion

A hook that inspects a tool call cannot reliably tell which paths a shell command will write. The authoritative ownership check is therefore performed at promotion: `verify` computes `git diff --name-only <base>..<wi-branch>` (and `datalad status` for annexed content) and rejects the WI if any changed path falls outside the role's globs; hooks remain as an early, best-effort warning. Writes that legitimately go through tools (e.g. `ledger add` writing `state/records/`) are attributed to the tool, not the worker, and are exempt by rule.

### Worktrees and DataLad subdatasets

A Git worktree of a superdataset does not initialize its subdatasets, and a subdataset branch cannot be shared by concurrent WIs or running Slurm jobs without conflicts; discarding a branch also leaves annexed objects behind. For project work the proposed unit of isolation is therefore an **ephemeral `datalad clone`** of the relevant subdataset per WI (on scratch or checkpoint storage), in which `datalad run` executes; results are pushed back to the subdataset on a `wi/<id>` branch and merged at promotion. Cleanup after discard is `datalad drop` plus removal of the ephemeral clone, recorded as a `purged` event. This is untested and belongs to Phase 2 acceptance. The source proposes a generated hook table from these globs so path ownership has one source of truth. Glob overlap, symlinks, submodules, renamed paths, and commands that write outside Git must be tested. A hook that watches changed paths is not a sandbox. Worker read access also needs enforcement if private data may be present; the source explicitly says briefings alone are only context management.

Proposed hook/tool invariants:

1. Reference repositories, inbox, and designated source material are read-only.
2. Project computation and containerized neuroimaging tools run as Slurm jobs, not hidden session background processes.
3. External push targets are allowlisted and subject to the required approval gate.
4. Workers write only their owned paths in WI worktrees; the desk controls promotion.
5. A missing expected input causes a failure. A path move or deletion runs `path-refs` first.
6. Session start checks unsaved state, pending remote decisions, schema compatibility, and index freshness. Session end records a handoff and attempts save/push, reporting failures.

The first runtime adapter proposed is Claude Code. A second adapter and actual equivalence of hooks, spawn depth, and capability enforcement are future work. If a runtime lacks a hook, the tool must enforce the same boundary or the capability must be disabled. Generated `.claude/` or `.codex/` output must not be mistaken for the runtime-neutral source definition.

### Action classification and external effects

`dispatch --stop` may terminate an agent, but cannot undo output. Worktree changes are reversible before external effects. Slurm jobs are cancelable until completion, with partial outputs handled separately. Records and remote pushes need compensating events or revert commits. Human or third-party disclosure is irreversible and must be gated beforehand. A promotion record should state action class, authority, destination, artifact hashes, and compensation plan. The source names these controls but supplies no machine-readable action-class schema; that schema is an implementation prerequisite.

## 6. Qualification exam contract

`org/exams/<role>/` is proposed to hold inputs, expected outputs, and scoring. A qualification record should link role/tier, model/provider/version, adapter, exam version, prompts/skills, run settings, pass/fail per task, costs, retries, and date. `dispatch` should refuse a model without a valid qualification for that tier, except for an explicit time-limited bootstrap waiver.

The source proposes at least ten tasks per role, 80% passing, one replacement-task retest, and bootstrap waivers while exams are built. Those are prototype settings; with ten items an observed 80% pass rate has a wide interval (roughly 0.44–0.97 at 95%), so a production exam should target at least thirty items per role, or fewer items with repeated stochastic runs, before a tier assignment is treated as reliable. A production exam must separate development cases from hidden hold-outs, include repeat runs and uncertainty, treat forbidden actions as hard failures, and requalify after meaningful version changes. Case ancestry from real WIs must be tracked to avoid near-duplicate contamination. A score threshold and sample size must be calibrated against observed failure cost, not inherited as fact. Provider/model mappings and prices are dated site data; no particular vendor or model is part of the specification.

## 7. Console and deployment modes

The source proposes a separately versioned Astro/Node console that reads the node-local index and Git history, and writes Director decisions through the ledger. Proposed views include timeline, WI board, decisions, sessions, runs, findings, lessons, and project status. The console's read/write identity, authorization for decision submission, CSRF protection, audit logging, and secret handling remain unspecified. OOD or VPN access alone does not complete that model.

| Mode | Proposed launch and data path | Acceptance test and open issue |
|---|---|---|
| A: OOD sandbox | `~/ondemand/dev/dureh-console` symlink to `apps/console`; OOD reads `manifest/form/submit/template` and starts an Apptainer image. | Real OOD launch, auth context, data read, decision write, schema match. Symlink recognition is unverified; fallback is a thin OOD directory that enters the submodule path. |
| B: cluster Apptainer | Same image on a compute node, via Slurm; SSH tunnel or OOD proxy for access. | HTTP response, local index rebuild, job cleanup, port and identity controls. Image build and launch are unverified. |
| C: external Docker VM | Same console code; clone harness and site state from authorized remotes/RIA, access over approved private network. | Two-way decision branch round trip, stale-clone handling, authorization, backup and restore. The original source proposed periodic sync but did not prove consistency. |

The console must never share the desk's operating checkout of the state dataset when it writes decisions; in mode A on the cluster it uses its own clone of the state dataset and writes to `decisions/<host>` like any other host, so that the desk remains the single writer of `main`. The environment variable naming the harness root is `DUREH_ROOT` everywhere (the source's console used `CAMRI_ROOT`; the rename is a console WI). For Open OnDemand Batch Connect the app must ship its own `view.html.erb`, and the served application must honour the proxy base path (`/rnode/<host>/<port>/` or `/node/...`, passed to the app as a base-path and allowed-host setting) — the source's console already solved both (script URLs without the proxy prefix were the recorded failure). `node:sqlite` is still marked experimental in Node 24 documentation; the console should read the index through it only with a tested fallback (a small native binding or a read-only sidecar) selected at build time. Mode C's periodic `datalad update` over SSH to a cluster with multi-factor login is not assumed to work unattended; the alternative is to place the RIA store (or a mirror) where the VM can reach it without interactive login, decided per site.

The source mentions a `Containerfile` using Node 24, conversion to `console.sif`, `DUREH_ROOT` bind mount, and `docker compose up`. These are proposed build and launch methods, not tested commands. Image provenance, version pinning, and patching need a deployment procedure. The external VM should not receive raw site data merely because it can sync RIA; its allowed dataset contents and permissions need an explicit policy.

### Slurm boundary

`slurm` is the proposed wrapper for project computations and long-running jobs. It should record submission ID, command or container digest, input snapshot, resource request, status changes, output paths, cancellation, and cost where available. `pipeline-runner` must work on a WI branch/subdataset; Gate 2 checks outputs before merge. A canceled job may have written partial outputs, so cancellation is followed by inspection and `purge` only for identified scratch/checkpoint paths. Scheduler partitions, accounts, time limits, and notification method are site configuration and are not provided by the source.

## 8. Bootstrap and package derivation

Proposed install flow:

```text
clone harness -> choose startup package and revision -> choose site path
-> validate config and permissions -> initialize console/package submodules
-> create or recover private state dataset -> configure RIA
-> ask persona preferences (unless supplied securely)
-> generate first runtime adapter -> create initial WI records
-> build node-local index -> self-test -> launch console acceptance check
```

The source proposes `./install.sh --startup <url|path> [--preset ...] [--site ...]`, `--new-package`, `--sync`, and `--rollback`. These flags are requirements to specify and implement, not working commands. Installation must distinguish **new company**, **new site with an existing transferable package**, and **authorized recovery of the same site**. The recovery path uses a pinned state revision and a compatible console; ordinary installation must not inherit private state. Idempotence and rollback after a partially failed install need tests.

The proposed `package-from-source` skill reads a reference in read-only mode, inventories files/tools/roles and observed incidents, extracts evidence-backed lessons, drafts values for human confirmation, maps output types to worker roles, decides what to link or template, drafts initial WIs, and prepares a Gate 1 proposal. Evidence-free claims remain findings, not lessons. The source's CAMRI inputs included a retired control repository, a console repository, a module repository, and a project example. Those are package-specific references; no reference content or old rule file is copied into the reusable harness.

### CAMRI package bootstrap material

The source identifies three domain values: refactor old code into `camri`/`camri-X` modules; use DataLad superdatasets with sourcedata, rawbids, derivatives, code, reports, and AI instructions; and assist interpretation of analysis outputs. It proposes initial work on LNA Phase D, camri v2, and container provenance. These are draft bootstrap inputs, not a claim that the work was approved or done. The source's `LESSONS.jsonl` v0 lists twelve incidents. They are reproduced here so that the package draft can be created from this repository alone; each row is an observation with its recorded measure and the design element that answers it. Operation period 2026-09-17 to 2026-09-30; measurements taken 2026-09-29/30; provenance pointers live in the private evidence registry.

| id | Observation (measure) | Design response |
|---|---|---|
| L-01 | 2,345 of 2,477 tracked files (95%) were records, evidence, staging or approvals; Markdown tables and a journal file could not be indexed or queried | Typed JSON records with a rebuildable index; no records in the harness |
| L-02 | 36 tools, 10,916 lines; 12 tool names (about 5,100 lines) absent from instructions, hooks and playbooks; a 1,416-line records tool received 15 commits in two days | Fixed small tool surface; self-test fails on a tool nothing invokes |
| L-03 | Silent failures: a missing input file produced "0 iterations" or "no manifest" and passed | Every tool fails on a missing expected input |
| L-04 | The same test was run by writer, manager, validator and desk (3–4 times) | One checker; `verify` owns repeatable execution |
| L-05 | In one parallel hour ten workers used 140,000–330,000 tokens each; managers finished first and re-dispatched four or more times; two workers edited the same file | Output-type specialization with path ownership; bounded span; at most two lanes |
| L-06 | A path move broke three dependants (a guard self-test, an inventory count 21→16, unresolved references 264→498) because nobody checked who read the path | `path-refs` before any move or deletion; layout fixed by the installer |
| L-07 | "Parallel" work as background processes inside the session shared CPU (guard self-test 258 s in-session vs 61 s as a job) | Compute only as scheduler jobs; no hidden session background work |
| L-08 | Waits that never ended: `pgrep -f` matching itself, a loop waiting on a display line, an exact-string comparison of a scheduler cancellation message | Bounded waits with job notifications and deadlines; state summarized by tools |
| L-09 | One work item per defect (16 in a day) inflated the desk conversation and the cost of every report | Findings are records, not work items; desk hands over per session |
| L-10 | 216 decision rows compiled into a 9,130-byte rules file; enforcement points (hook and auto-commit) kept separate path lists that drifted | One-page values plus hook-enforced invariants; a lesson survives only as a safeguard, tool or exam case with a regression test |
| L-11 | Tests themselves were fast (module 15 s, console 47 s, control repo 69 s); the slow parts were record indexing (18 → 9 min) and the verification process | Node-local index rebuilt in seconds; single verification |
| L-12 | Three repositories in three places (control on `/work`, console original under the OOD sandbox path, module on GitHub) so the company could not pin the console version | Submodule pins plus one symlink; console version locked to record schema |

## 9. Controlled reorganization and normalized phases

The source's seven reorganization rules are: isolate the change, freeze WI scope, use one reorganization lane (at most one ordinary work lane alongside it), close each phase with an acceptance gate, record a rollback procedure before cutover, track a provisional finding budget, and retain a read-only recovery path during burn-in. These are proposed workflow controls; hook enforcement and thresholds are untested.

| Phase | Deliverable | Minimum acceptance evidence |
|---|---|---|
| 0 — Design | Package v0, record schemas, installation and rollback plan. | Reviewed manifest, schema drafts, explicit open decisions. |
| 1 — Core harness | Installer, runtime-neutral roles, first adapter, records/index, guards, self-test, exam scaffold. | Clean install in an empty directory; query an event; path/permission tests. |
| 2 — State and console | Private state dataset, RIA round trip, schema/console compatibility, OOD launch. | Restore from snapshot; two-host decision merge/retry; real OOD launch. |
| 3a — Cluster | Apptainer launch under Slurm. | Page response, node-local index rebuild, job cleanup, access check. |
| 3b — External | Docker VM/private access and controlled RIA synchronization. | Decision round trip, identity/authorization, stale-clone and conflict test. |
| 4 — Cutover and burn-in | Controlled switch and measured operation. | Rollback exercised, prior system retained read-only, evaluation report. |
| 5 — Domain package | CAMRI project template, module and interpretation skills, expanded exams. | Clean project initialization, domain acceptance cases, no undocumented waiver. |

This numbering replaces the source sequence `0 -> 1 -> 2 -> 3b -> 3 -> 4`. The source proposed a one-week burn-in and specific pass counts, but those are not validated acceptance thresholds. Phase order may depend on available infrastructure; dependencies and deferrals must be recorded before cutover.

## 10. Evaluation and measurement recipe

Use the [white paper's three-stage evaluation](DUREH-public-white-paper.md#5-evaluation-plan). Pre-register WI inclusion criteria, task classes, exact model and adapter versions, resource pricing, acceptance rubric, and stop conditions. Report both successful and failed attempts. Compute:

```text
cost per successfully promoted WI =
  sum(model + execution costs for all attempted WIs, including retries/checks)
  / count(WIs passing acceptance and promoted)

Director attention per promoted WI =
  total measured Director minutes / count(promoted WIs)
```

If the denominator is zero, report it as undefined with the numerator and zero successes. Track interventions per *attempted* WI as well, so a high failure rate cannot disappear from the human burden measure. The source's old `usage.sh` values are not automatically comparable with a new model billing record; reconstruct compatible units or label the comparison unavailable.

Source measurement definitions, recorded on 2026-09-29/30 in the old control repository:

| Measure | Source method or denominator | Interpretation limit |
|---|---|---|
| 2,477 tracked files; 2,345 (95%) records/evidence/staging/approvals | `git ls-files`; four directory groups: `.agents/state` 1,331, `reports/evidence` 738, `.agents/staging` 223, `approvals` 53. | A repository composition measure, not a failure rate. |
| 36 tools, 10,916 lines | Line count of `.agents/tools/*.sh` and `*.py`. | Size, not execution frequency. |
| 12 apparently unused tools | Tool-name search in instructions, hooks, playbooks, and Slurm scripts. | Name search can miss indirect invocation; source notes separate self-tests. |
| 140,000–330,000 tokens per worker | `usage.sh metrics` over ten workers in one parallel hour. | One interval, no causal attribution. |
| Verification 3–4 times | Count of actors running the same test in session/WI histories. | Requires task-level reconstruction to compare with new workflow. |
| Three path-move breakages | Findings linked to WI-0129, WI-0079, WI-0117. | Incident count for one move. |
| Indexing 18 then 9 minutes | `records.sh index` over about 2,300 records. | Hardware, cache, and index version must be captured for comparison. |
| 216 decision rows; 9,130-byte rules file | Row count in `reports/DECISIONS.md`; byte count of `RULES.md`. | Text quantity is only a proxy for policy burden. |

The source proposed one archived bundle (WI-0114/0120/0122/0128) for replay. Use it only if original inputs, outputs, cost, and acceptance evidence can be recovered; add more bundles and prospective matched WIs to estimate variation. Do not use the replay bundle to both tune DUREH and claim an unbiased comparison.

## 11. Security requirements and unresolved decisions

Before deployment with external write authority or sensitive data, specify and test:

- Who can read/write each path and record kind, including symlink traversal and subprocess writes; role briefing limits are not access controls.
- How README, AGENTS.md, retrieved pages, and other reference text are treated as untrusted data; test prompt-injection attempts against dispatch and tool calls.
- How secrets are supplied, rotated, and kept out of model context, logs, RIA, Docker images, and upstream changes.
- Which network destinations and Git remotes each role can contact, and how external push, PR creation, deployment, and disclosure are approved and audited.
- Whether site data may leave the cluster or be sent to a model provider; classify PHI/PII and research data before any processing.
- Console identity, authorization, CSRF defense, audit log, and multi-user behavior. The original single-user/OOD or VPN assumption is insufficient for broader deployment.
- How privacy review of an upstream contribution inspects diffs, test fixtures, commit metadata, logs, paths, hostnames, and licenses. A grep scan is supplementary.

Key unresolved technical choices include record immutability versus `ledger update`, the formal JSON schemas and migration contract, package state-pin publication safety, two-host merge semantics, error handling when session-end push fails, exact console authorization, and the quantitative exam/evaluation thresholds. None should be silently filled by an implementer. A design review should resolve them before claiming a complete installable harness.

## 12. Selected source bibliography by evidence level

The uploaded document contains a longer bibliography; this is a compact carryover, **not independently verified here**. Verify metadata and primary URLs before external publication.

| Source cited in uploaded paper | Evidence level in source | Relevant design question |
|---|---|---|
| Cemri et al., *Why Do Multi-Agent LLM Systems Fail?* (MAST), NeurIPS 2025 Datasets & Benchmarks | Peer-reviewed empirical classification, as stated in source | Specification, coordination, verification failure taxonomy. |
| Kamoi et al., *When Can LLMs Actually Correct Their Own Mistakes?*, TACL 2024 | Peer-reviewed survey, as stated in source | Need for reliable external feedback in correction. |
| Wang et al., *OrgAgent*, 2026 | Preprint | Role hierarchy and controlled information flow. |
| Bogdanov et al., *Context, Reasoning, and Hierarchy*, 2026 | Conference study, as stated in source | Costs of additional deliberation layers. |
| Anthropic, *When to use multi-agent systems*; *How we built our multi-agent research system* | Vendor engineering guidance | Practical orchestration hypotheses. |
| oh-my-codex and related installable harness projects | Practitioner/software precedent | Hooks, shared skills, event logs, installation. |

Specific performance percentages and current vendor/model prices in the uploaded source are not carried into this specification as evidence for DUREH. They require primary-source and date checks before use.
