# Qualification exam scaffold

Each task: `<role>/<task-id>/task.yaml` plus an optional `fixture/`. A candidate works on a copy of the fixture; `dureh verify --exam <role> --task <task-id> --answers <workspace>` runs the task checks, compares the workspace against the fixture, and records an exam run.

Phase 1 ships one `split: example` task for some roles. These tasks demonstrate the scorer; they are not a hidden hold-out set and qualify no model. Production exams need diverse hold-out tasks, repeated stochastic runs, and uncertainty reporting (technical specification section 6). Until then each tier runs under a labeled, dated bootstrap waiver in the site config.

Release 90 adds per-tier model qualification on the company's own promoted work (`dureh exam build|run|score|compare|plan`,
the strict `qualification: run-<id>` gate): docs/exams.md. The scaffold tasks here remain the role-level scorer.
