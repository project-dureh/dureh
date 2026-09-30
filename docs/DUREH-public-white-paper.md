# DUREH: An Installable Harness for AI-Agent Organizations

*D*esk-led, *U*nbundled, *R*eversibility-aware, *E*vidence-accumulating *H*arness

*A design informed by two weeks of operational evidence from one deployment*  
RP-0010 · Public design proposal · 2026-09-30

## Abstract

A two-week AI-agent deployment accumulated many records and tools while repeating verification and losing coordination across workers. These observations point to orchestration and organizational structure as plausible operational bottlenecks; they do not isolate a model-level cause. DUREH proposes treating the agent organization itself as a versioned, testable software artifact. It separates reusable machinery, transferable organizational experience, and site-bound state; gives a persistent desk control over temporary specialists; and converts failures into lessons, executable safeguards, and qualification cases. Its value remains a hypothesis. This paper specifies how to test it through replay, matched new work, and operational burn-in, with cost per successfully promoted work item and human attention as primary outcomes.

## 1. Thesis and contributions

**DUREH treats an AI-agent organization itself as a versioned, testable, and controlled software artifact, separating organizational machinery, accumulated experience, and site-bound state.** Its name describes the proposed operating model: a desk leads; machinery, package, and site state are unbundled; actions are classified by reversibility; and lessons accumulate only with evidence. Runtime portability is an architectural intent; operation across multiple runtimes has not been demonstrated.

The proposal has three core contributions. These are claims about the combination and operating model, not assertions that each component is individually novel.

1. **Organizational abstraction.** A persistent desk is the human interface and controls temporary, specialized managers and workers through isolated workspaces, typed records, verification, and promotion gates.
2. **Portable organizational state model.** A reusable harness and transferable startup package are versioned separately from site-bound data. A private deployment manifest pins the harness, package, console, and site-owned state revisions needed to recover the *same* installation. The package contains no state pointer.
3. **Measurable adaptation.** A finding becomes a lesson, then an executable safeguard or evaluation case, then a qualified change. Site experience can contribute upstream through a reviewed change carrying its evidence.

DataLad, RIA, JSON records, SQLite, Git worktrees, OOD, and Slurm are proposed mechanisms for this design. Their presence alone does not establish the contributions or their effectiveness.

## 2. Case study: observations, consequences, responses

The source is one Claude Code based organization operated from 2026-09-17 to 2026-09-30, measured principally on 2026-09-29 and 2026-09-30. Its records support the observations below, subject to the measurement definitions in the [technical specification](DUREH-technical-specification.md). These are descriptive measures, not a controlled comparison. In particular, file counts alone do not prove operational failure.

| Observation | Operational consequence observed or inferred | Proposed design response |
|---|---|---|
| 2,345 of 2,477 tracked files (95%) were records, evidence, staging, or approvals; indexing roughly 2,300 records took 18 minutes, then 9 minutes after changes. | Record volume and indexing time made state retrieval costly. The causal contribution of file count itself has not been isolated. | Typed records as source of truth; a rebuildable local query index. |
| 36 tools totaled 10,916 lines; 12 tool names were absent from the inspected instructions, hooks, playbooks, and Slurm scripts. | The tool surface was difficult to audit. Name absence is only a proxy for nonuse, not proof that a tool never ran. | Smaller tool surface and usage checks during self-test. |
| The same verification ran under 3–4 roles in sampled work-item histories. | Repeated checks consumed time and tokens without a clear single verifier. | One checker with recorded, reproducible verification. |
| Ten parallel workers each used 140,000–330,000 tokens during a one-hour interval; logs also describe reassignment and overlapping file edits. | Coordination and context costs rose, with ownership conflicts. The evidence does not attribute all token use to coordination. | Specialized output ownership, bounded work items, manager-led integration, and briefings tailored to the task. |
| A path move was associated with three breakages; the prior index and path resolution also regressed. | Structural changes reached dependent code before reference checks. | Pre-change reference checks and isolated promotion. |
| Decisions reached 216 rows and a rules file reached 9,130 bytes. | Written policy became hard to reconcile with executable behavior. This is an interpretation of the case record. | Keep a short statement of values; encode enforceable invariants in tools and tests. |

This case motivates the design. It does not establish that DUREH will outperform the prior workflow, or that model capability was irrelevant.

