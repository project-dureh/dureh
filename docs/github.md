# The company's GitHub board (`dureh github`, release 69)

Director, 2026-10-16: company coordination moves from Slack to GitHub. Each company has its own GitHub
organization with

- a private repository `office`: the home of issues not tied to a project (no Discussions);
- one repository per project (one project = one repository of the organization);
- the organization Project (v2) board number 1 with the fields
  - **Status**: Backlog, Today, In progress, Needs approval (the Director approves, chooses or comments), Waiting for Director (a step only the Director does), Done
    (Today = decided work queued; In progress = agents working on it now, at most 2 open items per company, enforced by `item set`/`item add` since release 82 and by a layer 1-2 `item ask` since release 83)
  - **Project**: single select, one option per project
  - **Layer**: 1 (company), 2 (DUREH), 3 (Director)
  - **Decision**: Approve 1, Approve 2, Approve 3, Rejected, Discuss (Director 2026-10-27; see Decision values)
  - **Units**: number
- and the views All, Today, Director, Projects.

**The Director decides in the Decision column, and the Decision is read with his replies on the item.** Wherever
the desk reads a Decision (`github watch`, `github status`) it also reads the Director's comments on the item since the
ask (after the last `ANSWER:` comment; by `director_login`, without the desk's or DUREH's marker) and shows the latest
one. Approval layers
(CHARTER): layer 1, the company decides; layer 2, DUREH decides after one delegation; layer 3, the Director decides each
time. Layers 1 and 2 are recorded on the board by the desk; a layer-3 Decision is set only by the Director.

### Decision values (Director 2026-10-27)

| value | meaning (a decision ask) | meaning (a review) |
|---|---|---|
| Approve 1 | approve option 1 (the recommended one) | accept |
| Approve 2 | approve option 2 | accept, the follow-up from the replies next |
| Approve 3 | approve option 3 | rework before accepting |
| Rejected | no; nothing proceeds | revert |
| Discuss | hold: nothing proceeds; the desk answers the Director's comments on the item, then he decides again | the same |

Discuss is neither an approval nor a rejection: the watch line says "hold" and, 20 minutes later without a desk
comment, a `followup` line asks the desk to answer in the comments; a fail-closed ask with Discuss still counts as
undecided (`item set --status Done`, `item close` and `sweep` keep it). Until every board is migrated the harness
reads the other names as the new ones (`github.decision_value`): Approved and Option 1 -> Approve 1, Option 2 ->
Approve 2, Option 3 -> Approve 3, Reject -> Rejected, Later -> Discuss; a stored old value is no Decision change, and
`item set --decision` picks the board's old option of the same meaning while the board has no new one.
`item ask` writes the choices as a multiple choice in these names (`Approve 1..N` for its options, then Rejected and
Discuss). The early-close guard (`day end|close --early --approval`) accepts a board Decision of Approve 1 (or its old
names Approved / Option 1).

Slack (docs/slack.md) is unchanged and is switched off per company by config later. While both are on, the desk asks
on GitHub; Slack keeps the day thread reports, and the Slack opinions and consult paths drop out of the desk text.

## Site config

```json
"github": {
  "enabled": true,
  "org": "example-org",
  "project": 1,
  "office_repo": "office",
  "token_file": "~/.config/dureh/github-token-example-org",
  "director_login": "example-director"
}
```

| key | default | meaning |
|---|---|---|
| `enabled` | `false` | the switch; while not `true` every `dureh github` command prints `github: off ...` and does nothing |
| `org` | required when enabled | the company's organization login |
| `project` | required when enabled | the organization Project (v2) number |
| `office_repo` | `office` | the repository for issues not tied to a project |
| `token_file` | required when enabled | the file that holds the fine-grained token (`~` and `$VAR` expanded); it must exist and be mode 600 |
| `director_login` | unset | the Director's GitHub login; layer-3 asks are assigned to it (unset: no assignee) |
| `dashboard_issue` | unset | (release 86) the `office_repo` issue whose body `github digest --write` keeps as the company dashboard |

Unknown keys, a wrong type, and (when enabled) a missing `org`/`project`/`token_file` or a token file that is missing
or readable by group or others are refused: `adapter sync` and `verify --self` fail, `status` warns.

## Token

The desk authenticates with a fine-grained personal access token scoped to the company's organization (repository
access: `office` and the project repositories; permissions: Issues read and write, Metadata read; organization
permissions: Projects read and write).

```sh
mkdir -p ~/.config/dureh && chmod 700 ~/.config/dureh
# paste the token into the file named by github.token_file, then
chmod 600 ~/.config/dureh/github-token-example-org
```

