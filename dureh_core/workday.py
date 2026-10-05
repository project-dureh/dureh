"""Workday mode: an 8-hour day bounded by a morning plan and an evening report, with a usage budget.

Switched on and off only by the Director (`dureh site workday on|off`, refused inside agent
sessions). While on:

- the desk reports a day plan (`day plan`); the Director's approval (`day approve --memo`) starts
  the clock; until the end time the desk works toward the plan's goal without asking again;
- important decisions follow the desk's own proposal and are recorded with a state commit
  (`day decide`), so a rollback can return to that point;
- after the hours run out no new agent starts; the desk writes the end report (`day end`) with
  proposed overtime; overtime covers only the work items the Director accepts (`day overtime`),
  limited to fixing errors from the day's work and normalization tests;
- each work unit (an agent spawn) checks the account-wide Claude usage against the daily share of
  the weekly allowance (default 1/7, all models including Fable), shared by every company and
  the upstream team.

State is the event records (append-only); there is no other switch. The weekly allowance comes
from the Director's calibration, kept in the shared exchange (`<exchange>/usage-budget.json`) when
one is configured, so every site reads the same number, else in this site's records.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from . import claude_usage
from .records import Ledger
from .site import Site
from .util import DurehError, fail

DEFAULT_HOURS = 8.0
DEFAULT_OVERTIME_HOURS = 16.0  # the session job's wall time covers the day plus this, capped at 24 h
DAY_TYPES = ("day.planned", "day.approved", "day.decision", "day.ended", "day.overtime", "day.closed")
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


def _now() -> datetime:
    fake = os.environ.get("DUREH_FAKE_NOW")  # tests only
    return datetime.fromisoformat(fake) if fake else datetime.now().astimezone()


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def settings(site: Site) -> dict:
    """config.json `workday` (hours, overtime_hours, daily_share, model_weights, transcripts, weekdays, supervised,
    start/end fallback window); defaults otherwise."""
    cfg = site.config.get("workday") or {}
    ot = cfg.get("overtime_hours")
    return {"hours": float(cfg.get("hours") or DEFAULT_HOURS),
            "overtime_hours": float(DEFAULT_OVERTIME_HOURS if ot is None else ot),
            "daily_share": float(cfg.get("daily_share") or 1 / 7),
            "model_weights": cfg.get("model_weights") or {},
            "transcripts": cfg.get("transcripts") or claude_usage.DEFAULT_ROOTS,
            "weekdays": _weekdays(cfg.get("weekdays")),
            "phase": _phase(cfg.get("phase")),
            "start": cfg.get("start") or None, "end": cfg.get("end") or None}


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
    out = {"weekly_units": None, "calibrated": None, "source": None,
           "daily_share": st["daily_share"], "model_weights": st["model_weights"]}
    f = _exchange_budget_file(site)
    if f and f.is_file():
        try:
            data = json.loads(f.read_text())
            out.update(weekly_units=data.get("weekly_units"), calibrated=data.get("calibrated"), source=str(f))
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
    s = claude_usage.summary(b, roots, _now().date())
    s.update(budget_source=b["source"], calibrated=b["calibrated"])
    s["this_site_units"] = claude_usage.by_place({"site": str(site.root)}, b, roots, _now().date())["site"]
    return s


def day(site: Site, events: list[dict] | None = None) -> dict:
    """The current day: phase off-duty | planned | on-duty | past-end | overtime, plus its records."""
    all_evs = events if events is not None else _events(site)
    session = todays_session(all_evs)
    evs = [e for e in all_evs if e["type"] in DAY_TYPES]
    # the day starts at the last plan; older days are history
    start = max((i for i, e in enumerate(evs) if e["type"] == "day.planned"), default=None)
    if start is None:
        return {"phase": "off-duty", "reason": "no day plan", **_session_clock(site, session)}
    cur = evs[start:]
    plan = cur[0]
    approved = [e for e in cur if e["type"] == "day.approved"]
    ended = [e for e in cur if e["type"] == "day.ended"]
    closed = [e for e in cur if e["type"] == "day.closed"]
    overtime = [e for e in cur if e["type"] == "day.overtime"]
    decisions = [{"id": e["id"], "title": e["payload"]["title"], "commit": e["payload"].get("commit")}
                 for e in cur if e["type"] == "day.decision"]
    out = {"plan": plan["id"], "date": plan["payload"].get("date"), "goal": plan["payload"].get("goal"),
           "decisions": decisions, **_session_clock(site, session)}
    if closed:
        return {**out, "phase": "off-duty", "reason": f"day closed {closed[-1]['ts']}"}
    mode = hours_mode(site)
    if not approved:
        if mode == "block" and out.get("end") and _now() >= _parse(out["end"]) and not ended:
            return {**out, "phase": "past-end", "reason": f"the day ended at {out['end']} (never approved); "
                                                         "write the end report"}
        return {**out, "phase": "planned", "reason": "day plan waits for the Director's approval"}
    a = approved[-1]["payload"]
    out.update(start=a["start"], end=a["end"], approval=approved[-1]["id"])
    out["working_day"] = working_day(site, out["start"])
    items = sorted({w for e in overtime for w in e["payload"]["items"]})
    if not out["working_day"]:
        name = WEEKDAY_NAMES[_parse(out["start"]).weekday()].capitalize()
        out["reason"] = f"{name} is not a working day (workday.weekdays): overtime only"
    if ended:
        out["report"] = ended[-1]["id"]
        if items:
            return {**out, "phase": "overtime", "overtime": items}
        return {**out, "phase": "off-duty", "reason": "end report written, no overtime approved"}
    if mode and _now() >= _parse(a["end"]):
        if mode == "block":
            return {**out, "phase": "past-end", "reason": f"the day ended at {a['end']}; write the end report"}
        out["hours_exceeded"] = True  # development phase: a warning, not a block
        out["warning"] = f"the 8-hour day ended at {a['end']} (workday.phase development: not blocking); write the end report"
    if not out["working_day"]:
        return {**out, "phase": "overtime", "overtime": items}
    return {**out, "phase": "on-duty"}


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
        return {"enabled": False, **switches}
    out = {"enabled": True, "hours": settings(site)["hours"], "workday_phase": settings(site)["phase"], **switches,
           **day(site, evs)}
    out["urgent"] = _urgent(evs)
    if out.get("hours_exceeded") and not site.observe:
        note_hours_exceeded(site, out, evs)
    try:
        out["usage"] = usage(site, evs)
    except OSError as exc:
        out["usage"] = {"error": str(exc)}
    return out


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
        return head
    line = f"Workday: {st['phase']}"
    if st.get("workday_phase") and st["workday_phase"] != "production":
        line += f" [{st['workday_phase']} phase]"
    if st.get("start"):
        line += f" ({st['start'][11:16]}-{st['end'][11:16]}" + (f", session job {st['job_id']})" if st.get("job_id")
                                                                   else ")")
    if st.get("overtime"):
        line += "; overtime items " + ", ".join(st["overtime"])
    if st.get("reason"):
        line += f"; {st['reason']}"
    u = st.get("usage") or {}
    if u.get("daily_allowance"):
        line += (f"; usage today {u['today_units']}/{u['daily_allowance']} units"
                 f" ({round(100 * (u['today_fraction'] or 0))}% of the daily share, this site {u.get('this_site_units', 0)}),"
                 f" week {u['week_percent']}%")
    elif "today_units" in u:
        line += f"; usage today {u['today_units']} units, this site {u.get('this_site_units', 0)} (budget not calibrated)"
    if st.get("urgent"):
        line += "; urgent " + ", ".join(st["urgent"])
    if st.get("devices_off"):
        line += "; devices off: " + ", ".join(st["devices_off"])
    if st.get("warning"):
        line += f"\n⚠ {st['warning']}"
    return f"{head}\n{line}" if head else line


def _wi_family(led: Ledger, wi: str) -> set[str]:
    fam = {wi}
    try:
        parent = led.get("work-item", wi)["payload"].get("parent")
    except DurehError:
        parent = None
    if parent:
        fam.add(parent)
    return fam


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
                "and record the Director's approval with `dureh day approve --memo`.")
    if phase == "past-end":
        return (f"workday mode: the 8-hour day ended at {d['end']}. Start nothing new; write the end report with "
                "`dureh day end` and propose overtime items for the Director.")
    if phase == "overtime":
        if not wi or not (_wi_family(Ledger(site), wi) & set(d["overtime"])):
            if not d.get("working_day", True) and not d.get("report"):
                return (f"workday mode: {d['reason']}. Worker agents spawn only for items in a Director overtime "
                        "record: `dureh day overtime --items <WI>... --memo \"<Director's words>\"`.")
            return (f"workday mode: overtime covers only {', '.join(d['overtime']) or 'nothing yet'} "
                    "(fixes and normalization tests the Director accepted in the end report).")
        return None  # accepted overtime is not held back by the budget
    if d.get("hours_exceeded"):
        note_hours_exceeded(site, d, evs)
    if not device_on(site, "usage"):
        return None  # usage is counted and shown, not gated
    u = usage(site, evs)
    if u.get("daily_allowance") and u["today_units"] >= u["daily_allowance"]:
        return (f"workday mode: today's Claude usage {u['today_units']} units reached the daily share "
                f"{u['daily_allowance']} (1/{round(1 / u['daily_share'])} of the weekly allowance, shared by all "
                "companies and upstream). Finish running work and write the end report.")
    return None


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
    try:
        cal = claude_usage.calibrate(weekly_percent, st["model_weights"], st["transcripts"], _now().date())
    except ValueError as exc:
        fail(str(exc))
    cal["calibrated"] = _ts(_now())
    cal["model_weights"] = st["model_weights"]
    rec = Ledger(site).event("workday.calibrated", "desk", None, {**cal, "by": "Director", "via": "dureh site workday"})
    f = _exchange_budget_file(site)
    if f:
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({**cal, "by": "Director", "site": site.mailbox_name}, indent=2) + "\n")
        os.replace(tmp, f)
        cal["shared"] = str(f)
    return {**cal, "record": rec["id"]}


def plan(site: Site, actor: str, goal: str, text: str) -> dict:
    _require_on(site)
    if not goal.strip() or not text.strip():
        fail("a day plan needs --goal and the plan text")
    d = day(site)
    if d["phase"] in ("on-duty", "past-end"):
        fail(f"the current day ({d['plan']}) is still open; write its end report first")
    u = usage(site)
    return Ledger(site).event("day.planned", actor, None, {"date": _now().date().isoformat(), "goal": goal,
                                                           "plan": text, "usage_at_plan": u, "heads": heads(site)})


def approve(site: Site, actor: str, memo: str, via: str | None = None, extra: dict | None = None) -> dict:
    """Record the Director's approval of the day plan. The desk records the Director's words from the session;
    `via="slack"` is used only by `dureh slack check` after the Director's own ✅ reaction (release 33)."""
    _require_on(site)
    if not memo.strip():
        fail("--memo must carry the Director's words")
    d = day(site)
    if d["phase"] != "planned":
        fail(f"no day plan waits for approval (phase {d['phase']})")
    # the clock runs from the session dureh started today; approval covers the plan's content
    start, end = clock(site, todays_session(_events(site)), _now())
    if hours_mode(site) == "block" and _now() >= _parse(end):
        fail(f"the day started at {start} and ended at {end}; a plan approved now would be past its end")
    payload = {"start": start, "end": end, "memo": memo, "by": "Director", "session": d.get("session")}
    if via:
        payload.update(extra or {}, via=via)
    return Ledger(site).event("day.approved", actor, None, payload, [d["plan"]])


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


