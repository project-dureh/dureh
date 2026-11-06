# Company permissions at the site: `dureh site permit` (release 89)

Director 2026-10-30 (dureh-team#111 Approve 1; proposal `site-permissions-2026-10-30`, accepted with dureh-team#112).
A company's permissions over its own resources (push rules, a module's push, accepting modules and projects, the
dashboard issue) change at the site from one harness catalog, not by hand edits of `config.json`. The catalog lives in
`dureh_core/permits.py`; `dureh site permit list` prints it with the current values.

## Who switches what

| Path | Who | What |
|---|---|---|
| T | the Director's own terminal (no agent session: `DUREH_ACTOR` and `CLAUDECODE` unset) | any catalog key, no `--approval` needed; recorded `by: Director` |
| D+ | the desk with `--approval` | class B and C changes; the approval is a board Decision bound to the exact change (below) |
| Desk | the desk alone | class B changes only (recorded, one dashboard line) |

Classes are computed **per change**, not per key:

| Key / step | Class | Notes |
|---|---|---|
| `push.policy`, `push.allowed_remotes` (`set`, `add`, `remove`) | C; B when the change only tightens | tightening: a rule or entry removed that no later rule of the same owner could cover, a `deny` rule added anywhere, `allow.*` narrowed or `require.*` made stricter with the same `match` and order. Anything not proven narrower is C (fail-closed). A rule covering public repositories may be applied by the desk with a board Decision (proposal Q3). A `visibility` match is checked with the site's token; when that token gets GitHub's 404 (a company App token and a personal account's repository) an unauthenticated check decides (release 94, docs/github.md), so a rule for a personal account's private repositories needs no change |
| `push.standing_ff`, `push.standing_ff_new_branches` | C on, B off | |
| `modules.<id>.push` | B when set to `false`, C otherwise | push-off narrows |
| `github.dashboard_issue` | B | display only; checked on GitHub: an open issue of `office_repo` (a failed check refuses) |
| plan step `link-module`, `new-module`, `new-project` | B | reversible (`retire-module`); a module's push rule is a separate step |
| plan step `link-project` | C | a data project link, fail-closed (also `site link-project --approval`) |
| `github.enabled`, `org`, `project`, `office_repo`, `token_file`, `app`, `director_login`, `push_transport` | D | the Director's terminal only; refused inside an agent session. `push_transport` (release 95): `"app"` sends pushes to the App's organization over HTTPS with the App token, `"ssh"`/unset keeps the remote URL (docs/github.md, Pushing with the company App); the push records carry `transport` |
| `models.<tier>`, `models.routing`, `models.fallback_tiers`, `models.candidates` | B run id for the same model, C new model or table key, D waiver | release 91, only at a quiet point (no agent running); docs/exams.md |
| `members`, `office_hours`, `containers`, `slurm`, `limits`, `checks`, `modules.<id>.env_rebuild/submit/containers_cmd` | not yet | refused (running behaviour): not in the catalog in this release; they come in a later release with the quiet-point check `models.*` got in 91. Until then a config change under the Director's rule (release 93 wording; it named release 90 before) |
| `site`, `harness`, `startup`, `schema_version`, `state`, `index`, `references`, `runtime`, `upstream`, `session`, `env` | harness-owned | refused: install / `adapter sync` |

Any other key is refused as not in the catalog.

## Commands

```
dureh site permit list [--json]
dureh site permit show <key> [--history]
dureh site permit set <key> <json> --memo M [--approval A]
dureh site permit unset <key> --memo M [--approval A]
dureh site permit add <push.policy|push.allowed_remotes> <json> [--at N] --memo M [--approval A]
dureh site permit remove <push.policy|push.allowed_remotes> <rule id | url> --memo M [--approval A]
dureh site permit plan --file P [--json]
dureh site permit apply --file P --memo M [--approval A]
dureh site permit migrate push [--apply] [--memo M]
```

Keys are dotted paths (`push.policy`, `github.dashboard_issue`, `modules.<id>.push`). A plan file is a JSON list of
steps, applied in order:

```json
[{"op": "link-module", "id": "tool", "source": "git@github.com:example-org/tool.git", "branch": "main"},
 {"op": "add", "key": "push.policy", "value": {"id": "org-private", "match": {"owner": ["example-org"],
  "visibility": "private"}, "allow": {"standing_ff": true}, "require": {"decision": "never"}}, "at": 0},
 {"op": "link-project", "name": "dataset-a", "source": "/data/dataset-a"}]
```

Step keys: `set {key, value}`, `unset {key}`, `add {key, value, at?}`, `remove {key, item}`, `link-module {id,
source, rev?, branch?}` (rev defaults to the branch, else `HEAD`), `new-module {id, package, source?, python?,
depends?}`, `link-project {name, source}`, `new-project {id, purpose?}`. Unknown keys refuse.

Every write, in order: who (agent session = the desk; otherwise the Director) -> class and approval -> the whole
resulting config through `config_errors` (an error the change adds refuses; errors already there are shown as
warnings) -> backup of the previous `config.json` to `state/config-history/<utc>-<sha8>.json` -> atomic write
(other keys untouched) -> one record -> adapter files regenerated. A refusal writes and records nothing.

Records: `site.permit.set`, `site.permit.unset`, `site.permit.added`, `site.permit.removed` (one step: `{key, op,
old, new, ...}`), `site.permit.applied` (a plan: `{steps, ...}`, refs = the link steps' own records such as
`site.module-linked`), `site.permit.migrated`, `site.permit.failed` (`{failed_step, error, done, compensation}`:
a failed step stops the plan; what was done stays, with how to undo it). All carry `class, fail_closed, by, via, memo,
approval, binding, backup, generated`. `ledger add event` / `event add` refuse the `site.permit.` prefix.

## A board Decision as `--approval`

1. The desk writes the plan file and runs `site permit plan --file P`: each step's class, old -> new, why it widens,
   the approval it needs and the binding sha256 (the canonical JSON of the steps).
2. `github item ask --class C --fail-closed --permit P --title ... --body-file ... --decide ... --recommend ...
   --otherwise ... --priority P2` on the company board: the plan is checked as `plan` checks it (a B-only plan needs
   no ask; a class D plan is refused), shown in the issue body, and bound to **Approve 1** (at most one option); the
   `github.ask.created` record carries `permit: {sha256, steps, summary, option, class}` and the ask is fail-closed.
3. The Director sets Decision. The desk's `github watch` records `github.decision.changed` as always; a `--permit` ask
   moves on the Decision alone (no confirming comment needed, proposal Q1).
4. `site permit apply --file P --memo M --approval <item URL | github.decision.changed id>`. Checked, fail-closed:
   the item was asked with `--permit` whose sha256 equals this plan; the recorded Decision is Approve 1 and no later
   Decision is recorded for the item; a live board read shows the same Decision on an open item (a failed read
   refuses). The same change applied again is a no-op; the Decision used for a different change refuses. After the
   write the desk comments the record id on the item.

`set`/`unset`/`add`/`remove` with `--approval` bind the one-step plan `[{"op": ..., ...}]`, so the asked plan file
holds exactly that step.

## `site link-project` (proposal section 5)

A data project link is fail-closed. From an agent session the desk needs `--approval <item URL>` whose ask bound
`[{"op": "link-project", "name": N, "source": S}]`; the Director's own terminal needs none. Nothing is cloned before
the check. The `site.project-linked` record carries `approval`. A records project (`new-project`, where the package
allows it) stays class B.

## `migrate push`

Turns each release-86 pattern entry of `push.allowed_remotes` (`{"url": "git@github.com:<owner>/<glob>.git",
"pattern": true, ...}`) into a `push.policy` rule `<owner>-private` with the same meaning (`visibility: private`, the
entry's `branches`, `standing_ff` and `new_branches`, `tags: false`, `squash: false`, `decision: never`), placed at
the top of the policy in the entries' order. When policy rules already follow, a `deny` rule over the same
repositories (`<owner>-rest-private`) keeps what the pattern refused (another branch, a tag) refused. Exact entries
and the site-wide `standing_ff` stay. Class B. Without `--apply` it prints the rules and writes nothing (release
89.2; `--dry-run` is still accepted); `--apply` writes them, with a backup and a `site.permit.migrated` record.

## Accepting a new project without DUREH

1. The Director creates the GitHub repository or sets its visibility when needed (his hands, class D).
2. The desk writes `notes/permits/<name>.json` (link-module, the push rule, link-project ...) and runs `plan`.
3. B-only: the desk applies it. Otherwise one board item: `github item ask --class C --fail-closed --permit <plan>`.
4. The Director decides on the board (or runs `site permit apply --file <plan>` in his own terminal).
5. The desk applies with `--approval <item URL>`; the board item moves by its Decision; DUREH sees it through the
   read commands only.
