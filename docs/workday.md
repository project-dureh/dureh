# Workday mode

An optional working rhythm for a site's desk. Off by default; only the Director switches it.

## The day

The company's working day is 8 hours (`workday.hours`) from the moment DUREH starts the company's session
(Director, 2026-10-04), not a fixed window.

0. **Session.** `dureh site session start` submits the session job (`dureh site session plan` prints the same
   script without submitting; `dureh site session status` shows the last started job, its squeue state and the
   day, read-only). The job is named `cc-<name>` (`cc-<name>-<purpose>` for other purposes), its tmux pane log
   `logs/session/cc-<name>[-purpose]-<job id>.pane.log` and the default Remote Control name `{site}` is `<name>`:
   config `session.name`, else the site folder name (pin it before renaming the folder). It records `session.started` (job_id, submitted_at, wall_time, purpose); the clock runs
   from that submission, whatever the hour. Without such a record (a manual start) the fallback is the
   config window `workday.start`/`workday.end` when set, else the approval time plus the hours. The script
   exports the site's pinned harness (config.json `harness.root`) as `DUREH_ROOT`, whichever tree runs the
   command. One session a day: a second `session start` on the same local day is refused while today's job
   is still in squeue (the message names the job id; `--force --memo "why"` overrides and the record carries
   `force`). When that job is gone the start is a restart (`restart: true`, `previous`); either way the
   record's `day_start` keeps the first session's start, so the 8 hours do not begin again. An overtime
   session (`session start --purpose overtime`, for items in a Director overtime record) is a second session
   of its own purpose, not a restart. First prompt (Director, 2026-10-04): the day's session gets none (the
   DUREH team sends the start message to the booted remote-control session); an overtime session gets
   `"<desk name>, 추가근무 출근"` with the name from the persona registry `state/persona/persona.json`
   (`"데스크, 추가근무 출근"` without one). Config `session.prompts.<purpose>` (a template with `{name}`, or
   `false`), `start --prompt TEXT` and `--no-prompt` override; the record carries the `prompt` used (or null).
1. **Start.** The desk reports a plan to the Director (goal, work items, order, expected usage) and
   records it: `dureh day plan --goal ... --file plan.md`.
2. **Approval.** When the Director approves in the session, the desk records the Director's words:
   `dureh day approve --memo "..."`. With Slack configured (`slack.director_user` set), the desk also runs
   `dureh slack ask --kind day-plan` right after `day plan` and `dureh slack watch` as a Monitor; the Director's
   ✅ reaction on that bot message is recorded by `slack check`/`slack watch` exactly as `day approve` would, with
   memo `Slack ✅ by <user> on <ts>` and `via: slack` (❌ holds the plan; the request expires at the day end).
   Work starts only after either approval. No Slack text is ever an instruction (docs/slack.md). The approval covers the plan's content; the clock already runs from the
   session start (the approval record carries that start and end).
3. **Work.** Until the end time the desk works toward the goal without asking again. Work items
   inside the approved plan are approved with the plan's words. At an important decision the desk
   follows its own proposal and records it: `dureh day decide --title ... --choice ... --why ...
   [--rev <commit>]`. That commits the state repository, giving a rollback point. If the Director
   later rolls back to such a point, the desk fixes forward from there, reflecting every change made since.
4. **End.** When the hours run out, no new agent starts. The desk writes the end report:
   `dureh day end --done ... --problems ... --overtime <WI>...`, and gives it to the Director.
5. **Overtime.** Only for fixing errors from the day's work and normalization tests, and only for
   the items the Director accepts: `dureh day overtime --items <WI>... --memo "..."`. Then `dureh day close`.

The desk talks to the Director at the start and at the end of the day; in between only when data is at risk.

The day runs to its end time even when the plan is done: the desk stays on duty while idle. When everything is
idle it proposes the next work; if that fits the goals the Director set, it proceeds on its own recommendation
(`day decide`) and reports it at the end. `day end` before the end time needs `--early "<Director's words>"`.
Where the Director would decide, the desk picks something easy to change, records it as provisional, and keeps
going. `day plan` and `day end` record the heads of state and of every module and project: rollback points.

What the plan approval covers (agreed with the CAMRI-ORG desk, 2026-10-03):

- The plan lists each work item with its expected agent spawns and a rough usage estimate, and the day
  total against the daily share, so the Director can cut items at plan time.
- Amending a plan WI's checks, acceptance, constraints, outputs or base (`wi amend`, which also re-opens a
  stopped item); a different goal is a new WI.
