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
2. **Portable organizational state model.** A reusable harness and transferable startup package are versioned separately from site-bound data. A site-owned state snapshot can be referenced by a package version to recover the *same* installation, without making that state portable to a different company.
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
                                                 versioned recovery snapshot
```

The desk alone maintains persona and organizational memory and speaks for the organization to the Director. Managers and specialists receive bounded work-item context, produce a defined artifact, and terminate. Managers coordinate related work items; researcher and document work may be dispatched directly by the desk. The initial design has three manager domains and nine worker types, but their exact count is a package and implementation choice, not evidence of an optimal staffing ratio. A work item (WI) states its goal, scope, owner, and completion criteria. A bundle groups related WIs for integration. Verification precedes promotion into the main branch.

The harness contains reusable organization logic. The startup package contains a company's values, skills, lessons, evaluations, and bootstrap material. The site contains its configuration, projects, and operating data. **State is logically site-owned but package-referenced.** The package may pin a private state dataset for recovery of the same organization. Installing the package at a new site must start with new state unless an authorized recovery is explicitly requested.

| Layer | Portable to another installation? | Versioned? | Shared by default? |
|---|---|---|---|
| Harness | Intended | Yes | Yes |
| Startup package, excluding state pin | Intended | Yes | By authorized distribution |
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

The contribution path is deliberately different from an ordinary open-source pull request in three respects: the author of the change is the organization (desk and specialists), not a person; the change must carry machine-checkable evidence (a regression case and passing qualification/self-tests); and no site data leaves the installation. The proposed steps:

| Step | What | Who | Gate |
|---|---|---|---|
| 1 | Site finding becomes an evidence-backed lesson | Desk | No measurement, no lesson |
| 2 | Change on a `contrib/<site>-<lesson-id>` branch of the harness (or package) plus a regression case in `org/exams/` or the self-test suite | Specialist owning the affected paths | Path-ownership matrix |
| 3 | `verify --self`: self-test, qualification exams of affected roles, privacy scan (site paths, hostnames, record ids, personal names, project names in the diff cause rejection) | Checker | No pass, no PR |
| 4 | PR body generated from the lesson record (problem, measurement, change, regression case, affected roles); commit trailer `Lesson: <site-id>/<lesson-id>` | Desk | — |
| 5 | External push and PR creation require the site's approval gate (external action class) | Director and desk | One approval record |
| 6 | Upstream CI repeats `verify --self`, license and privacy review; maintainer merges; all sites receive the change on update | Upstream | PRs without a regression case are closed by CI |

One PR carries one lesson. Package-level lessons (domain skills, templates) go to the package upstream through the same path. The same exams gate both model qualification (Section 4) and contribution admission, so experience accumulates across installations only in verified form. Known limits: CI and maintainer load; whether a regression case means the same thing across runtimes and models (adapters must keep exams runtime-neutral); false positives and negatives in the privacy scan. This loop is untested until a second installation exists.

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

The source bibliography places DUREH beside empirical failure taxonomies, multi-agent hierarchy studies, vendor operating reports, and installable agent frameworks. MAST (Cemri et al., NeurIPS 2025; peer-reviewed empirical classification) supports examining specification, coordination, and verification failures in its trace set; it does not explain this site's failures causally. OrgAgent (Wang et al., 2026; preprint) and other hierarchy studies motivate testing stable roles and controlled information flow, but their benchmarks do not establish that this exact desk/manager/worker structure is optimal. Anthropic's multi-agent engineering guidance (vendor report) supplies practical design hypotheses, not independent validation. Performance and cost percentages from secondary commercial summaries are omitted here.

### Comparison with nearby designs

Entries reflect public documentation as of 2026-09-30. A dash means "not addressed in that document", not "impossible there"; taxonomies (MAST) and guidance (Anthropic) are not systems, so several cells are structurally empty for them.

| Axis | OrgAgent | Anthropic guidance / Research system | MAST | oh-my-codex / oh-my-agent | Claude Code subagents | DUREH (proposed) |
|---|---|---|---|---|---|---|
| Hierarchy | Governance / execution / compliance, fixed | Lead + 3–5 parallel subagents (one level) | — (taxonomy) | Role and team orchestration | Session → subagent (one level) | Desk → temporary manager → specialists (two levels, depth limited by tooling) |
| Persistent persona and memory | — | — | — | — | — | Desk only (versioned dataset) |
| Need-to-know briefing, distilled reports | Controlled information flow | Summaries of 50–100 tokens upward (as reported) | Context loss is 36.9% of failures | — | Results returned to parent | Bounded briefing and report sizes (provisional defaults); ownership matrix |
| Verification separation | Compliance layer | Dedicated verification subagent recommended | Verification failures 21.3% | Independent judges, artifact checks | — | One checker plus repeatable `verify`; retry cap |
| Reversible control | — | — | — | Stop-hook gates | Permissions, worktrees | Per-WI worktree, typed records, promotion gate, action classes with remedies |
| Enforced path ownership | — | — | — | — | Hooks available; not built in | Role-to-glob matrix enforced at promotion (diff vs. globs) and assisted by hooks |
| Records | — | — | Annotated traces | Append-only event log | Session transcripts | Schema-validated JSON records + disposable node-local index + DataLad |
| State backup and same-site recovery | — | — | — | — | — | DataLad/RIA snapshot pinned by a private deployment manifest |
| Installable / plugin | — | — | — | npm install, shared skills | Plugins, skills | `clone` + `install.sh --startup <package>`; harness / package / site layers |
| Runtime neutrality | Multiple models | Claude | Seven frameworks analyzed | Codex-centric (oh-my-agent claims portability) | Claude Code | Runtime-neutral `org/` definition with adapters (Claude Code first; portability not demonstrated) |
| Model selection rule | — | Opus lead / Sonnet workers (practice) | — | — | Per-agent model setting | Tier → provider mapping with span/size rules and role qualification exams |
| Internal reorganization protocol | — | — | — | — | — | Seven controls (isolation, frozen scope, one lane, phase gates, rollback record, finding budget, burn-in) |
| HPC (Slurm, OOD) and DataLad data | — | — | — | — | — | Compute only through Slurm; OOD back office; DataLad superdataset projects |
| Site → upstream contribution | — | — | — | Ordinary open-source PRs | — | Agent-authored PRs carrying lessons and regression cases; exams and privacy checks; human approval |
| Evaluation | Reasoning benchmarks | Internal evals (e.g., BrowseComp) | 1,642 annotated traces | — | — | Planned only (Section 5); no results yet |

### Related systems not in the table

- **Role-based multi-agent frameworks.** MetaGPT (Hong et al., 2023) encodes standard operating procedures and role specialization with structured hand-offs; ChatDev (Qian et al., 2023) runs a chat chain across software roles; AutoGen (Wu et al., 2023) provides conversable agents and a group-chat manager; CrewAI and LangGraph offer role-based crews and graph/supervisor orchestration. DUREH shares role specialization and manager-led integration; it differs in treating the organization as an installed, versioned artifact with site-bound state, promotion gates, and an audited external-action boundary rather than as an in-process orchestration library.
- **Persistent memory for agents.** MemGPT/Letta (Packer et al., 2023) gives a single agent tiered, self-managed memory. DUREH's asymmetry — only the desk has persona and memory, kept in a versioned dataset with a size cap and change events — is an organizational choice about where continuity lives, not a memory-management contribution.
- **Experience accumulation.** Reflexion (Shinn et al., 2023) stores verbal self-reflections; ExpeL (Zhao et al., 2023) extracts insights from experience; Voyager (Wang et al., 2023) grows a skill library; Agentic Context Engineering (Zhang et al., 2025) evolves playbooks. DUREH accumulates experience only as evidence-backed lessons that become executable safeguards, exam cases, or skills, and only through review.
- **Self-improving agents.** The Darwin Gödel Machine (Zhang et al., 2025) and the Self-Improving Coding Agent (Robeyns et al., 2025) let an agent modify its own code and keep an archive of variants. DUREH's contribution loop is deliberately narrower: changes to shared machinery are proposed by agents but admitted only with a regression case, qualification results, a privacy check, and human approval.
- **Model routing by cost.** FrugalGPT (Chen et al., 2023) and RouteLLM (Ong et al., 2024) route queries across models by cost and quality. DUREH's tier-and-exam rule is coarser and organizational: a role is filled by the lowest tier that has passed that role's exam; routing per query is out of scope.

What remains distinctive is the combination — a desk-led, reversibility-aware, evidence-accumulating organization installed as software with same-site recovery and an agent-authored contribution path — and that combination is unvalidated until Section 5 is executed.

## 8. Roadmap and limitations

The proposed sequence is Phase 0: design and package definition; Phase 1: core harness and first runtime adapter; Phase 2: state, record index, compatibility checks, and console launch; Phase 3a: cluster deployment; Phase 3b: external deployment and sync; Phase 4: controlled cutover and burn-in; Phase 5: domain package and qualification expansion. Each phase ends with an executable acceptance gate. The detailed gates and unresolved implementation choices are in the technical specification.

Case provenance (pinned source commits, evidence status per observation, and candidate acceptance tests) is kept in a private evidence registry maintained alongside this repository; the public paper carries only paraphrased observations. The evidence comes from one site, one Director, one initial runtime, and a short period. Multi-runtime support, the three console deployment modes, restoration from a pinned snapshot, cross-host record merging, security enforcement, and lower-cost model substitution are proposals until tested. DUREH should be judged by successful promotion, human burden, safety, and recovery—not by the presence of architecture components.

## 9. References

Evidence level in brackets: [PR] peer-reviewed venue as stated by the source; [pre] preprint; [vendor] vendor engineering report or pricing page; [sw] software project; [mgmt] management or engineering literature outside AI. Items marked "(consulted)" were read during the survey but are not cited in the text. Metadata should be verified against primary publications before formal publication.

**Failure taxonomy and multi-agent structure**
- Cemri, M. et al. (2025). Why Do Multi-Agent LLM Systems Fail? NeurIPS 2025 Datasets & Benchmarks. https://arxiv.org/abs/2503.13657 [PR]
- Wang, Y. et al. (2026). OrgAgent: Organize Your Multi-Agent System like a Company. https://consensus.app/papers/details/dbc1af9c61f055fda75eebc1bc88e57e/ [pre]
- Bogdanov, I. et al. (2026). Context, Reasoning, and Hierarchy: A Cost-Performance Study of Compound LLM Agent Design in an Adversarial POMDP. ACM Conference on AI and Agentic Systems. https://consensus.app/papers/details/cc3b0ac4a0495d589b0a8cea3b5b84b3/ [PR]
- Gao, M. et al. (2025). Single-agent or Multi-agent Systems? Why Not Both? https://consensus.app/papers/details/6febe98f1c4b588c882a5b8e29d00e22/ [pre]
- Tran, D. et al. (2026). Single-Agent LLMs Outperform Multi-Agent Systems on Multi-Hop Reasoning Under Equal Thinking Token Budgets. https://consensus.app/papers/details/01fb4c4a8f885bd095f6179b188e57f4/ [pre]
- Zywot, A. et al. (2026). Can Small Agents Collaborate to Beat a Single Large Language Model? https://consensus.app/papers/details/10f63a64e1065f40bf88e2a0782d5eda/ [pre]
- Klang, E. et al. (2025). Orchestrated multi agents sustain accuracy under clinical-scale workloads compared to a single agent. npj Health Systems. https://consensus.app/papers/details/9f038e165a4f567e900ef8c5851c1126/ [PR]
- Kulkarni, S. et al. (2026). Benchmarking Multi-Agent LLM Architectures for Financial Document Processing. https://consensus.app/papers/details/fffe869d957552e09cc71006b7839375/ [pre]
- Scaling Behavior of Single LLM-Driven Multi-Agent Systems (2026). https://arxiv.org/abs/2606.00655 [pre]
- Understanding Agent Scaling in LLM-Based Multi-Agent Systems via Diversity (2026). https://arxiv.org/abs/2602.03794 [pre]
- OrchBench: Evaluating Multi-Agent Orchestration Plans in Isolation via Deterministic Simulation (2026). https://arxiv.org/abs/2607.25656 [pre]
- Towards a Science of Scaling Agent Systems (2025). https://arxiv.org/abs/2512.08296 [pre]
- Literature Review of Multi-Agent Debate for Problem-Solving (2025). https://arxiv.org/abs/2506.00066 [pre]
- Estornell, A. et al. (2025). How to Train a Leader: Hierarchical Reasoning in Multi-Agent LLMs. https://consensus.app/papers/details/a448f4e32fed59398d3be2ed4729a9d3/ [pre] (consulted)
- Luo, Y. et al. (2026). AgentArk: Distilling Multi-Agent Intelligence into a Single LLM Agent. https://consensus.app/papers/details/0b481e282e2259648c93007fd2c7c48e/ [pre] (consulted)
- Liu, X. et al. (2026). Markets, Not Planners: Decentralized Orchestration of LLM Agents with Private Information. https://consensus.app/papers/details/c8e85efc892d598f872fbf2ef1676750/ [pre] (consulted)
- Wang, Z. et al. (2025). AgentDropout: Dynamic Agent Elimination for Token-Efficient and High-Performance LLM-Based Multi-Agent Collaboration. https://consensus.app/papers/details/524dc0fba898575f97ff2b064b2035c7/ [pre] (consulted)
- Scaling Multi-Agent Systems with Prospect-State (2026). https://arxiv.org/abs/2609.08033 [pre] (consulted)
- Anthropic (2025). When to use multi-agent systems (and when not to). https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them [vendor]
- Anthropic Engineering (2025). How we built our multi-agent research system. https://www.anthropic.com/engineering/multi-agent-research-system [vendor]
- Urwick, L. F. (1956). The Manager's Span of Control. Harvard Business Review; Graicunas, V. A. (1933). Relationship in Organization. [mgmt]

**Self-correction and retry limits**
- Arimbur, J. J. (2026). How Many Tries Does It Take? Iterative Self-Repair in LLM Code Generation Across Model Scales and Benchmarks. https://consensus.app/papers/details/d6ac6a8419005cef8321d69e1cede137/ [pre]
- Kamoi, R. et al. (2024). When Can LLMs Actually Correct Their Own Mistakes? A Critical Survey of Self-Correction of LLMs. TACL. https://consensus.app/papers/details/0b4b3d8d456a561dba463d2c33d1cb35/ [PR]
- Liu, A.-F. et al. (2026). Self-Correction as Feedback Control: Error Dynamics, Stability Thresholds, and Prompt Interventions in LLMs. https://consensus.app/papers/details/e3fedb9eb9c356d098301344c62f74ef/ [pre]
- Madaan, A. et al. (2023). Self-Refine: Iterative Refinement with Self-Feedback. https://consensus.app/papers/details/0a9a1801896254908000fce268aa58d0/ [pre] (consulted)
- Yang, Z. et al. (2025). A Probabilistic Inference Scaling Theory for LLM Self-Correction. https://consensus.app/papers/details/4be1a0e7efa752108ef0c898adf91d85/ [pre] (consulted)
- Chen, J. et al. (2024). MAgICoRe: Multi-Agent, Iterative, Coarse-to-Fine Refinement for Reasoning. https://consensus.app/papers/details/3fdd7dc426cb514b88ee0ae15b379455/ [pre] (consulted)
- Jiang, D. et al. (2024). SELF-[IN]CORRECT: LLMs Struggle with Refining Self-Generated Responses. https://consensus.app/papers/details/4413fc107db1552a910d587c29d7a1be/ [pre] (consulted)
- Xiong, W. et al. (2025). Self-rewarding correction for mathematical reasoning. https://consensus.app/papers/details/aadbc5e217b85efbaa9653b919b22c76/ [pre] (consulted)
- Liu, G.-D. et al. (2024). On the Intrinsic Self-Correction Capability of LLMs: Uncertainty and Latent Concept. https://consensus.app/papers/details/b8fa0779d8b65c0697bb2faecbb1e816/ [pre] (consulted)
- Pipal, C. et al. (2026). When Does Self-Correction Work for LLMs in Content Analysis? Computational Communication Research. https://consensus.app/papers/details/5ea5e583dc9b595bb4b93e3238493e9e/ [PR] (consulted)

**Task decomposition and work-item size**
- Xu, D. et al. (2025). AgentSynth: Scalable Task Generation for Generalist Computer-Use Agents. https://consensus.app/papers/details/8e81a6394bdb5301b86cff799808f961/ [pre]
- Yang, X. et al. (2026). When Parallelism Pays Off: Cohesion-Aware Task Partitioning for Multi-Agent Coding. https://consensus.app/papers/details/0441e482e4995676b62202b6469fb9b3/ [pre]
- Mehtiyev, T. et al. (2026). Beyond Resolution Rates: Behavioral Drivers of Coding Agent Success and Failure. https://consensus.app/papers/details/80fc8e76e2875d47a7875748f46da93e/ [pre]
- Albayaydh, W. S. et al. (2026). Beyond the Leaderboard: A Synthesis of Tool-Use, Planning, and Reasoning Failures in Large Language Model Agents. https://consensus.app/papers/details/86dffe21659059d8a29457cb572f30b1/ [pre]
- Sun, W. et al. (2025). Scaling Long-Horizon LLM Agent via Context-Folding. https://consensus.app/papers/details/64c6090637c756e6a5e29e59f6e22739/ [pre] (consulted)
- Wang, X. et al. (2026). The Long-Horizon Task Mirage? Diagnosing Where and Why Agentic Systems Break. https://consensus.app/papers/details/00ee5fb784795538b0bf9ed9df617e65/ [pre] (consulted)
- Wang, T. et al. (2026). A Subgoal-driven Framework for Improving Long-Horizon LLM Agents. https://consensus.app/papers/details/4bea9aaeadd15d97a1ab7e8b9675afca/ [pre] (consulted)
- Ge, C. et al. (2026). Agent psychometrics: Task-level performance prediction in agentic coding benchmarks. https://consensus.app/papers/details/7a953637537d5b6ab725d43202265a96/ [pre] (consulted)
- Liu, Y. (2026). KLong: Training LLM Agent for Extremely Long-horizon Tasks. https://consensus.app/papers/details/e91cc90552935dd3ada3d5d9f3588916/ [pre] (consulted); Zhou, T.-X. et al. (2025). STEP Planner. IROS 2025. https://consensus.app/papers/details/d7d4489355255040886376c6fd5942ce/ [PR] (consulted)
- Cohen, J. (2006). Best Kept Secrets of Peer Code Review. SmartBear/Cisco study. [mgmt]

**Model selection, small and open-weight models**
- Belcák, P. et al. (2025). Small Language Models are the Future of Agentic AI. NVIDIA. https://arxiv.org/abs/2506.02153 [pre]
- Sharma, R. et al. (2025). Small Language Models for Agentic Systems: A Survey of Architectures, Capabilities, and Deployment Trade-offs. https://consensus.app/papers/details/e2b6a6f169825d17a89b8658099dd356/ [pre]
- Sankaran, G. (2026). Small Language Models and Spec-Driven Development for High-Accuracy Agentic AI Systems. International Journal of Intelligent Systems and Data Science. https://consensus.app/papers/details/abedc9de1abc94b07df7e17c/ [PR]
- Bijoy, B. S. et al. (2025). ProST: Progressive Sub-task Training for Pareto-Optimal Multi-agent Systems Using Small Language Models. https://consensus.app/papers/details/e1fff8a6b49f57109b6964ac20dade19/ [pre] (consulted)
- Alameda, C. et al. (2026). Towards Reliable Reasoning with Multi-Agent Cognitive Collaboration in Small Language Models (MACoMoE). https://consensus.app/papers/details/2210572c9352576987b83dbc18768bb8/ [pre] (consulted)
- Xiong, Y. et al. (2025). ResilioMate: A Resilient Multi-Agent Task Executing Framework for Enhancing Small Language Models. IEEE Access. https://consensus.app/papers/details/1b2f678f280c5a50a0946d294b829125/ [PR] (consulted)
- Srivastava, G. et al. (2026). EffGen: Enabling Small Language Models as Capable Autonomous Agents. https://consensus.app/papers/details/a2c959a23bd45759bc80632aadc57a0b/ [pre] (consulted)
- Wang, F. et al. (2024). A Comprehensive Survey of Small Language Models in the Era of Large Language Models. ACM TIST. https://consensus.app/papers/details/16e090bb31c75ce2b85952ee4985289d/ [PR] (consulted)
- Nandi, P. et al. (2026). Beyond Large Language Models: How Small, Specialized Agents are Replacing Monolithic Systems. https://consensus.app/papers/details/5e242f0cde5c5f8c97966f5f49085e40/ [pre] (consulted)
- Chen, L., Zaharia, M., Zou, J. (2023). FrugalGPT: How to Use Large Language Models While Reducing Cost and Improving Performance. https://arxiv.org/abs/2305.05176 [pre]
- Ong, I. et al. (2024). RouteLLM: Learning to Route LLMs with Preference Data. https://arxiv.org/abs/2406.18665 [pre]
- Anthropic pricing page (2026-06 snapshot) for current model list and per-token prices. [vendor] (prices are dated site configuration, not evidence)
- CloudZero (2026), Claude Code agents; Data Science Dojo (2026), Fable 5 orchestrator / Sonnet executor workflow. [vendor, secondary] (consulted; percentages not carried into this paper)

**Role-based frameworks, memory, experience, self-improvement**
- Hong, S. et al. (2023). MetaGPT: Meta Programming for a Multi-Agent Collaborative Framework. https://arxiv.org/abs/2308.00352 [pre]
- Qian, C. et al. (2023). ChatDev: Communicative Agents for Software Development. https://arxiv.org/abs/2307.07924 [pre]
- Wu, Q. et al. (2023). AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation. https://arxiv.org/abs/2308.08155 [pre]
- CrewAI (https://github.com/crewAIInc/crewAI) and LangGraph (https://github.com/langchain-ai/langgraph). [sw]
- Packer, C. et al. (2023). MemGPT: Towards LLMs as Operating Systems. https://arxiv.org/abs/2310.08560 [pre]
- Shinn, N. et al. (2023). Reflexion: Language Agents with Verbal Reinforcement Learning. https://arxiv.org/abs/2303.11366 [pre]
- Zhao, A. et al. (2023). ExpeL: LLM Agents Are Experiential Learners. https://arxiv.org/abs/2308.10144 [pre]
- Wang, G. et al. (2023). Voyager: An Open-Ended Embodied Agent with Large Language Models. https://arxiv.org/abs/2305.16291 [pre]
- Zhang, Q. et al. (2025). Agentic Context Engineering: Evolving Contexts for Self-Improving Language Models. https://arxiv.org/abs/2510.04618 [pre]
- Zhang, J. et al. (2025). Darwin Gödel Machine: Open-Ended Evolution of Self-Improving Agents. https://arxiv.org/abs/2505.22954 [pre]
- Robeyns, M. et al. (2025). A Self-Improving Coding Agent. https://arxiv.org/abs/2504.15228 [pre]

**Installable harness precedents**
- oh-my-codex (https://github.com/Yeachan-Heo/oh-my-codex); oh-my-agent (https://github.com/min3754/oh-my-agent); oh-my-agents (https://github.com/sean2077/oh-my-agents); agent-harness, Nix-native (https://github.com/furedea/agent-harness); CodelyTV agent-harness (https://github.com/CodelyTV/agent-harness); awesome-harness-engineering (https://github.com/ai-boost/awesome-harness-engineering). [sw]