## 3. Architecture and operating model

```text
Director
   | requests, approvals
   v
Persistent desk ---- persona, memory, work-item ledger
   | dispatch / stop / verify / promote
   +---- temporary manager ---- temporary specialist(s)
   |                             isolated worktree per WI
   +---- direct researcher or document worker when appropriate
                  |
             checker + promotion gate

Reusable harness          Transferable startup package       Site-owned installation
roles, tools, schema  <--> values, skills, lessons, tests --> config, projects, state
console, adapters           and bootstrap template            records, memory, data
                                                            |
                                           private deployment manifest
                                            + state recovery snapshot
```

The desk alone maintains persona and organizational memory and speaks for the organization to the Director. Managers and specialists receive bounded work-item context, produce a defined artifact, and terminate. Managers coordinate related work items; researcher and document work may be dispatched directly by the desk. The initial design has three manager domains and nine worker types, but their exact count is a package and implementation choice, not evidence of an optimal staffing ratio. A work item (WI) states its goal, scope, owner, and completion criteria. A bundle groups related WIs for integration. Verification precedes promotion into the main branch.

The harness contains reusable organization logic. The startup package contains a company's values, skills, lessons, evaluations, and bootstrap material. The site contains its configuration, projects, and operating data. **State is site-owned and pinned only by a private deployment manifest.** Installing the package at a new site starts with new state unless recovery of the same installation is explicitly authorized.

| Layer | Portable to another installation? | Versioned? | Shared by default? |
|---|---|---|---|
| Harness | Intended | Yes | Yes |
| Startup package | Intended | Yes | By authorized distribution; no state pointer |
| Site state (persona, memory, records) | No | Yes | No |
| Site configuration, links, cache | No | Configuration may be tracked locally | No |

The proposed record model uses validated JSON files and a disposable query index. Its purpose is to make work-item state and decisions inspectable without treating conversation text as the database. The proposed DataLad/RIA snapshot supports backup and recovery; a successful restore, consistency under concurrent hosts, and schema-compatible console launch are acceptance tests, not established facts.

### A precise meaning of reversible control

DUREH cannot make every action reversible. The desk must classify an action before execution, gate it according to its consequences, and record the remedy.

| Class | Example | Required control |
|---|---|---|
| Reversible | Unmerged worktree edit | Discard or reset the isolated branch before promotion. |
| Cancelable | Queued or running Slurm job | Cancel the job; inspect and separately handle any output already produced. |
| Compensatable | Append-only record or remote push | Append a correction or create a revert; the original history remains. |
| Irreversible | Information already disclosed to a person or external system | Require an explicit gate before disclosure; issue a correction if needed, without claiming rollback. |

A job can move from cancelable to compensatable once it writes durable output. A merge is reversible only to the extent that no later work or external side effect depends on it. Approval, logs, and checks therefore matter most at the transition to irreversible exposure. The exact permissions and gates are specified in the companion document and require implementation testing.

### Learning and contribution lifecycle

```text
finding -> evidence-backed lesson -> regression case or safeguard
        -> harness/package change -> qualification and self-test
        -> human-approved contribution -> deployment -> measured outcome
```

The lesson belongs in the transferable package only when it is safe to share; raw records and research data remain site-bound. A change to shared machinery requires a regression case and review. A privacy scan can assist review but cannot certify that all sensitive information has been removed.

The proposed contribution path makes the organization (desk and specialists) the change author and requires a regression case, qualification/self-tests, and human approval. Only reviewed, authorized export artifacts may leave a site. Automated privacy scans are screening aids, not a guarantee of anonymization. The proposed steps:

| Step | What | Who | Gate |
|---|---|---|---|
| 1 | Site finding becomes a lesson with a source pointer, observation, evidence level, and stated uncertainty | Desk | Unsupported inference stays a finding |
| 2 | Change on a `contrib/<opaque-id>` branch plus a synthetic regression case in `org/exams/` or the self-test suite | Specialist owning the affected paths | Path ownership and site-data separation |
| 3 | `verify --self`: self-test and affected role exams; assemble the exact export bundle | Checker | Tests pass; scan the diff, fixtures, logs, prompts, commit metadata, and licenses |
| 4 | PR body generated from the sanitized lesson (problem, evidence level, change, regression case, affected roles); trailer `Contribution: <opaque-id>` | Desk | No site identifier in public Git metadata |
| 5 | Human reviews the full export bundle and authorizes the external push and PR | Director and desk | Approval is bound to the reviewed revision and destination |
| 6 | Upstream CI repeats tests and screening; maintainer reviews and merges; sites receive the change on update | Upstream | Missing regression case or privacy review blocks merge |

