"""Workday mode: an 8-hour day bounded by a morning plan and an evening report, with a usage budget.

Switched on and off only by the Director (`dureh site workday on|off`, refused inside agent
sessions). While on:

- the desk reports a day plan (`day plan`); the approval (`day approve --memo`, the Director's, or DUREH's under
  approval layer 2 with `--by DUREH`, release 71) starts the clock; until the end time the desk works toward the plan's goal without asking again;
- important decisions follow the desk's own proposal and are recorded with a state commit
  (`day decide`), so a rollback can return to that point;
- after the end time the day's plan is still finished the same day (Director, 2026-10-06: "버그는 수정되고,
  업무는 하루 계획량까지 해야하는게 하네스의 기본 골자입니다"; "문제가 발생시, 초과근무로 문제를 모두 해결하고
  당일 업무를 마무리 짓는다"): with config `workday.finish_plan` (default true) the gate keeps allowing work
  units for the plan's unfinished WIs and for fixes of problems they caused, and the harness records that
  overtime itself (`day.overtime`, rule `finish-plan`, by "Director (standing rule)"); work outside the plan
  stays refused unless the Director accepts it (`day overtime`, recorded while on duty as soon as the Director
  approves, or after the end report; UR-20261006-01). The stops are the daily usage share, data at
  risk (`day urgent`) and the session job's wall time. The end report (`day end`) is written when the plan is
  done or a stop hits;
- each work unit (an agent spawn) checks the account-wide Claude usage against the daily share of
  the weekly allowance (default 1/7, all models including Fable), shared by every company and
  the upstream team.

While off (release 70) `day plan`, `day approve` and `day end` still record the plan, its items, the Director's
actions, the approval and the report, with `workday: false` and no hours: no clock, gate, overtime or budget.

State is the event records (append-only); there is no other switch. The weekly allowance comes
from the Director's calibration, kept in the shared exchange (`<exchange>/usage-budget.json`) when
one is configured, so every site reads the same number, else in this site's records.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from . import claude_usage
from .records import Ledger, short_title, titled_text
from .site import Site
from .util import DurehError, fail

DEFAULT_HOURS = 8.0
DEFAULT_OVERTIME_HOURS = 16.0  # the session job's wall time covers the day plus this, capped at 24 h
OVERTIME_PLAN = "day.overtime-plan"  # the overtime portion's own plan (Director, 2026-10-06)
OVERTIME_BEGAN = "day.overtime-started"  # written by the gate at the first work unit it admits in overtime
DAY_CHOICE = "day-choice"  # decision record type written by `day decide --option ...` (release 52)
DAY_TYPES = ("day.planned", "day.approved", "day.decision", "day.ended", "day.overtime", OVERTIME_PLAN, OVERTIME_BEGAN,
             "day.closed")
UNIT_TYPES = ("wi.dispatched", "checker.dispatched")  # a work unit: an agent spawn the gate admitted
# Overtime plan (Director, 2026-10-06: 회사는 새로 오늘 초과근무분의 근무계획을 새로 작성하고, 초과근무를 수행하라고 하세요): once
# overtime started, the first work unit is in flight; the next waits for the overtime portion's plan.
PLAN_MISSING = ("overtime plan missing: write the overtime portion's plan and record it with "
                "`dureh day overtime-plan --file <plan>` (Director, 2026-10-06), then continue.")
URGENT = "day.urgent"
SESSION = "session.started"  # written by `dureh site session start`; the day's clock runs from it (Director, 2026-10-04)
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_WEEKDAYS = (0, 1, 2, 3, 4)
PHASES = ("development", "production")
DEFAULT_PHASE = "development"
DEVICES = ("hours", "weekday", "usage", "session")  # attendance safety devices, each a Director switch
DEVICE_EVENT, BLACK_EVENT = "workday.device", "workday.black-company"
BLACK_NAME = "블랙기업모드"  # identifier `black-company`: every device off at once, loudly
HOURS_EXCEEDED = "hours.exceeded"  # development phase: recorded once per day when the 8 hours pass
# Finish the plan (Director, 2026-10-06): after the end time the plan's unfinished items and their fixes go on as
# overtime the harness records itself; `day.overtime` records of this rule carry `rule: "finish-plan"`.
FINISH_RULE = "finish-plan"
FINISH_RULE_MEMO = "Director rule 2026-10-06: 초과근무로 문제를 모두 해결하고 당일 업무를 마무리"
FINISH_RULE_BY = "Director (standing rule)"
# The Director's own overtime record (`day overtime`): after the end report as before, or while on duty once the
# Director approves (UR-20261006-01, Director 2026-10-06: 초과근무 조건을 퇴근 직후 진행하는데 문제가 있어 보이네요); it takes
# effect at once, so the gate and the scheduler start check admit the items and their fixes past the end time.
DIRECTOR_RULE = "director-overtime"
OPEN_STATUSES = ("proposed", "approved", "dispatched", "staged", "rework")  # a work unit can still start
WI_RE = re.compile(r"\bWI-\d{4,}\b")
WALL_WARN = timedelta(hours=1)  # the session job's remaining wall time under this is a stop


def _now() -> datetime:
    fake = os.environ.get("DUREH_FAKE_NOW")  # tests only
    return datetime.fromisoformat(fake) if fake else datetime.now().astimezone()


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def settings(site: Site) -> dict:
    """config.json `workday` (hours, overtime_hours, daily_share, model_weights, transcripts, weekdays, supervised,
    start/end fallback window, finish_plan); defaults otherwise."""
    cfg = site.config.get("workday") or {}
    ot = cfg.get("overtime_hours")
    fp = cfg.get("finish_plan")
    return {"hours": float(cfg.get("hours") or DEFAULT_HOURS),
            "overtime_hours": float(DEFAULT_OVERTIME_HOURS if ot is None else ot),
            "daily_share": float(cfg.get("daily_share") or 1 / 7),
            "model_weights": cfg.get("model_weights") or {},
            "transcripts": cfg.get("transcripts") or claude_usage.DEFAULT_ROOTS,
            "weekdays": _weekdays(cfg.get("weekdays")),
            "phase": _phase(cfg.get("phase")),
            "start": cfg.get("start") or None, "end": cfg.get("end") or None,
            "finish_plan": True if fp is None else bool(fp)}


def _phase(raw) -> str:
    """`workday.phase` (Director, 2026-10-04; provisional name): `development` (default) or `production`."""
    phase = str(raw or DEFAULT_PHASE)
    if phase not in PHASES:
        fail(f"config.json workday.phase: unknown phase {phase!r} (use {' or '.join(PHASES)})")
    return phase


def devices(site: Site, events: list[dict] | None = None) -> dict:
    """The Director's device switches, replayed from the records: {"state": {device: bool}, "black_company":
    {since, memo} | None}. Black-company mode turns every device off and remembers the state it found;
    switching it off restores that state."""
    state = {d: True for d in DEVICES}
    black = None
    for e in (events if events is not None else _events(site)):
        pl = e["payload"]
        if pl.get("by") != "Director":
            continue
        if e["type"] == DEVICE_EVENT and pl.get("device") in DEVICES:
            state[pl["device"]] = bool(pl.get("enabled"))
        elif e["type"] == BLACK_EVENT:
            if pl.get("enabled"):
                black = {"since": e["ts"], "memo": pl.get("memo"), "record": e["id"], "previous": dict(state)}
                state = {d: False for d in DEVICES}
            elif black is not None:
                state = {d: bool(black["previous"].get(d, True)) for d in DEVICES}
                black = None
    return {"state": state, "black_company": black}


def device_on(site: Site, name: str, events: list[dict] | None = None) -> bool:
    """Whether an attendance device applies: `hours` (the 8-hour clock), `weekday` (workday.weekdays), `usage`
    (the daily usage gate), `session` (session start/close checks). Off when the Director switched it off
    (`dureh site workday set <device> off`, or black-company mode); in the development phase the weekday
    device is off and the hours device only warns (see `hours_mode`)."""
    if name not in DEVICES:
        fail(f"unknown attendance device {name!r}; devices: {', '.join(DEVICES)}")
    if not devices(site, events)["state"][name]:
        return False
    if name == "weekday" and settings(site)["phase"] == "development":
        return False
    return True


def hours_mode(site: Site) -> str | None:
    """`block` (production: no new agent after the end), `warn` (development: an `hours.exceeded` record and a
    warning line, never a block), None when the hours device is off."""
    if not device_on(site, "hours"):
        return None
    return "block" if settings(site)["phase"] == "production" else "warn"


def _weekdays(raw) -> set[int]:
    """`workday.weekdays`: names (Mon..Sun) or numbers (0 = Monday); default Monday to Friday."""
    if raw is None:
        return set(DEFAULT_WEEKDAYS)
    out = set()
    for item in raw:
        if isinstance(item, int):
            out.add(item % 7)
        elif str(item)[:3].lower() in WEEKDAY_NAMES:
            out.add(WEEKDAY_NAMES.index(str(item)[:3].lower()))
        else:
            fail(f"config.json workday.weekdays: unknown weekday {item!r} (use Mon..Sun)")
    return out


def _events(site: Site) -> list[dict]:
    return [e for e in Ledger(site).events() if e["type"].startswith(("workday.", "day.", "session.", "hours."))]


def session_start(e: dict) -> datetime:
    """When a `session.started` record's job was submitted (its `submitted_at`, else the record time), local."""
    return _local((e.get("payload") or {}).get("submitted_at") or e["ts"])