def decide(site: Site, actor: str, title: str, choice: str, why: str, revs: list[str]) -> dict:
    """Record a decision point (following the desk's own proposal) and commit state as a rollback point."""
    from .state import save_state

    _require_on(site)
    d = day(site)
    if d["phase"] not in ("on-duty", "overtime"):
        fail(f"decisions are recorded during the working day (phase {d['phase']})")
    if not (title.strip() and choice.strip() and why.strip()):
        fail("--title, --choice and --why are required")
    rec = Ledger(site).event("day.decision", actor, None, {"title": title, "choice": choice, "why": why,
                                                           "revs": revs, "state_before": _state_head(site)},
                             [d["plan"]])
    saved = save_state(site, f"workday decision {rec['id']}: {title}")
    return {"record": rec["id"], "state_commit": _state_head(site) if saved.get("saved") else None,
            "saved": saved, "rollback": "the Director decides rollbacks in the end report; after one, fix forward "
                                        "and reflect every change made since this point"}


def end(site: Site, actor: str, done: str, problems: str, overtime: list[str], early: str = "") -> dict:
    _require_on(site)
    d = day(site)
    if d.get("report") or d["phase"] not in ("on-duty", "past-end", "overtime"):
        fail(f"no open day to end (phase {d['phase']})")
    if d["phase"] in ("on-duty", "overtime") and not d.get("hours_exceeded") and hours_mode(site) and not early.strip():
        fail(f"the day runs to {d['end']}; stay on duty even when idle (propose next work, watch upstream messages)."
             " End early only on the Director's word: --early \"<Director's words>\"")
    if not done.strip():
        fail("--done is required")
    led = Ledger(site)
    for w in overtime:
        led.get("work-item", w)
    report = {"done": done, "problems": problems, "overtime_proposed": overtime,
              "decisions": d["decisions"], "start": d.get("start"), "end": d.get("end"), "ended_at": _ts(_now()),
              "session": d.get("session"), "working_day": d.get("working_day", True),
              "early": early or None, "usage": usage(site), "heads": heads(site)}
    rec = led.event("day.ended", actor, None, report, [d["plan"]])
    return {**rec, "next": "give the Director this report; record accepted overtime with "
                           "`dureh day overtime --items ... --memo \"<Director's words>\"`, else `dureh day close`"}