One PR carries one lesson. Package-level lessons (domain skills, templates) go to the package upstream through the same path. The same exams inform model qualification (Section 4) and test behavior-changing contributions; they do not replace release review. Known limits include CI and maintainer load, cross-runtime exam comparability, and privacy-scan false positives and negatives. This loop is untested until a second installation exists.

## 4. Capability tiers and qualification

Role assignment should follow `role -> capability tier -> qualification evidence -> provider:model`. Tiers describe tasks, not vendor products: deterministic recount or schema checking; bounded artifact production; orchestration of related work; and organization-level planning and Director interaction. A site can map a tier to a current model only after qualification. Model names and prices belong in dated site configuration, not in the architecture claim.

A qualification exam should include task inputs, explicit completion criteria, an executable scorer where possible, and adjudication rules for judgments that cannot be fully automated. The source proposal's ten items per role and 80% pass line are provisional bootstrap thresholds, too small by themselves for a robust reliability claim. The protocol should:

- Keep hidden hold-out tasks distinct from examples used to edit a skill or prompt; anonymize real WI and split related tasks together to limit leakage.
- Run each stochastic task repeatedly and report per-task and aggregate pass rates with uncertainty, not one score from one seed.
- Record model/provider/version, runtime adapter, date, settings, prompt and skill revisions, tokens, cost, latency, retries, and interventions.
- Include failure modes that matter for the role, including forbidden writes and unsafe external actions; passing the quality score cannot override a safety failure.
- Requalify after a model, adapter, prompt, or skill change that could affect performance. Preserve prior results for comparison.

Exam sizes, pass lines, repetition counts, and tier assignments should be set by pilot data and risk. Until that occurs, any bootstrap waiver must be labeled and limited to a documented role and period.

## 5. Evaluation plan

The design has not yet been implemented and tested end to end. Evaluation should proceed in three stages with the same definitions, run logging, and predeclared stop conditions.

1. **Retrospective replay.** Re-run several archived WI bundles with their original completion criteria, using a frozen copy of inputs. Compare with historical records only where old token, time, retry, and outcome data are complete. Treat prior knowledge of the task, changed models, and selection of replay cases as confounds; use replay mainly to find regressions and estimate feasibility.
2. **Prospective matched evaluation.** Assign multiple new, comparable WIs to the prior style of workflow and DUREH. Hold model/version, available tools, task type, and acceptance criteria as constant as possible; randomize or alternate assignment within matched pairs. Blind the checker to workflow where practical. Report task mix, failures, and uncertainty rather than a single aggregate win rate.
3. **Operational burn-in.** Operate the selected workflow for a predeclared 1–4 week period. Retain a recovery path to the prior system and audit actual promotion, external actions, and incidents. The original proposal specifies one week; a longer period may be needed to see rare failures.

The primary efficiency measure is **cost per successfully promoted WI**: total model and execution cost for all attempted WIs, including retries, verification, and failed attempts, divided by the number that pass the acceptance gate and are promoted. Report numerator, denominator, and the fraction promoted; an undefined value when none succeed must not be hidden. The other primary outcome is **human intervention burden**, measured as Director attention minutes per promoted WI and interventions, clarifications, and approval decisions per attempted WI. A reduction in model cost that raises human burden is not an unqualified improvement.

Secondary outcomes are promotion success rate, rework count, wall time, token use, coordination defects, duplicate verification, record indexing overhead, unsafe or unauthorized action attempts, rollback/compensation failures, and console/recovery success. Compare distributions by task class as well as totals. Predeclare safety stop conditions, including an unauthorized write or disclosure, unrecoverable state loss, or failed promotion integrity. Thresholds such as “half the tokens” or “three reorganization findings” in the source are targets or provisional defaults, not validated cutoffs.

## 6. Security and threat model

