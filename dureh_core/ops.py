"""Operational tools: status, slurm, push, path-refs, usage."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .org import HARNESS_ROOT
from .records import ACTIVE, Ledger
from .site import Site
from .util import fail, git, sha256_file

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
        "work_items": len(wis),
        "by_status": {k: [f"{w['id']} {w['role']}: {w['title']}" for w in v] for k, v in sorted(by_status.items())},
        "awaiting_gate1": [w["id"] for w in wis if w["status"] == "proposed"],
        "awaiting_gate2": [w["id"] for w in wis if w["status"] == "verified"],
        "orphaned": orphaned(led, wis),
        "active_lanes": f"{len(active_top)}/{site.limit('max_concurrent_lanes', 2)}",
        "reorg_findings": f"{len(reorg)}/{site.limit('reorg_finding_budget', 3)}",
        "findings": len(led.load("finding")),
        "last_session": sessions[-1]["ts"] if sessions else None,
        "index_fresh": led.index_fresh(),
    }


def render_status(st: dict) -> str:
    lines = [f"DUREH site {st['site']} (package {st['package']})",
             f"work items {st['work_items']} · active lanes {st['active_lanes']} · findings {st['findings']}"
             f" · reorg findings {st['reorg_findings']} · index fresh {st['index_fresh']}"]
    if st["awaiting_gate1"]:
        lines.append("Gate 1 (Director approval) pending: " + ", ".join(st["awaiting_gate1"]))
    if st["awaiting_gate2"]:
        lines.append("Gate 2 (promotion) pending: " + ", ".join(st["awaiting_gate2"]))
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
    for key, flag in (("account", "--account"), ("partition", "--partition"), ("time", "--time")):
        if cfg.get(key):
            cmd.append(f"{flag}={cfg[key]}")
    cmd += list(extra or []) + [str(path)]
    payload = {"run_type": "slurm", "status": "not-run" if dry_run else "submitted", "command": cmd,
               "script_sha256": sha256_file(path), "script": str(path)}
    if not dry_run:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=site.env())
        if proc.returncode != 0:
            payload.update(status="failed", stderr=proc.stderr[-2000:])
        else:
            payload["job_id"] = proc.stdout.strip().split(";")[0]
    run = led.append("run", "slurm", actor, wi, payload)
    if payload["status"] == "failed":
        fail(f"sbatch failed: {payload.get('stderr', '').strip()} (recorded {run['id']})")
    return run


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
