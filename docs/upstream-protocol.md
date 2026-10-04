# Upstream request protocol

Applies to every DUREH company. Problems and needs found at a site flow back to the harness
developers in one uniform, traceable form, so the harness improves from real use.

## Which side owns the problem

| Problem is in | Route |
|---|---|
| camri module, package, project | normal work item (WI) or finding at this site |
| harness: `dureh_core`, hooks, `org/roles.yaml`, schemas, permission model, `wi`/`dispatch` behavior | upstream request (this protocol) |

## Rules

1. Never work around the harness, and never widen a role's permissions to route around a blocker.
   Stop that WI, record the evidence, tell the Director.
2. A role must not change its own constraints. Governance changes (e.g. `org/roles.yaml`) go through
   upstream or a Gate 2 governed path, never through the constrained role itself.
3. The site team never sends or pushes anything. Only the Director forwards a request upstream
   (`dureh push` needs a Director decision and an allowlisted remote).
4. While a request is open, keep working on items that do not depend on it.

## Flow: finding -> UR -> forward -> decision -> landed

1. **Finding** (a record, not a WI): `dureh ledger add finding --tag upstream
   --evidence-level measured|reviewed --wi <WI> --summary "..."`. Use `measured` only for output you
   directly observed (command, output, path, harness rev); otherwise `reviewed` or `inferred`.
2. **UR (upstream request)**: the desk writes `notes/upstream/UR-<YYYYMMDD>-<NN>.md`, one issue per
   file, from the template below.
3. **Forward**: the desk runs `dureh upstream message` and gives the Director its output verbatim in
   one fenced code block (and saves it as `notes/upstream/FORWARD-<YYYYMMDD>.md`). The message lists
   new requests (`Status: open`), those awaiting a reply or a landing, and deferred ones in their own
   section; landed, declined and withdrawn requests are omitted. The Director pastes it upstream. Then set `Status: sent <date>`.
   Do the same after recording a reply, so the next message reflects it.
4. **Decision**: record the upstream answer or Director judgment with `dureh ledger add decision
   --ref <finding id> --disposition approve|revise|defer|reject|record`.
5. **Landed**: once a harness revision contains the change, set `Status: landed <rev>`, run
   `dureh adapter sync`, then `dureh verify --self`.

## Working in parallel with upstream

Site and upstream work at the same time. Four things can collide; each has one rule:

| Collision | Rule |
|---|---|
| Code: a site harness WI and upstream write the same branch | The site reports harness problems as URs, never fixes them in a harness WI. |
| Live swap: the harness changes under running agents | Sites run read-only release checkouts; a site switches release (`adapter sync --harness`) only in a merge window, while idle. |
| Project files: `adapter sync` commits into linked projects | Same merge window; no data-steward or pipeline-runner runs in that project meanwhile. |
| Site records | Upstream never writes them; it reads in observe mode. |

Site (desk) order: stop the affected WI at a safe point; record the finding and the UR with
`Blocked WIs` and `Continuing WIs`; batch requests into one `dureh upstream message` (its
`SITE ACTIVITY` section shows running agents and whether a merge window could open now); keep
unaffected WIs going and do not sync while waiting; in a merge window dispatch nothing new, let
running agents finish, confirm with `dureh status`, report "idle"; when the `landed` message names a
release checkout, the desk switches itself with `adapter sync --harness <checkout>` and runs `verify --self`,
records `landed <rev>`, reports the result, and resumes.

Upstream order: triage each UR, report the proposed order, site impact and whether a merge window
is needed to the Director, implement only after the Director confirms, then hand over the merge
commands and a reply that states per UR what behavior changes and whether a sync is needed.

## Harness and package work from a site

A site may still propose harness or package changes as work items (decision D2). Gate 2 does not merge
them: `dureh wi promote` leaves the verified head on an opaque branch `contrib/c-<8 hex>` in the harness
or package repository (no site id, no WI number in the name), records it in the `wi.promoted` event,
and sends upstream a `notice` through the mailbox. Upstream reviews the branch and merges it under its
own message; the site picks the change up with the next release it syncs.

WI branches are `wi/<site-id>/<WI>` (decision D4), so two sites never share a branch. Branches made by
older harnesses (`wi/<WI>`) keep working; `dureh purge` deletes a legacy branch only when its tip is a
commit this site recorded for that WI.

## Mailbox (DUREH-Exchange)

When the Director sets `config.json` `"upstream": {"exchange": "<dir>"}` (environment variables allowed,
for example `"$HOME/DUREH-Exchange"`), messages travel through `<dir>/<site>/` instead of copy and paste:

