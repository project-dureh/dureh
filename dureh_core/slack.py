"""Desk reports to the company's Slack thread (Director, 2026-10-05: one bot for every company; each company's
thread lives in #project and posts under the company's display name).

The channel, thread, display name and token file come only from site config `slack`; no flag changes them.
The token is read from its file at call time and never printed, logged or recorded. Only `slack.posted`
{ts, chars} is recorded, never the text.
Director, 2026-10-05: the thread is a one-way report channel. Nothing is read back from Slack; the Director's
instructions come only through the company session.

Director, 2026-10-05 (release 33), one narrow exception: the day plan may be approved by the Director's reaction.
`slack ask --kind day-plan` posts the recorded plan with a "[승인 요청]" line; `slack check` reads ONLY the reactions
on that bot-posted message (reactions.get), and ONLY a reaction by `slack.director_user` counts: ✅ approves the day
exactly as `dureh day approve` does (memo "Slack ✅ by <user> on <ts>", via: slack), ❌ holds it. No message text is
ever read; reactions by anyone else are listed and counted, never acted on.

Director, 2026-10-05 (release 34): one thread per working day instead of one long thread. With `slack.daily_thread`
(default: on when `thread_ts` is absent, off when it is set) the first `slack post` or `slack ask` of the company's
working day posts a top-level message in `slack.channel` and records `slack.thread.opened` {date, ts, channel}; later
posts that day reply in it (found from that event). The day's date is the open workday's plan date, else the
local date."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .records import Ledger
from .site import Site
from .util import fail

API = "https://slack.com/api/"
MAX_CHARS = 4000
DEFAULT_TOKEN_FILE = "~/.config/dureh/slack-bot-token"
SECRET_MARK = "xox"  # every Slack token prefix (xoxb-, xoxp-, xoxa-, ...)
_CHANNEL = re.compile(r"^[CGD][A-Z0-9]{6,}$")
_TS = re.compile(r"^\d{9,}\.\d{6}$")
_USER = re.compile(r"^[UW][A-Z0-9]{6,}$")
NO_CONFIG = ("Slack is not configured for this site: the Director sets `slack` in config.json "
             "({channel, username, thread_ts?, daily_thread?, icon_emoji?, icon_url?, token_file?}; see docs/slack.md)")
OPENED = "slack.thread.opened"
WEEKDAY_KO = "월화수목금토일"
DAY_LINE = "오늘의 업무 보고 스레드입니다."


def daily(raw: dict) -> bool:
    """`slack.daily_thread` as configured; default on without `thread_ts`, off with it (older configs keep working)."""
    v = raw.get("daily_thread")
    return (not raw.get("thread_ts")) if v is None else v is True


def _request(method: str, token: str, *, data: dict, timeout: int = 30) -> dict:
    """One Slack Web API call; tests replace this function. No retries."""
    req = urllib.request.Request(API + method, data=json.dumps(data).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        fail(f"slack {method}: HTTP {exc.code} (not retried)")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        fail(f"slack {method}: network error: {reason} (not retried)")


def _get(method: str, token: str, *, params: dict, timeout: int = 30) -> dict:
    """One Slack Web API GET (reads: reactions.get); tests replace this function. No retries."""
    req = urllib.request.Request(API + method + "?" + urllib.parse.urlencode(params), method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        fail(f"slack {method}: HTTP {exc.code} (not retried)")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        fail(f"slack {method}: network error: {reason} (not retried)")


def _require_desk(site: Site, actor: str) -> None:
    if site.org.role(actor).kind != "desk":
        fail(f"only the desk posts to the company's Slack thread (actor {actor!r} is not the desk); "
             "report to the desk instead")


def config(site: Site) -> dict:
    c = site.config.get("slack")
    if not c:
        fail(NO_CONFIG)
    if not isinstance(c, dict):
        fail("config.json `slack` must be an object; the Director sets it (see docs/slack.md)")
    channel, thread, user = c.get("channel"), c.get("thread_ts"), c.get("username")
    if not isinstance(channel, str) or not _CHANNEL.match(channel):
        fail(f"config.json slack.channel must be a Slack channel ID such as C0123456789, not {channel!r}; the Director sets it")
    if c.get("daily_thread") is not None and not isinstance(c.get("daily_thread"), bool):
        fail(f"config.json slack.daily_thread must be true or false, not {c.get('daily_thread')!r}; the Director sets it")
    is_daily = daily(c)
    if (thread is not None or not is_daily) and (not isinstance(thread, str) or not _TS.match(thread)):
        fail(f"config.json slack.thread_ts must be the thread's parent ts as a string such as \"1791213939.792479\", "
             f"not {thread!r} (or leave it out for one thread per working day); the Director sets it")
    if not isinstance(user, str) or not user.strip():
        fail("config.json slack.username (the company's display name) is required; the Director sets it")
    out = {"channel": channel, "thread_ts": thread, "daily_thread": is_daily, "username": user,
           "token_file": site.expand(c.get("token_file") or DEFAULT_TOKEN_FILE)}
    for k in ("icon_emoji", "icon_url"):
        if c.get(k):
            out[k] = str(c[k])
    if c.get("director_user") is not None:
        out["director_user"] = c["director_user"]
    return out


def director_user(cfg: dict) -> str:
    """The only Slack user whose reaction counts (config `slack.director_user`, set by the Director)."""
    u = cfg.get("director_user")
    if not isinstance(u, str) or not _USER.match(u):
        fail(f"config.json slack.director_user must be the Director's Slack user ID such as U0123456789, not {u!r}; "
             "the Director sets it (see docs/slack.md). Without it no Slack approval is asked or read")
    return u


def _token(cfg: dict) -> str:
    path = cfg["token_file"]
    try:
        token = open(path, encoding="utf-8").read().strip()
    except FileNotFoundError:
        fail(f"Slack bot token file is missing: {path} (the Director places the bot token there, mode 600)")
    except OSError as exc:
        fail(f"cannot read the Slack bot token file {path}: {exc.strerror}")
    if not token:
        fail(f"Slack bot token file is empty: {path}")
    return token


def _check(resp, method: str) -> None:
    if not isinstance(resp, dict):
        fail(f"slack {method}: unexpected response (not retried)")
    if not resp.get("ok"):
        if resp.get("error") == "missing_scope":
            needed = resp.get("needed") or ("reactions:read" if method == "reactions.get" else "?")
            fail(f"slack {method}: the bot needs {needed} (missing_scope); the Director adds the scope to the app "
                 "and reinstalls it (see docs/slack.md)")
        fail(f"slack {method} failed: {resp.get('error') or 'unknown error'} (not retried)")


def payload(cfg: dict, text: str, thread_ts: str | None = None, top: bool = False) -> dict:
    p = {"channel": cfg["channel"], "text": text, "username": cfg["username"],
         "mrkdwn": True, "unfurl_links": False, "unfurl_media": False}
    if not top:
        p["thread_ts"] = thread_ts or cfg["thread_ts"]
    for k in ("icon_emoji", "icon_url"):
        if k in cfg:
            p[k] = cfg[k]
    return p


# ------------------------------------------------------------------ one thread per working day (release 34)
def day_date(site: Site) -> str:
    """The company's working day: the open workday's plan date, else the local date."""
    from . import workday

    d = workday.day(site)
    if d.get("date") and d.get("phase") != "off-duty":
        return d["date"]
    return workday._now().date().isoformat()


def site_label(site: Site) -> str:
    s = site.config.get("site") or {}
    return s.get("short") or s.get("name") or site.root.name


def opening_text(site: Site, cfg: dict, date: str) -> str:
    from datetime import date as _date

    wd = WEEKDAY_KO[_date.fromisoformat(date).weekday()]
    return f"*{date} ({wd}) {site_label(site)} · {cfg['username']}*\n{DAY_LINE}"


def day_thread(site: Site, cfg: dict, date: str) -> str | None:
    """ts of the day's thread in this channel, from the last `slack.thread.opened` record; None if not opened yet."""
    found = [e for e in Ledger(site).events() if e["type"] == OPENED
             and e["payload"].get("date") == date and e["payload"].get("channel") == cfg["channel"]]
    return found[-1]["payload"]["ts"] if found else None


