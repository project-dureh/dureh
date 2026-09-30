# DUREH

DUREH is a desk-led, unbundled, reversibility-aware, evidence-accumulating harness for AI-agent organizations. This repository contains the executable harness; company values and domain experience belong in a separate startup package, while persona, memory, records, projects, and configuration belong to a site.

The first implementation is intentionally small. It installs a reviewed startup package into a new site, appends and queries immutable JSON events, and checks role-scoped artifact paths before a harness tool writes them. It does not start agents or claim to sandbox arbitrary subprocesses. Those capabilities need a real runtime isolation layer and their own acceptance evidence.

## Quick start

```sh
./install.sh --startup ../camri-org --site /path/to/new-site
./dureh --site /path/to/new-site event add \
  --kind work-item.created --wi WI-0001 --actor desk \
  --payload '{"title":"Review a bounded CAMRI module task"}'
./dureh --site /path/to/new-site event query --wi WI-0001
./dureh --site /path/to/new-site artifact write --role py-writer \
  --path projects/demo/modules/camri/src/example.py --input /tmp/example.py
```

Installation requires an absent or empty destination and a startup package with `startup.yaml`. It creates a private site deployment manifest and fresh empty state directories. It never imports package state or follows package symlinks.

`artifact write` is a create-only, role-scoped harness operation. Its path check protects writes made through this CLI; it is not a filesystem sandbox for arbitrary shell commands or runtime workers.

## Status

| Capability | Status |
|---|---|
| Phase 0 decisions and acceptance plan | Documented in `PHASE-0.md` |
| Empty-site install and startup package validation | Implemented; acceptance not yet recorded |
| Append-only event create and query | Implemented; acceptance not yet recorded |
| Role-scoped write authorization | Implemented for writes mediated by this CLI; not a process sandbox |
| Runtime adapters, isolated dispatch, state recovery, console, cluster deployment | Proposed / not implemented |

Read `AGENTS.md`, `PHASE-0.md`, then the pinned English design documents in `DESIGN-BASELINE.md` before extending this implementation. The public design is at [dvm-shlee/dureh-whitepaper](https://github.com/dvm-shlee/dureh-whitepaper); private operational evidence stays in its restricted registry.