def overtime(site: Site, actor: str, items: list[str], memo: str) -> dict:
    _require_on(site)
    d = day(site)
    if not d.get("report") and d.get("working_day", True):
        fail("overtime is agreed in the end report; write it with `dureh day end` first")
    if not d.get("report") and d["phase"] != "overtime":
        fail(f"no open day for overtime (phase {d['phase']})")
    if not memo.strip() or not items:
        fail("--items and --memo (the Director's words) are required")
    led = Ledger(site)
    for w in items:
        led.get("work-item", w)
    # on a day outside workday.weekdays the whole day is overtime: the record may precede the end report
    return led.event("day.overtime", actor, None, {"items": items, "memo": memo, "by": "Director"},
                     [d.get("report") or d["plan"]])


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


def _session_started_here(led: Ledger, job: str | None) -> bool:
    """True when this site's ledger says dureh started session job `job` (`session.started` with that job_id).
    Director, 2026-10-03: dureh releases only company session jobs it started; a session (or a test) that merely
    runs inside some Slurm job, the DUREH team's own above all, must never cancel it."""
    if not job:
        return False
    for e in led.events():
        if e.get("type") != "session.started":
            continue
        data = e.get("payload") or e.get("data") or {}
        if str(data.get("job_id")) == str(job):
            return True
    return False


