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
   new requests (`Status: open`) and those awaiting a reply or a landing; landed, declined and
   withdrawn requests are omitted. The Director pastes it upstream. Then set `Status: sent <date>`.
   Do the same after recording a reply, so the next message reflects it.
4. **Decision**: record the upstream answer or Director judgment with `dureh ledger add decision
   --ref <finding id> --disposition approve|revise|defer|reject|record`.
5. **Landed**: once a harness revision contains the change, set `Status: landed <rev>`, run
   `dureh adapter sync`, then `dureh verify --self`.

## Status values

`open` -> `sent` -> `accepted` | `declined` -> `landed`

- `open`: written at the site, not yet forwarded.
- `sent`: the Director forwarded it upstream.
- `accepted`: upstream agreed to make the change (or the Director ruled and it stands).
- `declined`: upstream (or the Director) rejected it; the finding stays as history.
- `landed <rev>`: the change shipped in harness revision `<rev>` and has been adopted at the site.

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
   `CLAUDE.md`, `.claude/`) from the harness checkout that `bin/dureh` runs, and re-pins
   `deployment.json` to that checkout's revision. Confirm the checkout is at the landed rev first;
   until release checkouts exist, the Director updates it.
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
