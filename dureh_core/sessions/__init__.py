"""Session runtimes, chosen by config.json `session.runtime`, and the day's session job: planned (printed),
started (submitted and recorded as `session.started`) and shown (`status`)."""
from __future__ import annotations

import re
import shlex
import subprocess

from .. import cluster, workday
from ..records import Ledger
from ..site import Site
from ..util import fail
from .base import PURPOSES, SessionRuntime
from .claude_code import ClaudeCode

RUNTIMES = {ClaudeCode.name: ClaudeCode}
DEFAULT_CPUS, DEFAULT_MEM = 2, "16G"
JOB_ID = re.compile(r"(\d+)\s*(?:;.*)?$")  # `Submitted batch job 123` or --parsable `123[;cluster]`


def get(site: Site) -> SessionRuntime:
    name = (site.config.get("session") or {}).get("runtime") or ClaudeCode.name
    if name not in RUNTIMES:
        fail(f"unknown session runtime {name!r} in config.json session.runtime; available: {', '.join(sorted(RUNTIMES))}")
    return RUNTIMES[name]()


def _build(site: Site, purpose: str) -> dict:
    """The sbatch script for the working day's session and what it was built from. `plan` prints exactly
    what `start` submits. Defaults work without `session`, `cluster` or `workday` config keys."""
    rt = get(site)
    argv = rt.launch_command(site, purpose)
    prof = cluster.profile(site)
    slurm = site.config.get("slurm") or {}
    ss = (site.config.get("session") or {}).get("slurm") or {}
    wd = workday.settings(site)
    hours = wd["hours"]
    # the job covers the day and possible overtime, within 24 h; the 8-hour day itself is enforced by `dureh day`
    wall = min(24.0, hours + max(0.0, wd["overtime_hours"]))
    partition = ss.get("partition") or slurm.get("partition") \
        or next((n for n, p in prof["partitions"].items() if p.get("default")), None)
    account = ss.get("account") or slurm.get("account") or prof.get("account")
    logs = site.root / "logs" / "session"
    name = f"cc-{site.root.name}" + ("" if purpose == "desk" else f"-{purpose}")
    head = ["#!/bin/bash", f"#SBATCH --job-name={name}"]
    for flag, value in (("--account", account), ("--partition", partition), ("--qos", ss.get("qos")),
                        ("--cpus-per-task", ss.get("cpus") or DEFAULT_CPUS), ("--mem", ss.get("mem") or DEFAULT_MEM),
                        ("--gres", ss.get("gres"))):
        if value:
            head.append(f"#SBATCH {flag}={value}")
    head += [f"#SBATCH --time={cluster.hms(wall)}", f"#SBATCH --chdir={site.root}",
             f"#SBATCH --output={logs}/%x-%j.out", f"#SBATCH --error={logs}/%x-%j.err",
             f"# dureh site session: {rt.name} session, purpose {purpose}, workday {hours:g} h, wall time "
             f"{cluster.hms(wall)} (day + overtime allowance, within 24 h); the 8-hour day is kept by `dureh day`",
             f"# harness: {site.pinned_harness_root} (config.json harness.root; exported as DUREH_ROOT)"]
    problems = list(rt.check(site))
    notes = list(problems)
    part = prof["partitions"].get(partition) if partition else None
    if prof["partitions"] and partition and part is None:
        notes.append(f"partition {partition!r} is not in the cluster profile")
    limit = cluster.parse_time((part or {}).get("time_limit"))
    if limit is not None and limit.total_seconds() < wall * 3600:
        notes.append(f"partition {partition!r} time limit {part['time_limit']} is shorter than the requested --time")
    body = [f"export {k}={shlex.quote(v)}" for k, v in sorted(rt.env(site).items())]
    body += _tmux_body(site, name, logs, argv)
    text = "\n".join([*head, *(f"# note: {n}" for n in notes), *body]) + "\n"
    return {"runtime": rt.name, "purpose": purpose, "name": name, "wall": cluster.hms(wall), "hours": hours,
            "logs": logs, "notes": notes, "problems": problems, "text": text}


