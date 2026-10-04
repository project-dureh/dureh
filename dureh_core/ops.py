"""Operational tools: status, slurm, push, path-refs, usage."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .org import HARNESS_ROOT
from .util import harness_label
from .records import ACTIVE, Ledger
from .site import Site
from .util import DurehError, fail, git, sha256_file

TEXT_SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml", ".json", ".toml", ".txt", ".mjs", ".js", ".ts", ".astro",
                 ".cfg", ".ini", ".erb", ".tsv", ".jsonl", ""}


# ----------------------------------------------------------------- status
def agent_state(led: Ledger, wi: str) -> str:
    """State of the agent bound to the latest dispatch of a WI: running, ended or not started."""
    evs = led.events(wi)
    last = max((i for i, e in enumerate(evs) if e["type"] == "wi.dispatched"), default=None)
    if last is None:
        return "not started"
    later = [e["type"] for e in evs[last + 1:]]
    if "agent.finished" in later:
        return "ended"
    if "agent.started" in later:
        return "running"
    return "starting" if _deferred_start(led, evs[last]) else "not started"


def _deferred_start(led: Ledger, dispatched: dict) -> bool:
    """An agent of the dispatched role started after this dispatch and is still waiting to be bound."""
    role = dispatched["payload"].get("role")
    all_events = led.events()
    done = {e["payload"].get("agent_id") for e in all_events
            if e["type"] == "agent.finished" or (e["type"] == "agent.started" and e["wi"])}
    return any(e["type"] == "agent.started" and not e["wi"] and e["actor"] == role and e["ts"] >= dispatched["ts"]
               and e["payload"].get("agent_id") not in done for e in all_events)


def orphaned(led: Ledger, wis: list[dict]) -> list[dict]:
    dispatched = [w["id"] for w in wis if w["status"] == "dispatched"]
    state = {wi: agent_state(led, wi) for wi in dispatched}
    out = []
    for wi in dispatched:
        if state[wi] in ("running", "starting"):
            continue
        kids = [w["id"] for w in wis if led.get("work-item", w["id"])["payload"].get("parent") == wi]
        running = [k for k in kids if state.get(k) in ("running", "starting")]
        if state[wi] == "not started":
            action = f"if no agent is starting, `dispatch stop --wi {wi} --reason ...` (resuming needs Gate 1)"
        elif running:
            action = f"wait for {', '.join(running)}, or re-adopt with `dispatch brief --wi {wi}`"
        else:
            action = f"re-adopt with `dispatch brief --wi {wi}`, or release with `dispatch stop --wi {wi} --reason ...`"
        out.append({"wi": wi, "role": led.get("work-item", wi)["payload"]["role"], "agent": state[wi],
                    "children_running": running, "action": action})
    return out


def _upstream(site: Site) -> dict:
    from .upstream import collaboration

    return collaboration(site)


def status(site: Site) -> dict:
    led = Ledger(site)
    wis = led.work_items()
    by_status: dict[str, list] = {}
    for w in wis:
        by_status.setdefault(w["status"] or "unknown", []).append(w)
    active_top = [w for w in wis if w["status"] in ACTIVE and not led.get("work-item", w["id"])["payload"].get("parent")]
    reorg = [f for f in led.load("finding") if "reorg" in (f["payload"].get("tags") or [])]
    sessions = led.load("session")
    return {
        "site": str(site.root),
        "package": (site.config.get("startup") or {}).get("id"),
        "harness": f"{harness_label(site.harness_root)} at {site.harness_root}",
        "work_items": len(wis),
        "by_status": {k: [f"{w['id']} {w['role']}: {w['title']}" for w in v] for k, v in sorted(by_status.items())},
        "awaiting_gate1": [w["id"] for w in wis if w["status"] == "proposed"],
        "awaiting_gate2": [w["id"] for w in wis if w["status"] == "verified"],
        "orphaned": orphaned(led, wis),
        "upstream": _upstream(site),
        "active_lanes": f"{len(active_top)}/{site.limit('max_concurrent_lanes', 2)}",
        "reorg_findings": f"{len(reorg)}/{site.limit('reorg_finding_budget', 3)}",
        "findings": len(led.load("finding")),
        "last_session": sessions[-1]["ts"] if sessions else None,
        "index_fresh": led.index_fresh(),
        "workday": _workday(site),
    }


def _workday(site: Site) -> dict:
    from .workday import status as workday_status

    try:
        return workday_status(site)
    except DurehError as exc:
        return {"enabled": None, "error": str(exc)}


def render_status(st: dict) -> str:
    lines = [f"DUREH site {st['site']} (package {st['package']})",
             f"harness {st['harness']}",
             f"work items {st['work_items']} · active lanes {st['active_lanes']} · findings {st['findings']}"
             f" · reorg findings {st['reorg_findings']} · index fresh {st['index_fresh']}"]
    if st["awaiting_gate1"]:
        lines.append("Gate 1 (Director approval) pending: " + ", ".join(st["awaiting_gate1"]))
    if st["awaiting_gate2"]:
        lines.append("Gate 2 (promotion) pending: " + ", ".join(st["awaiting_gate2"]))
    from .workday import render as render_workday

    wline = render_workday(st.get("workday") or {})
    if wline:
        lines.append(wline)
    u = st.get("upstream") or {}
    if u.get("mode") == "collaborating":
        n = u.get("new_from_upstream") or 0
        lines.append(f"Upstream: collaborating; open {', '.join(u['open']) or 'none'}; blocked WIs "
                     f"{', '.join(u['blocked_wis']) or 'none'}; merge window {u['merge_window']}"
                     + (f"; {n} new message{'s' if n != 1 else ''} from upstream (`dureh upstream inbox`)" if n else ""))
    for o in st.get("orphaned") or []:
        still = f"; still running: {', '.join(o['children_running'])}" if o["children_running"] else ""
        lines.append(f"Orphaned: {o['wi']} {o['role']} agent {o['agent']}{still} -> {o['action']}")
    for k, v in st["by_status"].items():
        lines.append(f"[{k}]")
        lines += [f"  {x}" for x in v]
    return "\n".join(lines)


# ------------------------------------------------------------------ slurm
def slurm_submit(site: Site, actor: str, wi: str, script: str, dry_run: bool = False,
                 extra: list[str] | None = None) -> dict:
    led = Ledger(site)
    rec = led.get("work-item", wi)
    if actor not in ("pipeline-runner", "desk"):
        fail("only pipeline-runner (or the desk) submits scheduler jobs")
    if actor != "desk" and rec["payload"]["role"] != actor:
        fail(f"{wi} is not a {actor} work item")
    if led.wi_status(wi)["status"] not in ("dispatched", "rework"):
        fail(f"{wi} is not dispatched")
    path = Path(script)
    if not path.is_file():
        fail(f"required job script is missing: {script}")
    cfg = site.config.get("slurm") or {}
    cmd = ["sbatch", "--parsable", f"--job-name=dureh-{wi}"]
    for key, flag in (("account", "--account"), ("partition", "--partition")):
        if cfg.get(key):
            cmd.append(f"{flag}={cfg[key]}")
    wall, wall_source = _requested_time(path, extra or [], cfg)
    if wall_source == "config":
        cmd.append(f"--time={wall}")  # config slurm.time is the fallback: a script or argument --time wins (UR-20261003-09)
    # relative #SBATCH -o/-e resolve against the job's directory: the WI target, not the agent's shell cwd
    chdir = _sbatch_opt(path, extra or [], "-D", "--chdir")
    if chdir is None:
        chdir = str(_job_root(site, rec["payload"].get("target")))
        cmd.append(f"--chdir={chdir}")
    else:
        chdir = os.path.join(os.getcwd(), site.expand(chdir))
    logs = None
    if not _sets_output(path, extra or []):
        # sbatch writes slurm-<job>.out into the submitting directory (a project tree) unless told otherwise
        logs = site.root / "logs" / "slurm" / wi
        cmd += [f"--output={logs}/%x-%j.out", f"--error={logs}/%x-%j.err"]
        log_out, log_err = f"{logs}/%x-%j.out", f"{logs}/%x-%j.err"
    else:
        out = _sbatch_opt(path, extra or [], "-o", "--output") or "slurm-%j.out"
        log_out = os.path.join(chdir, out)
        log_err = os.path.join(chdir, _sbatch_opt(path, extra or [], "-e", "--error") or out)
    cmd += list(extra or []) + [str(path)]
    payload = {"run_type": "slurm", "status": "not-run" if dry_run else "submitted", "command": cmd,
               "script_sha256": sha256_file(path), "script": str(path), "logs": str(logs) if logs else None,
               "chdir": chdir, "log_out": log_out, "log_err": log_err}
    if not dry_run:
        check = _day_check(site, cmd, path, extra or [], cfg)
        if check is not None:
            payload["start_check"] = check
        if logs:
            logs.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=site.env())
        if proc.returncode != 0:
            payload.update(status="failed", stderr=proc.stderr[-2000:])
        else:
            payload["job_id"] = proc.stdout.strip().split(";")[0]
    run = led.append("run", "slurm", actor, wi, payload)
    if payload["status"] == "failed":
        fail(f"sbatch failed: {payload.get('stderr', '').strip()} (recorded {run['id']})")
    return run


def _job_root(site: Site, target: str | None) -> Path:
    """The directory a WI's job runs in: its module or project, else the site."""
    from .paths import target_root

    if target in (None, "site", "harness", "package"):
        return site.root
    return target_root(site, target) or site.root


