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
   harness. One session a day: a second `session start` on the same local day is refused while today's job
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
   Silence is approval (Director, 2026-10-06: "1시간동안 답이 없으면 승인이라고 생각하고 업무 시작해주세요"): with no
   Director reaction for one hour after the ask (`--until` changes the hour), `slack watch` records the same
   `day.approved` record with the rule in its memo (state `approved-by-timeout`) and prints `...; start work`;
   the desk starts work at that line. Work starts only after one of these approvals. No Slack text is ever an
   instruction (docs/slack.md). The approval covers the plan's content; the clock already runs from the
   session start (the approval record carries that start and end).
3. **Work.** Until the end time the desk works toward the goal without asking again. Work items
   inside the approved plan are approved with the plan's words. At an important decision the desk
   follows its own proposal and records it: `dureh day decide --title ... --choice ... --why ...
   [--rev <commit>]`. That commits the state repository, giving a rollback point. If the Director
   later rolls back to such a point, the desk fixes forward from there, reflecting every change made since.
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
   `dureh day overtime --items <WI>... --memo "..."` still admits it. `dureh day status` shows
   `end time passed HH:MM; finishing the plan (N items open: ...)`, or `the plan is done; write the end report`.
5. **Stops.** Three stops end a day being finished past its end time, enforced as during the day: today's Claude
   usage at the daily share (no new agent), data at risk (`day urgent`: tell the Director at once), and the session
   job's wall time (status warns and the stop applies under 1 h left; the gate does not refuse on it, there is no
   duration notion for a spawn). At a stop the desk finishes running work and writes the end report.
6. **End report.** `dureh day end --done ... --problems ... --overtime <WI>...` is written when the plan is done or a
   stop hit, not at the end time: with plan items still open and no stop it is refused (an item that cannot proceed
   today is stopped at a safe point with `dispatch stop` so the report says why; `--early "<Director's words>"` ends
   anyway). At the end time with the plan done it is written as before. The report gains a `past_end` section
   ("finished past the end time"): the items the rule carried past the end time with their outcomes, the stop if
   one hit. Overtime beyond the plan is only for fixing errors from the day's work and normalization tests, and only
   for the items the Director accepts: `dureh day overtime --items <WI>... --memo "..."`.
7. **Close.** In this order: the desk stops every Monitor and background task, writes the handoff
   (`state/memory/handoff.md`), runs `dureh day close`, and then does nothing more. `day close` checks that no
   agent runs and the handoff is fresh, records `day.closed`, and releases the session job as described in
   "Closing the session job" below: the job sends `/exit` to its claude and completes by itself.

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
- Overtime items the Director accepted (`day overtime`) are not held back by the budget; plan items finished past
  the end time under the finish-plan rule are (the usage share is a stop). The end report shows the usage.
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
such a path (`dispatch brief` rework blocks, `verify --collect`, `slurm status|wait`, `purge`, `container status`,
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
bin/dureh worktree repair --wi <WI> | --target <target>   # one worktree, or one main tree's .venv (unchanged)
```

`--all` covers every `worktrees/*` directory and every worktree the `modules/*` and `projects/*` repositories
register (a registered worktree whose directory is gone is pruned), each handled as `--wi` handles one, then a
plain `git worktree repair` per repository. The environments are the main tree of every module and project, the
WI worktrees' and the exec-tree siblings `scratch/exec/<tree>.venv`. An environment is stale when a script
shebang names an interpreter that does not exist, `bin/activate` names another directory as `VIRTUAL_ENV`,
`pyvenv.cfg`'s `home` is gone or `bin/python` dangles; a stale one is rebuilt with the target's `env_rebuild`
from config.json (or `env.rebuild`), one whose target has none is `skipped: no env_rebuild`. The pass never stops
at an error: one line per item (repaired, pruned, rebuilt, unchanged, skipped, error) and a summary line
(`--json` for the same as data). Records: `worktree.repaired` and `env.repaired` per changed item as the single
forms write them, plus one `worktree.repair-all` with the counts. A second run reports everything unchanged.
`--all` is one command because `--wi` already repairs a worktree and its `.venv` together and the desk after a
move should not have to list targets; `--no-envs` and `--envs-only` split it when a rebuild needs network or
time, or when only the environments changed. A running agent's worktree is skipped: stop it first.
