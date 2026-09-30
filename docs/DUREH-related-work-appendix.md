# DUREH Related Work and Survey Inventory

Companion to the [public white paper](DUREH-public-white-paper.md). This appendix preserves the broad Revision 2 survey for comparison and future source verification. A dash in the matrix means only that the referenced document did not address a feature. System documentation, empirical taxonomies, and vendor guidance are different evidence types and should not be scored as equivalent systems.

The source survey supplied many discovery-index links. Those have been removed below; entries without a direct primary link are marked **[primary source to verify]**. Author, venue, and evidence-level labels inherited from that survey also need checking before scholarly publication.

## 1. Comparison and related systems

The source bibliography places DUREH beside empirical failure taxonomies, multi-agent hierarchy studies, vendor operating reports, and installable agent frameworks. MAST (Cemri et al., NeurIPS 2025; peer-reviewed empirical classification) supports examining specification, coordination, and verification failures in its trace set; it does not explain this site's failures causally. OrgAgent (Wang et al., 2026; preprint) and other hierarchy studies motivate testing stable roles and controlled information flow, but their benchmarks do not establish that this exact desk/manager/worker structure is optimal. Anthropic's multi-agent engineering guidance (vendor report) supplies practical design hypotheses, not independent validation. Performance and cost percentages from secondary commercial summaries are omitted here.

### Comparison with nearby designs

Entries reflect public documentation as of 2026-09-30. A dash means "not addressed in that document", not "impossible there"; taxonomies (MAST) and guidance (Anthropic) are not systems, so several cells are structurally empty for them.

| Axis | OrgAgent | Anthropic guidance / Research system | MAST | oh-my-codex / oh-my-agent | Claude Code subagents | DUREH (proposed) |
|---|---|---|---|---|---|---|
| Hierarchy | Governance / execution / compliance, fixed | Lead + 3–5 parallel subagents (one level) | — (taxonomy) | Role and team orchestration | Session → subagent (one level) | Desk → temporary manager → specialists (two levels, depth limited by tooling) |
| Persistent persona and memory | — | — | — | — | — | Desk only (versioned dataset) |
| Need-to-know briefing, distilled reports | Controlled information flow | Distilled summaries; Anthropic gives 1,000–2,000 tokens as an example | Inter-agent misalignment category: 36.9% in the studied traces | — | Results returned to parent | Bounded briefing and report sizes (provisional defaults); ownership matrix |
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

## 2. Survey inventory

Evidence level in brackets: [PR] peer-reviewed venue as stated by the source; [pre] preprint; [vendor] vendor engineering report or pricing page; [sw] software project; [mgmt] management or engineering literature outside AI. Items marked "(consulted)" were read during the survey but are not cited in the text. Metadata should be verified against primary publications before formal publication.

**Failure taxonomy and multi-agent structure**
- Cemri, M. et al. (2025). Why Do Multi-Agent LLM Systems Fail? NeurIPS 2025 Datasets & Benchmarks. https://arxiv.org/abs/2503.13657 [PR]
- Wang, Y. et al. (2026). OrgAgent: Organize Your Multi-Agent System like a Company. https://arxiv.org/abs/2604.01020 [pre]
- Bogdanov, I. et al. (2026). Context, Reasoning, and Hierarchy: A Cost-Performance Study of Compound LLM Agent Design in an Adversarial POMDP. ACM Conference on AI and Agentic Systems. https://arxiv.org/abs/2605.16205 [PR venue reported by source; venue metadata still to verify]
- Gao, M. et al. (2025). Single-agent or Multi-agent Systems? Why Not Both? [primary source to verify] [pre]
- Tran, D. et al. (2026). Single-Agent LLMs Outperform Multi-Agent Systems on Multi-Hop Reasoning Under Equal Thinking Token Budgets. [primary source to verify] [pre]
- Zywot, A. et al. (2026). Can Small Agents Collaborate to Beat a Single Large Language Model? [primary source to verify] [pre]
- Klang, E. et al. (2025). Orchestrated multi agents sustain accuracy under clinical-scale workloads compared to a single agent. npj Health Systems. [primary source to verify] [PR]
- Kulkarni, S. et al. (2026). Benchmarking Multi-Agent LLM Architectures for Financial Document Processing. [primary source to verify] [pre]
- Scaling Behavior of Single LLM-Driven Multi-Agent Systems (2026). https://arxiv.org/abs/2606.00655 [pre]
- Understanding Agent Scaling in LLM-Based Multi-Agent Systems via Diversity (2026). https://arxiv.org/abs/2602.03794 [pre]
- OrchBench: Evaluating Multi-Agent Orchestration Plans in Isolation via Deterministic Simulation (2026). https://arxiv.org/abs/2607.25656 [pre]
- Towards a Science of Scaling Agent Systems (2025). https://arxiv.org/abs/2512.08296 [pre]
- Literature Review of Multi-Agent Debate for Problem-Solving (2025). https://arxiv.org/abs/2506.00066 [pre]
- Estornell, A. et al. (2025). How to Train a Leader: Hierarchical Reasoning in Multi-Agent LLMs. [primary source to verify] [pre] (consulted)
- Luo, Y. et al. (2026). AgentArk: Distilling Multi-Agent Intelligence into a Single LLM Agent. [primary source to verify] [pre] (consulted)
- Liu, X. et al. (2026). Markets, Not Planners: Decentralized Orchestration of LLM Agents with Private Information. [primary source to verify] [pre] (consulted)
- Wang, Z. et al. (2025). AgentDropout: Dynamic Agent Elimination for Token-Efficient and High-Performance LLM-Based Multi-Agent Collaboration. [primary source to verify] [pre] (consulted)
- Scaling Multi-Agent Systems with Prospect-State (2026). https://arxiv.org/abs/2609.08033 [pre] (consulted)
- Anthropic (2025). When to use multi-agent systems (and when not to). https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them [vendor]
- Anthropic Engineering (2025). How we built our multi-agent research system. https://www.anthropic.com/engineering/multi-agent-research-system [vendor]
- Urwick, L. F. (1956). The Manager's Span of Control. Harvard Business Review; Graicunas, V. A. (1933). Relationship in Organization. [mgmt]

