"""Office hours (release 64): the site's model server for the `local` member runtime.

`dureh site office-hours start|stop|status|plan` (desk). `start` submits ONE GPU job from config `office_hours`
(the session job's pattern: harness-owned, recorded as `office-hours.started`); `site session start` also starts it
when `office_hours.enabled`, and `day close` and `site session release` stop it (`office-hours.stopped`). The job
starts the server bound to its node, creates each profile of `office_hours.profiles` from its package file (pulling
the base model into `server.models_dir` when missing), then writes `{host, port, models, profiles, started, job}` to
`logs/office-hours/server.json`; it removes the file when it ends. Its wall time follows the workday clock (the day's
end plus the overtime allowance, within the partition's limit and `office_hours.time`).
"""
from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

from .records import Ledger
from .site import Site
from .util import DurehError, fail

STARTED, STOPPED = "office-hours.started", "office-hours.stopped"
SERVER_KINDS = ("ollama",)
DEFAULT_PORT = 11434
KEYS = ("enabled", "partition", "account", "qos", "gres", "cpus", "mem", "time", "server", "profiles")
SERVER_KEYS = ("kind", "bin", "models_dir", "port", "start_timeout")


def logs_dir(site: Site) -> Path:
    return site.root / "logs" / "office-hours"


def server_file(site: Site) -> Path:
    return logs_dir(site) / "server.json"


def config(site: Site) -> dict:
    cfg = site.config.get("office_hours")
    return cfg if isinstance(cfg, dict) else {}


def enabled(site: Site) -> bool:
    return bool(config(site).get("enabled"))


def config_errors(site: Site) -> list[str]:
    """What is wrong with config `office_hours` (release 64)."""
    raw = site.config.get("office_hours")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["config office_hours must be an object (enabled, partition, qos, gres, cpus, mem, time, server, profiles)"]
    out = [f"config office_hours.{k}: unknown key (release 64: {', '.join(KEYS)})" for k in raw if k not in KEYS]
    server = raw.get("server") or {}
    if not isinstance(server, dict):
        out.append("config office_hours.server must be an object {kind, bin, models_dir, port}")
        server = {}
    out += [f"config office_hours.server.{k}: unknown key" for k in server if k not in SERVER_KEYS]
    if server.get("kind", "ollama") not in SERVER_KINDS:
        out.append(f"config office_hours.server.kind must be one of {', '.join(SERVER_KINDS)}")
    port = server.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        out.append("config office_hours.server.port must be a port number")
    profiles = raw.get("profiles") or []
    if not isinstance(profiles, list):
        out.append("config office_hours.profiles must be a list of {name, file, base}")
        profiles = []
    names = set()
    for i, p in enumerate(profiles):
        where = f"config office_hours.profiles[{i}]"
        if not isinstance(p, dict) or set(p) - {"name", "file", "base"} or not p.get("name") or not p.get("file"):
            out.append(f"{where} must be {{name, file (a package path), base}}")
            continue
        if not all(c.isalnum() or c in "._:-/" for c in str(p["name"])) or p["name"] in names:
            out.append(f"{where}.name must be a distinct model name (letters, digits, . _ : - /)")
        names.add(p["name"])
        f = str(p["file"])
        if f.startswith("/") or ".." in f.split("/"):
            out.append(f"{where}.file must be a path inside the startup package")
        elif not (site.startup / f).is_file():
            out.append(f"{where}.file {f} is not in the installed package (startup/)")
    if raw.get("enabled") and not profiles:
        out.append("config office_hours.enabled needs at least one profile")
    return out


def profile_for(site: Site, file: str | None) -> dict | None:
    """The `office_hours.profiles` entry whose package file is `file` (a local role's `profile`)."""
    if not file:
        return None
    for p in config(site).get("profiles") or []:
        if isinstance(p, dict) and str(p.get("file")) == str(file):
            return p
    return None