Trust boundaries run between the Director and desk, each temporary agent, the site filesystem, reference repositories, model providers, the console, and external destinations. Reference content—including README and AGENTS.md files—may contain instructions crafted to redirect an agent. It is task data, not authority. A short briefing reduces context burden but **does not create a security boundary**; enforcement must come from tool permissions, filesystem isolation, and audited gates.

The intended controls are least-privilege role capabilities and path ownership, no direct worker access to secrets or site records, scoped network access, allowlisted external destinations, and desk-controlled push/deployment gates. Secrets should be supplied at runtime and excluded from packages, logs, and contributions. Shell execution and Slurm submission need bounded commands, identities, outputs, and timeouts. Research data, including possible PHI/PII, requires site-specific classification and approved processing locations. Console access via OOD or VPN is an access path, not a complete authentication and authorization model; multi-user access remains out of scope. Upstream contributions need review of diffs, artifacts, metadata, and license status in addition to automated scans.

These are requirements for the proposed implementation. The case study does not demonstrate that they are enforced. A security review and adversarial tests should precede use with sensitive data or external write privileges.

## 7. Related work and evidence level

DUREH builds on existing role specialization, hierarchical coordination, memory, verification, and installable tooling. MAST classifies failures in 1,642 multi-agent traces into system design, inter-agent misalignment, and task verification; its taxonomy motivates what to inspect, but it cannot establish the cause of this site's failures. OrgAgent studies a company-style hierarchy on reasoning benchmarks; its results motivate a comparison, not a claim that DUREH's desk/manager/worker structure is optimal. Anthropic's engineering reports describe practical orchestration and context-management patterns, with vendor evidence rather than independent evaluation.

The proposed distinction is the combination of site-owned recoverable state, an installed organization, action-specific control gates, role qualification, and an evidence-backed contribution path. Worktrees, event records, skills, model routing, and multi-agent roles each have precedents. The combination's benefit remains untested. A broader feature matrix and the original survey inventory are kept in the [related-work appendix](DUREH-related-work-appendix.md); its unverified entries are not evidence for claims in this paper.

## 8. Roadmap and limitations

The proposed sequence is Phase 0: design and package definition; Phase 1: core harness and first runtime adapter; Phase 2: state, record index, compatibility checks, and console launch; Phase 3a: cluster deployment; Phase 3b: external deployment and sync; Phase 4: controlled cutover and burn-in; Phase 5: domain package and qualification expansion. Each phase ends with an executable acceptance gate. The detailed gates and unresolved implementation choices are in the technical specification.

Case provenance (pinned source commits, evidence status per observation, and candidate acceptance tests) is kept in a private evidence registry maintained alongside this repository; the public paper carries only paraphrased observations. The evidence comes from one site, one Director, one initial runtime, and a short period. Multi-runtime support, the three console deployment modes, restoration from a pinned snapshot, cross-host record merging, security enforcement, and lower-cost model substitution are proposals until tested. DUREH should be judged by successful promotion, human burden, safety, and recovery—not by the presence of architecture components.

## 9. Selected primary references

- Cemri et al. (2025), [*Why Do Multi-Agent LLM Systems Fail?*](https://arxiv.org/abs/2503.13657). Empirical failure taxonomy; findings apply to the studied trace set.
- Wang et al. (2026), [*OrgAgent: Organize Your Multi-Agent System like a Company*](https://arxiv.org/abs/2604.01020). Preprint on company-style hierarchy and reasoning tasks.
- Anthropic (2025), [*How we built our multi-agent research system*](https://www.anthropic.com/engineering/multi-agent-research-system). Vendor engineering report.
- Anthropic (2025), [*Effective context engineering for AI agents*](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents). Vendor guidance on distilled subagent context; it gives 1,000–2,000 tokens as an example, not a universal limit.
- Hong et al. (2023), [*MetaGPT*](https://arxiv.org/abs/2308.00352). Preprint on role-based software-agent workflows.
- Shinn et al. (2023), [*Reflexion*](https://arxiv.org/abs/2303.11366). Preprint on verbal experience in agents.
- Chen et al. (2023), [*FrugalGPT*](https://arxiv.org/abs/2305.05176). Preprint on cost-aware model cascades.

Bibliographic metadata and venue status should be checked against final publisher records before formal publication. The [survey inventory](DUREH-related-work-appendix.md) lists additional sources with their verification status.