def day_start(e: dict) -> datetime:
    """When the day of a `session.started` record began: its `day_start` (a restart or forced second start
    keeps the first session's start; the 8 hours do not begin again), else its own submission."""
    return _local((e.get("payload") or {}).get("day_start") or (e.get("payload") or {}).get("submitted_at") or e["ts"])


def _local(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=_now().tzinfo)).astimezone(_now().tzinfo)


def todays_session(events: list[dict]) -> dict | None:
    """The last session dureh started today: its submission is the start of the company's working day."""
    today = _now().date()
    found = [e for e in events if e["type"] == SESSION and session_start(e).date() == today]
    return found[-1] if found else None


def _at(day: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":")[:2])
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def clock(site: Site, session: dict | None, fallback: datetime) -> tuple[str, str]:
    """The day's start and end: `workday.hours` from the session start; without a session record (manual
    start) the config window `workday.start`/`workday.end` when set, else the hours from `fallback`."""
    st = settings(site)
    if session is not None:
        start = day_start(session)
        return _ts(start), _ts(start + timedelta(hours=st["hours"]))
    start = _at(fallback, st["start"]) if st["start"] else fallback
    end = _at(fallback, st["end"]) if st["end"] else start + timedelta(hours=st["hours"])
    return _ts(start), _ts(end)


def working_day(site: Site, start: str) -> bool:
    """False when the day falls outside `workday.weekdays`: the whole day is overtime (Director, 2026-10-04).
    Only with the weekday device on (production phase, not switched off by the Director)."""
    return not device_on(site, "weekday") or _parse(start).weekday() in settings(site)["weekdays"]


def enabled(site: Site, events: list[dict] | None = None) -> bool:
    sw = [e for e in (events if events is not None else _events(site)) if e["type"] == "workday.switched"
          and e["payload"].get("by") == "Director"]
    return bool(sw and sw[-1]["payload"].get("enabled"))


def _exchange_budget_file(site: Site) -> Path | None:
    raw = (site.config.get("upstream") or {}).get("exchange")
    if not raw:
        return None
    root = Path(site.expand(raw))
    return root / "usage-budget.json" if root.is_dir() else None


def budget(site: Site, events: list[dict] | None = None) -> dict:
    st = settings(site)
    out = {"weekly_units": None, "calibrated": None, "source": None, "weekly_reset": None,
           "daily_share": st["daily_share"], "model_weights": st["model_weights"]}
    f = _exchange_budget_file(site)
    if f and f.is_file():
        try:
            data = json.loads(f.read_text())
            out.update(weekly_units=data.get("weekly_units"), calibrated=data.get("calibrated"), source=str(f),
                       weekly_reset=data.get("weekly_reset"))  # release 72 (#24): the provider's reset instant
            out["model_weights"] = data.get("model_weights") or out["model_weights"]
            return out
        except (OSError, ValueError):
            pass
    cal = [e for e in (events if events is not None else _events(site)) if e["type"] == "workday.calibrated"
           and e["payload"].get("by") == "Director"]
    if cal:
        out.update(weekly_units=cal[-1]["payload"]["weekly_units"], calibrated=cal[-1]["ts"], source="records")
    return out


def usage(site: Site, events: list[dict] | None = None) -> dict:
    b = budget(site, events)
    roots = settings(site)["transcripts"]
    s = claude_usage.summary(b, roots, _now().date(), _now())  # the week: week_window() of the budget
    s.update(budget_source=b["source"], calibrated=b["calibrated"])
    s["this_site_units"] = claude_usage.by_place({"site": str(site.root)}, b, roots, _now().date())["site"]
    return s


def day(site: Site, events: list[dict] | None = None) -> dict:
    """The current day: phase off-duty | planned | on-duty | past-end | overtime, plus its records."""
    all_evs = events if events is not None else _events(site)
    session = todays_session(all_evs)
    evs = [e for e in all_evs if e["type"] in DAY_TYPES]
    # the day starts at the last plan; older days are history (an addendum, release 73, never starts a day)
    start = max((i for i, e in enumerate(evs) if e["type"] == "day.planned" and not _is_addendum(e)), default=None)
    if start is None:
        return {"phase": "off-duty", "reason": "no day plan", **_session_clock(site, session)}
    cur = evs[start:]
    plan = cur[0]
    approved = [e for e in cur if e["type"] == "day.approved" and not (e.get("payload") or {}).get("addendum")]
    ended = [e for e in cur if e["type"] == "day.ended"]
    closed = [e for e in cur if e["type"] == "day.closed"]
    overtime = [e for e in cur if e["type"] == "day.overtime"]
    decisions = [{"id": e["id"], "title": e["payload"]["title"], "commit": e["payload"].get("commit")}
                 for e in cur if e["type"] == "day.decision"]
    if decisions:  # typed blocks live on `day-choice` decision records that reference the event (release 52)
        typed = {ref: r for r in Ledger(site).load("decision") if r["type"] == DAY_CHOICE and r["payload"].get("typed")
                 for ref in r["refs"]}
        for dec in decisions:
            if dec["id"] in typed:
                dec["decision"], dec["typed"] = typed[dec["id"]]["id"], typed[dec["id"]]["payload"]["typed"]
                dec["why"] = typed[dec["id"]]["payload"].get("memo")  # release 56: the wait's prevention proposal
    out = {"plan": plan["id"], "date": plan["payload"].get("date"), "goal": plan["payload"].get("goal"),
           "decisions": decisions, **_session_clock(site, session)}
    if plan["payload"].get("director_actions"):
        out["director_actions"] = list(plan["payload"]["director_actions"])  # release 58
    adds = _addenda(cur)
    if adds:
        out["addenda"] = adds
    extra = addendum_items(adds)
    if closed:
        return {**out, "phase": "off-duty", "reason": f"day closed {closed[-1]['ts']}"}
    mode = hours_mode(site)
    on = enabled(site, all_evs)
    if on:  # release 70: an approval recorded with workday mode off starts no clock; switched on, approve again
        approved = [e for e in approved if not _unclocked(e)]
    elif approved and _unclocked(approved[-1]):
        return _unclocked_day(site, out, plan, approved[-1], ended, extra)
    if not approved:
        if mode == "block" and out.get("end") and _now() >= _parse(out["end"]) and not ended:
            return {**out, "phase": "past-end", "reason": f"the day ended at {out['end']} (never approved); "
                                                         "write the end report"}
        return {**out, "phase": "planned", "reason": "day plan waits for the Director's approval"}
    a = approved[-1]["payload"]
    out.update(start=a["start"], end=a["end"], approval=approved[-1]["id"], approved_by=a.get("by") or "Director",
               plan_items=sorted(set(plan_items(plan)) | set(extra)))
    out["working_day"] = working_day(site, out["start"])
    # the Director's accepted items (`day overtime --items`) and the ones the finish-plan rule recorded by itself
    director = sorted({w for e in overtime if e["payload"].get("rule") != FINISH_RULE for w in e["payload"]["items"]})
    out["overtime_auto"] = sorted({w for e in overtime if e["payload"].get("rule") == FINISH_RULE
                                   for w in e["payload"]["items"]})
    if not out["working_day"]:
        name = WEEKDAY_NAMES[_parse(out["start"]).weekday()].capitalize()
        out["reason"] = f"{name} is not a working day (workday.weekdays): overtime only"
    if ended:
        out["report"] = ended[-1]["id"]
        if director:
            return {**out, "phase": "overtime", "overtime": director}
        return {**out, "phase": "off-duty", "reason": "end report written, no overtime approved"}
    out["overtime"] = director
    out["overtime_memos"] = {w: e["payload"].get("memo") for e in overtime if e["payload"].get("rule") != FINISH_RULE
                             for w in e["payload"]["items"]}
    # the overtime portion's plan: `day overtime --plan` or a later `day overtime-plan`; the newest counts
    out["overtime_started"] = bool(overtime)
    began = [e for e in cur if e["type"] == OVERTIME_BEGAN]
    out["overtime_began"] = ({"record": began[0]["id"], "ts": began[0]["ts"], "at": began[0]["payload"].get("at"),
                              "work_unit": began[0]["payload"].get("work_unit")} if began else None)
    # (`plan` in a `day.overtime` record is the day plan's id; the text rides as `overtime_plan`)
    plans = [e for e in cur if e["type"] == OVERTIME_PLAN or (e["type"] == "day.overtime" and e["payload"].get("overtime_plan"))]
    out["overtime_plan"] = ({"record": plans[-1]["id"], "at": plans[-1]["payload"].get("at") or plans[-1]["ts"],
                             "goal": plans[-1]["payload"].get("goal"),
                             "plan": plans[-1]["payload"].get("overtime_plan") or plans[-1]["payload"].get("plan"),
                             "items": plans[-1]["payload"].get("items")} if plans else None)
    past = bool(mode) and _now() >= _parse(a["end"])
    if past and out["working_day"] and settings(site)["finish_plan"]:
        out.update(_finishing(site, plan, extra))  # finishing, open, fixes, statuses (Director rule 2026-10-06)
    if past:
        if mode == "block":
            if out.get("finishing"):
                return {**out, "phase": "past-end", "reason": _finishing_reason(out)}
            done = "; the plan is done" if out["plan_items"] and "open" in out else ""
            return {**out, "phase": "past-end", "reason": f"the day ended at {a['end']}{done}; write the end report"}
        out["hours_exceeded"] = True  # development phase: a warning, not a block
        out["warning"] = f"the 8-hour day ended at {a['end']} (workday.phase development: not blocking); " + (
            _finishing_reason(out) if out.get("finishing") else "write the end report")
        if out.get("finishing"):
            out["reason"] = _finishing_reason(out)
        if out["overtime_plan"]:
            out["overtime_started"] = True  # release 59: the plan itself opens the overtime portion in development
    if not out["working_day"]:
        return {**out, "phase": "overtime"}
    return {**out, "phase": "on-duty"}


