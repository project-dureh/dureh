# RP-0010 revision change log

Source: uploaded `RP-0010-camri-org.md.html` (an HTML rendering of the original Markdown). The source was read as reference and was not edited.

| Change | Result |
|---|---|
| Updated the DUREH expansion to Desk-led, Unbundled, Reversibility-aware, Evidence-accumulating Harness | README and both main documents; removed the former acronym's `Universal` wording from the public paper |
| Shortened title and abstract; made central thesis explicit | [Public white paper](DUREH-public-white-paper.md), abstract and Section 1 |
| Consolidated seven listed contributions into three | Public white paper, Section 1 |
| Reframed case study as observation, consequence, response with cautious causal language | Public white paper, Section 2 |
| Added architecture and learning-lifecycle diagrams; distinguished package reference from site ownership | Public white paper, Section 3; [technical specification](DUREH-technical-specification.md), Sections 1–2 |
| Defined reversible, cancelable, compensatable, and irreversible actions | Public white paper, Section 3; technical specification, Section 5 |
| Separated capability tiers from provider/model names; strengthened exams | Public white paper, Section 4; technical specification, Sections 3 and 6 |
| Added retrospective, prospective matched, and burn-in evaluation with cost per promoted WI and human attention | Public white paper, Section 5; technical specification, Section 10 |
| Added security/threat model and distinguished proposed controls from enforced controls | Public white paper, Section 6; technical specification, Section 11 |
| Shortened related work and labeled evidence levels | Public white paper, Section 7; technical specification, Section 12 |
| Normalized phase numbering and moved implementation details out of the public paper | Public white paper, Section 8; technical specification, Sections 2–9 |
| Excluded internal decision chronology, reviewer log, and naming deliberations | Neither deliverable reproduces those appendices. |

The revision preserves source measurements and terminology while marking implementation, deployment, security, and performance claims as unverified. The HTML did not provide the underlying repositories or executable results, so this is an editorial and design-specification revision rather than a validation report.

## Revision 2 (2026-09-30) — merged from the internal edition after two review rounds

Source: internal edition RP-0010 v20 and two independent review rounds (one OpenAI Codex CLI reviewer, one Claude reviewer). Acronym and remote follow this repository.

| Change | Result |
|---|---|
| Expanded the contribution lifecycle into a six-step gated procedure (agent-authored PR, regression case, exams, privacy scan, human approval, upstream CI) | Public white paper, Section 3 |
| Added a comparison table against OrgAgent, Anthropic guidance, MAST, oh-my-codex, Claude Code subagents, and a "related systems" note covering MetaGPT, ChatDev, AutoGen, CrewAI, LangGraph, MemGPT/Letta, Reflexion, ExpeL, Voyager, ACE, Darwin Gödel Machine, SICA, FrugalGPT, RouteLLM | Public white paper, Section 7 |
| Added the full bibliography from the survey with evidence levels; items consulted but not cited are marked | Public white paper, Section 9 |
| Replaced the package-held state submodule with a private deployment manifest; noted the `ria+` submodule limitation | Technical specification, Section 1 |
| Resolved `ledger update`: immutable records, status as an event projection | Technical specification, Section 4 |
| Added the dispatch adapter contract, manager dispatch authority, ownership enforcement at promotion, desk memory audit, and per-WI ephemeral DataLad clones | Technical specification, Section 5 |
| Raised the exam size guidance (≥30 items or repeated runs) with an interval note | Technical specification, Section 6 |
| Console: own state clone and `decisions/<host>` branch, `DUREH_ROOT`, OOD `view.html.erb` and proxy base path, `node:sqlite` fallback, mode C unattended-sync caveat | Technical specification, Section 7 |
| Reproduced the twelve lessons in full so a package draft can be made from this repository | Technical specification, Section 8 |
| Pointed to the private evidence registry for case provenance | Public white paper, Section 8; README |