def _open_day(site: Site, actor: str, cfg: dict, date: str) -> str:
    resp = _request("chat.postMessage", _token(cfg), data=payload(cfg, opening_text(site, cfg, date), top=True))
    _check(resp, "chat.postMessage")
    ts = str(resp.get("ts") or (resp.get("message") or {}).get("ts") or "")
    if not _TS.match(ts):
        fail("slack chat.postMessage: no ts for the day thread (not retried)")
    Ledger(site).event(OPENED, actor, None, {"date": date, "ts": ts, "channel": cfg["channel"]})
    return ts


def _target(site: Site, actor: str, cfg: dict, dry_run: bool = False) -> tuple[str | None, dict]:
    """(thread ts or None when a dry run would open it, info for the output)."""
    if not cfg["daily_thread"]:
        return cfg["thread_ts"], {}
    date = day_date(site)
    ts = day_thread(site, cfg, date)
    if ts:
        return ts, {"day_thread": {"date": date, "ts": ts, "opened": False}}
    if dry_run:
        return None, {"day_thread": {"date": date, "opens": True,
                                     "opening_payload": payload(cfg, opening_text(site, cfg, date), top=True)}}
    ts = _open_day(site, actor, cfg, date)
    return ts, {"day_thread": {"date": date, "ts": ts, "opened": True}}


