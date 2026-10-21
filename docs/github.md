# The company's GitHub board (`dureh github`, release 69)

Director, 2026-10-16: company coordination moves from Slack to GitHub. Each company has its own GitHub
organization with

- a private repository `office`: the home of issues not tied to a project (no Discussions);
- one repository per project (one project = one repository of the organization);
- the organization Project (v2) board number 1 with the fields
  - **Status**: Backlog, Today, In progress, Needs approval (the Director approves, chooses or comments), Waiting for Director (a step only the Director does), Done
  - **Project**: single select, one option per project
  - **Layer**: 1 (company), 2 (DUREH), 3 (Director)
  - **Decision**: Approved, Option 1, Option 2, Option 3, Rejected, Later
  - **Units**: number
- and the views All, Today, Director, Projects.

**The Director decides only through the Decision column.** Comments are opinions, never instructions. Approval layers
(CHARTER): layer 1, the company decides; layer 2, DUREH decides after one delegation; layer 3, the Director decides each
time. Layers 1 and 2 are recorded on the board by the desk; a layer-3 Decision is set only by the Director.

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

## Commands

| command | who | what |
|---|---|---|
| `github status [--json]` | anyone; allowed with `--observe` | the board's items grouped by Status, each with its Decision, Project and Layer |
| `github watch [--interval 120] [--once]` | desk, as a Monitor in working hours | one line per Decision change, per new comment and per newly opened issue not marked as the desk's; nothing else |
| `github item add --wi WI [--status S] [--project P] [--layer 1-3] [--units N] [--priority P0-P3] [--start D] [--target D] [--repo R \| --url U]` | desk | an issue for the work item (its title and goal only) in `office_repo` or `--repo` (or the existing issue `--url`), added to the board with its fields; desk rule: Priority P2 unless asked |
| `github item set (--wi WI \| --url U) [--status] [--project] [--layer] [--units] [--decision] [--priority] [--start] [--target]` | desk | change a linked item's fields; `--decision` only on a layer 1-2 item; `--priority P0..P3` matches the board option `P1 ...`; `--start`/`--target` YYYY-MM-DD set the `Start date`/`Target date` fields (#31) |
| `github item ask --title T --body-file F [--options O1 O2 O3] [--layer 3] [--wi WI] [--project P]` | desk | a decision ask: a `kind:decision` issue with the options and the Decision line; layer 3: assigned to `director_login`, Status Needs approval |
| `github item comment (--wi WI \| --url U) [--file F]` | desk | the desk's comment, carrying the marker `<!-- dureh-desk -->` |
| `github item close (--wi WI \| --url U) [--comment C]` | desk | close the item's issue (desk rule: at promote), the comment marked as the desk's; records `github.item.closed` (#34) |
| `github sweep [--dry-run]` | desk | close every open issue of the organization whose Status is Done and that carries no `waiting-director`, `needs-review` or `needs-approval` label (desk rule: before `day close`); records `github.sweep.done` {closed, kept} (#34) |

Every issue and comment the desk writes ends with the hidden marker `<!-- dureh-desk -->`; `watch` skips comments that
carry it.

### Watch

`watch` polls the board every `--interval` seconds (default 120 since release 73, at least 5) with one GraphQL query (paged by 100 items), then the
open issues of the organization's repositories (release 71: `office_repo` always; every unarchived repository the
token can list, up to 100 repositories and the newest 50 open issues each, when it can), and prints

```
decision: office#12 WI-0007 example item -> Option 1 (was none) [layer 3] https://github.com/example-org/office/issues/12
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

GitHub does not report who changed a field value; the Decision column's authority rests on the board's permissions
(only the Director should set Decision on a layer-3 item).

## Records

| event | payload |
|---|---|
| `github.item.added` | `{url, repo, number, item_id, fields}`, on the WI |
| `github.item.updated` | `{item_id, url, fields}` |
| `github.ask.created` | `{url, repo, number, item_id, layer, options, assignee, fields, chars}` (the WI when given) |
| `github.comment.posted` | `{url, chars}` |
| `github.decision.changed` | `{item_id, url, from, to, layer, first_poll}` |
| `github.issue.opened` | `{issue_id, repo, number, url, author, request, created}` |

No record carries issue or comment text. `github.*` events are written only by `dureh github` (`event add` refuses them).
