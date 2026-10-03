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


def _now() -> datetime:
    fake = os.environ.get("DUREH_FAKE_NOW")  # tests only
    return datetime.fromisoformat(fake) if fake else datetime.now().astimezone()


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def settings(site: Site) -> dict:
    """config.json `workday` (hours, overtime_hours, daily_share, model_weights, transcripts); defaults otherwise."""
    cfg = site.config.get("workday") or {}
    ot = cfg.get("overtime_hours")
    return {"hours": float(cfg.get("hours") or DEFAULT_HOURS),
            "overtime_hours": float(DEFAULT_OVERTIME_HOURS if ot is None else ot),
            "daily_share": float(cfg.get("daily_share") or 1 / 7),
            "model_weights": cfg.get("model_weights") or {},
            "transcripts": cfg.get("transcripts") or claude_usage.DEFAULT_ROOTS}


def _events(site: Site) -> list[dict]:
    return [e for e in Ledger(site).events() if e["type"].startswith(("workday.", "day."))]


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
    evs = [e for e in (events if events is not None else _events(site)) if e["type"] in DAY_TYPES]
    # the day starts at the last plan; older days are history
    start = max((i for i, e in enumerate(evs) if e["type"] == "day.planned"), default=None)
    if start is None:
        return {"phase": "off-duty", "reason": "no day plan"}
    cur = evs[start:]
    plan = cur[0]
    approved = [e for e in cur if e["type"] == "day.approved"]
    ended = [e for e in cur if e["type"] == "day.ended"]
    closed = [e for e in cur if e["type"] == "day.closed"]
    overtime = [e for e in cur if e["type"] == "day.overtime"]
    decisions = [{"id": e["id"], "title": e["payload"]["title"], "commit": e["payload"].get("commit")}
                 for e in cur if e["type"] == "day.decision"]
    out = {"plan": plan["id"], "date": plan["payload"].get("date"), "goal": plan["payload"].get("goal"),
           "decisions": decisions}
    if closed:
        return {**out, "phase": "off-duty", "reason": f"day closed {closed[-1]['ts']}"}
    if not approved:
        return {**out, "phase": "planned", "reason": "day plan waits for the Director's approval"}
    a = approved[-1]["payload"]
    out.update(start=a["start"], end=a["end"], approval=approved[-1]["id"])
    if ended:
        out["report"] = ended[-1]["id"]
        items = sorted({w for e in overtime for w in e["payload"]["items"]})
        if items:
            return {**out, "phase": "overtime", "overtime": items}
        return {**out, "phase": "off-duty", "reason": "end report written, no overtime approved"}
    if _now() >= _parse(a["end"]):
        return {**out, "phase": "past-end", "reason": f"the day ended at {a['end']}; write the end report"}
    return {**out, "phase": "on-duty"}


def status(site: Site) -> dict:
    evs = _events(site)
    if not enabled(site, evs):
        return {"enabled": False}
    out = {"enabled": True, "hours": settings(site)["hours"], **day(site, evs)}
    out["urgent"] = _urgent(evs)
    try:
        out["usage"] = usage(site, evs)
    except OSError as exc:
        out["usage"] = {"error": str(exc)}
    return out


def render(st: dict) -> str | None:
    if not st.get("enabled"):
        return None
    line = f"Workday: {st['phase']}"
    if st.get("start"):
        line += f" ({st['start'][11:16]}-{st['end'][11:16]})"
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
    return line


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
            return (f"workday mode: overtime covers only {', '.join(d['overtime'])} "
                    "(fixes and normalization tests the Director accepted in the end report).")
        return None  # accepted overtime is not held back by the budget
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


def _director_only() -> None:
    if os.environ.get("DUREH_ACTOR") or os.environ.get("CLAUDECODE"):
        fail("`dureh site workday` is the Director's switch; run it from your own terminal, not inside an agent session")


def switch(site: Site, on: bool, memo: str) -> dict:
    _director_only()
    if not memo.strip():
        fail("--memo is required (the Director's reason)")
    settings(site)  # validates numbers
    # records name a role as actor; the Director's switch is marked by `by` and refused inside agent sessions
    return Ledger(site).event("workday.switched", "desk", None, {"enabled": on, "memo": memo, "by": "Director",
                                                                 "via": "dureh site workday"})


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
        tmp.write_text(json.dumps({**cal, "by": "Director", "site": site.root.name}, indent=2) + "\n")
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


def approve(site: Site, actor: str, memo: str) -> dict:
    _require_on(site)
    if not memo.strip():
        fail("--memo must carry the Director's words")
    d = day(site)
    if d["phase"] != "planned":
        fail(f"no day plan waits for approval (phase {d['phase']})")
    start = _now()
    end = start + timedelta(hours=settings(site)["hours"])
    return Ledger(site).event("day.approved", actor, None,
                              {"start": _ts(start), "end": _ts(end), "memo": memo, "by": "Director"}, [d["plan"]])


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
    if d["phase"] not in ("on-duty", "past-end"):
        fail(f"no open day to end (phase {d['phase']})")
    if d["phase"] == "on-duty" and not early.strip():
        fail(f"the day runs to {d['end']}; stay on duty even when idle (propose next work, watch upstream messages)."
             " End early only on the Director's word: --early \"<Director's words>\"")
    if not done.strip():
        fail("--done is required")
    led = Ledger(site)
    for w in overtime:
        led.get("work-item", w)
    report = {"done": done, "problems": problems, "overtime_proposed": overtime,
              "decisions": d["decisions"], "start": d["start"], "end": d["end"], "ended_at": _ts(_now()),
              "early": early or None, "usage": usage(site), "heads": heads(site)}
    rec = led.event("day.ended", actor, None, report, [d["plan"]])
    return {**rec, "next": "give the Director this report; record accepted overtime with "
                           "`dureh day overtime --items ... --memo \"<Director's words>\"`, else `dureh day close`"}


def overtime(site: Site, actor: str, items: list[str], memo: str) -> dict:
    _require_on(site)
    d = day(site)
    if not d.get("report"):
        fail("overtime is agreed in the end report; write it with `dureh day end` first")
    if not memo.strip() or not items:
        fail("--items and --memo (the Director's words) are required")
    led = Ledger(site)
    for w in items:
        led.get("work-item", w)
    return led.event("day.overtime", actor, None, {"items": items, "memo": memo, "by": "Director"}, [d["report"]])


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


def close(site: Site, actor: str, note: str) -> dict:
    _require_on(site)
    d = day(site)
    if d["phase"] not in ("overtime", "off-duty") or not d.get("report"):
        fail(f"close a day after its end report (phase {d['phase']})")
    return Ledger(site).event("day.closed", actor, None, {"note": note}, [d["plan"]])