def _tmux_body(site: Site, name: str, logs, argv: list[str]) -> list[str]:
    """Measured 2026-10-04 (labs/session-test, job 3735671): a batch job keeps `claude --remote-control` alive only
    inside a detached tmux on a private socket, with the submitter's TMUX scrubbed (sbatch exports it; a stale
    socket path kills the new server at once) and a sane TERM; the job then holds while the tmux session lives."""
    return ["unset TMUX TMUX_PANE  # sbatch exports the submitter's environment; a stale tmux socket kills the server",
            'export TERM="${TERM:-xterm-256color}"; case "$TERM" in screen*) export TERM=xterm-256color;; esac',
            f"SOCK={shlex.quote(f'dureh-{site.site_id}')}  # private tmux socket for this site",
            f'NAME={shlex.quote(name)}-"${{SLURM_JOB_ID:-local}}"',
            f'PANE={shlex.quote(str(logs))}/"$NAME".pane.log',
            f"CMD={shlex.quote(shlex.join(argv))}",
            f'tmux -L "$SOCK" new-session -d -s "$NAME" -x 200 -y 50 -c {shlex.quote(str(site.root))} '
            '"$CMD; echo CLAUDE_REMOTE_EXIT \\$?; sleep 60" || exit $?',
            'tmux -L "$SOCK" pipe-pane -o -t "$NAME" "cat >> $(printf %q "$PANE")"',
            '# hold the job while the session lives (claude running, or its 60 s hold after exit)',
            'while tmux -L "$SOCK" has-session -t "$NAME" 2>/dev/null; do sleep 20; done']


def plan(site: Site, purpose: str = "desk") -> str:
    """The sbatch script `start` would submit. Nothing is submitted, nothing is created."""
    return _build(site, purpose)["text"]