**Self-correction and retry limits**
- Arimbur, J. J. (2026). How Many Tries Does It Take? Iterative Self-Repair in LLM Code Generation Across Model Scales and Benchmarks. [primary source to verify] [pre]
- Kamoi, R. et al. (2024). When Can LLMs Actually Correct Their Own Mistakes? A Critical Survey of Self-Correction of LLMs. TACL. https://aclanthology.org/2024.tacl-1.78/ [PR]
- Liu, A.-F. et al. (2026). Self-Correction as Feedback Control: Error Dynamics, Stability Thresholds, and Prompt Interventions in LLMs. [primary source to verify] [pre]
- Madaan, A. et al. (2023). Self-Refine: Iterative Refinement with Self-Feedback. [primary source to verify] [pre] (consulted)
- Yang, Z. et al. (2025). A Probabilistic Inference Scaling Theory for LLM Self-Correction. [primary source to verify] [pre] (consulted)
- Chen, J. et al. (2024). MAgICoRe: Multi-Agent, Iterative, Coarse-to-Fine Refinement for Reasoning. [primary source to verify] [pre] (consulted)
- Jiang, D. et al. (2024). SELF-[IN]CORRECT: LLMs Struggle with Refining Self-Generated Responses. [primary source to verify] [pre] (consulted)
- Xiong, W. et al. (2025). Self-rewarding correction for mathematical reasoning. [primary source to verify] [pre] (consulted)
- Liu, G.-D. et al. (2024). On the Intrinsic Self-Correction Capability of LLMs: Uncertainty and Latent Concept. [primary source to verify] [pre] (consulted)
- Pipal, C. et al. (2026). When Does Self-Correction Work for LLMs in Content Analysis? Computational Communication Research. [primary source to verify] [PR] (consulted)

**Task decomposition and work-item size**
- Xu, D. et al. (2025). AgentSynth: Scalable Task Generation for Generalist Computer-Use Agents. [primary source to verify] [pre]
- Yang, X. et al. (2026). When Parallelism Pays Off: Cohesion-Aware Task Partitioning for Multi-Agent Coding. [primary source to verify] [pre]
- Mehtiyev, T. et al. (2026). Beyond Resolution Rates: Behavioral Drivers of Coding Agent Success and Failure. [primary source to verify] [pre]
- Albayaydh, W. S. et al. (2026). Beyond the Leaderboard: A Synthesis of Tool-Use, Planning, and Reasoning Failures in Large Language Model Agents. [primary source to verify] [pre]
- Sun, W. et al. (2025). Scaling Long-Horizon LLM Agent via Context-Folding. [primary source to verify] [pre] (consulted)
- Wang, X. et al. (2026). The Long-Horizon Task Mirage? Diagnosing Where and Why Agentic Systems Break. [primary source to verify] [pre] (consulted)
- Wang, T. et al. (2026). A Subgoal-driven Framework for Improving Long-Horizon LLM Agents. [primary source to verify] [pre] (consulted)
- Ge, C. et al. (2026). Agent psychometrics: Task-level performance prediction in agentic coding benchmarks. [primary source to verify] [pre] (consulted)
- Liu, Y. (2026). KLong: Training LLM Agent for Extremely Long-horizon Tasks. [primary source to verify] [pre] (consulted); Zhou, T.-X. et al. (2025). STEP Planner. IROS 2025. [primary source to verify] [PR] (consulted)
- Cohen, J. (2006). Best Kept Secrets of Peer Code Review. SmartBear/Cisco study. [mgmt]

**Model selection, small and open-weight models**
- Belcák, P. et al. (2025). Small Language Models are the Future of Agentic AI. NVIDIA. https://arxiv.org/abs/2506.02153 [pre]
- Sharma, R. et al. (2025). Small Language Models for Agentic Systems: A Survey of Architectures, Capabilities, and Deployment Trade-offs. [primary source to verify] [pre]
- Sankaran, G. (2026). Small Language Models and Spec-Driven Development for High-Accuracy Agentic AI Systems. International Journal of Intelligent Systems and Data Science. [primary source to verify] [PR]
- Bijoy, B. S. et al. (2025). ProST: Progressive Sub-task Training for Pareto-Optimal Multi-agent Systems Using Small Language Models. [primary source to verify] [pre] (consulted)
- Alameda, C. et al. (2026). Towards Reliable Reasoning with Multi-Agent Cognitive Collaboration in Small Language Models (MACoMoE). [primary source to verify] [pre] (consulted)
- Xiong, Y. et al. (2025). ResilioMate: A Resilient Multi-Agent Task Executing Framework for Enhancing Small Language Models. IEEE Access. [primary source to verify] [PR] (consulted)
- Srivastava, G. et al. (2026). EffGen: Enabling Small Language Models as Capable Autonomous Agents. [primary source to verify] [pre] (consulted)
- Wang, F. et al. (2024). A Comprehensive Survey of Small Language Models in the Era of Large Language Models. ACM TIST. [primary source to verify] [PR] (consulted)
- Nandi, P. et al. (2026). Beyond Large Language Models: How Small, Specialized Agents are Replacing Monolithic Systems. [primary source to verify] [pre] (consulted)
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
