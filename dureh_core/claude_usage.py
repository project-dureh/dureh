"""Account-wide Claude usage from Claude Code transcripts, for the workday budget.

Every DUREH company and the upstream team run as the same user, so they draw on one weekly
allowance. The allowance itself is not readable from disk; what is readable is each session's
transcript (`~/.claude/projects/**/*.jsonl`), whose assistant lines carry the model and token
usage. This module turns those into weighted units per local day. The Director calibrates units
against the weekly percentage Claude Code shows (`/usage`), which gives the weekly allowance in
units; the daily share is a fraction of it (default 1/7).

Stdlib only and free of Site, so the upstream workspace can use it too. Parsing is incremental:
a per-user cache (`~/.cache/dureh/claude-usage.json`) keeps each file's offset and per-day totals.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

# Relative cost of token kinds (cache reads are cheap, output is dear). Calibration absorbs the scale.
TOKEN_WEIGHTS = {"input_tokens": 1.0, "cache_creation_input_tokens": 1.25,
                 "cache_read_input_tokens": 0.1, "output_tokens": 5.0}
DEFAULT_ROOTS = ["~/.claude/projects"]
CACHE_VERSION = 1
WINDOW_DAYS = 8  # today plus the seven days before it


def _cache_path() -> Path:
    base = os.environ.get("DUREH_USAGE_CACHE")
    if base:
        return Path(base)
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "dureh" / "claude-usage.json"


def _local_day(ts: str) -> str | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except (ValueError, AttributeError):
        return None


def _scan_file(path: Path, entry: dict) -> dict:
    """Read new complete lines from entry['offset']; return the updated entry."""
    size = path.stat().st_size
    if size < entry.get("offset", 0):  # truncated or replaced: start over
        entry = {}
    offset = entry.get("offset", 0)
    days = entry.setdefault("days", {})
    last = entry.get("last_id")
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read(size - offset)
    end = chunk.rfind(b"\n")
    if end < 0:
        return {**entry, "offset": offset, "size": size}
    for raw in chunk[:end].split(b"\n"):
        if b'"usage"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        msg = d.get("message") if isinstance(d, dict) else None
        if d.get("type") != "assistant" or not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
            continue
        mid = d.get("requestId") or msg.get("id")
        if mid and mid == last:  # one response is written as several lines carrying the same usage
            continue
        last = mid
        day = _local_day(d.get("timestamp") or "")
        if not day:
            continue
        model = msg.get("model") or "unknown"
        if model.startswith("<"):  # synthetic messages
            continue
        tot = days.setdefault(day, {}).setdefault(model, {k: 0 for k in TOKEN_WEIGHTS})
        for k in TOKEN_WEIGHTS:
            v = msg["usage"].get(k)
            if isinstance(v, int):
                tot[k] += v
    entry.update(offset=offset + end + 1, size=size, last_id=last)
    return entry


def _model_weight(model: str, weights: dict) -> float:
    for key, w in (weights or {}).items():
        if key in model:
            return float(w)
    return 1.0


def scan(roots: list[str] | None = None, today: date | None = None,
         by_project: bool = False) -> dict:
    """{day: {model: {token kind: count}}} for the last WINDOW_DAYS local days, across all transcripts.

    by_project: {project dir: {day: {model: ...}}}, the project dir being the transcript folder that
    Claude Code names after the session's working directory (see project_key)."""
    today = today or date.today()
    first = (today - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    cutoff = time.time() - (WINDOW_DAYS + 1) * 86400
    cache_file = _cache_path()
    try:
        cache = json.loads(cache_file.read_text())
        if cache.get("version") != CACHE_VERSION:
            cache = {}
    except (OSError, ValueError):
        cache = {}
    files = cache.setdefault("files", {})
    seen = set()
    for root in roots or DEFAULT_ROOTS:
        base = Path(os.path.expanduser(os.path.expandvars(root)))
        if not base.is_dir():
            continue
        for path in base.rglob("*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            key = str(path)
            seen.add(key)
            project = path.relative_to(base).parts[0]
            entry = files.get(key) or {}
            if entry.get("size") != st.st_size:
                try:
                    entry = _scan_file(path, entry)
                except OSError:
                    continue
            entry["days"] = {d: v for d, v in entry.get("days", {}).items() if d >= first}
            entry["project"] = project
            files[key] = entry
    cache["files"] = {k: v for k, v in files.items() if k in seen}
    cache["version"] = CACHE_VERSION
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cache))
        os.replace(tmp, cache_file)
    except OSError:
        pass
    out: dict = {}
    for entry in cache["files"].values():
        target = out.setdefault(entry.get("project", "?"), {}) if by_project else out
        for day, models in entry.get("days", {}).items():
            for model, tot in models.items():
                acc = target.setdefault(day, {}).setdefault(model, {k: 0 for k in TOKEN_WEIGHTS})
                for k in TOKEN_WEIGHTS:
                    acc[k] += tot.get(k, 0)
    return out