def _server(site: Site) -> dict:
    s = config(site).get("server") or {}
    models = Path(site.expand(str(s.get("models_dir") or "cache/office-hours/models")))
    return {"kind": s.get("kind") or "ollama", "bin": s.get("bin") or "ollama",
            "models_dir": str(models if models.is_absolute() else site.root / models),
            "port": int(s.get("port") or DEFAULT_PORT), "start_timeout": int(s.get("start_timeout") or 180)}


def wall_hours(site: Site) -> tuple[float, list[str]]:
    """The job's wall time: from now to the day's end plus the overtime allowance when the day runs, else the
    working day plus the allowance; never over `office_hours.time` or the partition's time limit."""
    from datetime import timedelta

    from . import cluster, workday

    wd = workday.settings(site)
    notes = []
    d = workday.day(site)
    if d.get("end"):
        left = (workday._parse(d["end"]) - workday._now()).total_seconds() / 3600
        hours = max(0.5, left + max(0.0, wd["overtime_hours"]))
        notes.append(f"day ends {d['end']}; + overtime allowance {wd['overtime_hours']:g} h")
    else:
        hours = wd["hours"] + max(0.0, wd["overtime_hours"])
        notes.append(f"no running day: workday {wd['hours']:g} h + overtime allowance {wd['overtime_hours']:g} h")
    cap = cluster.parse_time(config(site).get("time"))
    if cap is not None and cap < timedelta(hours=hours):
        hours = cap.total_seconds() / 3600
        notes.append(f"capped by office_hours.time {config(site)['time']}")
    part = (cluster.profile(site)["partitions"].get(config(site).get("partition") or "") or {})
    limit = cluster.parse_time(part.get("time_limit"))
    if limit is not None and limit < timedelta(hours=hours):
        hours = limit.total_seconds() / 3600
        notes.append(f"capped by the partition limit {part['time_limit']}")
    return min(hours, 24.0 * 7), notes


def _profiles(site: Site) -> list[dict]:
    out = []
    for p in config(site).get("profiles") or []:
        f = site.startup / str(p["file"])
        out.append({"name": str(p["name"]), "file": str(f), "package_file": str(p["file"]),
                    "base": str(p.get("base") or ""),
                    "sha256": hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None})
    return out