def close(site: Site, actor: str, note: str) -> dict:
    """Close the day: everything idle, the handoff written after today's approval, then leave.
    Inside a session job (SLURM_JOB_ID) the runtime releases the node (Director, 2026-10-03)."""
    from .ops import status as site_status
    from .upstream import _live_agents

    _require_on(site)
    d = day(site)
    if d["phase"] not in ("overtime", "off-duty") or not d.get("report"):
        fail(f"close a day after its end report (phase {d['phase']})")
    handoff = _handoff_path(site)
    checked = device_on(site, "session")
    if checked:
        live = _live_agents(site_status(site))
        if live:
            fail(f"agents are still running or starting ({', '.join(live)}); close the day when everything is idle")
        if not handoff.is_file():
            fail(f"write the handoff first: {handoff} does not exist")
        started = _parse(d["start"]).timestamp()
        if handoff.stat().st_mtime < started:
            fail(f"the handoff {handoff} predates today's start ({d['start']}); update it before closing")
    led = Ledger(site)
    rec = led.event("day.closed", actor, None, {"note": note, "idle": checked or None, "checked": checked,
                                               "handoff": str(handoff)}, [d["plan"]])
    job = os.environ.get("SLURM_JOB_ID")
    release_on = (site.config.get("session") or {}).get("release_on_close", True)
    owned = _session_started_here(led, job)
    if not job or not release_on or not owned:
        reason = ("not running in a session job" if not job else
                  "release_on_close is off" if not release_on else
                  f"job {job} has no session.started record; dureh cancels only jobs it started")
        out = {"released": False, "reason": reason, "job_id": job}
        led.event("session.released", actor, None, out, [rec["id"]])
        return {**rec, "release": out}
    from . import sessions

    out = {"released": True, **sessions.get(site).release(site, job)}
    led.event("session.released", actor, None, out, [rec["id"]])
    return {**rec, "release": out}