def _unclocked(approval: dict) -> bool:
    """A `day.approved` record written with workday mode off (release 70): no start or end, `workday: false`."""
    return (approval.get("payload") or {}).get("workday") is False


def _unclocked_day(site: Site, out: dict, plan: dict, approval: dict, ended: list[dict], extra=()) -> dict:
    """The day of a plan approved with workday mode off (release 70): no hours, no clock, no overtime; the plan, its
    items, the Director's actions and the approval are on record; `on-duty` until `day end`, then `off-duty`."""
    out.update(approval=approval["id"], approved_by=(approval.get("payload") or {}).get("by") or "Director",
               plan_items=sorted(set(plan_items(plan)) | set(extra)), workday=False, approved_at=approval["ts"])
    if ended:
        return {**out, "report": ended[-1]["id"], "phase": "off-duty",
                "reason": "end report written (workday mode off)"}
    return {**out, "phase": "on-duty", "reason": "plan approved; workday mode off: no hours"}


def _is_addendum(e: dict) -> bool:
    return bool((e.get("payload") or {}).get("addendum"))


def _addenda(cur: list[dict]) -> list[dict]:
    """The day's addenda (release 73, project-dureh/dureh#26): `day.planned` records marked `addendum`, oldest first,
    each with its approval (a `day.approved` record whose `addendum` names it) or None."""
    appr = {(e.get("payload") or {}).get("addendum"): e for e in cur
            if e["type"] == "day.approved" and (e.get("payload") or {}).get("addendum")}
    out = []
    for e in cur:
        if e["type"] != "day.planned" or not _is_addendum(e):
            continue
        a = appr.get(e["id"])
        out.append({"record": e["id"], "ts": e["ts"], "at": e["payload"].get("at") or e["ts"], "goal": e["payload"].get("goal"), "items": plan_items(e),
                    "titles": e["payload"].get("titles") or {},
                    "approval": a["id"] if a else None, "approved_by": (a["payload"].get("by") or "Director") if a else None,
                    "approved_at": a["ts"] if a else None})
    return out


def addendum_items(addenda: list[dict]) -> list[str]:
    """The WIs of the approved addenda: they count into the day like the plan's own (finish-plan rule, end report)."""
    return sorted({w for a in addenda or [] if a.get("approval") for w in a["items"]})


def plan_items(plan_event: dict) -> list[str]:
    """The WIs a day plan lists: `items` recorded by `day plan --wi`, else every WI id named in the plan text."""
    p = plan_event.get("payload") or {}
    if p.get("items"):
        return sorted(set(p["items"]))
    return sorted(set(WI_RE.findall(p.get("plan") or "")))


def plan_progress(site: Site, plan_event: dict, extra=()) -> dict:
    """Where the plan stands: its WIs (the ones that exist), the fixes they caused, and which are still open.
    A fix is a WI created after the plan whose record names a plan WI (or an earlier fix) as `parent` or
    `fix_for`; a rework or amendment of a plan WI keeps its id and needs no link. Open means a work unit can
    still start for it (OPEN_STATUSES); verified, promoted, stopped and reverted items are finished for the day."""
    led = Ledger(site)
    ids = sorted(set(plan_items(plan_event)) | set(extra))  # release 73: plus the approved addenda's WIs
    recs = {r["id"]: r for r in led.load("work-item")}
    items = [w for w in ids if w in recs]
    related, fixes = set(items), []
    for r in sorted(recs.values(), key=lambda r: r["ts"]):
        if r["id"] in related or r["ts"] <= plan_event["ts"]:
            continue
        links = {r["payload"].get("parent"), r["payload"].get("fix_for")} - {None}
        if links & related:
            related.add(r["id"])
            fixes.append(r["id"])
    status = {w["id"]: w["status"] for w in led.work_items() if w["id"] in related}
    titles = {w: recs[w]["payload"].get("title") for w in items + fixes}
    return {"items": items, "fixes": fixes, "statuses": status, "titles": titles,
            "open": [w for w in items + fixes if status.get(w) in OPEN_STATUSES]}


def _finishing(site: Site, plan_event: dict, extra=()) -> dict:
    pr = plan_progress(site, plan_event, extra)
    out = {"open": pr["open"], "fixes": pr["fixes"], "statuses": pr["statuses"]}
    if pr["open"]:
        out["finishing"] = True
    return out


def plan_open(site: Site, d: dict) -> dict:
    """`d` (from `day()`) with the finish-plan fields `open`, `fixes`, `statuses` (plan_progress) filled in when
    the day has an approved plan and `day()` did not compute them yet (it does so only once the end time passed).
    Lets a check before the end time, such as a scheduler job that would run past it, ask the same question."""
    if "open" in d or not d.get("plan") or not d.get("approval"):
        return d
    plan = [e for e in _events(site) if e["type"] == "day.planned" and e["id"] == d["plan"]]
    if not plan:
        return d
    pr = plan_progress(site, plan[-1], addendum_items(d.get("addenda")))
    return {**d, "open": pr["open"], "fixes": pr["fixes"], "statuses": pr["statuses"]}


def in_plan(site: Site, wi: str | None, d: dict | None = None) -> bool:
    """The finish-plan predicate (Director rule 2026-10-06), shared by the gate and the scheduler start check:
    `wi` (or its parent) is one of the plan's open items or an open fix for one. False with `workday.finish_plan`
    off, without an approved plan, or for a WI outside the plan. `d` is `day()` when the caller has it."""
    if not wi or not settings(site)["finish_plan"]:
        return False
    d = plan_open(site, d if d is not None else day(site))
    if not d.get("open"):
        return False
    return bool(_wi_family(Ledger(site), wi) & set(d["open"]))


def past_end_rule(site: Site, wi: str | None, d: dict | None = None) -> tuple[str, str] | None:
    """Why `wi` may start, or run, past today's end time, as (rule, reason), or None. Rule DIRECTOR_RULE with reason
    "Director overtime (<memo>)" when `wi`, or the WI it fixes or belongs to (`fix_for`/`parent`), is in a
    `day.overtime` record of today by the Director (pre-approved while on duty, or accepted after the end report);
    else rule FINISH_RULE with reason "plan item" or "fix for WI-x" (`in_plan`, before the end report of a working
    day). The gate and the scheduler start check both ask this one question, so they can never disagree."""
    if not wi:
        return None
    d = d if d is not None else day(site)
    fam = _wi_family(Ledger(site), wi)
    hit = sorted(fam & set(d.get("overtime") or []))
    if hit:
        memos = d.get("overtime_memos") or {}
        memo = memos.get(wi) if wi in hit else memos.get(hit[0])
        via = "" if wi in hit else f", as a fix for {hit[0]}"
        return DIRECTOR_RULE, f"Director overtime ({memo}{via})"
    if d.get("report") or d.get("phase") not in ("on-duty", "past-end") or not d.get("working_day", True):
        return None
    d = plan_open(site, d)
    if not in_plan(site, wi, d):
        return None
    if wi in (d.get("plan_items") or []):
        return FINISH_RULE, "plan item"
    links = sorted(fam - {wi})
    return FINISH_RULE, f"fix for {links[0]}" if links else "plan item"