def _scan_opt(words: list[str], short: str, long: str) -> str | None:
    """Last value of an sbatch option in a list of arguments (`--long=v`, `--long v`, `-s v`, `-sv`)."""
    found = None
    for i, w in enumerate(words):
        if w.startswith(long + "="):
            found = w.split("=", 1)[1]
        elif w in (short, long):
            found = words[i + 1] if i + 1 < len(words) else found
        elif w.startswith(short) and not w.startswith("--"):
            found = w[len(short):].lstrip("=")
    return found


def _script_opt(script: Path, short: str, long: str) -> str | None:
    found = None
    for line in script.read_text(encoding="utf-8", errors="replace").splitlines():
        words = line.strip().split()
        if words[:1] == ["#SBATCH"]:
            found = _scan_opt(words[1:], short, long) or found
    return found


def _sbatch_opt(script: Path, extra: list[str], short: str, long: str) -> str | None:
    """An sbatch option as the job gets it: command-line arguments win over #SBATCH directives."""
    value = _scan_opt(extra, short, long)
    return value if value is not None else _script_opt(script, short, long)


def _requested_time(script: Path, extra: list[str], cfg: dict) -> tuple[str | None, str | None]:
    """The job's effective --time and where it comes from: the last sbatch argument (`sbatch-arg`), else the
    script's #SBATCH directive (`script`), else config slurm.time (`config`); (None, None) when nowhere."""
    for value, source in ((_scan_opt(extra, "-t", "--time"), "sbatch-arg"),
                          (_script_opt(script, "-t", "--time"), "script"),
                          (str(cfg["time"]) if cfg.get("time") else None, "config")):
        if value:
            return value, source
    return None, None