def plan(site: Site) -> dict:
    """The office-hours job script and what it was built from; nothing is submitted."""
    from . import cluster, ops, sessions

    errs = config_errors(site)
    if errs:
        fail("; ".join(errs))
    cfg = config(site)
    if not cfg.get("profiles"):
        fail("config office_hours has no profiles; nothing to serve")
    srv = _server(site)
    hours, notes = wall_hours(site)
    wall = cluster.hms(hours)
    logs = logs_dir(site)
    name = f"{ops.job_prefix(site)}-office-hours"
    head = sessions.slurm_head(site, name, cfg, wall, logs)
    profiles = _profiles(site)
    missing = [p["package_file"] for p in profiles if p["sha256"] is None]
    if missing:
        fail(f"office_hours profiles missing from the installed package: {', '.join(missing)}")
    q = shlex.quote
    info = {p["name"]: {"file": p["package_file"], "sha256": p["sha256"], "base": p["base"]} for p in profiles}
    body = [f"# dureh office hours: {srv['kind']} server for the local member runtime; wall time {wall} ("
            + "; ".join(notes) + ")",
            "unset DUREH_ACTOR DUREH_AGENT_ID TMUX TMUX_PANE",
            f"OH_DIR={q(str(logs))}", 'INFO="$OH_DIR/server.json"', f"BIN={q(srv['bin'])}", f"PORT={srv['port']}",
            'HOST=$(hostname -f 2>/dev/null || hostname)',
            'export OLLAMA_HOST="$HOST:$PORT"  # bound to this node; the member jobs connect here',
            f"export OLLAMA_MODELS={q(srv['models_dir'])}", 'mkdir -p "$OLLAMA_MODELS" "$OH_DIR"',
            'cleanup() { rm -f "$INFO" "$INFO.tmp"; if [ -n "${SERVER:-}" ]; then kill "$SERVER" 2>/dev/null; wait "$SERVER"'
            ' 2>/dev/null; fi; }',
            "trap cleanup EXIT", "trap 'exit 143' TERM INT",
            '"$BIN" serve > "$OH_DIR/serve-${SLURM_JOB_ID:-local}.log" 2>&1 &', "SERVER=$!",
            f"up=; for i in $(seq 1 {max(1, srv['start_timeout'] // 2)}); do"
            ' if "$BIN" list >/dev/null 2>&1; then up=1; break; fi; kill -0 "$SERVER" 2>/dev/null || break; sleep 2; done',
            '[ -n "$up" ] || { echo "dureh office hours: the server did not start" >&2; exit 1; }']
    for p in profiles:
        body.append(f"# profile {p['name']} from {p['package_file']} (sha256 {p['sha256'][:12]})")
        if p["base"]:
            body.append(f'"$BIN" show {q(p["base"])} >/dev/null 2>&1 || "$BIN" pull {q(p["base"])} ||'
                        f' {{ echo "dureh office hours: cannot pull {p["base"]}" >&2; exit 1; }}')
        body.append(f'"$BIN" create {q(p["name"])} -f {q(p["file"])} ||'
                    f' {{ echo "dureh office hours: cannot create {p["name"]}" >&2; exit 1; }}')
    models = json.dumps([p["name"] for p in profiles])
    profiles_json = json.dumps(info, sort_keys=True)
    body += ['printf \'{"kind": "%s", "host": "%s", "port": %s, "models": %s, "profiles": %s, "started": "%s",'
             ' "job": "%s"}\\n\' ' + f"{q(srv['kind'])} " + '"$HOST" "$PORT" ' + f"{q(models)} {q(profiles_json)} "
             + '"$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${SLURM_JOB_ID:-local}" > "$INFO.tmp" && mv "$INFO.tmp" "$INFO"',
             'echo "dureh office hours: serving on $HOST:$PORT"',
             'wait "$SERVER"']
    text = "\n".join(head + [f"# note: {n}" for n in notes] + body) + "\n"
    return {"text": text, "name": name, "wall": wall, "hours": round(hours, 2), "notes": notes, "logs": str(logs),
            "profiles": profiles, "server": srv}


def _last(events: list[dict]) -> tuple[dict | None, dict | None]:
    """The newest `office-hours.started` record and the stop that followed it (if any)."""
    started = [e for e in events if e["type"] == STARTED]
    if not started:
        return None, None
    last = started[-1]
    stop = next((e for e in events if e["type"] == STOPPED and last["id"] in (e["refs"] or [])), None)
    return last, stop


def status(site: Site) -> dict:
    """Read-only: the last office-hours job, whether it is queued or running, and the server file."""
    from .sessions import _squeue

    led = Ledger(site)
    rec, stop = _last(led.events())
    out = {"enabled": enabled(site), "record": None, "job": None, "stopped": None, "server": server(site)}
    if rec:
        out["record"] = {"id": rec["id"], "job_id": rec["payload"].get("job_id"), "submitted": rec["ts"],
                         "wall": rec["payload"].get("wall")}
        out["job"] = _squeue(site, rec["payload"]["job_id"]) if rec["payload"].get("job_id") else None
        out["stopped"] = {"id": stop["id"], "ts": stop["ts"], "reason": stop["payload"].get("reason")} if stop else None
    return out


def render_status(st: dict) -> str:
    lines = [f"office hours: {'enabled' if st['enabled'] else 'not enabled'} (config office_hours.enabled)"]
    r = st["record"]
    if not r:
        lines.append("no office-hours job started by dureh")
    else:
        j = st["job"] or {}
        state = (f"{j['state']}, time left {j['time_left']}" if j.get("queued") else
                 "not in squeue" if j.get("queued") is False else f"squeue unavailable ({j.get('error')})")
        lines.append(f"job {r['job_id']} ({r['id']}, submitted {r['submitted']}, wall {r['wall']}): {state}"
                     + (f"; stopped {st['stopped']['ts']} ({st['stopped']['reason']})" if st["stopped"] else ""))
    s = st["server"]
    lines.append(f"server: {s['host']}:{s['port']} serving {', '.join(s.get('models') or [])} since {s.get('started')}"
                 if s else "server: none (no server file)")
    return "\n".join(lines)


