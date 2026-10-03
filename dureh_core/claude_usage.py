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
from datetime import date, datetime, timedelta
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


def scan(roots: list[str] | None = None, today: date | None = None) -> dict[str, dict[str, dict]]:
    """{day: {model: {token kind: count}}} for the last WINDOW_DAYS local days, across all transcripts."""
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
            entry = files.get(key) or {}
            if entry.get("size") != st.st_size:
                try:
                    entry = _scan_file(path, entry)
                except OSError:
                    continue
            entry["days"] = {d: v for d, v in entry.get("days", {}).items() if d >= first}
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
    out: dict[str, dict[str, dict]] = {}
    for entry in cache["files"].values():
        for day, models in entry.get("days", {}).items():
            for model, tot in models.items():
                acc = out.setdefault(day, {}).setdefault(model, {k: 0 for k in TOKEN_WEIGHTS})
                for k in TOKEN_WEIGHTS:
                    acc[k] += tot.get(k, 0)
    return out


def units(tokens: dict, model: str, model_weights: dict | None = None) -> float:
    """Weighted usage units (millions of input-token equivalents)."""
    return _model_weight(model, model_weights or {}) * sum(TOKEN_WEIGHTS[k] * tokens.get(k, 0) for k in TOKEN_WEIGHTS) / 1e6


def summary(budget: dict | None = None, roots: list[str] | None = None, today: date | None = None) -> dict:
    """Today's and the rolling week's units against the calibrated allowance.

    budget: {"weekly_units": float|None, "daily_share": float (default 1/7), "model_weights": {...}}
    """
    budget = budget or {}
    today = today or date.today()
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
    if weekly:
        allowance = float(weekly) * share
        out.update(daily_allowance=round(allowance, 3),
                   today_fraction=round(out["today_units"] / allowance, 3) if allowance else None,
                   week_percent=round(100 * out["week_units"] / float(weekly), 1))
    else:
        out.update(daily_allowance=None, today_fraction=None, week_percent=None)
    return out


def calibrate(weekly_percent: float, model_weights: dict | None = None, roots: list[str] | None = None,
              today: date | None = None) -> dict:
    """Weekly allowance in units from the percentage Claude Code reports for the current week."""
    if not 0 < weekly_percent <= 100:
        raise ValueError("weekly percent must be in (0, 100]")
    s = summary({"model_weights": model_weights}, roots, today)
    if s["week_units"] <= 0:
        raise ValueError("no usage recorded in the last seven days; calibrate after some work")
    return {"weekly_percent": weekly_percent, "week_units": s["week_units"],
            "weekly_units": round(s["week_units"] * 100 / weekly_percent, 3)}