- `dureh upstream send` writes the `upstream message` text to `to-upstream/` and keeps a `FORWARD-*` copy
  in `notes/upstream/`; `--kind idle` reports the site idle for a merge window and is refused while an
  agent runs; `--kind notice|landed --body-file F` sends other notes.
- `dureh upstream inbox` lists new messages in `from-upstream/`; `--record` marks them received and records
  an `approval` as a decision `by: Director` with the Director's words as the memo and
  `binding: {via: upstream, file}`. Upstream merges, pushes and cuts releases on its own (the Director's charter, 2026-10-03: anything but `main`);
  the site switches release itself in the merge window; changes to `main` stay the Director's.
- `dureh status` shows `Upstream: collaborating` while a request is open or a merge window is in play, with
  the open requests, blocked WIs, the merge window (`closed`, `requested`, `open (site idle)`) and unread
  upstream messages. An upstream `merge-window` message whose body starts with `open` requests a window;
  one starting with `closed` ends it.

The site writes only `to-upstream/`, upstream only `from-upstream/`; each side records what it has read
on its own side. Message format: `DUREH-Exchange/README.md`.

## Status values

`open` -> `sent` -> `accepted` | `declined` | `landed`; `accepted` -> `landed` | `deferred`;
`deferred` -> `accepted` | `declined`

- `open`: written at the site, not yet forwarded.
- `sent`: the Director forwarded it upstream.
- `accepted`: upstream agreed to make the change (or the Director ruled and it stands).
- `deferred`: an accepted request the Director postponed. `upstream list` and `upstream message`
  show it as deferred (its own section), not as awaiting a reply; it may go back to `accepted` or be
  declined.
- `declined`: upstream (or the Director) rejected it; the finding stays as history.
- `landed <rev>`: the change shipped in harness revision `<rev>` and has been adopted at the site.
  Upstream may land a fix without a separate accepted reply, so `sent` -> `landed` is allowed.

## UR template

```
# UR-<YYYYMMDD>-<NN>: <one-line title>

- Type: bug | change | decision | friction
- Severity: blocking | degrading | minor
- Site: <site>, package <name>, harness rev <sha>
- Finding: <finding id(s)>
- Status: open

## Evidence (<measured|reviewed|inferred>)
<commands, outputs, paths, WI ids; how to reproduce>

## Request
<expected behavior or the exact diff>

## Options and recommendation
<at least two if a decision is needed>

## Impact at this site
<which WIs wait, what proceeds meanwhile>
```

Keep each file short enough to read in one minute. Do not include secrets, personal data, or
project data.

## Adopting a landed revision

When a UR's `Status` reaches `landed <rev>`:

1. `dureh adapter sync` does not fetch anything. It regenerates the site's adapter files (`bin/dureh`,
   `CLAUDE.md`, `.claude/`) from the harness checkout that runs it, re-pins `deployment.json` to that
   checkout's revision and records the checkout in `config.json` `harness.root`. A landed revision
   arrives as a release checkout (below); the Director switches the site to it with
   `bin/dureh adapter sync --harness <releases>/dureh-<tag>`, which runs that checkout's own `dureh`
   so `bin/dureh` and the hooks exec it from then on.
2. Run `dureh verify --self` to self-test the harness and this site against that revision.
3. Only after both succeed, resume or re-open any WI that was waiting on the change, citing the
   landed rev and the `verify --self` result as evidence.

## Upstream access to a site

Upstream developers inspect a site only in observe mode: `dureh --site <site> --observe <command>`
(or `DUREH_OBSERVE=1`). It allows `status`, `wi show`, `wi list`, `ledger query`, `upstream list`,
`path-refs`, `usage` and `verify --self`; every other command is refused, records refuse appends,
and `verify --self` records no run and builds its index in a private temporary directory. Observe
mode prevents mistakes; it is not a security boundary.

## Source

Based on `notes/upstream/README.md` at the site (`$DUREH_SITE`); see that file and the existing
`notes/upstream/UR-*.md` records for worked examples.

## Releases (decision D1)

Sites never run the integration clone. Upstream cuts a release with `tools/release.py cut` (in the
harness repository): it tags the pushed revision `rYYYY.MM.DD-N`, pushes the tag, and clones it
read-only to `<workspace>/releases/dureh-<tag>/` with `origin` set to the harness remote, so the
pinned revision in `deployment.json` is always fetchable. The working tree has no write bits; `.git`
keeps them because a site's harness WIs branch from the release it runs and leave their
`contrib/c-<8 hex>` branches there (upstream fetches them from that clone). `tools/release.py list`
shows which site runs which release; `tools/release.py prune` deletes releases no site runs, keeping
for each release in use the one cut just before it, and holds any release that still has WI
worktrees or branches attached. `verify --self` fails when `config.json` names a different harness
than the one running the command, and `dureh status` shows the release the site runs.
