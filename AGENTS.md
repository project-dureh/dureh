# DUREH implementation instructions

This repository is a design blueprint. Treat all claims about installation, deployment, security, recovery, and performance as unverified until demonstrated by executable evidence.

## Source of truth and scope

Read `docs/DUREH-public-white-paper.md` for the thesis and evaluation goals, and `docs/DUREH-technical-specification.md` for proposed interfaces, phase gates, and unresolved choices. The technical specification is a proposal, not executable code. Preserve the distinctions among reusable harness, transferable startup package, and site-owned state. Do not put site records, persona, memory, secrets, or research data in this repository.

Start at Phase 0 of the technical specification. Create a short implementation plan that names the first deliverable, its acceptance tests, dependencies, and the open decisions blocking it. Then implement the smallest complete slice that can be tested. Continue phase by phase; do not claim that a later phase works because its directory or configuration file exists.

## Engineering rules

- Mark a requirement as `proposed`, `implemented`, or `verified`; use `verified` only with a recorded test result.
- Do not invent missing config schemas, record transitions, security guarantees, exam thresholds, or infrastructure paths as if the documents had settled them. Document a proposed choice and its rationale, or request a decision when it materially affects ownership, privacy, or irreversible action.
- Keep the runtime-neutral organization definition separate from generated runtime adapter files. Claude Code is the proposed first adapter; a second runtime must pass equivalent acceptance tests before claiming portability.
- Enforce write and external-action boundaries in tools or sandbox permissions. Briefing size and prompt instructions are not security controls.
- Use isolated workspaces for implementation and run the acceptance checks for each phase. Record commands, outcomes, known failures, and any deferred checks in the pull request or implementation report.
- Do not perform external push, deployment, data migration, or access to sensitive site data without the project's authorization and the applicable phase gate.

The original uploaded HTML is not in this repository. The documents here preserve its supported measurements but are not a substitute for raw operational data when validating the case study.