def _day_check(site: Site, cmd: list[str], script: Path, extra: list[str], cfg: dict):
    """In workday mode, during an on-duty day: refuse a job whose expected start (`sbatch --test-only`)
    plus its requested --time ends after today's end. None when not checked, "unavailable" when the
    scheduler gave no usable answer (the job is submitted as usual)."""
    import re
    from datetime import datetime

    from . import cluster, workday

    if not workday.enabled(site):
        return None
    d = workday.day(site)
    if d["phase"] != "on-duty":
        return None
    requested, source = _requested_time(script, extra, cfg)
    wall = cluster.parse_time(requested)
    if wall is None:
        return "skipped: no --time requested"
    try:
        proc = subprocess.run([cmd[0], "--test-only", *cmd[1:]], capture_output=True, text=True, timeout=120,
                              env=site.env())
        m = re.search(r"to start at (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", proc.stderr + proc.stdout)
    except (OSError, subprocess.SubprocessError):
        m = None
    if not m:
        return "unavailable"
    day_end = datetime.fromisoformat(d["end"])
    start = datetime.fromisoformat(m.group(1))
    if start.tzinfo is None:
        start = start.replace(tzinfo=day_end.tzinfo)  # sbatch prints the cluster's local time
    ends = start + wall
    if ends > day_end:
        fail(f"workday mode: this job is expected to start at {start.isoformat(timespec='seconds')} and, with "
             f"--time {requested} (from the {source}), end at {ends.isoformat(timespec='seconds')}, after "
             f"today's end {d['end']}. Nothing was submitted; ask for a shorter --time, or submit it in the next "
             "approved day.")
    return {"expected_start": start.isoformat(timespec="seconds"), "expected_end": ends.isoformat(timespec="seconds"),
            "day_end": d["end"], "time": requested, "time_source": source}