def allowed_past_end(site: Site, wi: str | None, d: dict | None = None) -> str | None:
    """The reason `wi` may go on past today's end time ("plan item", "fix for WI-x", "Director overtime (...)"), or
    None when it may not (`past_end_rule`)."""
    r = past_end_rule(site, wi, d)
    return r[1] if r else None


def way_out(site: Site, wi: str, d: dict, time_hint: str | None = None) -> str:
    """The last sentence of a refusal past (or over) the end time: what admits `wi` (UR-20261006-01). `time_hint`
    (HH:MM) adds the scheduler's option of a shorter --time."""
    opts = [f"record the Director's approval with `dureh day overtime --items {wi} --memo \"<Director's words>\"` "
            "(allowed while on-duty)"]
    plan_rule = settings(site)["finish_plan"] and d.get("working_day", True)
    if plan_rule:
        opts.append("add it to the plan's bundle (`dureh wi new --fix-for <plan WI>` or `--parent <plan WI>`)")
    if time_hint:
        opts.append(f"shorten --time to end before {time_hint}")
    joined = opts[0] if len(opts) == 1 else ", ".join(opts[:-1]) + (", or " if len(opts) > 2 else " or ") + opts[-1]
    what = (f"{wi} is not in today's plan and has no overtime record" if plan_rule
            else f"{wi} has no overtime record and the finish-plan rule is off")
    return f"{what}: either {joined}."


def _finishing_reason(d: dict) -> str:
    n = len(d["open"])
    return f"end time passed {d['end'][11:16]}; finishing the plan ({n} item{'s' if n != 1 else ''} open: {', '.join(d['open'])})"


def _session_clock(site: Site, session: dict | None) -> dict:
    if session is None:
        return {}
    start, end = clock(site, session, _now())
    return {"session": session["id"], "job_id": (session.get("payload") or {}).get("job_id"), "start": start, "end": end}


def status(site: Site) -> dict:
    evs = _events(site)
    dv = devices(site, evs)
    switches = {"devices": dv["state"], "devices_off": [d for d in DEVICES if not dv["state"][d]],
                "black_company": dv["black_company"]}
    if not enabled(site, evs):
        out = {"enabled": False, **switches}
        d = day(site, evs)
        if d.get("plan") and d.get("date") == _now().date().isoformat():  # release 70: the plan without hours
            out["day"] = {k: d[k] for k in ("phase", "reason", "plan", "date", "goal", "approval", "approved_by",
                                            "report", "plan_items", "director_actions", "addenda") if d.get(k) is not None}
        return out
    out = {"enabled": True, "hours": settings(site)["hours"], "workday_phase": settings(site)["phase"], **switches,
           **day(site, evs)}
    out["urgent"] = _urgent(evs)
    if out.get("hours_exceeded") and not site.observe:
        note_hours_exceeded(site, out, evs)
    try:
        out["usage"] = usage(site, evs)
    except OSError as exc:
        out["usage"] = {"error": str(exc)}
    if out.get("finishing"):
        hs = hard_stop(site, out, evs, out["usage"])
        if hs:
            out["hard_stop"] = hs
            note = f"{hs['why']}: finish running work and write the end report (`dureh day end`)"
            out["warning"] = f"{out['warning']}\n⚠ {note}" if out.get("warning") else note
    if out.get("overtime_started") and not out.get("overtime_plan") and (
            out["phase"] in ("past-end", "overtime") or out.get("hours_exceeded")):
        out["overtime_plan_missing"] = True
        out["warning"] = f"{out['warning']}\n⚠ {PLAN_MISSING}" if out.get("warning") else PLAN_MISSING
    return out


def overtime_items(d: dict) -> list[str]:
    """Every WI in today's overtime: the Director's records and the finish-plan rule's own."""
    return sorted(set(d.get("overtime") or []) | set(d.get("overtime_auto") or []))


def overtime_units(site: Site, d: dict) -> list[dict]:
    """The work units (agent spawns) recorded after the gate admitted the first one in today's overtime
    (`day.overtime-started`, ledger order). Empty before that marker."""
    began = d.get("overtime_began")
    if not began:
        return []
    key = (began["ts"], began["record"])
    return [e for e in Ledger(site).events() if e["type"] in UNIT_TYPES and (e["ts"], e["id"]) > key]


def _overtime_plan_missing(site: Site, d: dict) -> str | None:
    """The gate's overtime-plan rule (Director, 2026-10-06): once overtime started and no overtime plan is recorded,
    the first overtime work unit is admitted (it is in flight), the next are refused until `day overtime-plan` (or
    `day overtime --plan`) records the overtime portion's plan."""
    if d.get("overtime_began") and not d.get("overtime_plan") and overtime_units(site, d):
        return f"workday mode: {PLAN_MISSING}"
    return None


def note_overtime_began(site: Site, d: dict, wi: str, rule: str) -> dict | None:
    """The gate's marker for the first work unit it admits in today's overtime (once per day)."""
    if d.get("overtime_began") or site.observe:
        return None
    return Ledger(site).event(OVERTIME_BEGAN, "desk", None, {"at": _ts(_now()), "work_unit": wi, "rule": rule,
                                                             "end": d.get("end")}, [d["plan"]])


def wall_left(site: Site, d: dict) -> timedelta | None:
    """Remaining wall time of the day's session job (squeue %L), None without a recorded job or an answer."""
    job = d.get("job_id")
    if not job:
        return None
    from . import cluster, sessions

    q = sessions._squeue(site, str(job))
    if not q.get("queued"):
        return None
    return cluster.parse_time(q.get("time_left"))


def hard_stop(site: Site, d: dict, evs: list[dict] | None = None, u: dict | None = None) -> dict | None:
    """The stops that end a day being finished past its end time (Director, 2026-10-06): today's usage at the
    daily share, data at risk (a `day urgent` record), the session job's wall time under WALL_WARN."""
    evs = evs if evs is not None else _events(site)
    if device_on(site, "usage", evs):
        u = u if u is not None else usage(site, evs)
        if u.get("daily_allowance") and u.get("today_units", 0) >= u["daily_allowance"]:
            return {"stop": "usage", "why": f"today's Claude usage {u['today_units']} units reached the daily share "
                                            f"{u['daily_allowance']}"}
    urgent_items = _urgent(evs)
    if urgent_items:
        return {"stop": "urgent", "why": f"data at risk: urgent {', '.join(urgent_items)}"}
    left = wall_left(site, d)
    if left is not None and left < WALL_WARN:
        return {"stop": "wall-time", "why": f"session job {d['job_id']} has {str(left).split('.')[0]} of wall time left"}
    return None


def note_hours_exceeded(site: Site, d: dict, evs: list[dict]) -> dict | None:
    """Development phase: record `hours.exceeded` once for this day (by plan id) when the 8 hours have passed."""
    if any(e["type"] == HOURS_EXCEEDED and e["payload"].get("plan") == d.get("plan") for e in evs):
        return None
    return Ledger(site).event(HOURS_EXCEEDED, "desk", None, {"plan": d.get("plan"), "start": d.get("start"),
                                                             "end": d.get("end"), "noticed_at": _ts(_now()),
                                                             "phase": settings(site)["phase"]}, [d["plan"]])


def banner(st: dict) -> str | None:
    """First line of `dureh status` and `dureh day` while black-company mode is on; after 24 h it nags."""
    b = st.get("black_company")
    if not b:
        return None
    line = f"⚠ {BLACK_NAME} (since {b['since']}, by Director)"
    if _now() - _parse(b["since"]).astimezone(_now().tzinfo) >= timedelta(hours=24):
        line += " — 24시간이 지났습니다; 끄세요: `dureh site workday black-company off --memo \"...\"`"
    return line


