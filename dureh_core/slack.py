"""Desk reports to the company's Slack thread (Director, 2026-10-05: one bot for every company; each company's
thread lives in #project and posts under the company's display name).

The channel, thread, display name and token file come only from site config `slack`; no flag changes them.
The token is read from its file at call time and never printed, logged or recorded. Only `slack.posted`
{ts, chars} is recorded, never the text.
Director, 2026-10-05: the thread is a one-way report channel. Nothing is read back from Slack; the Director's
instructions come only through the company session."""
from __future__ import annotations

import json
import os
import re
import urllib.error
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
NO_CONFIG = ("Slack is not configured for this site: the Director sets `slack` in config.json "
             "({channel, thread_ts, username, icon_emoji?, icon_url?, token_file?}; see docs/slack.md)")


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
    if not isinstance(thread, str) or not _TS.match(thread):
        fail(f"config.json slack.thread_ts must be the thread's parent ts as a string such as \"1791213939.792479\", "
             f"not {thread!r}; the Director sets it")
    if not isinstance(user, str) or not user.strip():
        fail("config.json slack.username (the company's display name) is required; the Director sets it")
    out = {"channel": channel, "thread_ts": thread, "username": user,
           "token_file": site.expand(c.get("token_file") or DEFAULT_TOKEN_FILE)}
    for k in ("icon_emoji", "icon_url"):
        if c.get(k):
            out[k] = str(c[k])
    return out


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
        fail(f"slack {method} failed: {resp.get('error') or 'unknown error'} (not retried)")


def payload(cfg: dict, text: str) -> dict:
    p = {"channel": cfg["channel"], "thread_ts": cfg["thread_ts"], "text": text, "username": cfg["username"],
         "mrkdwn": True, "unfurl_links": False, "unfurl_media": False}
    for k in ("icon_emoji", "icon_url"):
        if k in cfg:
            p[k] = cfg[k]
    return p


def post(site: Site, actor: str, text: str, dry_run: bool = False) -> dict:
    _require_desk(site, actor)
    cfg = config(site)
    text = text.strip()
    if not text:
        fail("nothing to post: the text is empty")
    if len(text) > MAX_CHARS:
        fail(f"text is {len(text)} characters; the limit is {MAX_CHARS}: shorten it or split it into posts")
    if SECRET_MARK in text.lower():
        fail(f"refused: the text contains `{SECRET_MARK}` (a Slack token prefix); never post secrets")
    body = payload(cfg, text)
    if dry_run:
        return {"dry_run": True, "method": "chat.postMessage", "payload": body, "token_file": cfg["token_file"],
                "token_file_present": os.path.isfile(cfg["token_file"])}
    resp = _request("chat.postMessage", _token(cfg), data=body)
    _check(resp, "chat.postMessage")
    ts = str(resp.get("ts") or (resp.get("message") or {}).get("ts") or "")
    Ledger(site).event("slack.posted", actor, None, {"ts": ts, "chars": len(text)})
    return {"posted": True, "ts": ts, "chars": len(text), "channel": cfg["channel"], "thread_ts": cfg["thread_ts"]}
