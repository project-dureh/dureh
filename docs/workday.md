# Workday mode

An optional working rhythm for a site's desk. Off by default; only the Director switches it.

## The day

1. **Start.** The desk reports a plan to the Director (goal, work items, order, expected usage) and
   records it: `dureh day plan --goal ... --file plan.md`.
2. **Approval.** When the Director approves, the desk records the Director's words:
   `dureh day approve --memo "..."`. This starts an 8-hour clock (`workday.hours` in config.json).
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
- A replacement WI that keeps a plan WI's goal and only fixes its checks, base or constraints (`day decide`).
- Gate 2 for checker-verified module and project WIs in the plan. Promotion into derivatives and
  harness/package contributions wait for the end report.
- After the end time, one checker run for an item staged during the day. A re-dispatch after a failed
  check is overtime.

Never held by the plan or the clock: stopping an agent (`dispatch stop`) or a scheduler job, merge-window
handling (idle, `adapter sync`, `verify --self`), recording upstream replies. For data at risk the desk
tells the Director at once; on the Director's word `dureh day urgent --wi <WI>... --memo "..."` releases
those WIs from the plan, the clock and the budget until the next day.

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
bin/dureh site workday calibrate --weekly-percent 23
bin/dureh site workday status
```

`dureh status` and the session start show a `Workday:` line while the mode is on.