def server(site: Site) -> dict | None:
    """The server file the job writes once it serves (None while it starts, after it ended, or never)."""
    p = server_file(site)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("host") and data.get("port") else None


def start(site: Site, actor: str, via: str = "command") -> dict:
    """Submit the office-hours job and record `office-hours.started`. Refused while the last one is still queued or
    running (or squeue cannot tell)."""
    from . import sessions, workday

    if site.org.role(actor).kind != "desk":
        fail("only the desk starts office hours")
    led = Ledger(site)
    rec, stop = _last(led.events())
    if rec and not stop and rec["payload"].get("job_id"):
        q = sessions._squeue(site, rec["payload"]["job_id"])
        if q.get("queued") or q.get("queued") is None:
            fail(f"office hours already run: job {rec['payload']['job_id']} ({rec['id']})"
                 + (f" {q['state']}" if q.get("queued") else f"; squeue unavailable ({q.get('error')})")
                 + "; `dureh site office-hours status` shows it")
    b = plan(site)
    logs = Path(b["logs"])
    logs.mkdir(parents=True, exist_ok=True)
    script = logs / f"{b['name']}-{workday._now().strftime('%Y%m%d-%H%M%S')}.sbatch"
    script.write_text(b["text"], encoding="utf-8")
    job_id, cmd = sessions.submit_script(site, script, "office-hours job")
    return led.event(STARTED, actor, None, {"job_id": job_id, "wall": b["wall"], "hours": b["hours"], "script": str(script),
                                            "command": cmd, "notes": b["notes"], "via": via, "server": b["server"],
                                            "profiles": [{k: p[k] for k in ("name", "package_file", "sha256", "base")}
                                                         for p in b["profiles"]]})


def stop(site: Site, actor: str, reason: str, must_run: bool = False) -> dict | None:
    """Cancel the recorded office-hours job (only a job dureh started) and record `office-hours.stopped`. Nothing to
    stop: None (or a refusal with `must_run`)."""
    import subprocess

    from . import sessions

    led = Ledger(site)
    rec, done = _last(led.events())
    if not rec or done:
        if must_run:
            fail("no office-hours job to stop (none recorded, or already stopped)")
        return None
    job = str(rec["payload"].get("job_id") or "")
    q = sessions._squeue(site, job) if job else {"queued": False}
    rc = None
    if q.get("queued") or q.get("queued") is None:
        try:
            rc = subprocess.run(["scancel", job], capture_output=True, text=True, timeout=60, env=site.env()).returncode
        except (OSError, subprocess.SubprocessError) as exc:
            rc = f"scancel did not run: {exc}"
    return led.event(STOPPED, actor, None, {"job_id": job, "reason": reason, "scancel": rc,
                                            "was_queued": q.get("queued")}, [rec["id"]])


def stop_quietly(site: Site, actor: str, reason: str) -> dict | None:
    """`day close` and `session release`: stop office hours when they run; never fail the caller."""
    try:
        ev = stop(site, actor, reason)
    except (DurehError, OSError) as exc:
        return {"error": str(exc)}
    return {"event": ev["id"], "job_id": ev["payload"]["job_id"]} if ev else None


def start_quietly(site: Site, actor: str) -> dict | None:
    """`site session start` with `office_hours.enabled`: start office hours; a failure is a note, not a refusal."""
    if not enabled(site):
        return None
    try:
        ev = start(site, actor, via="session start")
    except (DurehError, OSError) as exc:
        return {"error": str(exc)}
    return {"event": ev["id"], "job_id": ev["payload"]["job_id"], "wall": ev["payload"]["wall"]}