def render(st: dict, with_banner: bool = True) -> str | None:
    head = banner(st) if with_banner else None
    if not st.get("enabled"):
        dd = st.get("day")
        if not dd:
            return head
        # release 70: today's plan recorded with workday mode off (no hours, no clock)
        line = f"Day (workday mode off): {dd['phase']}, plan {dd['plan']}" + (
            f", approved {dd['approval']} by {dd.get('approved_by') or 'Director'}" if dd.get("approval") else "") + (
            f", end report {dd['report']}" if dd.get("report") else "") + (f"; {dd['reason']}" if dd.get("reason") else "")
        line += addenda_text(dd.get("addenda"))
        if dd.get("director_actions"):
            line += "\n" + director_actions_text(dd["director_actions"])
        return f"{head}\n{line}" if head else line
    line = f"Workday: {st['phase']}"
    if st.get("workday_phase") and st["workday_phase"] != "production":
        line += f" [{st['workday_phase']} phase]"
    if st.get("start"):
        line += f" ({st['start'][11:16]}-{st['end'][11:16]}" + (f", session job {st['job_id']})" if st.get("job_id")
                                                                   else ")")
    if st.get("overtime_started"):
        items = overtime_items(st)
        line += f"; overtime: {len(items)} item{'s' if len(items) != 1 else ''}" + (f" ({', '.join(items)})" if items else "")
        line += f", plan recorded {st['overtime_plan']['at'][11:16]}" if st.get("overtime_plan") else ", plan missing"
    if st.get("reason"):
        line += f"; {st['reason']}"
    u = st.get("usage") or {}
    if u.get("daily_allowance"):
        line += (f"; usage today {u['today_units']}/{u['daily_allowance']} units"
                 f" ({round(100 * (u['today_fraction'] or 0))}% of the daily share, this site {u.get('this_site_units', 0)}),"
                 f" week {u['week_percent']}%" + (f" since reset {u['window_start'][:16]}"
                                                   if u.get("window_kind") == "reset" else ""))
    elif "today_units" in u:
        line += f"; usage today {u['today_units']} units, this site {u.get('this_site_units', 0)} (budget not calibrated)"
    if st.get("urgent"):
        line += "; urgent " + ", ".join(st["urgent"])
    if st.get("devices_off"):
        line += "; devices off: " + ", ".join(st["devices_off"])
    if st.get("approval"):  # release 71: who approved the day plan (the Director, or DUREH under layer 2)
        line += f"; approved {st['approval']} by {st.get('approved_by') or 'Director'}"
    line += addenda_text(st.get("addenda"))
    if st.get("director_actions"):
        line += "\n" + director_actions_text(st["director_actions"])
    if st.get("warning"):
        line += f"\n⚠ {st['warning']}"
    return f"{head}\n{line}" if head else line


def addenda_text(addenda: list[dict] | None) -> str:
    """One line per addendum of the day (release 73): "\nAddendum <id> (<HH:MM>): <WIs>; approved <id> by <who>"."""
    out = ""
    for a in addenda or []:
        items = ", ".join(a["items"]) or "no WI"
        goal = f" {a['goal']}:" if a.get("goal") else ""
        state = f"approved {a['approval']} by {a['approved_by']}" if a.get("approval") else "waits for approval"
        out += f"\nAddendum {a['record']} ({a['at'][11:16]}):{goal} {items}; {state}"
    return out


DIRECTOR_ACTIONS_LABEL = "대표님 조치 필요:"


def director_actions_text(actions: list[str]) -> str:
    """The plan's Director-only actions (release 58) as the label and one line per action."""
    return "\n".join([DIRECTOR_ACTIONS_LABEL, *(f"- {a}" for a in actions)])


def _wi_family(led: Ledger, wi: str) -> set[str]:
    """`wi` with the WI it belongs to or fixes (`parent`, `fix_for`): a Director record for the one covers the other."""
    fam = {wi}
    try:
        pl = led.get("work-item", wi)["payload"]
    except DurehError:
        return fam
    return fam | ({pl.get("parent"), pl.get("fix_for")} - {None})


def _urgent(evs: list[dict]) -> list[str]:
    """WIs the Director released for data at risk; they hold until the day after their record."""
    today = _now().date().isoformat()
    return sorted({w for e in evs if e["type"] == URGENT and e["payload"].get("until", "") >= today
                   for w in e["payload"]["items"]})


def _first_check_of_staged(site: Site, wi: str, end: str) -> bool:
    """A checker run for an item staged before the day's end, and the first since that staging."""
    evs = Ledger(site).events(wi)
    staged = [e for e in evs if e["type"] == "wi.staged"]
    if not staged or _parse(staged[-1]["ts"]) >= _parse(end):
        return False
    return not [e for e in evs if e["type"] == "checker.dispatched" and e["ts"] > staged[-1]["ts"]]


def gate(site: Site, wi: str | None, checking: bool = False) -> str | None:
    """Reason to refuse starting a work unit (an agent for `wi`; `checking` for a checker run) now, or None."""
    evs = _events(site)
    if not enabled(site, evs):
        return None
    if wi and _wi_family(Ledger(site), wi) & set(_urgent(evs)):
        return None  # data at risk: released by the Director, not held by the clock or the budget
    d = day(site, evs)
    phase = d["phase"]
    if checking and wi and d.get("end") and phase in ("past-end", "overtime", "off-duty") \
            and _first_check_of_staged(site, wi, d["end"]):
        return None  # one checker run for an item staged during the day (desk request, UP-0030)
    if phase in ("off-duty", "planned"):
        return (f"workday mode: no approved day ({d.get('reason')}). Report the day plan with `dureh day plan` "
                "and record the approval with `dureh day approve --memo` (`--by DUREH` for a layer-2 approval).")
    fam = _wi_family(Ledger(site), wi) if wi else set()
    if phase == "past-end":
        rule = past_end_rule(site, wi, d)
        if rule:
            missing = _overtime_plan_missing(site, d)
            if missing:
                return missing
            note_overtime_began(site, d, wi, rule[0])
            if rule[0] == DIRECTOR_RULE:
                return None  # the Director's overtime (pre-approved on duty or after the report) is not held by the budget
            if not site.observe:
                note_finish_overtime(site, d, wi)  # the harness records the overtime itself; the stops follow
        elif d.get("finishing"):
            return (f"workday mode: end time passed {d['end'][11:16]}; only the plan's open items "
                    f"({', '.join(d['open'])}) and fixes for problems they caused go on (Director rule 2026-10-06). "
                    + (way_out(site, wi, d) if wi else "Work outside the plan needs the Director's word: "
                       "`dureh day overtime --items <WI>... --memo \"<Director's words>\"`."))
        else:
            return (f"workday mode: the 8-hour day ended at {d['end']}. Start nothing new; write the end report with "
                    "`dureh day end` and propose overtime items for the Director.")
    elif phase == "overtime":
        if not (fam & set(d["overtime"])):
            if not d.get("working_day", True) and not d.get("report"):
                return (f"workday mode: {d['reason']}. Worker agents spawn only for items in a Director overtime "
                        "record: `dureh day overtime --items <WI>... --memo \"<Director's words>\"`.")
            return (f"workday mode: overtime covers only {', '.join(d['overtime']) or 'nothing yet'} "
                    "(fixes and normalization tests the Director accepted in the end report).")
        missing = _overtime_plan_missing(site, d)
        if missing:
            return missing
        note_overtime_began(site, d, wi, DIRECTOR_RULE)
        return None  # accepted overtime is not held back by the budget
    else:
        if d.get("hours_exceeded"):
            note_hours_exceeded(site, d, evs)
        if d.get("finishing") and in_plan(site, wi, d) and not site.observe:
            note_finish_overtime(site, d, wi)  # development phase: nothing blocks, the overtime is still recorded
    if not device_on(site, "usage"):
        return None  # usage is counted and shown, not gated
    u = usage(site, evs)
    if u.get("daily_allowance") and u["today_units"] >= u["daily_allowance"]:
        return (f"workday mode: today's Claude usage {u['today_units']} units reached the daily share "
                f"{u['daily_allowance']} (1/{round(1 / u['daily_share'])} of the weekly allowance, shared by all "
                "companies and upstream). Finish running work and write the end report.")
    return None


def note_finish_overtime(site: Site, d: dict, wi: str) -> dict | None:
    """The finish-plan rule's own overtime record (Director, 2026-10-06), written at the first work unit after the
    end time with the plan's unfinished WIs; later work units append the fixes (and re-opened items) that appeared
    since. Nothing is written when every open item is already recorded."""
    recorded = set(d.get("overtime_auto") or [])
    new = [w for w in d["open"] if w not in recorded]
    if not new:
        return None
    payload = {"items": new, "memo": FINISH_RULE_MEMO, "by": FINISH_RULE_BY, "rule": FINISH_RULE, "plan": d["plan"],
               "end": d["end"], "at": _ts(_now()), "work_unit": wi}
    payload["appended" if recorded else "first_work_unit"] = True
    return Ledger(site).event("day.overtime", "desk", None, payload, [d["plan"]])


# ------------------------------------------------------------------ commands
def _require_on(site: Site) -> None:
    if not enabled(site):
        fail("workday mode is off for this site; only the Director switches it on (`dureh site workday on`)")


def _director_only(message: str | None = None) -> None:
    if os.environ.get("DUREH_ACTOR") or os.environ.get("CLAUDECODE"):
        fail(message or "`dureh site workday` is the Director's switch; run it from your own terminal, not inside an agent session")


def switch(site: Site, on: bool, memo: str) -> dict:
    _director_only()
    if not memo.strip():
        fail("--memo is required (the Director's reason)")
    settings(site)  # validates numbers
    # records name a role as actor; the Director's switch is marked by `by` and refused inside agent sessions
    return Ledger(site).event("workday.switched", "desk", None, {"enabled": on, "memo": memo, "by": "Director",
                                                                 "via": "dureh site workday"})