def _sets_output(script: Path, extra: list[str]) -> bool:
    """The job chooses its own log files (an sbatch argument or an #SBATCH directive)."""
    flags = ("-o", "--output", "-e", "--error")
    if any(a.split("=")[0] in flags for a in extra):
        return True
    for line in script.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#SBATCH") and any(w.split("=")[0] in flags for w in line.split()[1:]):
            return True
    return False


def slurm_status(site: Site, actor: str, run_id: str) -> dict:
    led = Ledger(site)
    run = led.get("run", run_id)
    job = run["payload"].get("job_id")
    if not job:
        fail(f"{run_id} has no scheduler job id")
    proc = subprocess.run(["sacct", "-n", "-P", "-X", "-j", job, "--format=State,ExitCode,Elapsed"],
                          capture_output=True, text=True, timeout=60, env=site.env())
    line = (proc.stdout.strip().splitlines() or [""])[0]
    state = line.split("|")[0] if line else "UNKNOWN"
    ev = led.event("run.status", actor if actor in ("pipeline-runner", "desk") else "desk", run["wi"],
                   {"run": run_id, "job_id": job, "scheduler_state": state, "raw": line}, [run_id])
    return ev


TERMINAL_STATES = ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED",
                   "BOOT_FAIL", "DEADLINE")


def slurm_wait(site: Site, actor: str, run_id: str, timeout: int = 3600, interval: int = 60) -> dict:
    """Check a job until it reaches a terminal state or the timeout; record only the final state."""
    import time

    led = Ledger(site)
    run = led.get("run", run_id)
    job = run["payload"].get("job_id") or fail(f"{run_id} has no scheduler job id")
    deadline = time.monotonic() + max(0, timeout)
    while True:
        proc = subprocess.run(["sacct", "-n", "-P", "-X", "-j", job, "--format=State,ExitCode,Elapsed"],
                              capture_output=True, text=True, timeout=60, env=site.env())
        line = (proc.stdout.strip().splitlines() or [""])[0]
        state = line.split("|")[0] if line else "UNKNOWN"
        if state.split()[0] in TERMINAL_STATES or time.monotonic() >= deadline:
            break
        time.sleep(max(1, min(interval, deadline - time.monotonic())))
    final = state.split()[0] in TERMINAL_STATES
    ev = led.event("run.status", actor if actor in ("pipeline-runner", "desk") else "desk", run["wi"],
                   {"run": run_id, "job_id": job, "scheduler_state": state, "raw": line,
                    "waited": True, "final": final}, [run_id])
    if not final:
        ev["note"] = f"still {state} after {timeout} s; wait again or check later"
    return ev


def slurm_cancel(site: Site, actor: str, run_id: str) -> dict:
    led = Ledger(site)
    run = led.get("run", run_id)
    job = run["payload"].get("job_id") or fail(f"{run_id} has no job id")
    proc = subprocess.run(["scancel", job], capture_output=True, text=True, timeout=60, env=site.env())
    return led.event("run.status", actor, run["wi"], {"run": run_id, "job_id": job, "scheduler_state": "CANCEL-REQUESTED",
                                                      "scancel_exit": proc.returncode,
                                                      "note": "cancellation does not undo partial outputs; inspect and purge"},
                     [run_id])


# ------------------------------------------------------------------- push
def push(site: Site, actor: str, repo: str, remote: str, ref: str, approval: str, dry_run: bool = False) -> dict:
    """Push only to an allowlisted remote, with a Director decision bound to this exact revision."""
    if site.org.role(actor).kind != "desk":
        fail("only the desk pushes")
    led = Ledger(site)
    allowed = (site.config.get("push") or {}).get("allowed_remotes") or []
    repo_path = Path(repo).resolve()
    url = git(["remote", "get-url", remote], repo_path, check=False) or remote
    if url not in allowed and remote not in allowed:
        fail(f"remote {url!r} is not in push.allowed_remotes")
    rev = git(["rev-parse", ref], repo_path)
    dec = led.get("decision", approval)
    b = dec["payload"].get("binding") or {}
    if dec["payload"]["disposition"] != "approve" or b.get("revision") != rev or b.get("remote") not in (url, remote):
        fail(f"decision {approval} does not approve pushing {rev[:12]} to {url}")
    dest = b.get("ref") or ref
    if not dest.startswith("refs/"):
        dest = f"refs/heads/{dest}"
    if not dry_run:
        git(["push", remote, f"{rev}:{dest}"], repo_path, env=site.env())
    return led.event("push.completed" if not dry_run else "push.planned", actor, dec.get("wi"),
                     {"repo": str(repo_path), "remote": url, "revision": rev, "ref": dest, "decision": approval,
                      "action_class": "compensatable"}, [approval])