Every `gh` call goes through one runner that reads the token at call time and passes it to the `gh` subprocess as
`GH_TOKEN` only (`GITHUB_TOKEN` from the environment is dropped). The token is never on a command line, never printed
(gh's error text is redacted), never logged and never recorded. Text that carries a GitHub token prefix is refused.

**Company GitHub App (release 84, dureh-team#56 step 5).** With `github.app: {app_id, installation_id, key_file}` (the
App's private key, mode 600) the harness mints the App's installation token itself and writes it to `token_file`
(mode 600): `dureh github token --once`, or `--every 1800`, which the session job runs beside the session, so the desk
writes as `<app>[bot]` instead of the Director. The key is read by path only; neither key nor token is printed. While
`app` is set, a missing token file is not an error (the first refresh writes it). Without `app` nothing changes.

## Commands

| command | who | what |
|---|---|---|
| `github token [--every 1800] [--once]` | the session job (config `github.app`) | mint the company App's installation token into `token_file`; one line per refresh, never the token |
| `github status [--json]` | anyone; allowed with `--observe` | the board's items grouped by Status, each with its Decision, Project and Layer; under a decided or waiting item the Director's latest comment since the ask (`Director: ...`) and, on Discuss, the hold line; `--json` rows carry `director_comments` |
| `github watch [--interval 120] [--once]` | desk, as a Monitor in working hours | one line per Decision change (with ` | Director: <latest comment since the ask> <url>`, or `none`), per new comment and per newly opened issue not marked as the desk's; nothing else |
| `github item add --wi WI [--status S] [--project P] [--layer 1-3] [--units N] [--priority P0-P3] [--start D] [--target D] [--repo R \| --url U]` | desk | an issue for the work item (its title and goal only) in `office_repo` or `--repo` (or the existing issue `--url`), added to the board with its fields; desk rule: Priority P2 unless asked |
| `github item set (--wi WI \| --url U) [--status] [--project] [--layer] [--units] [--decision] [--priority] [--start] [--target] [--on-hold]` | desk | change a linked item's fields; `--decision` only on a layer 1-2 item; `--priority P0..P3` matches the board option `P1 ...`; `--start`/`--target` YYYY-MM-DD set the `Start date`/`Target date` fields (#31). Release 82 (WIP limit, Director 2026-10-26, dureh-team#74): `--status "In progress"` reads the board once and is refused while 2 open items of the company (a repository of `org`, or a draft) other than this one are In progress, naming both and the way out (finish one, or `item set --url <url> --status Today --on-hold`); `item add --status "In progress"` is refused the same way. `--on-hold` sets Status Today and adds the label `on-hold` (created when missing); a later move to In progress removes it. The record carries `labels` {add, remove} when labels changed |
| `github item ask --title T --body-file F [--options O1 O2 O3] [--layer 3] [--priority P0..P3] [--state S] [--decide D --recommend R --otherwise O] [--fail-closed] [--on-hold] [--class C\|D [--command C]] [--wi WI] [--project P]` | desk | a decision ask: a `kind:decision` issue with the options and the Decision line; layer 3: assigned to `director_login`, Status Needs approval, and (release 80) it carries **결정할 것** / **추천** / **답이 없으면** from `--decide`/`--recommend`/`--otherwise` (required on layer 3; on a fail-closed ask `--otherwise` names no fallback option). Release 87 (Director, dureh-team#108; his item format of 2026-10-27): the body is `## 현재 상태` (`--state`, one line; default the body's first paragraph), `## 결정할 것` with the question and right under it the options as `- **Approve N:** ...` lines, then the Rejected and Discuss lines, `**추천:**`, `**답이 없으면:**`, `---` and the body as background (`github digest` reads this body); `--decide` holds the question only: it is refused when it names an option (`Approve 1..3`, `Option N`) or is longer than 150 characters (the choices go in `--options`); `--priority` sets the board's Priority and is required on layer 3 (a decision and `--class D`; desk rule P2 unless asked), optional on layer 1-2; a layer-3 option that is a step only the Director's hands do (cues `github.DIRECTOR_HANDS`: 대표님이/대표님께서 ... 생성/만들/만드/직접 실행, "the Director creates/makes/runs/sets up/logs in") is refused with the hint to file it with `--class D --command "..."`; the desk never asks the Director for a decision in the chat (the chat holds one status line with the item's link), and harness or company config values (`github.dashboard_issue`, push rules) are DUREH's (layer 2), asked by mailbox, not on the board; `--fail-closed` (release 78): label `fail-closed`, a "no fallback" line, a body or option naming a fallback option is refused, the record keeps `fail_closed: true` (`day decide --ask <url>` then refuses a provisional choice); `--wi` names the WI the ask is about, never the WI's board link (release 81): `item set/close/comment --wi` keep acting on the WI's own item, and the ask is changed only by `--url`; on a fail-closed ask (label or record) whose Decision is empty, `item set --url <ask> --status Done` and `item close --url <ask>` are refused, naming the Decision column (other undecided asks stay allowed by hand). Release 83: a layer 1-2 ask opens In progress, so it reads the board once and is refused like `item set --status "In progress"` while 2 open items of the company are In progress (nothing is created); `--on-hold` (layer 1-2 only) opens it with Status Today and the label `on-hold`, which a later `item set --url <ask> --status "In progress"` removes |
| `github item comment (--wi WI \| --url U) [--file F]` | desk | the desk's comment, carrying the marker `<!-- dureh-desk -->` |
| `github item close (--wi WI \| --url U) [--comment C]` | desk | close the item's issue (desk rule: at promote), the comment marked as the desk's; records `github.item.closed` (#34) |
| `github digest [--write] [--json]` | anyone (print); desk (`--write`) | release 86 (dureh-team#91 item 5): the company dashboard in the DUREH dashboard's format (dureh-team#31): `## 1. 정하실 것` lists the open Needs approval items, each with **결정할 것** / **데스크 추천** / **안 하면 / 답이 없으면** read from its body (`dureh_core/board_digest.py`; a missing line prints `<채워 주세요>`, a fail-closed item `없음 (fail-closed), 답을 기다립니다`), `## 2. 대표님이 하실 일` the open Waiting for Director items with the first line under `## 대표님이 하실 일`; Priority first, and (release 87, dureh-team#108 item 5) an item without Priority is marked `· Priority 없음` in its heading. `--write` replaces the body of issue `dashboard_issue` (refused when unset) only when something besides the time line changed; records `github.digest.written` {url, decide, steps}. Release 87: with `dashboard_issue` set, every desk `item add/set/ask/close` and `sweep`, and a `watch` poll that moved an item's Status, refresh it too (one `digest:` line on stderr; a failure is reported, never fails the write); a `--class D` step shows its fenced command |
| `github sweep [--dry-run]` | desk | close every open issue of the organization whose Status is Done and that carries no `waiting-director`, `needs-review` or `needs-approval` label (desk rule: before `day close`); release 81 also keeps a decision ask (label `kind:decision`, or recorded by `item ask`) whose Decision is empty, and every fail-closed ask (label `fail-closed`, or recorded `fail_closed`) whatever its Decision; records `github.sweep.done` {closed, kept} (#34) |

Every issue and comment the desk writes ends with the hidden marker `<!-- dureh-desk -->`; `watch` skips comments that
carry it.

### Watch

`watch` polls the board every `--interval` seconds (default 120 since release 73, at least 5) with one GraphQL query (paged by 100 items), then the
open issues of the organization's repositories (release 71: `office_repo` every poll; every unarchived repository the
token can list, when it can; release 79: the newest 20 open issues of each, the other repositories 10 a page and only
every 600 s), and prints

```
decision: office#12 WI-0007 example item -> Approve 1 (was none) [layer 3] https://github.com/example-org/office/issues/12 | Director: 1번으로, 테스트 먼저 https://github.com/example-org/office/issues/12#issuecomment-1
comment: office#12 by example-director: please also check the edge case https://github.com/example-org/office/issues/12#issuecomment-1
work request office#14 by example-director: rerun the example analysis | https://github.com/example-org/office/issues/14
issue example-project#3 by example-director: the plot axis is wrong | https://github.com/example-org/example-project/issues/3
```

A newly opened issue whose body carries the desk marker is not reported. An issue labelled `kind:request` (the
office repository's "일 요청 · Work request" issue template) is a **work request** from the Director: the desk creates
or picks the work item, links the issue with `item add --wi WI --url <issue url>` (desk rule 12a), and replies on the
issue with `item comment --url <issue url>` when work starts. Every reported issue is recorded as
`github.issue.opened` `{issue_id, repo, number, url, author, request, created}`.

The last seen Decision of every item and the comment ids already seen are kept in `cache/github/watch.json` under the
site (a runtime cache, not a record; deleting it only makes the next poll a first poll). A first poll reports the
Decisions already set and takes the existing comments as seen. The ids of the issues already seen and the repositories
already scanned are kept there too: a restart does not report an issue again; a first poll (or a repository scanned
for the first time) takes its open issues as seen, except those opened after the last saved poll time, so an issue
opened while no watch ran is still reported once. A failed `gh` call (network, API error) skips that
poll silently; the next one sees the same changes.

Rate limits (release 73, project-dureh/dureh#25): the team's watch and every desk's `github watch` poll as the same
GitHub user, so GitHub's per-minute secondary limit is shared; on 2026-10-19 three watches at 30 s made every board
read fail with "API rate limit already exceeded" while the hourly quota was far from spent. The default interval is
therefore 120 s; do not lower it. When a poll fails with a rate-limit error ("API rate limit already exceeded",
"API rate limit exceeded", "secondary rate limit"), `watch` prints one line

```
github watch: rate limited by GitHub; backing off (next poll in 120 s, doubling up to 900 s) until a poll succeeds
```

and waits `--interval`, then twice that after each further rate-limited poll (240, 480, ... at most 900 s; at least a
Retry-After the error names), printing nothing more; the first poll that succeeds prints
`github watch: resumed after N rate-limited polls` and the normal interval returns. Each Decision change is recorded as `github.decision.changed`
`{item_id, url, from, to, layer, first_poll}` (with the WI when the item is linked).

Cost (release 79, v0.4 bundle 1 step 3): GitHub prices a GraphQL query by the page sizes it asks for, not by what
comes back. Until release 78 the organization scan asked for 100 repositories x 50 issues x 20 labels, about 51 of the
hourly 5,000 points per poll for an organization of two or three repositories (about 1,700 points an hour per desk at
120 s, and the desks and the team share one user's quota). Since release 79 the watch's board read leaves out assignees
and labels (`status` and `sweep` keep them), the issue reads ask for 20 issues and 10 labels, the organization scan pages
10 repositories at a time and runs at the first poll and then every 600 s (`ORG_SCAN_INTERVAL`; the office
repository is still read every poll, so a work request is seen within one interval and an issue in a project
repository within ten minutes). Only the newest 20 open issues of a repository come back: when that page is full the
first time a repository is scanned, its oldest creation time becomes the repository's floor (`issue_floors` in
`watch.json`), and an older issue that later slides into the page because newer ones were closed is taken as seen,
not reported.

Every watch query asks for `rateLimit { cost remaining resetAt }` (no extra call) and the watch appends one line per
query to `cache/github/usage.jsonl` under the site (a runtime cache next to `watch.json`, not a record; trimmed to its
newer half above 2 MB):

```
{"caller": "watch", "cost": 3, "query": "board", "remaining": 4890, "reset_at": "2026-10-21T18:21:00Z", "ts": "2026-10-21T17:40:02Z"}
```

The first rate-limited poll of a back-off also appends an `exhausted` line with the error and, from `gh api rate_limit`
(which GitHub does not count), the GraphQL and core `remaining`, `used`, `limit` and `reset_at`. These lines are the
measured cost the expected budget is checked against.

GitHub does not report who changed a field value; the Decision column's authority rests on the board's permissions
(only the Director should set Decision on a layer-3 item).


Follow-up reminder (release 80, project-dureh/dureh-team#63): when a Decision the desk must act on (Approve 1-3,
or Discuss: an answer in the comments) is seen, the watch keeps it in `watch.json` `followups`; if the item is still open 20 minutes later and no
new comment carries the desk marker, it prints once
`followup: <ref> <title> -> <Decision> N min ago, still open and no desk comment since: ...`. The desk comments on the
item: what it did, or what blocks it and the Director's step (Discuss: the answer to his comments). The first poll tracks nothing; a closed item or a
changed Decision drops the reminder. An empty board answer is a skipped poll (the state is kept).
## Records

| event | payload |
|---|---|
| `github.item.added` | `{url, repo, number, item_id, fields}`, on the WI |
| `github.item.updated` | `{item_id, url, fields}` |
| `github.ask.created` | `{url, repo, number, item_id, layer, options, assignee, fields, chars}` (the WI when given; `fail_closed: true` with `--fail-closed`, release 78) |
| `github.comment.posted` | `{url, chars}` |
| `github.decision.changed` | `{item_id, url, from, to, layer, first_poll, director_comments}` (`to`/`from` in the current names; `director_comments` = the URLs of his comments since the ask, never text) |
| `github.issue.opened` | `{issue_id, repo, number, url, author, request, created}` |

No record carries issue or comment text. `github.*` events are written only by `dureh github` (`event add` refuses them).