def start(site: Site, actor: str, purpose: str = "desk", force: bool = False, memo: str | None = None) -> dict:
    """Submit the planned session job and record `session.started` (job_id, submitted_at, wall_time, purpose):
    the company's working day runs 8 hours from this moment (Director, 2026-10-04); `day close` releases
    only a job with this record. One session a day: a second start on the same local day is refused while
    today's job is still in squeue (`force` with a `memo` overrides and records why); when that job is gone
    the start is a restart (`restart: true`) and the day's clock keeps the first session's start."""
    if purpose not in PURPOSES:
        fail(f"unknown session purpose {purpose!r}; available: {', '.join(PURPOSES)}")
    b = _build(site, purpose)
    if b["problems"] and workday.device_on(site, "session"):
        fail("the session cannot start here: " + "; ".join(b["problems"]))
    again = _second_start(site, workday.todays_session(Ledger(site).events()), force, memo)
    now = workday._now()
    b["logs"].mkdir(parents=True, exist_ok=True)  # Slurm opens the logs before the job runs
    script = b["logs"] / f"{b['name']}-{now.strftime('%Y%m%d-%H%M%S')}.sbatch"
    script.write_text(b["text"], encoding="utf-8")
    cmd = ["sbatch", "--parsable", str(script)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=site.env())
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"sbatch did not run: {exc}")
    m = JOB_ID.search((proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else "")
    if proc.returncode != 0 or not m:
        fail(f"sbatch failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[-500:]}; nothing was recorded")
    payload = {"job_id": m.group(1), "submitted_at": workday._ts(now), "wall_time": b["wall"], "purpose": purpose,
               "runtime": b["runtime"], "script": str(script), "command": cmd, "notes": b["notes"], **again}
    return Ledger(site).event(workday.SESSION, actor, None, payload)


def _second_start(site: Site, prev: dict | None, force: bool, memo: str | None) -> dict:
    """What a second `session start` on the same local day adds to its record, or why it is refused.
    The previous job still in squeue (or squeue unavailable, so possibly still running): refused unless
    `force`, which needs a `memo` and is recorded as `force`. The job gone: `restart: true`. Either way
    `previous` names the earlier record and `day_start` keeps the day's clock at the first start."""
    if prev is None:
        return {}
    pp = prev.get("payload") or {}
    job = pp.get("job_id")
    previous = {"record": prev["id"], "job_id": job, "submitted_at": pp.get("submitted_at") or prev["ts"]}
    out = {"previous": previous, "day_start": pp.get("day_start") or previous["submitted_at"]}
    q = _squeue(site, job) if job else {"queued": False}
    if q.get("queued") is False:
        return {**out, "restart": True}
    if q.get("queued"):
        why = f"today's session job {job} ({prev['id']}, submitted {previous['submitted_at']}) is still in squeue: " \
              f"{q['state']}, time left {q['time_left']}"
    else:
        why = f"today's session job {job} ({prev['id']}, submitted {previous['submitted_at']}) may still be running: " \
              f"squeue unavailable ({q.get('error')})"
    if not force:
        fail(f"{why}. A company has one session a day; `dureh site session status` shows it. To start another anyway: "
             f"`dureh site session start --force --memo \"why\"`")
    if not (memo or "").strip():
        fail(f"{why}; --force needs --memo: why a second session starts today")
    return {**out, "force": {"memo": memo.strip(), "why": why}}


def last_started(site: Site) -> dict | None:
    found = [e for e in Ledger(site).events() if e["type"] == workday.SESSION]
    return found[-1] if found else None


def _squeue(site: Site, job: str) -> dict:
    try:
        proc = subprocess.run(["squeue", "-h", "-j", str(job), "-o", "%T|%L"], capture_output=True, text=True,
                              timeout=60, env=site.env())
    except (OSError, subprocess.SubprocessError) as exc:
        return {"queued": None, "error": str(exc)}
    line = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 and not line:
        return {"queued": None, "error": (proc.stderr or "").strip()[-200:] or f"squeue exit {proc.returncode}"}
    if not line:
        return {"queued": False}
    state, _, left = line[0].partition("|")
    return {"queued": True, "state": state.strip(), "time_left": left.strip()}


def status(site: Site) -> dict:
    """Read-only: the last session dureh started, whether its job is still queued or running, and the day."""
    out = {"session": None, "job": None, "day": workday.day(site), "workday": workday.enabled(site)}
    rec = last_started(site)
    if rec:
        p = rec["payload"]
        out["session"] = {"record": rec["id"], "job_id": p.get("job_id"), "submitted_at": p.get("submitted_at") or rec["ts"],
                          "wall_time": p.get("wall_time"), "purpose": p.get("purpose")}
        if p.get("job_id"):
            out["job"] = _squeue(site, p["job_id"])
    return out


def render_status(st: dict) -> str:
    s = st["session"]
    if not s:
        lines = ["no session job started by dureh"]
    else:
        lines = [f"session: {s['record']} job {s['job_id']} submitted {s['submitted_at']} "
                 f"(wall time {s['wall_time'] or '?'}, purpose {s['purpose'] or '?'})"]
        j = st["job"] or {}
        if j.get("queued"):
            lines.append(f"job {s['job_id']}: {j['state']}, time left {j['time_left']}")
        elif j.get("queued") is False:
            lines.append(f"job {s['job_id']}: not in squeue (finished or cancelled)")
        else:
            lines.append(f"job {s['job_id']}: squeue unavailable ({j.get('error', 'no job id')})")
    d = st["day"]
    line = f"day: {d['phase']}" if st["workday"] else f"day: workday mode off ({d['phase']})"
    if d.get("start"):
        line += f", {d['start']} to {d['end']}"
    if d.get("reason"):
        line += f"; {d['reason']}"
    lines.append(line)
    return "\n".join(lines)