def push_approve(site: Site, actor: str, repo: str, remote: str, ref: str, memo: str, by: str = "Director") -> dict:
    repo_path = Path(repo).resolve()
    rev = git(["rev-parse", ref], repo_path)
    url = git(["remote", "get-url", remote], repo_path, check=False) or remote
    return Ledger(site).append("decision", "push-approval", actor, None,
                               {"subject": f"push {rev[:12]} to {url}", "disposition": "approve", "by": by, "memo": memo,
                                "binding": {"revision": rev, "remote": url, "ref": ref}})


# -------------------------------------------------------------- path-refs
def path_refs(site: Site, target: str, roots: list[Path] | None = None, limit: int = 200) -> dict:
    """Find text references to a path (and its basename) before moving or deleting it."""
    if not target.strip():
        fail("path-refs needs a path")
    needles = {target.rstrip("/")}
    base = os.path.basename(target.rstrip("/"))
    if len(base) >= 4:
        needles.add(base)
    roots = roots or [site.root, HARNESS_ROOT]
    hits = []
    skip = {".git", "node_modules", "__pycache__", "cache", ".cache", "worktrees", "derivatives", "sourcedata", "rawbids"}
    for root in roots:
        for base_dir, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in skip]
            for name in files:
                p = Path(base_dir) / name
                if p.suffix not in TEXT_SUFFIXES or p.is_symlink():
                    continue
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                    text = p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for n, line in enumerate(text.splitlines(), 1):
                    if any(nd in line for nd in needles):
                        hits.append(f"{p}:{n}: {line.strip()[:160]}")
                        if len(hits) >= limit:
                            return {"needles": sorted(needles), "hits": hits, "truncated": True}
    return {"needles": sorted(needles), "hits": hits, "truncated": False}


# ------------------------------------------------------------------ usage
def transcript_usage(path: Path) -> dict:
    """Sum token usage in a Claude Code transcript JSONL. Unknown formats return coverage 'none'."""
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    seen = set()
    messages = 0
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = d.get("message") if isinstance(d, dict) else None
                if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                    continue
                mid = msg.get("id") or d.get("uuid")
                if mid in seen:
                    continue
                seen.add(mid)
                messages += 1
                for k in totals:
                    v = msg["usage"].get(k)
                    if isinstance(v, int):
                        totals[k] += v
    except OSError:
        return {"coverage": "none", "reason": f"transcript unreadable: {path}"}
    return {**totals, "messages": messages, "coverage": "transcript" if messages else "none",
            "source": str(path), "unit": "tokens"}


def usage(site: Site) -> dict:
    led = Ledger(site)
    runs = [r for r in led.load("run") if r["payload"]["run_type"] == "agent"]
    per_wi: dict[str, dict] = {}
    for r in runs:
        u = r["payload"].get("usage") or {}
        w = per_wi.setdefault(r["wi"] or "-", {"agents": 0, "tokens": 0, "uncovered": 0})
        w["agents"] += 1
        if u.get("coverage") == "transcript":
            w["tokens"] += sum(u.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                      "cache_creation_input_tokens"))
        else:
            w["uncovered"] += 1
    wis = led.work_items()
    promoted = [w for w in wis if w["status"] == "promoted"]
    attempted = [w for w in wis if w["status"] not in (None, "proposed", "approved")]
    total = sum(v["tokens"] for v in per_wi.values())
    uncovered = sum(v["uncovered"] for v in per_wi.values())
    return {"per_wi": per_wi, "attempted_wis": len(attempted), "promoted_wis": len(promoted),
            "tokens_total": total, "agent_runs_without_usage": uncovered,
            "tokens_per_promoted_wi": (round(total / len(promoted)) if promoted else "undefined (0 promoted)"),
            "note": "Token counts come from runtime transcripts; cost requires dated pricing in site config and is not computed."}