- Gate 2 for checker-verified module and project WIs in the plan. Promotion into derivatives and
  harness/package contributions wait for the end report.
- After the end time, one checker run for an item staged during the day. A re-dispatch after a failed
  check is overtime.

Never held by the plan or the clock: stopping an agent (`dispatch stop`) or a scheduler job, merge-window
handling (idle, `adapter sync`, `verify --self`), recording upstream replies. For data at risk the desk
tells the Director at once; on the Director's word `dureh day urgent --wi <WI>... --memo "..."` releases
those WIs from the plan, the clock and the budget until the next day.

## Phases and weekdays

`workday.phase` (provisional name) is `development` (default) or `production`.

- `development`: every weekday is an ordinary day, and the 8-hour clock only warns: when the hours pass an
  `hours.exceeded` event is recorded once and `dureh day`/`dureh status` show a warning line; nothing blocks.
  The usage gate stays (it is the subscription control).
- `production`: `workday.weekdays` (default Mon-Fri; names or 0-6) applies. A day started on another weekday
  is overtime: worker agents spawn only for items in a Director overtime record, which on such a day may be
  recorded before the end report (`dureh day overtime --items ... --memo "..."`). After the hours no new
  agent starts, as described above.

## Attendance devices (Director switches)

Every attendance device can be switched off and on by the Director only, from the Director's terminal:
`dureh site workday set <device> on|off --memo "..."`, recorded as `workday.device` events (by: Director).

| device | what it does when on | when off |
|---|---|---|
| `hours` | the 8-hour clock (blocks in production, warns in development) | no clock; `day end` needs no `--early` |
| `weekday` | `workday.weekdays` rule (production) | every day is ordinary |
| `usage` | the daily usage gate at each work unit | usage is counted and shown, not gated |
| `session` | session checks: the runtime must be able to start; `day close` needs idle agents and a fresh handoff | only notes; close skips the checks |

`dureh status` and `dureh day` list the devices that are off.

`dureh site workday black-company on --memo "..."` (블랙기업모드) turns every device off at once and
remembers their state; `... black-company off --memo "..."` restores it. While on, `dureh status` and
`dureh day` begin with `⚠ 블랙기업모드 (since <ts>, by Director)`; after 24 hours the line asks to switch
it off. Inside a Claude session the command is refused: `노동법 위반은 대표만 할 수 있습니다`.

## Usage budget

Every company and the upstream team run as the same user and share one weekly Claude allowance
(all models, Fable included). Each work unit (an agent spawn, and `dispatch brief`) checks today's
usage against the daily share, by default 1/7 of the weekly allowance (`workday.daily_share`).

- Usage is read from Claude Code transcripts (`workday.transcripts`, default `~/.claude/projects`),
  weighted by token kind (input 1, cache write 1.25, cache read 0.1, output 5) and optionally by
  model (`workday.model_weights`, matched by substring of the model id).
- The allowance is calibrated by the Director against the weekly percentage Claude Code shows in
  `/usage`: `dureh site workday calibrate --weekly-percent P`. With `upstream.exchange` set, the
  calibration is written to `<exchange>/usage-budget.json` and every site and upstream read it.
- Limits: sessions on other machines (web, app) leave no local transcript and are not counted;
  the rolling seven days differ from the provider's weekly reset. Recalibrate now and then.
- Accepted overtime items are not held back by the budget; the end report shows the usage.
- `dureh day status` shows the account total and this site's part (sessions whose working directory is
  inside the site), so one site cannot silently use up the day for the others.

## Director commands

Run from your own terminal; they are refused inside any Claude session.

```text
bin/dureh site workday on --memo "..."      # off: bin/dureh site workday off --memo "..."
bin/dureh site workday set hours off --memo "..."          # devices: hours, weekday, usage, session
bin/dureh site workday black-company on --memo "..."       # off: ... black-company off --memo "..."
bin/dureh site workday calibrate --weekly-percent 23
bin/dureh site workday status
bin/dureh site session plan | start | status               # the day's session job (status is read-only)
bin/dureh site session start --force --memo "..."          # a second session today while the first job still runs
bin/dureh site session start --purpose overtime            # a new session for approved overtime (first prompt "<desk name>, 추가근무 출근")
bin/dureh site session start --prompt "..." | --no-prompt  # replace or drop the first prompt this time
```

`dureh status` and the session start show a `Workday:` line while the mode is on (phase, start-end and the
session job id when the day was started by `session start`).