def _clean(text: str) -> str:
    text = text.strip()
    if not text:
        fail("nothing to post: the text is empty")
    if len(text) > MAX_CHARS:
        fail(f"text is {len(text)} characters; the limit is {MAX_CHARS}: shorten it or split it into posts")
    if SECRET_MARK in text.lower():
        fail(f"refused: the text contains `{SECRET_MARK}` (a Slack token prefix); never post secrets")
    return text


def _send(cfg: dict, text: str, thread_ts: str | None = None) -> str:
    resp = _request("chat.postMessage", _token(cfg), data=payload(cfg, text, thread_ts))
    _check(resp, "chat.postMessage")
    return str(resp.get("ts") or (resp.get("message") or {}).get("ts") or "")


def post(site: Site, actor: str, text: str, dry_run: bool = False) -> dict:
    _require_desk(site, actor)
    cfg = config(site)
    text = _clean(text)
    if dry_run:
        thread, info = _target(site, actor, cfg, dry_run=True)
        body = payload(cfg, text, thread or "(the new day thread)")
        return {"dry_run": True, "method": "chat.postMessage", "payload": body, **info,
                "token_file": cfg["token_file"], "token_file_present": os.path.isfile(cfg["token_file"])}
    _token(cfg)  # a missing token fails before any day thread is opened
    thread, info = _target(site, actor, cfg)
    ts = _send(cfg, text, thread)
    Ledger(site).event("slack.posted", actor, None, {"ts": ts, "chars": len(text)})
    return {"posted": True, "ts": ts, "chars": len(text), "channel": cfg["channel"], "thread_ts": thread, **info}


# ------------------------------------------------------------------ approval by reaction (release 33)
KINDS = ("day-plan",)
ASK_LINE = "[승인 요청] 오늘 계획을 승인하시겠습니까? ✅ 승인 / ❌ 보류"
APPROVE = ("white_check_mark", "heavy_check_mark", "ballot_box_with_check")
DECLINE = ("x", "negative_squared_cross_mark", "no_entry")
REPLY = {"approved": "✅ 승인 확인, 업무 시작", "declined": "❌ 보류: 디렉터님을 기다립니다", "expired": "만료"}
REQUESTED, DECIDED = "slack.approval.requested", "slack.approval.decided"


def _approval_events(site: Site) -> tuple[list[dict], set[str]]:
    evs = Ledger(site).events()
    asked = [e for e in evs if e["type"] == REQUESTED]
    decided = {e["payload"].get("ts") for e in evs if e["type"] == DECIDED}
    return asked, decided


def open_requests(site: Site) -> list[dict]:
    asked, decided = _approval_events(site)
    return [e for e in asked if e["payload"].get("ts") not in decided]


def ask(site: Site, actor: str, kind: str, text: str | None = None) -> dict:
    """Post the recorded day plan with the approval line; record {kind, ts, channel} (never the text)."""
    from . import workday

    _require_desk(site, actor)
    if kind not in KINDS:
        fail(f"unknown approval kind {kind!r} (known: {', '.join(KINDS)})")
    cfg = config(site)
    director_user(cfg)
    d = workday.day(site)
    if d.get("phase") != "planned" or d.get("date") != workday._now().date().isoformat():
        fail(f"no day plan for today waits for approval (phase {d.get('phase')}); record it first with "
             "`dureh day plan --goal ... --file <plan>`, then run `dureh slack ask --kind day-plan`")
    for e in open_requests(site):
        if d["plan"] in (e.get("refs") or []):
            fail(f"an approval request for this plan is already open (ts {e['payload']['ts']}); "
                 "run `dureh slack check` or `dureh slack watch`")
    if text is None or not text.strip():
        plan = next(e for e in Ledger(site).events() if e["id"] == d["plan"])["payload"]
        text = f"*오늘 계획* ({plan.get('date')})\n목표: {plan.get('goal')}\n\n{plan.get('plan', '').strip()}"
    body = text.strip() + "\n\n" + ASK_LINE
    body = _clean(body)
    # the request expires at the end of the company's day (the session clock; without a session, the config window
    # or the hours from now)
    _start, end = workday.clock(site, workday.todays_session(workday._events(site)), workday._now())
    _token(cfg)
    thread, info = _target(site, actor, cfg)
    ts = _send(cfg, body, thread)
    rec = Ledger(site).event(REQUESTED, actor, None, {"kind": kind, "ts": ts, "channel": cfg["channel"],
                                                      "thread_ts": thread, "expires": end}, [d["plan"]])
    return {"asked": True, "kind": kind, "ts": ts, "channel": cfg["channel"], "thread_ts": thread, "expires": end,
            "record": rec["id"], **info, "next": "run `dureh slack watch` as a Monitor; start work only after approval"}