def project_key(path: str | Path) -> str:
    """The transcript folder name Claude Code uses for a working directory (non-alphanumerics -> '-')."""
    import re

    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def by_place(places: dict[str, str], budget: dict | None = None, roots: list[str] | None = None,
             today: date | None = None) -> dict[str, float]:
    """Today's units per named place ({label: directory}); sessions under a directory count for it
    (longest match wins), the rest as `other`."""
    today = (today or date.today()).isoformat()
    mw = (budget or {}).get("model_weights") or {}
    keys = sorted(((project_key(Path(d).expanduser()), label) for label, d in places.items()),
                  key=lambda kv: -len(kv[0]))
    out = {label: 0.0 for label in places}
    out["other"] = 0.0
    for project, days in scan(roots, None if today is None else date.fromisoformat(today), by_project=True).items():
        u = sum(units(t, m, mw) for m, t in days.get(today, {}).items())
        label = next((lab for k, lab in keys if project == k or project.startswith(k + "-")), "other")
        out[label] += u
    return {k: round(v, 3) for k, v in out.items()}


def units(tokens: dict, model: str, model_weights: dict | None = None) -> float:
    """Weighted usage units (millions of input-token equivalents)."""
    return _model_weight(model, model_weights or {}) * sum(TOKEN_WEIGHTS[k] * tokens.get(k, 0) for k in TOKEN_WEIGHTS) / 1e6


def week_window(budget: dict | None, at: datetime) -> dict:
    """The week window as of `at`: {"kind": "reset"|"rolling", "start": ISO, "end": ISO} (release 72,
    project-dureh/dureh#24; the rule of upstream tools/workday.py week_window()).

    reset: the budget has `weekly_reset`, one reset instant; the provider resets every 7 days from it, so the
    window starts at the latest reset <= at (a reset given in the future counts back by whole weeks).
    rolling: no `weekly_reset`, the 7 local days ending at's day."""
    reset = (budget or {}).get("weekly_reset")
    if reset:
        r = datetime.fromisoformat(reset)
        r = r if r.tzinfo else r.astimezone()
        k = (at - r) // timedelta(days=7)
        start = r + k * timedelta(days=7)
        return {"kind": "reset", "start": start.isoformat(timespec="seconds"), "end": at.isoformat(timespec="seconds")}
    start = datetime.combine(at.date() - timedelta(days=6), dtime.min, tzinfo=at.tzinfo)
    return {"kind": "rolling", "start": start.isoformat(timespec="seconds"), "end": at.isoformat(timespec="seconds")}


def _exact_day_tokens(day: str, lo: datetime | None, hi: datetime | None) -> dict:
    """{model: {token kind: count}} for one local day restricted to [lo, hi], read from the transcripts
    themselves (the scan cache keeps days, not instants). Only files the cache lists for that day are read."""
    try:
        cache = json.loads(_cache_path().read_text())
        files = [k for k, e in cache.get("files", {}).items() if day in e.get("days", {})]
    except (OSError, ValueError):
        files = []
    out: dict = {}
    for name in files:
        last = None
        try:
            lines = Path(name).read_bytes().split(b"\n")
        except OSError:
            continue
        for raw in lines:
            if b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            msg = d.get("message") if isinstance(d, dict) else None
            if d.get("type") != "assistant" or not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            mid = d.get("requestId") or msg.get("id")
            if mid and mid == last:  # one response is written as several lines carrying the same usage
                continue
            last = mid
            try:
                when = datetime.fromisoformat((d.get("timestamp") or "").replace("Z", "+00:00")).astimezone()
            except ValueError:
                continue
            model = msg.get("model") or "unknown"
            if model.startswith("<") or when.date().isoformat() != day:
                continue
            if (lo and when < lo) or (hi and when > hi):
                continue
            tot = out.setdefault(model, {k: 0 for k in TOKEN_WEIGHTS})
            for k in TOKEN_WEIGHTS:
                v = msg["usage"].get(k)
                if isinstance(v, int):
                    tot[k] += v
    return out