def set_device(site: Site, device: str, on: bool, memo: str) -> dict:
    """The Director switches one attendance device (hours, weekday, usage, session) on or off."""
    _director_only()
    if device not in DEVICES:
        fail(f"unknown attendance device {device!r}; devices: {', '.join(DEVICES)}")
    if not memo.strip():
        fail("--memo is required (the Director's reason)")
    if devices(site)["black_company"]:
        fail(f"{BLACK_NAME} is on: every device is off until `dureh site workday black-company off`")
    return Ledger(site).event(DEVICE_EVENT, "desk", None, {"device": device, "enabled": on, "memo": memo,
                                                           "by": "Director", "via": "dureh site workday set"})


def black_company(site: Site, on: bool, memo: str) -> dict:
    """`black-company on` turns every device off at once, remembering their state; `off` restores it."""
    _director_only("노동법 위반은 대표만 할 수 있습니다")
    if not memo.strip():
        fail("--memo is required (the Director's reason)")
    dv = devices(site)
    if on and dv["black_company"]:
        fail(f"{BLACK_NAME} is already on (since {dv['black_company']['since']})")
    if not on and not dv["black_company"]:
        fail(f"{BLACK_NAME} is not on")
    payload = {"enabled": on, "memo": memo, "by": "Director", "via": "dureh site workday black-company"}
    payload["previous" if on else "restored"] = dv["state"] if on else dv["black_company"]["previous"]
    return Ledger(site).event(BLACK_EVENT, "desk", None, payload)


def calibrate(site: Site, weekly_percent: float) -> dict:
    _director_only()
    st = settings(site)
    f = _exchange_budget_file(site)
    reset = None  # release 72 (#24): the shared file's weekly_reset sets the window and is kept on rewrite
    if f and f.is_file():
        try:
            reset = json.loads(f.read_text()).get("weekly_reset")
        except (OSError, ValueError):
            reset = None
    try:
        cal = claude_usage.calibrate(weekly_percent, st["model_weights"], st["transcripts"], _now().date(),
                                     weekly_reset=reset, at=_now())
    except ValueError as exc:
        fail(str(exc))
    cal["calibrated"] = _ts(_now())
    cal["model_weights"] = st["model_weights"]
    rec = Ledger(site).event("workday.calibrated", "desk", None, {**cal, "by": "Director", "via": "dureh site workday"})
    if f:
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({**cal, "by": "Director", "site": site.mailbox_name,
                                   **({"weekly_reset": reset} if reset else {})}, indent=2) + "\n")
        os.replace(tmp, f)
        cal["shared"] = str(f)
    return {**cal, "record": rec["id"]}


def plan(site: Site, actor: str, goal: str, text: str, items: list[str] | None = None,
         director_actions: list[str] | None = None, addendum: bool = False) -> dict:
    """Record the day plan. Its work items are `items` (`day plan --wi`, each an existing WI) or, without them,
    every WI id the text names: the finish-plan rule reads them (`plan_items`). `director_actions` (release 58,
    `--director-action`, repeatable) lists every action only the Director can take today (a dataset link, a push
    approval, a config change); `slack ask --kind day-plan` posts them so the Director handles them in one go."""
    if addendum:
        return plan_addendum(site, actor, text, goal, items)
    if not goal.strip() or not text.strip():
        fail("a day plan needs --goal and the plan text")
    on = enabled(site)  # release 70: with workday mode off the plan is recorded the same way, without hours
    d = day(site)
    if on and d["phase"] in ("on-duty", "past-end"):
        fail(f"the current day ({d['plan']}) is still open; write its end report first")
    supersedes = None
    if not on and d.get("plan") and d["phase"] != "off-duty":
        if d.get("approval") and d.get("date") == _now().date().isoformat():
            fail(f"today's plan {d['plan']} is approved and open; write its end report (`dureh day end`) or close the"
                 " day (`dureh day close`) first")
        supersedes = d["plan"]  # an unapproved plan, or an earlier day's: no clock to protect
    led = Ledger(site)
    for w in items or []:
        led.get("work-item", w)
    u = usage(site)
    payload = {"date": _now().date().isoformat(), "goal": goal, "plan": text, "usage_at_plan": u, "heads": heads(site)}
    if items:
        payload["items"] = sorted(set(items))
    actions = [a.strip() for a in director_actions or [] if a and a.strip()]
    if actions:
        payload["director_actions"] = actions
    payload["titles"] = _titles(led, plan_items({"payload": payload}))  # UR-20261008-02
    if not on:
        payload["workday"] = False
        if supersedes:
            payload["supersedes"] = supersedes
    return led.event("day.planned", actor, None, payload, [supersedes] if supersedes else None)


def plan_addendum(site: Site, actor: str, text: str, goal: str | None = None, items: list[str] | None = None) -> dict:
    """An addendum to today's approved, open day plan (release 73, project-dureh/dureh#26): a `day.planned` record
    with `addendum: true` and `addendum_to` (the plan's id, also a ref). It never starts a new day; once approved
    (`day approve --addendum`) its WIs count into the day like the plan's own."""
    if not text.strip():
        fail("an addendum needs the plan text (--file or stdin)")
    d = day(site)
    today = _now().date().isoformat()
    if not d.get("plan") or not d.get("approval") or d.get("report") or d["phase"] == "off-duty" \
            or d.get("date") != today:
        fail(f"an addendum needs today's day plan approved and open (phase {d['phase']}"
             + (f", plan {d['plan']}" if d.get("plan") else "") + "); record a day plan with `dureh day plan` instead")
    led = Ledger(site)
    for w in items or []:
        led.get("work-item", w)
    payload = {"date": today, "goal": (goal or "").strip() or None, "plan": text, "addendum": True,
               "addendum_to": d["plan"], "at": _ts(_now()), "usage_at_plan": usage(site), "heads": heads(site)}
    if items:
        payload["items"] = sorted(set(items))
    payload["titles"] = _titles(led, plan_items({"payload": payload}))
    if not enabled(site):
        payload["workday"] = False
    return led.event("day.planned", actor, None, payload, [d["plan"]])


def _titles(led: Ledger, wis) -> dict[str, str]:
    """{WI: short title} of the existing work items among `wis` (UR-20261008-02: reports name WIs with titles)."""
    payloads = led.work_item_payloads()
    return {w: short_title(payloads[w]) for w in sorted(set(wis)) if w in payloads}


APPROVERS = ("Director", "DUREH")  # release 71: who approved the day plan (`day approve --by`)


def approve(site: Site, actor: str, memo: str, via: str | None = None, extra: dict | None = None,
            by: str = "Director", addendum: bool = False) -> dict:
    """Record the approval of the day plan. The desk records the approver's words from the session;
    `via="slack"` is used only by `dureh slack check` after the Director's own ✅ reaction (release 33).
    `by` (release 71) is the approver: the Director (layer 3, the default) or DUREH (approval layer 2, CHARTER: the
    company day plan is a layer-2 kind); the record keeps its shape with `by` set, with workday mode on or off."""
    if by not in APPROVERS:
        fail(f"--by must be one of {', '.join(APPROVERS)} (not {by!r})")
    if via and by != "Director":
        fail(f"an approval via {via} is the Director's own")
    if not memo.strip():
        fail(f"--memo must carry the {by}'s words")
    d = day(site)
    if addendum:
        if via:
            fail("an addendum is approved in the session (`day approve --addendum`), not via " + via)
        return _approve_addendum(site, actor, memo, by, d)
    if d["phase"] != "planned":
        fail(f"no day plan waits for approval (phase {d['phase']})")
    if not enabled(site):  # release 70: the approval is recorded without hours; no clock starts
        payload = {"memo": memo, "by": by, "session": d.get("session"), "workday": False}
        if via:
            payload.update(extra or {}, via=via)
        return Ledger(site).event("day.approved", actor, None, payload, [d["plan"]])
    # the clock runs from the session dureh started today; approval covers the plan's content
    start, end = clock(site, todays_session(_events(site)), _now())
    if hours_mode(site) == "block" and _now() >= _parse(end):
        fail(f"the day started at {start} and ended at {end}; a plan approved now would be past its end")
    payload = {"start": start, "end": end, "memo": memo, "by": by, "session": d.get("session")}
    if via:
        payload.update(extra or {}, via=via)
    return Ledger(site).event("day.approved", actor, None, payload, [d["plan"]])


def _approve_addendum(site: Site, actor: str, memo: str, by: str, d: dict) -> dict:
    """Approve the day's latest addendum (release 73): a `day.approved` record with `addendum` (its id), refs the
    addendum and the plan; the day's own approval, start and end stay as they were."""
    adds = d.get("addenda") or []
    if not adds:
        fail(f"no addendum to approve (record one with `dureh day plan --addendum --file F`; phase {d['phase']})")
    last = adds[-1]
    if last["approval"]:
        fail(f"the latest addendum {last['record']} is already approved ({last['approval']} by {last['approved_by']})")
    if d.get("report") or d["phase"] == "off-duty" or not d.get("approval"):
        fail(f"the day of {d.get('plan')} is not open (phase {d['phase']}); an addendum is approved while the day runs")
    payload = {"memo": memo, "by": by, "addendum": last["record"], "plan": d["plan"], "session": d.get("session")}
    if not enabled(site):
        payload["workday"] = False
    return Ledger(site).event("day.approved", actor, None, payload, [last["record"], d["plan"]])