def _base(name: str) -> str:
    return name.split("::", 1)[0]


def _reactions(cfg: dict, ts: str) -> list[dict]:
    resp = _get("reactions.get", _token(cfg), params={"channel": cfg["channel"], "timestamp": ts, "full": "true"})
    _check(resp, "reactions.get")
    return list((resp.get("message") or {}).get("reactions") or [])


def _decide(director: str, reactions: list[dict]) -> tuple[str | None, list[str]]:
    """(state or None while waiting, ignored 'user:emoji' entries). Only the Director's ✅/❌ count."""
    yes = no = False
    ignored = []
    for r in reactions:
        name = _base(str(r.get("name") or ""))
        if name not in APPROVE + DECLINE:
            continue
        for u in r.get("users") or []:
            if u == director:
                yes |= name in APPROVE
                no |= name in DECLINE
            else:
                ignored.append(f"{u}:{name}")
    return ("declined" if no else "approved" if yes else None), ignored


def check(site: Site, actor: str) -> dict:
    """Decide every open request from the Director's reaction; idempotent (a decided request is never read again)."""
    from . import workday

    _require_desk(site, actor)
    cfg = config(site)
    director = director_user(cfg)
    led = Ledger(site)
    decisions, waiting = [], []
    for req in open_requests(site):
        p = req["payload"]
        ts, kind = p["ts"], p.get("kind")
        plan_id = (req.get("refs") or [None])[0]
        d = workday.day(site)
        if p.get("expires") and workday._now() >= workday._parse(p["expires"]):
            state, ignored = "expired", []
        elif d.get("phase") != "planned" or d.get("plan") != plan_id:
            state, ignored = "closed", []  # the plan was approved in the session, replaced or closed meanwhile
        else:
            state, ignored = _decide(director, _reactions(cfg, ts))
        if state is None:
            waiting.append({"ts": ts, "kind": kind, "ignored": ignored})
            continue
        out = {"ts": ts, "kind": kind, "state": state, "by": director if state in ("approved", "declined") else None,
               "ignored": ignored}
        if state == "approved" and kind == "day-plan":
            rec = workday.approve(site, actor, f"Slack ✅ by {director} on {ts}", via="slack",
                                  extra={"slack_ts": ts, "slack_user": director})
            out["approval"] = rec["id"]
        led.event(DECIDED, actor, None, {"ts": ts, "state": state, "by": out["by"], "ignored": len(ignored),
                                         **({"approval": out["approval"]} if "approval" in out else {})}, [req["id"]])
        reply_thread = p.get("thread_ts") or cfg["thread_ts"] or day_thread(site, cfg, day_date(site))
        if state in REPLY and reply_thread:
            try:
                out["reply"] = _send(cfg, REPLY[state], reply_thread)
            except Exception as exc:  # the decision stands; only the courtesy reply failed
                out["reply_error"] = str(exc)
        decisions.append(out)
    return {"decisions": decisions, "waiting": waiting}


def decision_line(dec: dict) -> str:
    line = f"slack approval {dec['ts']} ({dec['kind']}): {dec['state']}"
    if dec.get("by"):
        line += f" by {dec['by']}"
    if dec.get("approval"):
        line += f"; day approved ({dec['approval']}, via slack)"
    if dec.get("ignored"):
        line += f"; ignored reactions (not the Director): {', '.join(dec['ignored'])}"
    if dec.get("reply_error"):
        line += f"; reply not posted: {dec['reply_error']}"
    return line


def watch(site: Site, actor: str, interval: float = 60, emit=print, sleep=time.sleep) -> int:
    """Check until nothing is open; emit only decision lines. Returns the number of decisions."""
    _require_desk(site, actor)
    n = 0
    while True:
        res = check(site, actor)
        for dec in res["decisions"]:
            emit(decision_line(dec))
            n += 1
        if not res["waiting"]:
            return n
        sleep(max(float(interval), 5.0))