def window_tokens(win: dict, roots: list[str] | None = None) -> dict:
    """{model: {token kind: count}} summed over the window; the boundary days are cut at the window's start
    instant (when not midnight) and at its end instant (when in the past) by re-reading only the transcripts
    the scan cache lists for those days."""
    # local instants: the scan's days are local days, a reset instant may carry another offset
    lo, hi = datetime.fromisoformat(win["start"]).astimezone(), datetime.fromisoformat(win["end"]).astimezone()
    data = scan(roots, hi.date())
    now = datetime.now().astimezone()
    out: dict = {}
    d = lo.date()
    while d <= hi.date():
        cut_lo = lo if (d == lo.date() and lo.time() != dtime.min) else None
        cut_hi = hi if (d == hi.date() and hi < now) else None
        models = _exact_day_tokens(d.isoformat(), cut_lo, cut_hi) if (cut_lo or cut_hi) else data.get(d.isoformat(), {})
        for m, tok in models.items():
            acc = out.setdefault(m, {k: 0 for k in TOKEN_WEIGHTS})
            for k in TOKEN_WEIGHTS:
                acc[k] += tok.get(k, 0)
        d += timedelta(days=1)
    return out


def summary(budget: dict | None = None, roots: list[str] | None = None, today: date | None = None,
            at: datetime | None = None) -> dict:
    """Today's and the week's units against the calibrated allowance.

    budget: {"weekly_units": float|None, "daily_share": float (default 1/7), "model_weights": {...},
    "weekly_reset": ISO instant|None}. The week is week_window() as of `at` (default now, or the end of
    `today` when that is another day): from the latest weekly reset with `weekly_reset`, else the rolling
    7 local days (whole days, as before release 72). window_kind/window_start/window_end name it."""
    budget = budget or {}
    if at is None:
        now = datetime.now().astimezone()
        at = now if today is None or today == now.date() else \
            datetime.combine(today, dtime(23, 59, 59), tzinfo=now.tzinfo)
    today = today or at.date()
    data = scan(roots, today)
    mw = budget.get("model_weights") or {}
    per_day = {d: sum(units(t, m, mw) for m, t in models.items()) for d, models in data.items()}
    week_days = [(today - timedelta(days=i)).isoformat() for i in range(7)]
    t = today.isoformat()
    by_model: dict[str, float] = {}
    for m, tok in data.get(t, {}).items():
        by_model[m] = round(units(tok, m, mw), 3)
    weekly = budget.get("weekly_units")
    share = float(budget.get("daily_share") or 1 / 7)
    out = {"today": t, "today_units": round(per_day.get(t, 0.0), 3), "today_by_model": by_model,
           "week_units": round(sum(per_day.get(d, 0.0) for d in week_days), 3),
           "weekly_units": weekly, "daily_share": round(share, 4)}
    win = week_window(budget, at)
    if win["kind"] == "reset":
        out["week_units"] = round(sum(units(t, m, mw) for m, t in window_tokens(win, roots).items()), 3)
    out.update(window_kind=win["kind"], window_start=win["start"], window_end=win["end"])
    if weekly:
        allowance = float(weekly) * share
        out.update(daily_allowance=round(allowance, 3),
                   today_fraction=round(out["today_units"] / allowance, 3) if allowance else None,
                   week_percent=round(100 * out["week_units"] / float(weekly), 1))
    else:
        out.update(daily_allowance=None, today_fraction=None, week_percent=None)
    return out


def calibrate(weekly_percent: float, model_weights: dict | None = None, roots: list[str] | None = None,
              today: date | None = None, weekly_reset: str | None = None, at: datetime | None = None) -> dict:
    """Weekly allowance in units from the percentage Claude Code reports for the current week (the window
    from `weekly_reset` when given, else the rolling seven days)."""
    if not 0 < weekly_percent <= 100:
        raise ValueError("weekly percent must be in (0, 100]")
    s = summary({"model_weights": model_weights, "weekly_reset": weekly_reset}, roots, today, at)
    if s["week_units"] <= 0:
        raise ValueError("no usage recorded in the current week window; calibrate after some work")
    return {"weekly_percent": weekly_percent, "week_units": s["week_units"],
            "weekly_units": round(s["week_units"] * 100 / weekly_percent, 3),
            "window": {"kind": s["window_kind"], "start": s["window_start"], "end": s["window_end"]}}