def heads(site: Site) -> dict[str, str | None]:
    """HEAD of state and of every linked module and project repository: a rollback point for the day."""
    out = {"state": _state_head(site)}
    for group in ("modules", "projects"):
        base = site.root / group
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if (d / ".git").exists():
                try:
                    p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True, timeout=30)
                    out[f"{group}/{d.name}"] = p.stdout.strip() or None
                except (OSError, subprocess.SubprocessError):
                    out[f"{group}/{d.name}"] = None
    return out


def _state_head(site: Site) -> str | None:
    try:
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=site.state, capture_output=True, text=True, timeout=30)
        return p.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def decide(site: Site, actor: str, title: str, choice: str, why: str, revs: list[str],
           typed: dict | None = None, by: str | None = None) -> dict:
    """Record a decision point (following the desk's own proposal) and commit state as a rollback point.
    With a typed block (release 52) a `day-choice` decision record is appended too, referencing the event; the
    event keeps its shape and stays the rollback point."""
    from .state import save_state

    _require_on(site)
    d = day(site)
    if d["phase"] not in ("on-duty", "overtime") and not d.get("finishing"):
        fail(f"decisions are recorded during the working day (phase {d['phase']})")
    if not (title.strip() and choice.strip() and why.strip()):
        fail("--title, --choice and --why are required")
    led = Ledger(site)
    rec = led.event("day.decision", actor, None, {"title": title, "choice": choice, "why": why,
                                                  "revs": revs, "state_before": _state_head(site)}, [d["plan"]])
    out = {"record": rec["id"]}
    if typed:
        dec = led.append("decision", DAY_CHOICE, actor, None,
                         {"subject": title, "disposition": "record", "by": by or actor, "memo": why, "typed": typed},
                         [rec["id"], d["plan"]])
        out["decision"] = dec["id"]
    saved = save_state(site, f"workday decision {rec['id']}: {title}")
    return {**out, "state_commit": _state_head(site) if saved.get("saved") else None,
            "saved": saved, "rollback": "the Director decides rollbacks in the end report; after one, fix forward "
                                        "and reflect every change made since this point"}


def end(site: Site, actor: str, done: str, problems: str, overtime: list[str], early: str = "") -> dict:
    """The end report. With workday mode off (release 70) it ends a plan approved without hours: no clock, so
    no early-end or finish-plan refusal; the report carries `workday: false` and no start or end."""
    d = day(site)
    if d.get("report") or d["phase"] not in ("on-duty", "past-end", "overtime"):
        fail(f"no open day to end (phase {d['phase']})")
    on = enabled(site)
    if not on and d.get("workday") is not False:
        _require_on(site)  # a clocked day of a site switched off since: as before
    if not on and overtime:
        fail("workday mode is off: there is no overtime to propose (--overtime needs workday mode)")
    if on and d["phase"] in ("on-duty", "overtime") and not d.get("hours_exceeded") and hours_mode(site) \
            and not early.strip():
        fail(f"the day runs to {d['end']}; stay on duty even when idle (propose next work, watch upstream messages)."
             " End early only on the Director's word: --early \"<Director's words>\"")
    evs = _events(site)
    stop = hard_stop(site, d, evs) if d.get("finishing") else None
    if d.get("finishing") and not stop and not early.strip():
        n = len(d["open"])
        fail(f"end time passed {d['end'][11:16]}, but the plan is not finished ({n} item{'s' if n != 1 else ''} open: "
             f"{titled_text(', '.join(d['open']), Ledger(site).labels())}). Director rule 2026-10-06: fix the problems and finish today's plan; the harness "
             "records the overtime. The end report is written when the plan is done or a stop hits (today's usage at "
             "the daily share, data at risk, the session job's wall time). An item that cannot proceed today: stop it "
             "at a safe point (`dureh dispatch stop --wi <WI> --reason ...`) so the report says why; to end anyway: "
             "--early \"<Director's words>\"")
    if not done.strip():
        fail("--done is required")
    led = Ledger(site)
    for w in overtime:
        led.get("work-item", w)
    u = usage(site, evs)
    report = {"done": done, "problems": problems, "overtime_proposed": overtime,
              "decisions": d["decisions"], "provisional": provisional(d["decisions"]), "waiting": waiting(d["decisions"]),
              "start": d.get("start"), "end": d.get("end"), "ended_at": _ts(_now()),
              "session": d.get("session"), "working_day": d.get("working_day", True),
              "early": early or None, "usage": u, "heads": heads(site), **({} if on else {"workday": False})}
    if d.get("addenda"):  # release 73: the day's addenda, each with its WIs and approval
        report["addenda"] = [{k: a[k] for k in ("record", "at", "goal", "items", "approval", "approved_by")}
                             for a in d["addenda"]]
    past = past_end_section(site, d, stop, early) if on else None
    if past:
        report["past_end"] = past
    from .ops import day_jobs

    plan_ev = next((e for e in evs if e["id"] == d["plan"]), None)
    report["jobs"] = day_jobs(site, (plan_ev["ts"], plan_ev["id"]) if plan_ev else None)  # Director 2026-10-06
    from .ops import spawn_table

    # release 54: the day's spawns per kind per model (since the plan), next to the usage units
    report["spawns"] = spawn_table(led.events(), (plan_ev["ts"], plan_ev["id"]) if plan_ev else None)
    named = list(overtime) + [r["wi"] for r in report["jobs"]["runs"] if r.get("wi")]
    named += plan_items(plan_ev) if plan_ev else []
    named += [w for a in d.get("addenda") or [] for w in a["items"]]
    named += [x["wi"] for x in (past or {}).get("items", []) + (past or {}).get("director", [])]
    report["titles"] = _titles(led, named)  # UR-20261008-02
    rec = led.event("day.ended", actor, None, report, [d["plan"]])
    if not on:
        return {**rec, "next": "give the Director this report, then `dureh day close`"}
    return {**rec, "next": "give the Director this report; record accepted overtime with "
                           "`dureh day overtime --items ... --memo \"<Director's words>\"`, else `dureh day close`"}


def provisional(decisions: list[dict]) -> list[dict]:
    """The end report's `provisional` line (release 56, Director 2026-10-12: the desk never stalls on the Director):
    the day's typed decisions recorded with `--action provisional` (title, choice, question, decision record), the
    choices the Director may still overturn."""
    return [{"id": dec["id"], "decision": dec.get("decision"), "title": dec["title"], "choice": dec["typed"]["chosen"],
             "question": dec["typed"].get("question")}
            for dec in decisions if (dec.get("typed") or {}).get("action") == "provisional"]


def waiting(decisions: list[dict]) -> list[dict]:
    """The end report's `waiting` line (release 56, Director 2026-10-12: "꼭 필요할때는 기다릴수 있지만, 이를 예방하기 위한 제안을
    제게 하고 제가 선택적으로 해결해주기로 합니다"): the day's decisions recorded with `--action escalated`, each the wait for
    an action only the Director can take, with the desk's prevention proposal (`--why`) for the Director to choose from."""
    return [{"id": dec["id"], "decision": dec.get("decision"), "title": dec["title"], "choice": dec["typed"]["chosen"],
             "question": dec["typed"].get("question"), "proposal": dec.get("why")}
            for dec in decisions if (dec.get("typed") or {}).get("action") == "escalated"]


def past_end_section(site: Site, d: dict, stop: dict | None, early: str = "") -> dict | None:
    """The end report's "finished past the end time" section: the items the finish-plan rule carried past the end
    time (recorded `day.overtime`, plus anything still open) with their outcomes; None when nothing went past."""
    auto = list(d.get("overtime_auto") or [])
    still_open = list(d.get("open") or [])
    ids = sorted(set(auto) | set(still_open))
    director = [w for w in (d.get("overtime") or []) if w not in ids]  # the Director's own items (`day overtime`)
    if not ids and not director and not d.get("overtime_plan"):
        return None
    led = Ledger(site)
    every = set(ids) | set(director)
    titles = {r["id"]: r["payload"].get("title") for r in led.load("work-item") if r["id"] in every}
    status = {w["id"]: w["status"] for w in led.work_items() if w["id"] in every}

    def outcome(w):
        return {"wi": w, "title": titles.get(w), "outcome": status.get(w), "finished": status.get(w) not in OPEN_STATUSES}

    return {"rule": FINISH_RULE, "memo": FINISH_RULE_MEMO, "by": FINISH_RULE_BY, "from": d.get("end"),
            "until": _ts(_now()), "items": [outcome(w) for w in ids], "open": still_open, "stop": stop,
            "early": early or None, "director": [outcome(w) for w in director],
            "director_memos": {w: (d.get("overtime_memos") or {}).get(w) for w in director},
            "overtime_plan": d.get("overtime_plan")}


