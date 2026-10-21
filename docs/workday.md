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
   command. Pending landed release (Director, 2026-10-06): when the newest `landed` message in the site's
   mailbox names a release checkout (`adapter sync --harness <checkout>`) that is not the pinned harness,
   `session start` applies it first, after its refusal checks and before anything is submitted: it runs that
   checkout's own `dureh --site <site> adapter sync` (the same switch as `bin/dureh adapter sync --harness`),
   records `harness.switched` (from, to, landed message id, output tail), re-reads the site and builds the
   job from the new checkout, so the session boots on the new release instead of the desk syncing after
   clock-in. A failed switch stops the start with the sync output; nothing is submitted. `session plan`
   prints a `will switch harness to ...` line when one is pending; `--no-apply-landed` boots on the current
   harness. **Never a downgrade (release 70).** On 2026-10-16 a start switched a site from r2026.10.15-3 back to
   r2026.10.13-2 because the newest landed message named it. Now the checkout is applied only when it is strictly
   newer than the pinned harness (`upstream.release_order`): a different revision; with release tags on both
   (`rYYYY.MM.DD-N`), a later tag; and, when git can tell, a history that contains the current revision (when it
   cannot, the tag order alone decides; with neither, it is not newer). Otherwise the start goes on with the
   current harness, prints `skipped landed <id>: <to> is not newer than <from> (<why>)`, and records
   `harness.switch-skipped` (from, to, landed id, reason; `harness_switch_skipped` on `session.started`). A skipped
   message is consumed: it is never pending again. `session plan` prints `will skip landed ...` instead. One session a day: a second `session start` on the same local day is refused while today's job
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
   records it: `dureh day plan --goal ... --file plan.md`. **Director actions (release 58).** The plan also lists
   every action only the Director can take that day (a dataset link, a push approval, a config change), one
   `--director-action "<text>"` each (repeatable); they ride on `day.planned` as `director_actions`, `day status`
   prints them under `대표님 조치 필요:` (one line per action) and `slack ask --kind day-plan` appends the same list
   to the approval request, so the Director handles them in one go at approval instead of being asked one by one
   during the day. (Director, 2026-10-12: "꼭 필요할때는 기다릴수 있지만, 이를 예방하기 위한 제안을 제게 하고 제가 선택적으로
   해결해주기로 합니다"; the Director adopted this and the standing fast-forward push below: "1번과 2번 복수 선택".)
   When one is done, `dureh day action done <n|text> [--ref <commit, issue URL or record>]` (desk, #32) appends
   `day.action.done`: `day status` stops listing it and the end report shows `director_actions` {open, done}.
2. **Approval.** When the Director approves in the session, the desk records the Director's words:
   `dureh day approve --by Director --memo "..."` (`--by` is required since release 78; see below). With Slack configured (`slack.director_user` set), the desk also runs
   `dureh slack ask --kind day-plan` right after `day plan` and `dureh slack watch` as a Monitor; the Director's
   ✅ reaction on that bot message is recorded by `slack check`/`slack watch` exactly as `day approve` would, with
   memo `Slack ✅ by <user> on <ts>` and `via: slack` (❌ holds the plan; the request expires at the day end).
   Silence is approval (Director, 2026-10-06: "1시간동안 답이 없으면 승인이라고 생각하고 업무 시작해주세요"): with no
   Director reaction for one hour after the ask (`--until` changes the hour), `slack watch` records the same
   `day.approved` record with the rule in its memo (state `approved-by-timeout`) and prints `...; start work`;
   the desk starts work at that line. **DUREH's approval (release 71).** The company day plan is an approval-layer-2
   kind (CHARTER, layer table): when DUREH approves the plan (in the session or by a mailbox reply), the desk records
   `dureh day approve --by DUREH --memo "<DUREH's words>"`. `--by` takes `Director` or
   `DUREH` and is refused otherwise; since release 78 it is required (no default: an approval left without `--by`
   used to be recorded as the Director's, improvement plan v0.4 bundle 1), and old records keep their `by` (and with `via: slack`, which is the Director's own reaction); the `day.approved`
   record keeps its shape with `by` set, the approval counts the same with workday mode on or off, and `day status`
   shows `approved <id> by <who>`. **Addenda (release 73, project-dureh/dureh#26).** When the plan is done early and
   the desk proposes more work the same day, it records an addendum instead of a second plan (which stays refused
   while today's plan is approved and open): `dureh day plan --addendum --file addendum.md [--goal G] [--wi WI ...]`
   appends a `day.planned` record with `addendum: true`, `addendum_to` (the plan's id, also a ref) and `at`; it is
   refused unless today's plan is approved and its end report not yet written. The day does not restart: plan,
   approval, start and end stay as they were. The approver's OK is recorded with
   `dureh day approve --addendum --by Director|DUREH --memo "<words>"` (a layer-2 kind like the plan): a
   `day.approved` record with `addendum` (the addendum's id) and `plan`, for the latest addendum only, once. From
   then on the addendum's WIs (its `--wi`, else every WI id its text names) count into the day like the plan's own:
   `plan_items`, the finish-plan rule after the end time, and the end report's titles. `day status` prints one line
   per addendum (`Addendum <id> (HH:MM): <goal>: <WIs>; approved <id> by <who>` or `waits for approval`), with workday
   mode on or off, and the end report carries `addenda` `[{record, at, goal, items, approval, approved_by}]`.
   Work starts only after one of these approvals. No Slack text is ever an
   instruction (docs/slack.md). The approval covers the plan's content; the clock already runs from the
   session start (the approval record carries that start and end).
3. **Work.** Until the end time the desk works toward the goal without asking again. Work items
   inside the approved plan are approved with the plan's words. At an important decision the desk
   follows its own proposal and records it: `dureh day decide --title ... --choice ... --why ...
   [--rev <commit>]`. That commits the state repository, giving a rollback point. If the Director
   later rolls back to such a point, the desk fixes forward from there, reflecting every change made since.
   When the choice was one of a fixed list, name the list (release 52): `--option A --option B ...
   [--question "..."] [--confidence high|medium|low] [--action acted|escalated|provisional] [--fact name=yes|no]`.
   `--choice` must be one of the options. The `day.decision` event keeps its shape and stays the rollback
   point; the typed block goes on a `day-choice` decision record that references the event, and `day status
   --json` and the end report show it next to the decision. `confidence low` needs `--action escalated` (with
   `--by` naming the Director) or `provisional`; a provisional choice is the "easy to change" one below.
   **Never stall on the Director (release 56).** Director, 2026-10-12: "제가 바뻐서 못볼때 진행못하고 멈춰있는것을
   근무시간으로 보는것은 어려울것같아요. 가능하면 계속 업무가 수행되야하는거라." A question to the Director holds only the
   item that needs the Director's own action (a push, a link-project, a config change, an approval-exec item);
   every other item continues on an easy-to-change provisional choice recorded with `dureh day decide ...
   --action provisional`. A question with no answer after 30 minutes proceeds the same way on the option that
   needs no Director action, recorded with `--action provisional` too; the end report lists these choices under
   `provisional` (title, choice). The day plan names, for every choice it offers the Director, the fallback the
   desk takes after 30 minutes. **Fail-closed (release 78, improvement plan v0.4 bundle 1).** A fail-closed kind
   (a push other than the standing fast-forward, a dataset or project link, a config change that changes running
   behaviour, GPU or Slurm above the caps, a harness merge or release, training data or fine-tuning, public or
   organization settings, a structural change, an early close, a data write or delete outside a WI worktree, anything
   about credentials) has no 30-minute fallback and no timeout approval: it waits for an explicit answer; independent
   work continues. `github item ask --fail-closed` labels the ask `fail-closed` and refuses a body that names a
   fallback option; `day decide --fail-closed` (or `--ask <url>` of such an ask) refuses `--action provisional` and an
   `acted` choice without the approver in `--by`; `day plan` refuses a line marked `fail-closed` that names a fallback
   option. A wait is allowed only when the Director alone can act (a push, a dataset link,
   an approval-exec item). Director, 2026-10-12: "꼭 필요할때는 기다릴수 있지만, 이를 예방하기 위한 제안을 제게 하고 제가
   선택적으로 해결해주기로 합니다": the desk records such a wait with `dureh day decide ... --action escalated --by
   Director`, putting a prevention proposal in `--why` (a standing authorization, bundling the Director's actions
   into the morning plan ask, a harness change), and the end report lists every wait with its proposal under
   `waiting` for the Director to choose from. Two of those proposals are built in since release 58, the Director
   having chosen both ("1번과 2번 복수 선택"): the plan's Director actions (step 1) and the **standing fast-forward
   push**: with config `push.standing_ff` true, `dureh push --repo <path> --remote <remote> --ref <branch>` without
   `--approval` pushes when the remote is in `push.allowed_remotes`, the branch exists there and its tip is an
   ancestor of the pushed revision (a new branch only with `push.standing_ff_new_branches`); nothing is ever forced,
   the event carries `approval: "standing-ff", fast_forward: true`, and a non-fast-forward is refused naming the
   approval path (`dureh push --approve ...`, then `--approval <decision>`). `site retire-module`'s "push it first"
   refusal names the same way out (docs/config.md).
4. **End time: finish the plan.** The day's plan is finished the same day. Director, 2026-10-06: "버그는 수정되고,
   업무는 하루 계획량까지 해야하는게 하네스의 기본 골자입니다", and to the desk: "문제가 발생시, 초과근무로 문제를 모두
   해결하고 당일 업무를 마무리 짓는다." When a problem appears, the desk fixes it and finishes; a plan item is never moved
   to tomorrow because of the clock, and plan items need no per-day overtime approval. With config
   `workday.finish_plan` (default true) the gate keeps allowing work units after the end time for (a) the plan's
   WIs that are not yet finished (verified, promoted, stopped or reverted) and (b) fixes for problems they caused:
   a WI created after the plan whose record names a plan WI (or an earlier fix) as `parent` or `fix_for`
   (`dureh wi new --fix-for <WI>`), or a rework/amendment of a plan WI. The plan's WIs are `day plan --wi ...`, else
   every WI id the plan text names. The first work unit after the end time records one `day.overtime` event by
   itself (`rule: finish-plan`, memo `Director rule 2026-10-06: 초과근무로 문제를 모두 해결하고 당일 업무를 마무리`,
   `by: Director (standing rule)`, `items`: the plan's unfinished WIs); fixes that appear later are appended by
   further events. The scheduler start check follows the same rule: during the day a job for a plan item or a fix
   for one that would end after the end time is submitted anyway (the run's `start_check` says `past_end: true`,
   the same `day.overtime` record is written, the submit warns) unless a stop holds, while a job outside the plan
   is refused as before. New work outside the plan stays refused after the end time; the Director's explicit
   `dureh day overtime --items <WI>... --memo "..."` admits it. `dureh day status` shows
   `end time passed HH:MM; finishing the plan (N items open: ...)`, or `the plan is done; write the end report`.
   **Overtime pre-approval (release 42, UR-20261006-01).** Director, 2026-10-06: "초과근무 조건을 퇴근 직후 진행하는데
   문제가 있어 보이네요." `day overtime` no longer waits for the end report: while on duty, or while finishing the plan,
   the desk records the Director's approval of an item outside the plan at once (`day.overtime` with
   `pre_approved: true`, the Director's memo, `at`, `phase`); a second call appends items. It takes effect at once:
   the item and its fixes (`--fix-for`/`--parent`) pass the gate after the end time and the scheduler start check
   while on duty, like plan items; a Director item is not held by the budget or a stop, as before. One predicate
   decides for the gate and the start check, `workday.past_end_rule(site, wi)` / `allowed_past_end` (reason
   "plan item", "fix for WI-x" or "Director overtime (<memo>)"), so they cannot disagree; the automatic
   `day.overtime` record is written for plan items only (Director items have their record; a start check past the
   end says `rule: director-overtime`). A refusal names the way out: "WI-0128 is not in today's plan and has no
   overtime record: either record the Director's approval with `dureh day overtime --items WI-0128 --memo
   "<Director's words>"` (allowed while on-duty), add it to the plan's bundle, or shorten --time to end before
   HH:MM." With the day ended and its plan done, overtime is still agreed in the end report.
   **Overtime plan (release 42).** Director, 2026-10-06: "회사는 새로 오늘 초과근무분의 근무계획을 새로 작성하고, 초과근무를
   수행하라고 하세요." When overtime starts (the Director's approval, an early end, or the finish-plan rule's own
   record at the first work unit after the end time) the desk writes a new plan for the overtime portion only:
   its items, their order, the expected agent spawns and rough usage. It is recorded with the approval
   (`day overtime ... --plan <file> [--goal G]`, kept as `overtime_plan`/`goal` in the record) or afterwards with
   `dureh day overtime-plan --file <plan> [--goal G] [--usage U]` (`day.overtime-plan` {plan, goal, items: the
   current overtime items, Director and automatic, expected_usage, updates}); the newest counts. The Director's
   overtime approval covers that plan; there is no second approval. The gate: once overtime started and no
   overtime plan exists, the first overtime work unit is admitted (the gate marks it, `day.overtime-started`) and
   the next are refused with "overtime plan missing: write the overtime portion's plan and record it with
   `dureh day overtime-plan --file <plan>` (Director, 2026-10-06), then continue." Status shows
   `overtime: N items (...), plan recorded HH:MM` or `plan missing` with that warning; the end report's `past_end`
   section carries the overtime plan (`overtime_plan`), the plan-rule items with outcomes (`items`) and the
   Director's items with outcomes and memos (`director`, `director_memos`). In the development phase the clock
   never blocks, so no record opens the overtime by itself (release 59): there `day overtime-plan` is allowed as
   soon as the end time passed and the day is still open (`hours_exceeded`); the record carries `opened_by:
   hours.exceeded` and `items` (the plan's open items when no overtime record names any), it counts as the start
   of the overtime, and `day status` and the end report's `past_end` show it as above.
5. **Stops.** Three stops end a day being finished past its end time, enforced as during the day: today's Claude
   usage at the daily share (no new agent), data at risk (`day urgent`: tell the Director at once), and the session
   job's wall time (status warns and the stop applies under 1 h left; the gate does not refuse on it, there is no
   duration notion for a spawn). At a stop the desk finishes running work and writes the end report.
6. **End report.** `dureh day end --done ... --problems ... --overtime <WI>...` is written when the plan is done or a
   stop hit, not at the end time: with plan items still open and no stop it is refused (an item that cannot proceed
   today is stopped at a safe point with `dispatch stop` so the report says why; `--early --reason "<Director's
   words>"` ends anyway). At the end time with the plan done it is written as before. The report gains a `past_end` section
   ("finished past the end time"): the items the rule carried past the end time with their outcomes, the stop if
   one hit. Overtime beyond the plan is only for fixing errors from the day's work and normalization tests, and only
   for the items the Director accepts: `dureh day overtime --items <WI>... --memo "..."` (recorded while on duty as
   soon as the Director approves, or after the report). **Jobs of the day (release 42).** Director, 2026-10-06:
   "일과가 끝날 때 각 job에 대해 몇 개의 job으로 수행했는지, 각 job ID와 수행 command를 보고서에 작성하라고 해주세요." The report
   gains a `jobs` section: every scheduler run recorded since the day plan (`slurm submit` jobs and arrays, `--via`
   submissions, `verify --submit` jobs), each with its WI, step (script) or via command, level, the number of
   tasks and of jobs, every job id (an array as `<id>_[0-N]` plus each task id), the exact command recorded at
   submit time, the state summary from the recorded states (`slurm watch|status|wait`; the scheduler is not asked
   at day end) and the log dir, plus the totals and the line `jobs today: N runs, M tasks, K failed`. The Slack
   end-report summary (`dureh report --kind end-report`: the first paragraph) carries that line. **Provisional
   (release 56).** The report's `provisional` list names every decision of the day recorded with `--action
   provisional` (its event id, the `day-choice` record, title, choice and question) next to `decisions`, so the
   Director sees at the end which choices were taken without waiting (step 3) and may overturn them. Its `waiting`
   list names every decision recorded with `--action escalated` the same way, plus the desk's prevention
   proposal (`proposal`, the record's `--why`), so the Director chooses which waits to prevent.
7. **Close.** In this order: the desk stops every Monitor and background task, writes the handoff
   (`state/memory/handoff.md`), runs `dureh day close`, and then does nothing more. `day close` checks that no
   agent runs and the handoff is fresh, records `day.closed`, and releases the session job as described in
   "Closing the session job" below: the job sends `/exit` to its claude and completes by itself. Release 45:
   scheduler jobs of the site user that no harness record owns (`External jobs` in `dureh status`, docs/slurm.md)
   do not refuse the close; they are recorded in `day.closed` (`external_jobs`) and the result carries a
   `warning` naming them, since they keep running after the close. **Workday mode off (release 67).** After
   10-15 at a site without workday mode (`day close` was refused, the job was scancelled by hand) the close also
   runs with the mode off: there is no plan or end report to wait for; the idle and handoff checks still apply
   (session device on), the handoff must be newer than the running session's `session.started` (when one is
   recorded), `day.closed` carries `workday: false` and no plan ref, and the job is released the same way.
   When a plan was recorded today with the mode off (below), and the day is not closed yet, `day.closed`
   references it (`refs` and `plan`), whether or not `day end` was written.

### Workday mode off: the plan without hours (release 70)

`day plan` was refused with the mode off ("workday mode is off"), so a site without workday mode had no
recorded plan, items or Director actions. Now, with the mode off:

- `day plan` records `day.planned` as with the mode on (goal, text, `items`, `director_actions`, titles, heads,
  usage) plus `workday: false`. A plan of an earlier day, or one not yet approved, is replaced by the new one
  (`supersedes`, and a ref); today's approved plan must be ended (`day end`) or the day closed first.
- `day approve` (or the Director's Slack ✅, or the silence rule through `slack ask --kind day-plan`) records
  `day.approved` with the approver's words (`--by DUREH` for a layer-2 approval) and `workday: false`, and no start or end: no clock starts, nothing is
  gated, there is no overtime and no finish-plan rule. `day status` and `dureh status` show a
  `Day (workday mode off): <phase>, plan <id>[, approved <id> by <who>]` line and the Director actions.
- `day end` writes the end report without the finish-plan check (`--overtime` is refused): `day.ended` with
  `workday: false`. `day close` does not need it. Both keep to today's session clock (the session start plus
  `workday.hours`): before that end they need the Director's recorded approval, as below (release 74).
- `day decide` records `day.decision` against today's approved plan, as with the mode on (#27); without an
  approved plan it is refused. `day overtime`, `day overtime-plan` and `day urgent` stay workday-mode commands.
- When the Director switches the mode on later that day, an approval recorded with the mode off does not count:
  the day is `planned` again and `day approve` starts the clock as usual.

### Closing the session job (release 39)

Director, 2026-10-06, after the 10-05 close: an upstream `day close` from outside the job recorded `day.closed`
but could not release the job (only the caller's own `SLURM_JOB_ID` was ever cancelled), the desk inside was
still writing its handoff with Monitors running, and a manual `scancel` three minutes later killed it (SIGTERM,
unclean). The normal path was unclean too: `day close` inside the job scancelled it at once, mid-turn.

- **Close marker.** The job script holds while its tmux session lives and watches
  `logs/session/<NAME>.close` next to the pane log (`NAME` is the tmux session `cc-<name>[-purpose]-<job id>`;
  `sessions.close_marker(site, job_id, purpose)` in Python). When the marker appears the script sends
  `Escape`, then `/exit` + Enter to the pane, waits up to the grace (`session.close_grace_seconds`, default
  90 s) for claude to leave (its exit prints `CLAUDE_REMOTE_EXIT <rc>`; with the marker present the tmux
  session holds 10 s after that instead of 60), then kills the tmux session if it still lives, and returns:
  the job completes normally, no scancel. The job's `.out` and the pane log get `dureh session: close marker
  seen at <time>; sending /exit, grace N s` and then `graceful exit after N s` or `killed after grace N s`.
- **`dureh day close`** (default `session.release_on_close` true) writes that marker for the site's running
  session job: the newest `session.started` record whose job squeue still lists (the last three distinct jobs
  are asked, newest first; squeue unavailable counts the newest as possibly running). Only a recorded job is
  ever touched, whether the close runs inside that job (the desk), in another job (the upstream team's own;
  never touched) or in none. It records `session.released` with `mode: "marker"`, `job_id`, `marker`, `grace`,
  `from: inside|outside` and prints `session job <id> ends by itself within ~<grace+30> s (close marker written);
  do nothing more in the session`. The marker's content is `{"actor", "closed_at", "record": <day.closed id>,
  "job_id"}`. No running recorded job: `released: false, reason: "no running session job"`.
- **`dureh day close --cancel-now`** is the old immediate `scancel` of the recorded job (`mode: "scancel"`):
  the fallback when a marker was ignored (claude hung, the script predates release 39). Same ownership rule.
- **`dureh site session release [--cancel-now]`** writes the marker (or scancels) for the recorded running job
  without a day close: for a day already closed whose job still runs (the 10-05 situation), recorded the same
  way (`session.released`, referencing the `session.started` record). It refuses when no recorded job runs.

The desk talks to the Director at the start and at the end of the day; in between only when data is at risk.

The day runs to its end time even when the plan is done: the desk stays on duty while idle. When everything is
idle it proposes the next work; if that fits the goals the Director set, it proceeds on its own recommendation
(`day decide`) and reports it at the end.

### Ending before the end (release 74, project-dureh/dureh#33)

Incident 2026-10-19: a desk closed its day at 11:35 EDT, four hours before its 15:39 EDT end, because it read the
local end time as UTC (Director: "무단퇴근입니다"). The rule: **never end or close the day before its end without the
Director's recorded approval; check `day status` first; times are local.**

- `day status` shows the end in local time with UTC beside it and the time left: text `ends 15:39 EDT (19:39 UTC),
  4 h 04 min left`; `--json` `end_local`, `end_utc`, `start_utc`, `end_in`. With workday mode off it shows today's
  session clock the same way (`Day clock (workday mode off): session start ..., ends ...`).
- `day end` and `day close` before the planned end are refused unless `--early --approval <id> --reason R` is
  given. The planned end is the approved end (workday mode on), else today's session clock (the session start plus
  `workday.hours`; workday mode off), else the config window's `workday.end`; with the hours device off (the
  Director's switch) there is none. The refusal names the end in local time and UTC, the time now and how long is
  left.
- `<id>` is a recorded approval, at most 24 h old: a `decision` record with disposition `approve` by `Director` or
  `DUREH` (approval layer 2), not one bound to another action (`gate1`, `gate2`, `push-approval`, `day-choice`),
  e.g. `dureh ledger add decision --disposition approve --by Director --summary "early close" --memo "<words>"`;
  or a board Decision = Approved recorded by `dureh github watch` (`github.decision.changed`), named by its event
  id, the issue URL or `org/repo#N`.
- The end report and `day.closed` carry `early_close` (`planned_end`, `planned_end_utc`, `left`, `approval`,
  `reason`) and reference the approval. A close after an end report that recorded an early approval is covered by
  it (`covered_by`). `--early "<words>"` (the old form) still reads as the reason; past the end with plan items
  open the reason alone ends the day, as before.
- Office hours end with the day: their wall time runs to the day's end (workday mode on or off) and they do not
  start after it (project-dureh/dureh#8).

Where the Director would decide, the desk picks something easy to change, records it as provisional, and keeps
going. `day plan` and `day end` record the heads of state and of every module and project: rollback points.

What the plan approval covers (agreed with a company desk, 2026-10-03):

- The plan lists each work item with its expected agent spawns and a rough usage estimate, and the day
  total against the daily share, so the Director can cut items at plan time.
- Amending a plan WI's checks, acceptance, constraints, outputs or base (`wi amend`, which also re-opens a
  stopped item); a different goal is a new WI.
- Re-opening a verified plan WI that is not promoted (release 45): `wi amend --wi <WI> --reopen --memo
  "<Director's words>" [--add-constraint ...] [--check ...]` records the amendment with `reopened_from: verified`
  and a `wi.reopened` event (refs the amendment, names the verify run it voids). The item goes back to
  `approved` and is dispatched again as usual; no `dispatch stop`. Only `wi.reopened` moves an item from verified
  to approved (`wi approve` cannot); a promoted item never changes, and an item of a verified or promoted bundle
  is refused (re-open the bundle first). Without `--reopen`, amending a verified item is refused with this
  command in the message. A Gate 2 refused on a merge conflict (`wi.promote-failed`, release 60; docs/work-items.md)
  is recovered the same way, with `--rebase <target HEAD>` so the next dispatch rebases the work.
- Gate 2 for checker-verified module and project WIs in the plan. Harness/package
  contributions wait for the end report.
- After the end time, one checker run for an item staged during the day. A re-dispatch after a failed
  check is overtime (for a plan item, recorded by the harness under the finish-plan rule).

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
  recorded before the end report (`dureh day overtime --items ... --memo "..."`); the finish-plan rule does not
  replace that memo on Sat/Sun. After the hours only the plan's open items and their fixes go on (finish the
  plan, above); everything else waits for the Director's word.

The finish-plan rule applies in both phases: in development nothing blocks anyway, but the overtime for plan items
is still recorded and the end report waits for the plan (or a stop) the same way. `workday.finish_plan: false`
restores the earlier rule (after the end time nothing new starts; overtime only by the Director's record).

## Attendance devices (Director switches)

Every attendance device can be switched off and on by the Director only, from the Director's terminal:
`dureh site workday set <device> on|off --memo "..."`, recorded as `workday.device` events (by: Director).

| device | what it does when on | when off |
|---|---|---|
| `hours` | the 8-hour clock (blocks in production, warns in development) | no clock; `day end`/`day close` need no `--early` |
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
- The week (release 72, project-dureh/dureh#24): when `<exchange>/usage-budget.json` has `weekly_reset` (one
  instant of the provider's weekly reset, set upstream by `tools/workday.py`), the week runs from the latest reset
  at or before now (every 7 days from that instant; one given in the future counts back by whole weeks) to now,
  the boundary day cut at the reset instant; without it, the rolling seven local days ending today. `day status
  --json` names it in `usage`: `window_kind` (`reset` or `rolling`), `window_start`, `window_end`; the text line
  adds `since reset <start>`. `site workday calibrate` measures the same window and keeps `weekly_reset` when it
  rewrites the shared file.
- Limits: sessions on other machines (web, app) leave no local transcript and are not counted;
  without `weekly_reset` the rolling seven days differ from the provider's weekly reset. Recalibrate now and then.
- Overtime items the Director accepted (`day overtime`, before or after the end report) are not held back by the
  budget; plan items finished past the end time under the finish-plan rule are (the usage share is a stop). The
  end report shows the usage.
- `dureh day status` shows the account total and this site's part (sessions whose working directory is
  inside the site), so one site cannot silently use up the day for the others.
- `dureh usage` prints `spawns_today`, today's spawns (the `wi.dispatched` / `checker.dispatched` events) counted
  per kind per model (`{kinds: {kind: {model: n}}, total}`; a spawn recorded before release 54 counts under
  `unknown`), and the end report carries the same table since the day plan as `spawns` (release 54; the kinds are
  in docs/work-items.md "Kind and model of a brief"). Both tables name their window (release 59): `window:
  {since, day}`, where `since` is the day plan's record id (the end report) or null (`usage`, which counts the
  UTC day `day`, as the records' `ts` are written). With `models.routing` set, compare it with
  `today_by_model` over the week.

## Director commands

Run from your own terminal; they are refused inside any Claude session.

```text
bin/dureh site workday on --memo "..."      # off: bin/dureh site workday off --memo "..."
bin/dureh site workday set hours off --memo "..."          # devices: hours, weekday, usage, session
bin/dureh site workday black-company on --memo "..."       # off: ... black-company off --memo "..."
bin/dureh site workday calibrate --weekly-percent 23
bin/dureh site workday status
bin/dureh site session plan | start | status               # the day's session job (status is read-only)
bin/dureh site session release [--cancel-now]              # end the recorded running job: close marker (/exit, grace), or scancel now
bin/dureh day close [--cancel-now]                         # the desk's close; --cancel-now is the fallback when a marker was ignored
bin/dureh site session start --force --memo "..."          # a second session today while the first job still runs
bin/dureh site session start --purpose overtime            # a new session for approved overtime (first prompt "<desk name>, 추가근무 출근")
bin/dureh site session start --prompt "..." | --no-prompt  # replace or drop the first prompt this time
bin/dureh site session start --no-apply-landed             # boot on the current harness even if a landed release is pending
```

`dureh status` and the session start show a `Workday:` line while the mode is on (phase, start-end and the
session job id when the day was started by `session start`).

## After the site folder moved or was renamed

One procedure, in this order, with the company idle (no agent running, no session job):

```text
mv DUREH-Companies/<old name> DUREH-Companies/<new name>   # the move or rename itself (Director)
<new>/bin/dureh adapter sync                 # generated files name the new location; config site.root refreshed; site.moved recorded
<new>/bin/dureh worktree repair --all --dry-run && <new>/bin/dureh worktree repair --all   # worktree links and virtualenvs
<new>/bin/dureh verify --self                # confirms the moved site
```

`adapter sync` (release 41) regenerates `.claude/settings.json`, `CLAUDE.md`, the agents and the project guidance
with the new absolute paths (`bin/dureh` and the hooks find the site from their own location and
`$CLAUDE_PROJECT_DIR`, so they work before the sync too), rewrites config `site.root` to where the site is now
(informational: every command uses the directory it runs in) and records one `site.moved` event `{from, to}` the
first time it sees the new location; a second sync records nothing. Records are append-only and keep the paths
they were written with (worktrees, job scripts, log directories, exec trees, container caches); readers that open
such a path (`dispatch brief` rework blocks, `verify --collect`, `slurm status|wait`, `purge`,
`slurm submit --resubmit-failed`, `path-refs`) first map it through `Site.relocate`: a path under the live root is
used as is, one under a previous root (the `from` of the `site.moved` events, config `site.previous_roots` when
present, or a stale `site.root` the sync has not seen yet) is re-rooted, and a path outside the site is left alone.
`path-refs <path>` searches the path under every root the site had, so references written before the move are
found. Session names, the mailbox and WI branches come from config (`session.name`, `upstream.mailbox`, `site.id`),
not from the folder name (steps 1-2).

Absolute paths live in two places the harness owns: the links between a repository and its WI worktrees
(`worktrees/<WI>/.git` and `modules/<m>/.git/worktrees/<WI>/gitdir`) and the virtualenvs (script shebangs,
`bin/activate`'s `VIRTUAL_ENV`, `pyvenv.cfg`). A move or rename breaks both; everything else resolves against
`$DUREH_SITE` (proposals/site-rename-independence.md). Repair with one desk command (release 40):

```text
bin/dureh worktree repair --all --dry-run   # what would change; nothing written
bin/dureh worktree repair --all             # every worktree and environment of the site
bin/dureh worktree repair --all --no-envs   # worktrees only (no network needed)
bin/dureh worktree repair --all --envs-only # environments only; --force rebuilds them stale or not
bin/dureh worktree repair --wi <WI> | --target <target>   # one worktree, or one main tree's .venv
bin/dureh worktree repair --target <target> --force       # rebuild that one .venv, stale or not (release 51; --wi too)
bin/dureh worktree repair --target <target> --dry-run     # report only (the lock check runs; the rebuild does not)
bin/dureh worktree repair --all --prune-stale [--remove]  # list (then remove) the .venv.stale-* copies left behind
```

`--all` covers every `worktrees/*` directory and every worktree the `modules/*` and `projects/*` repositories
register (a registered worktree whose directory is gone is pruned), each handled as `--wi` handles one, then a
plain `git worktree repair` per repository. The environments are the main tree of every module and project, the
WI worktrees' and the exec-tree siblings `scratch/exec/<tree>.venv`. An environment is stale when a script
shebang names an interpreter that does not exist, `bin/activate` names another directory as `VIRTUAL_ENV`,
`pyvenv.cfg`'s `home` is gone or `bin/python` dangles (`stale_interpreter`), or, since release 51
(UR-20261011-01), when it does not match the lock (`stale_lock`): for a target whose `env_rebuild` is one `uv
sync ...` command (optionally after `cd X &&` or `VAR=value` prefixes), the repair runs that command plus
`--check` in the tree (without the caller's `VIRTUAL_ENV`/`UV_PROJECT_ENVIRONMENT`/`UV_NO_SYNC`, 120 s), once per
environment, and a non-zero exit is the reason (its last lines). An environment synced without an extra the lock
declares is thus rebuilt alone, instead of `--force --all` rebuilding every environment of the site. The result
says `lock_check`: `ok`, `stale`, `unavailable` (the `env_rebuild` is not a `uv sync`: the interpreter check
alone decides, as before) or `shared` (a shared-form worktree `.venv` carries no packages of its own; its main
tree's row, or `--target`, answers for it). A stale one is rebuilt with the target's `env_rebuild`
from config.json (or `env.rebuild`), one whose target has none is `skipped: no env_rebuild`. The rebuild starts
from nothing: the old directory is moved to `.venv.stale-<time>` first (a `uv sync` into it keeps the installed
scripts and their old shebangs) and removed once the rebuild succeeds; it stays, named in the error, when the
rebuild fails. A worktree with no `.venv` at all gets one created the same way. Release 47: a WI worktree's
missing `.venv`, or a stale (or `--force`d) shared-form one, is provisioned as `dispatch brief` does it (the
shared form when its `uv.lock` matches the main tree's and the main env is usable, else `env_rebuild` within
`env.rebuild_timeout`; docs/slurm.md "Worktree environment at dispatch"); an own `.venv` is rebuilt as before. `--prune-stale` lists the
`.venv.stale-*` copies under `worktrees/*`, the main trees and `scratch/exec` with their size (`--target` for one
main tree) and removes them with `--remove`, one `env.repaired` record (`env: pruned`) each; a plain `.venv` is
never touched, and `verify` ignores the copies as it ignores `.venv`. The pass never stops at an error: one line
per item (repaired, pruned, rebuilt, unchanged, skipped, error) and a summary line (`--json` for the same as
data). Records: `worktree.repaired` and `env.repaired` per changed item as the single
forms write them, plus one `worktree.repair-all` with the counts. A second run reports everything unchanged.
A `worktrees/<WI>` directory its checkout no longer registers (a deregistered leftover, absent from that
checkout's `git worktree list`) is skipped by `--all` and `--envs-only` with the line `skipped  not registered:
<path>` and is never repaired, pruned or deleted (release 59); removing it is the desk's own decision.
`--all` is one command because `--wi` already repairs a worktree and its `.venv` together and the desk after a
move should not have to list targets; `--no-envs` and `--envs-only` split it when a rebuild needs network or
time, or when only the environments changed. A running agent's worktree is skipped: stop it first. `--force`,
`--dry-run` and `--json` go with `--wi` and `--target` as well (release 51): one environment can be rebuilt
stale or not, or reported on, without touching the others; `--envs-only` and `--no-envs` stay `--all` options.