def overtime(site: Site, actor: str, items: list[str], memo: str, plan_text: str | None = None,
             goal: str | None = None) -> dict:
    """Record the overtime items the Director accepted. After the end report as before; before it on a day outside
    `workday.weekdays` (the whole day is overtime), while on duty, or while finishing the plan past the end time
    (UR-20261006-01): the record then says `pre_approved: true` and takes effect at once, so the items and their
    fixes pass the gate after the end time and the scheduler's end-time check (`past_end_rule`). A second call
    appends items. With the day ended and its plan done, overtime is still agreed in the end report. `plan_text`
    (`--plan <file>`) and `goal` record the overtime portion's plan in the same record (else `day overtime-plan`)."""
    _require_on(site)
    d = day(site)
    pre = not d.get("report")
    if pre and d["phase"] not in ("on-duty", "overtime") and not d.get("finishing"):
        if d["phase"] == "past-end":
            fail("the day ended and its plan is done: overtime is agreed in the end report; write it with "
                 "`dureh day end` first")
        fail(f"no open day for overtime (phase {d['phase']})")
    if not memo.strip() or not items:
        fail("--items and --memo (the Director's words) are required")
    led = Ledger(site)
    for w in items:
        led.get("work-item", w)
    payload = {"items": items, "memo": memo, "by": "Director", "at": _ts(_now())}
    if pre:
        payload.update(pre_approved=True, phase=d["phase"], end=d.get("end"))
    if plan_text and plan_text.strip():
        payload["overtime_plan"] = plan_text  # `plan` names the day plan's id in these records
    if goal:
        payload["goal"] = goal
    return led.event("day.overtime", actor, None, payload, [d.get("report") or d["plan"]])


def overtime_plan(site: Site, actor: str, text: str, goal: str | None = None, usage: str | None = None) -> dict:
    """Record (or update) the overtime portion's plan (Director, 2026-10-06): the overtime items, their order, the
    expected agent spawns and rough usage. Allowed once overtime started today: a Director record (`day overtime`,
    pre-approved or after the report), the end report, or the finish-plan rule's own record at the first work unit
    after the end time. The Director's overtime approval covers the plan; there is no second approval. Release 59:
    in the development phase the clock never blocks, so no record opens the overtime by itself; there the plan is
    allowed as soon as the day's end time has passed and the day is still open (`hours_exceeded`), and it then
    counts as the start of the overtime (status and the end report show it)."""
    _require_on(site)
    if not text.strip():
        fail("the overtime plan text is required (--file <plan>)")
    d = day(site)
    if not d.get("approval"):
        fail(f"no open day for an overtime plan (phase {d['phase']}: {d.get('reason')})")
    if not (d.get("overtime_started") or d.get("report") or d.get("hours_exceeded")):
        fail("no overtime started today: it starts with the Director's record (`dureh day overtime --items ... --memo`), "
             "the end report, the finish-plan rule's own record at the first work unit after the end time, or (development "
             "phase) the end time passing")
    payload = {"plan": text, "goal": goal, "items": overtime_items(d) or list(d.get("open") or []), "at": _ts(_now()),
               "end": d.get("end"), "updates": (d.get("overtime_plan") or {}).get("record")}
    if d.get("hours_exceeded") and not (d.get("overtime_started") or d.get("report")):
        payload["opened_by"] = HOURS_EXCEEDED  # release 59: development phase, past the end time, nothing recorded yet
    if usage:
        payload["expected_usage"] = usage
    return Ledger(site).event(OVERTIME_PLAN, actor, None, payload, [d.get("report") or d["plan"]])


def urgent(site: Site, actor: str, items: list[str], memo: str) -> dict:
    """Data at risk: the Director releases WIs from the day plan, the clock and the budget (today and tomorrow)."""
    _require_on(site)
    if not memo.strip() or not items:
        fail("--wi and --memo (the Director's words) are required")
    led = Ledger(site)
    for w in items:
        led.get("work-item", w)
    until = (_now().date() + timedelta(days=1)).isoformat()
    return led.event(URGENT, actor, None, {"items": items, "memo": memo, "by": "Director", "until": until})


def _handoff_path(site: Site) -> Path:
    rel = (site.config.get("session") or {}).get("handoff") or "state/memory/handoff.md"
    p = Path(site.expand(rel))
    return p if p.is_absolute() else site.root / p


def close(site: Site, actor: str, note: str, cancel_now: bool = False) -> dict:
    """Close the day: everything idle, the handoff written after today's approval, then leave. Then the site's
    running session job (the newest `session.started` record still in squeue; never any other job, whoever
    calls) is released through `sessions.release`: by default the close marker, so the job sends `/exit` to
    its claude and completes by itself within the grace (Director, 2026-10-06, after the 10-05 close: an
    outside `day close` could not release and the job was scancelled by hand mid-handoff; an inside close
    scancelled the desk mid-turn); `cancel_now` is the old immediate scancel. Config `session.release_on_close`
    false releases nothing (unless `cancel_now` asks for it explicitly). With workday mode off (release 67,
    after 10-15 at a site without it: the close was refused and the job was scancelled by hand) there is no
    plan or end report to wait for: the idle and handoff checks still apply, the handoff must postdate the
    running session's start (when one is recorded), `day.closed` carries `workday: false`, and the session is
    released the same way; it references today's plan (release 70: `day plan` with the mode off) when one is
    recorded and the day is not closed yet, else nothing."""
    from .ops import status as site_status
    from .upstream import _live_agents

    on = enabled(site)  # release 67: workday mode off has no plan or end report; the close still checks and releases
    d = day(site)
    # release 70: with workday mode off the close references today's plan (`day plan` with the mode off) when one is
    # recorded and not closed yet; none needs to exist
    plan_ref = d["plan"] if on else (d["plan"] if d.get("plan") and d.get("date") == _now().date().isoformat()
                                      and not str(d.get("reason", "")).startswith("day closed") else None)
    if on and (d["phase"] not in ("overtime", "off-duty") or not d.get("report")):
        fail(f"close a day after its end report (phase {d['phase']})")
    from .ops import external_jobs, render_external
    from . import sessions

    handoff = _handoff_path(site)
    checked = device_on(site, "session")
    st = site_status(site) if checked else None
    ext = (st or {}).get("external_jobs") if st else external_jobs(site)
    if checked:
        live = _live_agents(st)
        if live:
            fail(f"agents are still running or starting ({', '.join(live)}); close the day when everything is idle")
        if not handoff.is_file():
            fail(f"write the handoff first: {handoff} does not exist")
        if on:
            started = _parse(d["start"]).timestamp()
            if handoff.stat().st_mtime < started:
                fail(f"the handoff {handoff} predates today's start ({d['start']}); update it before closing")
        else:  # no day start: the running session's own start, when one is recorded
            found, _ = sessions.running_session(site)
            sess = next((e for e in Ledger(site).events() if e["id"] == found["record"]), None) if found else None
            if sess is not None:
                since = (sess.get("payload") or {}).get("submitted_at") or sess["ts"]
                if handoff.stat().st_mtime < _local(since).timestamp():
                    fail(f"the handoff {handoff} predates the running session's start ({since}, job "
                         f"{found['job_id']}); update it before closing")
    led = Ledger(site)
    extra = {}
    if ext and (ext["count"] or ext.get("tool_count")):  # a warning, never a refusal (they may run overnight)
        extra = {"external_jobs": ext}
    rec = led.event("day.closed", actor, None, {"note": note, "idle": checked or None, "checked": checked,
                                               "handoff": str(handoff), **({} if on else {"workday": False}),
                                               **({"plan": plan_ref} if plan_ref and not on else {}),
                                               **extra}, [plan_ref] if plan_ref else None)
    if extra:
        rec = {**rec, "warning": f"{render_external(ext)}. They keep running after the close; cancel any that"
                                 " should not (they are not the harness's to end)."}

    release_on = (site.config.get("session") or {}).get("release_on_close", True)
    if not release_on and not cancel_now:
        from . import office_hours

        oh = office_hours.stop_quietly(site, actor, "day closed")  # release 64: the model server ends with the day
        out = {"released": False, "reason": "release_on_close is off", "job_id": None,
               "caller_job": os.environ.get("SLURM_JOB_ID"), **({"office_hours": oh} if oh else {})}
        led.event(sessions.RELEASED, actor, None, out, [rec["id"]])
        return {**rec, "release": out}
    return {**rec, "release": sessions.release(site, actor, cancel_now=cancel_now, parent=rec["id"])}
