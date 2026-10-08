"""Operational tools: status, slurm, push, path-refs, usage."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .org import HARNESS_ROOT
from .util import harness_label
from .records import ACTIVE, Ledger
from .site import Site
from .util import DurehError, fail, git, is_within, sha256_file

TEXT_SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml", ".json", ".toml", ".txt", ".mjs", ".js", ".ts", ".astro",
                 ".cfg", ".ini", ".erb", ".tsv", ".jsonl", ""}


# ----------------------------------------------------------------- status
def agent_state(led: Ledger, wi: str, events: list[dict] | None = None) -> str:
    """State of the agent bound to the latest dispatch of a WI: running, ended, starting or not started.
    `events` (all of the site's events, preloaded) spares one load per call."""
    evs = [e for e in events if e["wi"] == wi] if events is not None else led.events(wi)
    last = max((i for i, e in enumerate(evs) if e["type"] == "wi.dispatched"), default=None)
    if last is None:
        return "not started"
    later = [e["type"] for e in evs[last + 1:]]
    if "agent.finished" in later:
        return "ended"
    if "agent.started" in later:
        return "running"
    return "starting" if _deferred_start(led, evs[last], events) else "not started"


def _deferred_start(led: Ledger, dispatched: dict, events: list[dict] | None = None) -> bool:
    """An agent of the dispatched role started after this dispatch and is still waiting to be bound."""
    role = dispatched["payload"].get("role")
    all_events = events if events is not None else led.events()
    done = {e["payload"].get("agent_id") for e in all_events
            if e["type"] == "agent.finished" or (e["type"] == "agent.started" and e["wi"])}
    return any(e["type"] == "agent.started" and not e["wi"] and e["actor"] == role and e["ts"] >= dispatched["ts"]
               and e["payload"].get("agent_id") not in done for e in all_events)


BUNDLE_SETTLED = ("staged", "verified", "promoted", "stopped")  # child states that need no manager


def orphaned(led: Ledger, wis: list[dict], closable: list | None = None) -> list[dict]:
    """Dispatched items whose agent is gone. Release 45 (6h): a bundle whose manager ended and whose every item is
    staged, verified, promoted or stopped needs no manager any more: the desk dispatches the checker for a staged
    item and closes the bundle itself. Such a bundle is not listed here but appended to `closable` (when given)."""
    dispatched = [w["id"] for w in wis if w["status"] == "dispatched"]
    if not dispatched:
        return []
    events = led.events()
    state = {wi: agent_state(led, wi, events) for wi in dispatched}
    payloads = led.work_item_payloads(events)
    status_of = {w["id"]: w["status"] for w in wis}
    out = []
    for wi in dispatched:
        if state[wi] in ("running", "starting"):
            continue
        kids = [w["id"] for w in wis if (payloads.get(w["id"]) or {}).get("parent") == wi]
        running = [k for k in kids if state.get(k) in ("running", "starting")]
        manager = led.site.org.role(payloads[wi]["role"]).is_manager
        if manager and state[wi] == "ended" and kids and all(status_of.get(k) in BUNDLE_SETTLED for k in kids):
            if closable is not None:
                closable.append(_bundle_to_close(wi, payloads[wi]["role"], {k: status_of[k] for k in kids}))
            continue
        if state[wi] == "not started":
            action = (f"if no agent is starting, re-dispatch with `dispatch brief --wi {wi}` (allowed once the dispatch is"
                      f" {int(led.site.limit('dispatch_start_grace_seconds', 120))} s old; it supersedes the one that never"
                      f" started) or release with `dispatch stop --wi {wi} --reason ...`")
        elif running:
            action = f"wait for {', '.join(running)}, or re-adopt with `dispatch brief --wi {wi}`"
        else:
            action = f"re-adopt with `dispatch brief --wi {wi}`, or release with `dispatch stop --wi {wi} --reason ...`"
        out.append({"wi": wi, "role": payloads[wi]["role"], "agent": state[wi],
                    "children_running": running, "action": action})
    return out


LIVE_AGENT = ("running", "starting")


def _bundle_to_close(wi: str, role: str, kids: dict[str, str]) -> dict:
    """What the desk does with a bundle whose manager ended and whose items are all settled (release 45, 6h)."""
    staged = sorted(k for k, v in kids.items() if v == "staged")
    if staged:
        action = ("dispatch the checker for " + ", ".join(f"`dispatch brief --wi {k} --role checker`" for k in staged)
                  + f"; then close the bundle: `wi stage --wi {wi} --summary ...`, `verify --wi {wi}`")
    else:
        action = f"close it: `wi stage --wi {wi} --summary ...`, then `verify --wi {wi}` (a recount) and Gate 2"
    counts: dict[str, int] = {}
    for v in kids.values():
        counts[v] = counts.get(v, 0) + 1
    return {"wi": wi, "role": role, "children": kids, "counts": counts, "staged": staged, "action": action}


def _recorded_states(events: list[dict]) -> dict[str, tuple[str, bool]]:
    """Latest recorded scheduler state per run id from run.status events: {run: (STATE, final)}."""
    out: dict[str, tuple[str, bool]] = {}
    for e in events:
        if e["type"] == "run.status" and e["payload"].get("run"):
            state = (e["payload"].get("scheduler_state") or "").split()
            out[e["payload"]["run"]] = (state[0] if state else "UNKNOWN", bool(e["payload"].get("final")))
    return out


def _tracked_runs(led: Ledger) -> list[dict]:
    """Runs submitted through `slurm submit` (single jobs and `--each` arrays) and verify jobs submitted through
    `verify --wi --submit` (release 37), with a scheduler job id."""
    return [r for r in led.load("run") if r["payload"].get("run_type") in ("slurm", "verify")
            and r["payload"].get("job_id") and r["payload"].get("status") == "submitted"]


def _probe_run(site: Site, run: dict) -> dict | None:
    """Ask the scheduler for one run's state: {state, final, raw | array}; None when it cannot be asked.
    Used for a single run; rounds over many runs use `_probe_runs` (one `sacct` call)."""
    try:
        if run["payload"].get("jobs"):
            st = _via_status(site, run)
            return {"state": st["scheduler_state"], "final": st["final"], "multi": st, **({"array": st} if st["items"] else {})}
        if run["payload"].get("array"):
            st = _array_status(site, run)
            return {"state": st["scheduler_state"], "final": st["final"], "array": st}
        state, line = job_state(site, str(run["payload"]["job_id"]))
        return {"state": state.split()[0] if state else "UNKNOWN", "final": state.split()[0] in TERMINAL_STATES, "raw": line}
    except (OSError, subprocess.SubprocessError):
        return None


SACCT_CHUNK = 200  # job ids per batched `sacct` call


def sacct_lines(site: Site, jobs: list[str]) -> dict[str, list[str]]:
    """Accounting lines of many jobs, grouped by job id, from ONE `sacct -n -P -X -j id1,id2,...
    --format=JobID,State,ExitCode,Elapsed` per SACCT_CHUNK ids. Array tasks (`<job>_<task>`, pending ranges
    `<job>_[0-3]`) are grouped under their array job id. Raises OSError/SubprocessError like a single call."""
    jobs = list(dict.fromkeys(str(j) for j in jobs))
    out: dict[str, list[str]] = {j: [] for j in jobs}
    for i in range(0, len(jobs), SACCT_CHUNK):
        chunk = jobs[i:i + SACCT_CHUNK]
        proc = subprocess.run(["sacct", "-n", "-P", "-X", "-j", ",".join(chunk),
                               "--format=JobID,State,ExitCode,Elapsed"],
                              capture_output=True, text=True, timeout=60, env=site.env())
        for line in proc.stdout.splitlines():
            line = line.strip()
            jid = line.split("|")[0]
            if not line or "." in jid:
                continue
            base = jid.split("_")[0].split("+")[0]
            if base in out:
                out[base].append(line)
    return out


def _probe_runs(site: Site, runs: list[dict]) -> dict[str, dict | None]:
    """`_probe_run` for many runs with one batched `sacct` call: {run id: probe or None}. Per-item array
    states are parsed as by `array_states`; a single job's `raw` is `State|ExitCode|Elapsed` as by `job_state`."""
    if not runs:
        return {}
    ids: list[str] = []
    for r in runs:
        ids += [str(j["job_id"]) for j in r["payload"].get("jobs") or []] or [str(r["payload"]["job_id"])]
    try:
        lines = sacct_lines(site, ids)
    except (OSError, subprocess.SubprocessError):
        return {r["id"]: None for r in runs}
    out: dict[str, dict | None] = {}
    for run in runs:
        job = str(run["payload"]["job_id"])
        if run["payload"].get("jobs"):  # a via submission: several jobs, waited for together
            st = _via_summary(run, lines)
            out[run["id"]] = {"state": st["scheduler_state"], "final": st["final"], "multi": st,
                              **({"array": st} if st["items"] else {})}
            continue
        if run["payload"].get("array"):
            arr = run["payload"]["array"]
            st = _array_summary(run, _parse_array_lines(job, lines.get(job, []), arr["count"]))
            out[run["id"]] = {"state": st["scheduler_state"], "final": st["final"], "array": st}
            continue
        own = [ln for ln in lines.get(job, []) if ln.split("|")[0] == job] or lines.get(job, [])
        raw = "|".join(own[0].split("|")[1:]) if own else ""
        state = (raw.split("|")[0].split() or ["UNKNOWN"])[0] if raw else "UNKNOWN"
        out[run["id"]] = {"state": state, "final": state in TERMINAL_STATES, "raw": raw}
    return out


def _stale(site: Site, run: dict, now=None) -> bool:
    """A tracked run submitted more than `watch_stale_days` (limits, default 7) ago."""
    from datetime import datetime, timedelta, timezone

    days = float(site.limit("watch_stale_days", 7))
    try:
        ts = datetime.fromisoformat(run["ts"])
    except (KeyError, TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - ts > timedelta(days=days)


def _no_state(seen: dict | None) -> bool:
    """sacct answered and has no state at all for the run (no line, UNKNOWN; an array: no task known)."""
    if seen is None:
        return False
    if seen.get("multi"):
        return all(v == "UNKNOWN" for v in seen["multi"]["jobs"].values())
    if seen.get("array"):
        return all(v == "UNKNOWN" for v in seen["array"]["items"].values())
    return seen["state"] == "UNKNOWN"


def live_job_runs(site: Site, led: Ledger, wis: set[str], events: list[dict], probe: bool = True) -> list[dict]:
    """Tracked runs of these WIs whose job is not known to have ended: [{run, job_id, wi, state}]. A run with a
    recorded terminal state has ended; otherwise the scheduler is asked (`probe`, one batched `sacct` call for
    all runs). A stale run (submitted more than `watch_stale_days` ago and sacct has no state at all for it) has
    ended; a run sacct reports in any non-final state stays live however old, and a run the scheduler cannot be
    asked about counts as live until a state is recorded."""
    recorded = _recorded_states(events)
    pending = []
    for run in _tracked_runs(led):
        if run["wi"] not in wis:
            continue
        state, final = recorded.get(run["id"], ("UNKNOWN", False))
        if final or state in TERMINAL_STATES:
            continue
        pending.append((run, state))
    probed = _probe_runs(site, [r for r, _ in pending]) if probe else {}
    out = []
    for run, state in pending:
        seen = probed.get(run["id"])
        if seen is not None and (seen["final"] or (_no_state(seen) and _stale(site, run))):
            continue
        state = seen["state"] if seen is not None else state
        out.append({"run": run["id"], "job_id": str(run["payload"]["job_id"]), "wi": run["wi"], "state": state})
    return out


def lane_holders(site: Site, led: Ledger, wis: list[dict] | None = None, exclude: str | None = None,
                 probe: bool = True) -> list[dict]:
    """Top-level work items that hold a lane (release 30): active items (dispatched, staged, rework, verified)
    with a live agent (started, not finished; a deferred start counts) or a scheduler job not known to have
    ended, on the item itself or on an item of its bundle. Orphaned bundles and staged/rework/verified items
    waiting for the desk hold no lane. Each entry: {wi, lane, role, status, by: [what holds it]}."""
    wis = led.work_items() if wis is None else wis
    events = led.events()
    payloads = led.work_item_payloads(events)
    parent_of = {w["id"]: (payloads.get(w["id"]) or {}).get("parent") for w in wis}
    role_of = {w["id"]: w["role"] for w in wis}
    top = [w for w in wis if w["status"] in ACTIVE and not parent_of[w["id"]] and w["id"] != exclude]
    if not top:
        return []
    members = {t["id"]: [t["id"]] + [w for w, par in parent_of.items() if par == t["id"]] for t in top}
    jobs = live_job_runs(site, led, {m for ms in members.values() for m in ms}, events, probe)
    out = []
    for t in top:
        by = []
        for m in members[t["id"]]:
            st = agent_state(led, m, events)
            if st in LIVE_AGENT:
                by.append(f"{role_of[m]} agent {st} on {m}")
        by += [f"job {j['job_id']} {j['state']} on {j['wi']} ({j['run']})" for j in jobs if j["wi"] in members[t["id"]]]
        if by:
            out.append({"wi": t["id"], "lane": t["lane"], "role": t["role"], "status": t["status"], "by": by})
    return out


def lane_refusals(led: Ledger, hours: int = 24) -> dict:
    """Lane refusals (`lane.refused` events) of the last `hours`: count and the latest one."""
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    recent = [e for e in led.events() if e["type"] == "lane.refused" and e["ts"] >= since]
    last = recent[-1] if recent else None
    return {"hours": hours, "count": len(recent),
            "last": {"wi": last["wi"], "ts": last["ts"], "by": last["actor"],
                     "holders": [h["wi"] for h in last["payload"].get("holders") or []]} if last else None}


EXTERNAL_LIMIT = 10  # external jobs listed by status; the rest are counted
EXTERNAL_IGNORE_DEFAULT = ["cc-*"]  # session jobs (`cc-<session name>[-<purpose>]`): this and other sessions' desks


def _base_job(job) -> str:
    return str(job).split(";")[0].strip().split("_")[0].split("+")[0].split(".")[0]


def _owned_jobs(led: Ledger) -> set[str]:
    """Scheduler job ids a harness record owns: every run record's job(s) and every session job started."""
    owned = set()
    for r in led.load("run"):
        p = r["payload"]
        if p.get("job_id"):
            owned.add(_base_job(p["job_id"]))
        owned |= {_base_job(j["job_id"]) for j in p.get("jobs") or [] if j.get("job_id")}
        if isinstance(p.get("job"), dict) and p["job"].get("job_id"):
            owned.add(_base_job(p["job"]["job_id"]))
    for e in led.events():
        if e["type"] in ("session.started", "container.built") and (e["payload"] or {}).get("job_id"):
            owned.add(_base_job(e["payload"]["job_id"]))
    return owned


def external_jobs(site: Site, led: Ledger | None = None) -> dict | None:
    """Release 45: the site user's scheduler jobs (one `squeue -h -u $USER`) that no harness record owns, so the
    desk sees work started outside `dureh slurm submit|verify|site session` (by hand, by a module's own
    framework, or left over). Excluded: jobs a run or session record names, the calling job ($SLURM_JOB_ID, the
    session itself), and names matching config `slurm.external_ignore` (default `cc-*`, the session jobs of this
    and other sites). {jobs: [{job_id, name, state, time}] (at most EXTERNAL_LIMIT), count}; None when config
    `slurm.external_jobs` is false or squeue cannot be asked (never an error)."""
    cfg = site.config.get("slurm") or {}
    if cfg.get("external_jobs", True) is False:
        return None
    import getpass

    try:
        user = os.environ.get("USER") or getpass.getuser()
        proc = subprocess.run(["squeue", "-h", "-u", user, "-o", "%i|%j|%T|%M"], capture_output=True, text=True,
                              timeout=30, env=site.env())
    except (OSError, subprocess.SubprocessError, KeyError):
        return None
    if proc.returncode != 0:
        return None
    led = led or Ledger(site)
    owned = _owned_jobs(led)
    own_job = os.environ.get("SLURM_JOB_ID")
    if own_job:
        owned.add(_base_job(own_job))
    ignore = cfg.get("external_ignore", EXTERNAL_IGNORE_DEFAULT) or []
    out = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 4 or not parts[0]:
            continue
        job, name, state, used = (x.strip() for x in parts[:4])
        if _base_job(job) in owned or any(fnmatch.fnmatchcase(name, str(g)) for g in ignore):
            continue
        out.append({"job_id": job, "name": name, "state": state, "time": used})
    return {"jobs": out[:EXTERNAL_LIMIT], "count": len(out)}


def render_external(ext: dict | None) -> str | None:
    if not ext or not ext["count"]:
        return None
    more = ext["count"] - len(ext["jobs"])
    return (f"External jobs ({ext['count']}, no harness record): "
            + "; ".join(f"{j['job_id']} {j['name']} {j['state']} {j['time']}" for j in ext["jobs"])
            + (f"; ... and {more} more" if more > 0 else ""))


def _upstream(site: Site) -> dict:
    from .upstream import collaboration

    return collaboration(site)


def status(site: Site) -> dict:
    led = Ledger(site)
    wis = led.work_items()
    by_status: dict[str, list] = {}
    for w in wis:
        by_status.setdefault(w["status"] or "unknown", []).append(w)
    lanes = lane_holders(site, led, wis)
    reorg = [f for f in led.load("finding") if "reorg" in (f["payload"].get("tags") or [])]
    sessions = led.load("session")
    closable: list[dict] = []
    return {
        "site": str(site.root),
        "package": (site.config.get("startup") or {}).get("id"),
        "harness": f"{harness_label(site.harness_root)} at {site.harness_root}",
        "work_items": len(wis),
        "by_status": {k: [f"{w['id']} {w['role']}: {w['title']}" for w in v] for k, v in sorted(by_status.items())},
        "awaiting_gate1": [w["id"] for w in wis if w["status"] == "proposed"],
        "awaiting_gate2": [w["id"] for w in wis if w["status"] == "verified"],
        "orphaned": orphaned(led, wis, closable),
        "bundles_to_close": closable,
        "upstream": _upstream(site),
        "active_lanes": f"{len(lanes)}/{site.limit('max_concurrent_lanes', 2)}",
        "lanes": lanes,
        "lane_refusals": lane_refusals(led),
        "reorg_findings": f"{len(reorg)}/{site.limit('reorg_finding_budget', 3)}",
        "findings": len(led.load("finding")),
        "last_session": sessions[-1]["ts"] if sessions else None,
        "index_fresh": led.index_fresh(),
        "workday": _workday(site),
        "external_jobs": external_jobs(site, led),
        "labels": led.labels(),  # UR-20261008-02: `WI-xxxx (short title)` wherever the text names a WI
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
    from .workday import banner, render as render_workday

    wd = st.get("workday") or {}
    wline = render_workday(wd, with_banner=False)
    if wline:
        lines.append(wline)
    if banner(wd):
        lines.insert(0, banner(wd))  # black-company mode: the first line, always
    u = st.get("upstream") or {}
    if u.get("mode") == "collaborating":
        n = u.get("new_from_upstream") or 0
        lines.append(f"Upstream: collaborating; open {', '.join(u['open']) or 'none'}; blocked WIs "
                     f"{', '.join(u['blocked_wis']) or 'none'}; merge window {u['merge_window']}"
                     + (f"; {n} new message{'s' if n != 1 else ''} from upstream (`dureh upstream inbox`)" if n else ""))
    for lane in st.get("lanes") or []:
        lines.append(f"Lane: {lane['wi']} {lane['role']} [{lane['status']}] held by {'; '.join(lane['by'])}")
    lr = st.get("lane_refusals") or {}
    if lr.get("count"):
        last = lr["last"]
        lines.append(f"Lane refusals ({lr['hours']}h): {lr['count']}; last {last['wi']} by {last['by']} at {last['ts'][11:19]} UTC"
                     f" (held by {', '.join(last['holders']) or 'none'})")
    for o in st.get("orphaned") or []:
        still = f"; still running: {', '.join(o['children_running'])}" if o["children_running"] else ""
        lines.append(f"Orphaned: {o['wi']} {o['role']} agent {o['agent']}{still} -> {o['action']}")
    ext = render_external(st.get("external_jobs"))
    if ext:
        lines.append(ext)
    for b in st.get("bundles_to_close") or []:
        counts = ", ".join(f"{k} {v}" for k, v in sorted(b["counts"].items()))
        lines.append(f"Bundle to close (manager not needed): {b['wi']} {b['role']} items {counts} -> {b['action']}")
    from .records import titled_text

    labels = st.get("labels") or {}
    lines = [titled_text(x, labels) for x in lines]  # the lists below carry the full title already
    for k, v in st["by_status"].items():
        lines.append(f"[{k}]")
        lines += [f"  {x}" for x in v]
    return "\n".join(lines)


# ------------------------------------------------------------------ slurm
ITEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


LEVELS = ("file", "session", "subject")  # finest first; config `slurm.each_level` (default file) is the loop rule's level
BIDS_SKIP = {"derivatives", "sourcedata", "code", "stimuli", "phenotype", ".git", ".datalad", ".heudiconv"}
SIDECAR_EXT = (".json", ".tsv", ".bval", ".bvec")
FILE_ENTITY_RE = re.compile(r"(?:^|_)(sub|ses)-([A-Za-z0-9]+)")
LOOP_RULE = ("workflows loop at file level (Director 2026-10-06): use --each-from-bids <dataset> --level file --suffix <s>, "
             "or --single --reason '<why one job>'")


JOB_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def job_prefix(site: Site) -> str:
    """The prefix of the scheduler job names the harness gives (`<prefix>-<WI>`, `<prefix>-verify-<WI>`): config
    `slurm.job_prefix`, else the site's session name (the company part of its session job `cc-<name>`)."""
    raw = (site.config.get("slurm") or {}).get("job_prefix")
    prefix = str(raw if raw is not None else site.session_name).strip()
    if not JOB_PREFIX_RE.match(prefix):
        fail(f"config slurm.job_prefix (else the session name) {prefix!r} is not a job name prefix: use letters, digits,"
             " '.', '_', '+' or '-', starting with a letter or digit")
    return prefix


def each_level(site: Site) -> str:
    """Config `slurm.each_level`: the level workflow steps loop at (default `file`; Director 2026-10-06: 모든 workflow는
    file 기준으로 loop). A coarser `--level` needs `--reason`, a bare single job `--single --reason`; `any` switches the
    rule off."""
    lv = str((site.config.get("slurm") or {}).get("each_level") or "file")
    if lv not in (*LEVELS, "any"):
        fail(f"config slurm.each_level must be one of {', '.join(LEVELS)} or any: {lv!r}")
    return lv


WATCH_MODES = ("monitor", "on-demand", "off")


def watch_mode(site: Site) -> str:
    """Config `slurm.watch` (release 45): how the desk learns that a job ended. `monitor` (the default, the
    behaviour before the key existed): a standing `slurm watch` Monitor. `on-demand`: no standing Monitor; a short
    job is waited for with `slurm wait --run`, a long one is checked with `slurm status` (or one `slurm watch
    --once` pass) at the next turn or when the Director asks. `off`: no job-end notification at all; job states
    are read with `slurm status`. Only generated text follows the mode: `slurm watch|wait|status` work in every mode."""
    mode = str((site.config.get("slurm") or {}).get("watch") or "monitor")
    if mode not in WATCH_MODES:
        fail(f"config slurm.watch must be one of {', '.join(WATCH_MODES)}: {mode!r}")
    return mode


def job_end_phrase(site: Site) -> str:
    """How tool output names the moment a job's end is noticed, in the site's `slurm.watch` mode."""
    try:
        mode = watch_mode(site)
    except DurehError:
        mode = "monitor"
    if mode == "monitor":
        return "when `slurm watch` reports the job ended"
    return "once the job has ended (the desk sees it with `slurm status`)"


def file_entities(stem: str) -> dict:
    """{sub, ses} labels of a BIDS file stem (`sub-01_ses-1_task-rest_run-01_bold`)."""
    return {m.group(1): m.group(2) for m in FILE_ENTITY_RE.finditer(stem)}


def _labels(raw, prefix: str) -> set[str] | None:
    if not raw:
        return None
    out = {x[len(prefix):] if x.startswith(prefix) else x for r in raw for x in re.split(r"[,\s]+", str(r)) if x}
    return out or None


def bids_files(root: Path, suffix: str, datatype: str | None = None, match: str | None = None,
               subs: set[str] | None = None, sess: set[str] | None = None) -> tuple[list[dict], int]:
    """File-level items of a BIDS dataset from a names-only walk of its `sub-*` trees (nothing is opened, stat'ed
    for content or fetched; a dangling git-annex symlink is a name like any other): the files whose BIDS suffix (the
    last `_` part of the stem, the name before its first `.`) is `suffix`, optionally inside `datatype` directories
    (func, anat, ...), matching `match` (a glob on the path relative to the dataset), of the `subs`/`sess` labels.
    One item per stem: a sidecar (.json, .tsv, .bval, .bvec) never stands for the data file beside it. Returns the
    rows {index, item, sub, ses, path} sorted by stem and the number of BIDS file names seen."""
    rows: dict[str, dict] = {}
    seen = 0
    for sub in sorted(d.name for d in root.iterdir() if d.name.startswith("sub-") and d.is_dir()):
        for dirpath, dirnames, filenames in os.walk(root / sub):
            dirnames[:] = sorted(d for d in dirnames if d not in BIDS_SKIP and not d.startswith("."))
            for name in sorted(filenames):
                stem = name.split(".", 1)[0]
                if name.startswith(".") or not stem.startswith("sub-") or "_" not in stem:
                    continue
                seen += 1
                if stem.rsplit("_", 1)[-1] != suffix:
                    continue
                if datatype and os.path.basename(dirpath) != datatype:
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                if match and not fnmatch.fnmatch(rel, match):
                    continue
                ents = file_entities(stem)
                if subs is not None and ents.get("sub") not in subs:
                    continue
                if sess is not None and ents.get("ses") not in sess:
                    continue
                prev = rows.get(stem)
                if prev is None or (prev["path"].endswith(SIDECAR_EXT) and not rel.endswith(SIDECAR_EXT)):
                    rows[stem] = {"item": stem, "sub": ents.get("sub"), "ses": ents.get("ses"), "path": rel}
    return [{"index": i, **rows[s]} for i, s in enumerate(sorted(rows))], seen


def item_table(files: list[dict]) -> str:
    """The file-level item table (the `.tsv` beside the item list; printed by `--dry-run`)."""
    cols = ("index", "item", "sub", "ses", "path")
    return "".join("\t".join(str(r.get(c) if r.get(c) is not None else "") for c in cols) + "\n"
                   for r in ({c: c for c in cols}, *files))


def validate_items(items: list[str], level: str | None = None) -> list[str]:
    if not items:
        fail("--each needs at least one item")
    bad = [i for i in items if not ITEM_RE.match(i)]
    if bad:
        fail(f"invalid --each item(s) {', '.join(repr(b) for b in bad[:5])}: items must match {ITEM_RE.pattern}")
    seen, dup = set(), []
    for i in items:
        if i in seen and i not in dup:
            dup.append(i)
        seen.add(i)
    if dup:
        fail(f"duplicate --each item(s): {', '.join(dup[:5])}")
    items_level(items, level)
    return items


def parse_items(site: Site, spec: str) -> tuple[list[str], str | None]:
    """`--each` items: a comma/space list, or `@file` (one per line; blank lines and # comments ignored; the
    file must lie inside the site). Returns (items, resolved file or None)."""
    spec = (spec or "").strip()
    each_file = None
    if spec.startswith("@"):
        p = _inside_site(site, spec[1:].strip(), "--each @file")
        if not p.is_file():
            fail(f"--each file is missing: {p}")
        words = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line)
        each_file = str(p)
    else:
        words = [w for w in re.split(r"[,\s]+", spec) if w]
    return validate_items(words), each_file


def _inside_site(site: Site, raw: str, what: str) -> Path:
    if not raw:
        fail(f"{what} needs a path")
    p = Path(os.path.realpath(os.path.join(os.getcwd(), site.expand(raw))))
    root = os.path.realpath(site.root)
    if not str(p).startswith(root + os.sep):
        fail(f"{what} must lie inside the site {root}: {p}")
    return p


def items_from_bids(site: Site, dataset: str, level: str = "subject", suffix: str | None = None,
                    datatype: str | None = None, match: str | None = None, subs=None, sess=None
                    ) -> tuple[list[str], str, list[dict] | None]:
    """Items from a BIDS dataset (inside the site; names only, nothing read): level `file` (Director 2026-10-06)
    enumerates the files of BIDS suffix `suffix` (`bids_files`), item = the file stem; `subject` the `sub-*`
    directories; `session` `sub-*/ses-*` as `sub-X_ses-Y`. Returns (items, resolved dataset, file rows | None)."""
    if level not in LEVELS:
        fail(f"--level must be one of {', '.join(LEVELS)}")
    root = _inside_site(site, dataset, "--each-from-bids")
    if not root.is_dir():
        fail(f"--each-from-bids: not a directory: {root}")
    if level == "file":
        if not suffix or not re.match(r"^[A-Za-z0-9]+$", suffix):
            fail("--level file needs --suffix <BIDS suffix> (letters and digits, e.g. bold, T1w)")
        files, seen = bids_files(root, suffix, datatype, match, _labels(subs, "sub-"), _labels(sess, "ses-"))
        if not files:
            filt = ", ".join(f"{k} {v}" for k, v in (("datatype", datatype), ("match", match), ("sub", subs), ("ses", sess)) if v)
            fail(f"--each-from-bids --level file: no *_{suffix} file under {root}" + (f" ({filt})" if filt else "")
                 + f"; {seen} BIDS file name(s) seen")
        return validate_items([f["item"] for f in files], "file"), str(root), files
    if any(x for x in (suffix, datatype, match, subs, sess)):
        fail("--suffix, --datatype, --match, --sub and --ses go with --level file")
    subs = sorted(d.name for d in root.iterdir() if d.name.startswith("sub-") and d.is_dir())
    items = subs if level == "subject" else [f"{s}_{d.name}" for s in subs for d in sorted((root / s).iterdir())
                                             if d.name.startswith("ses-") and d.is_dir()]
    if not items:
        fail(f"--each-from-bids: no {'sub-*' if level == 'subject' else 'sub-*/ses-*'} directories in {root}")
    return validate_items(items), str(root), None


def _get_run(led: Ledger, run_id: str) -> dict:
    if not RUN_ID_RE.match(run_id or ""):
        fail(f"invalid run id {run_id!r}")
    return led.get("run", run_id)


def _latest_state(led: Ledger, run: dict) -> str | None:
    states = [e["payload"].get("scheduler_state") or "" for e in led.events(run["wi"])
              if e["type"] == "run.status" and e["payload"].get("run") == run["id"]]
    return states[-1].split()[0] if states and states[-1] else None


def _after_deps(led: Ledger, refs: list[str]) -> list[dict]:
    """`--after` runs as [{run, job_id}]: each must be a submitted scheduler run not known to have failed."""
    out = []
    for ref in refs:
        for rid in [r for r in re.split(r"[,\s]+", ref or "") if r]:
            run = _get_run(led, rid)
            p = run["payload"]
            if p.get("run_type") != "slurm":
                fail(f"--after {rid}: not a scheduler run")
            if not p.get("job_id") or p.get("status") in ("failed", "not-run"):
                fail(f"--after {rid}: no submitted scheduler job (status {p.get('status')})")
            state = _latest_state(led, run)
            if state and state != "COMPLETED" and (state in TERMINAL_STATES or state == "CANCEL-REQUESTED"):
                again = [r["id"] for r in led.load("run") if r["payload"].get("resubmit_of") == rid]
                hint = f"; it was resubmitted as {', '.join(again)}: use --after {again[-1]}" if again else \
                    f"; resubmit its failed items with --resubmit-failed {rid}" if p.get("array") else ""
                fail(f"--after {rid}: job {p['job_id']} is known {state}, so a dependent job would never start{hint}")
            if rid not in [d["run"] for d in out]:
                ids = [str(j.get("job_id")).split(";")[0].strip() for j in p.get("jobs") or [] if j.get("job_id")] \
                    or [str(p["job_id"]).split(";")[0].strip()]
                out.append({"run": rid, "job_id": ids[0], **({"job_ids": ids} if len(ids) > 1 else {})})
    if not out:
        fail("--after needs at least one run id")
    return out


def _dep_job_ids(deps: list[dict]) -> list[str]:
    """Every job id an `--after` list waits for (a via submission may hold several jobs)."""
    out: list[str] = []
    for d in deps:
        for jid in d.get("job_ids") or [d["job_id"]]:
            if jid not in out:
                out.append(jid)
    return out


DEFAULT_ITEM_ENV = {"item": "DUREH_ITER", "by": "DUREH_ITER_BY", "index": "DUREH_ITER_INDEX",
                    "sub": "DUREH_ITER_SUB", "ses": "DUREH_ITER_SES",
                    "path": "DUREH_ITER_PATH", "root": "DUREH_ITER_ROOT"}  # path, root: file-level arrays only
VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SUBJECT_ITEM_RE = re.compile(r"^sub-([A-Za-z0-9]+)(?:_ses-([A-Za-z0-9]+))?$")


def item_entities(item: str) -> tuple[str, dict]:
    """(level, {sub, ses}) of an --each item. An item starting with `sub-` must be exactly `sub-<label>`
    (subject) or `sub-<label>_ses-<label>` (session), labels [A-Za-z0-9]+; any other `sub-` item is refused.
    Every other item is plain and not parsed (`dim-20`, `SIGMA`, `JDRS001LNA`)."""
    if not item.startswith("sub-"):
        return "plain", {}
    m = SUBJECT_ITEM_RE.match(item)
    if not m:
        fail(f"--each item {item!r} starts with `sub-` but is not `sub-<label>` or `sub-<label>_ses-<label>` "
             "(labels [A-Za-z0-9]+)")
    return ("session" if m.group(2) else "subject"), ({"sub": m.group(1), "ses": m.group(2)} if m.group(2)
                                                      else {"sub": m.group(1)})


def items_level(items: list[str], level: str | None = None) -> str:
    """file (given: items are BIDS file stems, not parsed), subject, session or plain; one array holds one level only."""
    if level == "file":
        return "file"
    levels = {item_entities(i)[0] for i in items}
    if len(levels) > 1:
        fail(f"--each mixes {' and '.join(sorted(levels))} items in one array; submit one level per array")
    return levels.pop()


def item_env(site: Site) -> dict:
    """Names the --each job gets the item, its index and its subject/session labels under: config
    `slurm.each_env` {"item", "index", ["by"], ["sub"], ["ses"], ["path"], ["root"]} (the company's own names, e.g. CAMRI_SUBJECT;
    an unmapped by/sub/ses/path/root is not exported), else DUREH_ITER, DUREH_ITER_BY, DUREH_ITER_INDEX,
    DUREH_ITER_SUB, DUREH_ITER_SES, DUREH_ITER_PATH, DUREH_ITER_ROOT (Director 2026-10-04; path and root for file-level
    arrays, 2026-10-06). `by` is the array level: file, subject, session or plain."""
    cfg = (site.config.get("slurm") or {}).get("each_env")
    if not cfg:
        return dict(DEFAULT_ITEM_ENV)
    ok = (isinstance(cfg, dict) and {"item", "index"} <= set(cfg) <= set(DEFAULT_ITEM_ENV)
          and all(isinstance(v, str) and VAR_RE.match(v) for v in cfg.values())
          and len(set(cfg.values())) == len(cfg))
    if not ok:
        fail('config slurm.each_env must be {"item": <NAME>, "by": <NAME>, "index": <NAME>, "sub": <NAME>, '
             f'"ses": <NAME>, "path": <NAME>, "root": <NAME>}} (by, sub, ses, path and root optional) with different '
             f'variable names: {cfg!r}')
    return {k: cfg[k] for k in DEFAULT_ITEM_ENV if k in cfg}


def item_lines(items: list[str], items_file: str | None, env: dict | None = None, level: str | None = None,
               files: list[dict] | None = None, root: str | None = None) -> list[str]:
    """Shell lines that map $SLURM_ARRAY_TASK_ID to the item and export the item, the array level (`by`), its
    index and its subject and session labels (empty when the item has none) under the configured names; a
    file-level array (`level` file, `files` rows, dataset `root`) also exports the file's path relative to the
    dataset and the dataset root."""
    import shlex

    env = env or DEFAULT_ITEM_ENV
    ents = [file_entities(i) for i in items] if level == "file" else [item_entities(i)[1] for i in items]
    it, ix = env["item"], env["index"]
    arr = lambda vals: "(" + " ".join(shlex.quote(v) if v else "''" for v in vals) + ")"  # noqa: E731
    out = [f"# --each: {len(items)} item(s), array task i runs item i" + (f" (list: {items_file})" if items_file else ""),
           "_DUREH_ITERS=" + arr(items),
           ': "${SLURM_ARRAY_TASK_ID:?dureh: not a Slurm array task}"',
           f'export {ix}="$SLURM_ARRAY_TASK_ID"',
           f'export {it}="${{_DUREH_ITERS[$SLURM_ARRAY_TASK_ID]:-}}"',
           f'[ -n "${it}" ] || {{ echo "dureh: no item for array task $SLURM_ARRAY_TASK_ID" >&2; exit 98; }}']
    if env.get("by"):
        out.append(f"export {env['by']}={items_level(items, level)}")
    for key in ("sub", "ses"):
        if env.get(key):
            out += [f"_DUREH_{key.upper()}=" + arr([e.get(key) or "" for e in ents]),
                    f'export {env[key]}="${{_DUREH_{key.upper()}[$SLURM_ARRAY_TASK_ID]}}"']
    shown = [k for k in ("item", "by", "index", "sub", "ses") if env.get(k)]
    if files is not None and env.get("path"):
        out += ["_DUREH_PATH=" + arr([f["path"] for f in files]),
                f'export {env["path"]}="${{_DUREH_PATH[$SLURM_ARRAY_TASK_ID]}}"']
        shown.append("path")
    if root and env.get("root"):
        out.append(f"export {env['root']}={shlex.quote(str(root))}")
        shown.append("root")
    names = ", ".join(f"{env[k]}=${env[k]}" for k in shown)
    out.append(f'echo "dureh: array task: {names}" >&2')
    return out


def _each_wrapper(script: Path, items: list[str], items_file: str, env: dict | None = None, level: str | None = None,
                  files: list[dict] | None = None, root: str | None = None) -> str:
    import shlex

    lines = script.read_text(encoding="utf-8", errors="replace").splitlines()
    first = lines[0] if lines and lines[0].startswith("#!") else ""
    interp = shlex.split(first[2:]) if first else ["/bin/bash"]
    directives = [ln for ln in lines if ln.strip().startswith("#SBATCH")]
    out = ["#!/bin/bash", *directives,
           f"# Generated by `dureh slurm submit --each`: runs {script} once per item (one array task each).",
           *item_lines(items, items_file, env, level, files, root),
           # absolute: the job runs in its --chdir, not where the script path was given
           "exec " + " ".join(shlex.quote(w) for w in interp) + f" {shlex.quote(os.path.abspath(script))} \"$@\""]
    return "\n".join(out) + "\n"


def _submit_guards(led: Ledger, actor: str, wi: str) -> dict:
    """Who may submit for which WI: pipeline-runner (or the desk), on a dispatched WI of that role."""
    rec = led.get("work-item", wi)
    if actor not in ("pipeline-runner", "desk"):
        fail("only pipeline-runner (or the desk) submits scheduler jobs")
    if actor != "desk" and rec["payload"]["role"] != actor:
        fail(f"{wi} is not a {actor} work item")
    if led.wi_status(wi)["status"] not in ("dispatched", "rework"):
        fail(f"{wi} is not dispatched")
    return rec


def slurm_submit(site: Site, actor: str, wi: str, script: str | None, dry_run: bool = False,
                 extra: list[str] | None = None, extra_payload: dict | None = None,
                 module: str | None = None, rev: str | None = None, each: str | None = None,
                 after: list[str] | None = None, max_parallel: int | None = None,
                 items: list[str] | None = None, each_from_bids: str | None = None,
                 level: str | None = None, suffix: str | None = None, datatype: str | None = None,
                 match: str | None = None, subs: list[str] | None = None, sess: list[str] | None = None,
                 single: bool = False, reason: str | None = None) -> dict:
    """Submit one scheduler job (or one array, `--each*`) for a dispatched WI and record the run. File-level loop
    rule (Director 2026-10-06, config `slurm.each_level`, default file): a workflow step loops over files
    (`--each-from-bids <dataset> --level file --suffix <s>`); a coarser `--level` needs `--reason`, a bare single
    job `--single --reason`, a group step goes `--after` an array. `level`, `reason` and `single` are recorded."""
    from datetime import datetime, timezone

    led = Ledger(site)
    rec = _submit_guards(led, actor, wi)
    if not script:
        fail("--script is required")
    path = Path(script)
    if not path.is_file():
        fail(f"required job script is missing: {script}")
    if rev is not None and module is None:
        fail("--rev needs --module")
    extra = list(extra or [])
    each_file = bids = files = None
    rule_level = each_level(site)
    if each is not None and each_from_bids is not None:
        fail("--each and --each-from-bids exclude each other")
    if level is not None and each_from_bids is None:
        fail("--level goes with --each-from-bids")
    if any(x for x in (suffix, datatype, match, subs, sess)) and each_from_bids is None:
        fail("--suffix, --datatype, --match, --sub and --ses go with --each-from-bids --level file")
    if each is not None:
        items, each_file = parse_items(site, each)
    elif each_from_bids is not None:
        level = level or (rule_level if rule_level != "any" else "subject")
        items, bids, files = items_from_bids(site, each_from_bids, level, suffix, datatype, match, subs, sess)
    elif items is not None:
        validate_items(items)
    reason = (reason or "").strip() or None
    if single and items is not None:
        fail("--single is for a job without --each")
    if single and not reason:
        fail("--single needs --reason '<why one job>'")
    if rule_level != "any":  # the file-level loop rule (Director 2026-10-06)
        if items is None and not after and not single:
            fail(LOOP_RULE)
        if each_from_bids is not None and LEVELS.index(level) > LEVELS.index(rule_level) and not reason:
            fail(f"--level {level} is coarser than the loop rule's level {rule_level} (config slurm.each_level; Director "
                 f"2026-10-06: workflows loop at file level): add --reason '<why {level} level>'")
    if max_parallel is not None:
        if items is None:
            fail("--max-parallel needs --each")
        if max_parallel < 1:
            fail("--max-parallel must be a positive number")
    if items is not None and _sbatch_opt(path, extra, "-a", "--array") is not None:
        fail("--each makes the job array itself; remove --array from the script and the sbatch arguments")
    deps = _after_deps(led, after) if after else []
    cfg = site.config.get("slurm") or {}
    cmd = ["sbatch", "--parsable"]
    # the job's name: the script's own #SBATCH -J (or an sbatch argument) wins, else `<job prefix>-<WI>`
    own_name = _sbatch_opt(path, extra, "-J", "--job-name")
    job_name = own_name or f"{job_prefix(site)}-{wi}"
    if own_name is None:
        cmd.append(f"--job-name={job_name}")
    for key, flag in (("account", "--account"), ("partition", "--partition")):
        if cfg.get(key):
            cmd.append(f"{flag}={cfg[key]}")
    wall, wall_source = _requested_time(path, extra, cfg)
    if wall_source == "config":
        cmd.append(f"--time={wall}")  # config slurm.time is the fallback: a script or argument --time wins (UR-20261003-09)
    if items is not None:
        cmd.append(f"--array=0-{len(items) - 1}" + (f"%{max_parallel}" if max_parallel else ""))
    if deps:
        cmd.append("--dependency=afterok:" + ":".join(_dep_job_ids(deps)))
    # the job runs in the WI target, not the agent's shell cwd (its scheduler logs never do: see job_log_paths)
    chdir = _sbatch_opt(path, extra, "-D", "--chdir")
    if chdir is None:
        chdir = str(_job_root(site, rec["payload"].get("target")))
        cmd.append(f"--chdir={chdir}")
    else:
        chdir = os.path.join(os.getcwd(), site.expand(chdir))
    site_logs = wi_log_dir(site, wi)
    logs = site_logs
    warnings = []
    is_array = items is not None or _sbatch_opt(path, extra, "-a", "--array") is not None
    log_out, log_err, log_warn = job_log_paths(site, site_logs, path, extra, is_array)
    warnings += log_warn
    # command-line options win over #SBATCH directives: the scheduler logs always land in <log_dir>/<WI>/
    log_args = [f"--output={log_out}"] + ([f"--error={log_err}"] if log_err != log_out else [])
    array = None
    if items is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        items_file = site_logs / f"each-{stamp}.items"
        array = {"job_id": None, "items": list(items), "count": len(items), "items_file": str(items_file),
                 "env": item_env(site), "level": items_level(items, "file" if files is not None else None)}
        if files is not None:
            array.update(table=str(items_file.with_suffix(".tsv")), files=len(files), root=bids)
    mod = None
    if module is not None:  # UR-20261004-04: the job runs against a pinned, read-only tree of modules/<m>
        from . import exectree

        mod = exectree.prepare(site, wi, rec["payload"].get("target"), module, rev, path, dry_run,
                               items=items, items_file=array["items_file"] if array else None,
                               item_env=array["env"] if array else None, item_level=array["level"] if array else None,
                               item_files=files, item_root=bids)
        warnings += mod["warnings"]
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    job_file = mod["wrapper"] if mod else str(path)
    if array is not None and not mod:
        job_file = str(site_logs / (Path(array["items_file"]).stem + ".sbatch"))
        array["wrapper"] = job_file
    cmd += extra + log_args + [job_file]
    payload = {"run_type": "slurm", "status": "not-run" if dry_run else "submitted", "command": cmd,
               "script_sha256": sha256_file(path), "script": str(path), "logs": str(logs), "job_name": job_name,
               "chdir": chdir, "log_out": log_out, "log_err": log_err, "sbatch_args": extra,
               "level": array["level"] if array else ("group" if deps else "single"), **(extra_payload or {})}
    if reason:
        payload["reason"] = reason
    if single:
        payload["single"] = True
    if array is not None:
        payload["array"] = array
        if max_parallel:
            payload["max_parallel"] = max_parallel
        if each_file:
            payload["each_file"] = each_file
        if bids:
            payload["each_from_bids"] = {"dataset": bids, "level": level or "subject",
                                         **{k: v for k, v in (("suffix", suffix), ("datatype", datatype), ("match", match),
                                                              ("sub", subs), ("ses", sess)) if v},
                                         **({"files": len(files)} if files is not None else {})}
    if dry_run and files is not None:
        print(item_table(files), end="", file=sys.stderr)  # the item table a --dry-run shows
    if deps:
        payload["after"] = deps
    if mod:
        payload.update(module=mod["module"], wrapper=mod["wrapper"])
    if warnings:
        payload["warnings"] = warnings

    def undo():
        if mod:
            exectree.cleanup(Path(mod["module"]["tree"]))
        if array is not None:
            for f in (array["items_file"], array.get("wrapper"), array.get("table")):
                if f and os.path.isfile(f):
                    os.unlink(f)

    if not dry_run:
        if array is not None:
            site_logs.mkdir(parents=True, exist_ok=True)
            Path(array["items_file"]).write_text("".join(f"{i}\n" for i in items), encoding="utf-8")
            if files is not None:
                Path(array["table"]).write_text(item_table(files), encoding="utf-8")
            if array.get("wrapper"):
                Path(array["wrapper"]).write_text(_each_wrapper(path, items, array["items_file"], array["env"],
                                                                array["level"], files, bids))
                Path(array["wrapper"]).chmod(0o755)
        try:
            check = _day_check(site, cmd, path, extra, cfg, wi=wi)
        except DurehError:
            undo()
            raise
        if check is not None:
            payload["start_check"] = check
        logs.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=site.env())
        if proc.returncode != 0:
            payload.update(status="failed", stderr=proc.stderr[-2000:])
            undo()
        else:
            payload["job_id"] = proc.stdout.strip().split(";")[0]
            if array is not None:
                array["job_id"] = payload["job_id"]
    run = led.append("run", "slurm", actor, wi, payload)
    if payload["status"] == "failed":
        fail(f"sbatch failed: {payload.get('stderr', '').strip()} (recorded {run['id']})")
    return run


# ------------------------------------------------------------ slurm --via
# A module with a submit command of its own (proposals/module-submit-contract.md, Director 2026-10-06): the harness
# runs that command inside the WI context and ingests the submission record it writes. No module name in code: what
# the harness may add to the command comes from config `modules[<m>].submit` {record_arg, log_arg, after_arg, items_arg}.
VIA_RECORD_LINE = re.compile(r"^submission-record:[ \t]*(.+?)[ \t]*$", re.M)
VIA_SUBMIT_KEYS = ("record_arg", "log_arg", "after_arg", "items_arg")
VIA_TIMEOUT_DEFAULT = 600
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _via_text(argv: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(w) for w in argv)


def via_module_config(site: Site, argv: list[str], via_module: str | None = None) -> tuple[str | None, dict]:
    """(module id, its `submit` block) for a via command: the `modules[]` entry named by `--via-module`, else the one
    whose `cli` (or `id`) is the command's first word (basename). (None, {}) when no entry matches: the harness then
    adds nothing to the command."""
    entries = [e for e in site.config.get("modules") or [] if isinstance(e, dict) and e.get("id")]
    if via_module is not None:
        entry = next((e for e in entries if e["id"] == via_module), None)
        if entry is None:
            fail(f"--via-module {via_module}: config.json modules has no entry with that id")
    else:
        first = os.path.basename(argv[0]) if argv else ""
        entry = next((e for e in entries if first and first == str(e.get("cli") or e["id"])), None)
    if entry is None:
        return None, {}
    sub = entry.get("submit") or {}
    ok = (isinstance(sub, dict) and set(sub) <= set(VIA_SUBMIT_KEYS)
          and all(isinstance(v, str) and v.startswith("-") and " " not in v for v in sub.values()))
    if not ok:
        fail(f"config modules[{entry['id']}].submit must map {', '.join(VIA_SUBMIT_KEYS)} (each optional) to one "
             f"option name of the module's command, such as \"--submission-record\": {sub!r}")
    return str(entry["id"]), dict(sub)


def _via_path(base: Path, value, site: Site) -> str | None:
    if value in (None, ""):
        return None
    v = site.expand(str(value))
    return v if os.path.isabs(v) else os.path.normpath(os.path.join(base, v))


def ingest_submission(path: Path, root: Path, site: Site) -> dict:
    """Read and check a module's submission record (minimal schema; a module's own `<tool>.submission.v1` may be a superset):
    `schema_version` (or `schema`), `jobs: [{job_id, kind: array|single, count?, max_parallel?, script?, logs: {stdout,
    stderr}? (or log_out/log_err), dependency?, name?, items?}]`; `items[]` (top level, for the array jobs that carry
    none of their own) as strings or {index, item, level, sub, ses, path}; optional tool, tool_version, submitted_at,
    cwd, manifest, selection, level, records. Relative paths resolve against the record's `cwd`, else the job root.
    Unknown top-level keys are kept under `extra`."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"submission record {path} is not readable JSON: {exc}")
    if not isinstance(data, dict):
        fail(f"submission record {path} must be a JSON object")
    schema = data.get("schema_version") or data.get("schema")
    if not isinstance(schema, str) or not schema.strip():
        fail(f"submission record {path} has no schema_version")
    base = Path(_via_path(root, data.get("cwd"), site) or root)
    jobs_raw = data.get("jobs")
    if not isinstance(jobs_raw, list) or not jobs_raw:
        fail(f"submission record {path} has no jobs[]")

    def items_of(raw, where: str) -> tuple[list[str], str | None]:
        if not isinstance(raw, list) or not raw:
            fail(f"submission record {path}: {where} needs a non-empty items list (an array job names its items)")
        entries = [{"item": x} if isinstance(x, str) else x for x in raw]
        if not all(isinstance(e, dict) and isinstance(e.get("item"), str) for e in entries):
            fail(f"submission record {path}: {where} items must be strings or objects with an `item` string")
        if all(isinstance(e.get("index"), int) for e in entries):
            entries = sorted(entries, key=lambda e: e["index"])
        names = [e["item"] for e in entries]
        bad = [n for n in names if not ITEM_RE.match(n)]
        if bad:
            fail(f"submission record {path}: {where} item(s) {', '.join(repr(b) for b in bad[:5])} do not match "
                 f"{ITEM_RE.pattern}")
        if len(set(names)) != len(names):
            fail(f"submission record {path}: {where} has duplicate items")
        levels = {str(e["level"]) for e in entries if e.get("level")}
        if len(levels) > 1:
            fail(f"submission record {path}: {where} mixes item levels {', '.join(sorted(levels))}")
        return names, (levels.pop() if levels else None)

    top_items = top_level = None
    if data.get("items") is not None:
        top_items, top_level = items_of(data.get("items"), "items")
    declared = (data.get("selection") or {}).get("level") if isinstance(data.get("selection"), dict) else None
    declared = declared or data.get("level") or top_level
    jobs = []
    for i, j in enumerate(jobs_raw):
        where = f"jobs[{i}]"
        if not isinstance(j, dict):
            fail(f"submission record {path}: {where} must be an object")
        jid = str(j.get("job_id") if j.get("job_id") is not None else "").strip()
        if not jid or not JOB_ID_RE.match(jid):
            fail(f"submission record {path}: {where} has no usable job_id ({j.get('job_id')!r})")
        kind = j.get("kind")
        if kind not in ("array", "single"):
            fail(f"submission record {path}: {where}.kind must be array or single ({kind!r})")
        logs = j.get("logs") if isinstance(j.get("logs"), dict) else {}
        job = {"job_id": jid, "kind": kind,
               "script": _via_path(base, j.get("script"), site),
               "log_out": _via_path(base, logs.get("stdout") or j.get("log_out"), site),
               "log_err": _via_path(base, logs.get("stderr") or j.get("log_err"), site)}
        if j.get("name"):
            job["name"] = str(j["name"])
        if isinstance(j.get("dependency"), list) and j["dependency"]:
            job["dependency"] = [str(d) for d in j["dependency"]]
        if isinstance(j.get("max_parallel"), int):
            job["max_parallel"] = j["max_parallel"]
        if kind == "array":
            items, level = (items_of(j["items"], f"{where}.items") if j.get("items") is not None
                            else (top_items, top_level))
            if items is None:
                fail(f"submission record {path}: {where} is an array job but neither it nor the record lists items")
            count = j.get("count")
            if count is not None and (not isinstance(count, int) or count != len(items)):
                fail(f"submission record {path}: {where}.count {count!r} differs from its {len(items)} items")
            job.update(items=items, count=len(items), level=_via_level(items, level or declared),
                       level_declared=bool(level or declared))
        jobs.append(job)
    known = {"schema_version", "schema", "tool", "tool_version", "submitted_at", "jobs", "items", "cwd", "manifest",
             "selection", "records", "level"}
    return {"schema_version": schema.strip(), "tool": data.get("tool"), "tool_version": data.get("tool_version"),
            "submitted_at": data.get("submitted_at"), "jobs": jobs, "cwd": str(base),
            "manifest": data.get("manifest"), "selection": data.get("selection"), "records": data.get("records"),
            "extra": {k: v for k, v in data.items() if k not in known}}


def _via_level(items: list[str], declared) -> str:
    """The array's item level: what the record declares (any word), else subject/session/plain by the item grammar
    when every item fits it, else plain (a module's own item grammar, e.g. BIDS file stems, is not parsed)."""
    if declared:
        return str(declared)
    try:
        return items_level(items)
    except DurehError:
        return "plain"


def _via_day_check(site: Site, payload: dict, cfg: dict, wi: str | None = None):
    """Warn-only start check for a via run (decision 4, 2026-10-06): the job already exists, so after ingest the
    requested --time comes from the recorded script's #SBATCH line (else config slurm.time) and the expected start
    from `squeue --start`; when the expected end is past today's end the record says `late: true` (`past_end`
    likewise) and the submit warns; for a plan item, a fix for one, or a WI in a Director overtime record
    (`workday.past_end_rule`) the record says why and, for plan work, the overtime is recorded as for a plain
    submit. None when not in an on-duty workday, "skipped"/"unavailable" as `_day_check`."""
    from datetime import datetime

    from . import cluster, workday

    if not workday.enabled(site):
        return None
    d = workday.day(site)
    if d["phase"] != "on-duty":
        return None
    script = payload.get("script")
    requested = source = None
    if script and os.path.isfile(script):
        requested, source = _script_opt(Path(script), "-t", "--time"), "script"
    if not requested and cfg.get("time"):
        requested, source = str(cfg["time"]), "config"
    wall = cluster.parse_time(requested)
    if wall is None:
        return "skipped: no --time in the recorded script"
    try:
        proc = subprocess.run(["squeue", "-h", "-j", str(payload["job_id"]), "--start", "-o", "%S"],
                              capture_output=True, text=True, timeout=60, env=site.env())
        m = re.search(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", proc.stdout)
    except (OSError, subprocess.SubprocessError):
        m = None
    if not m:
        return "unavailable"
    day_end = datetime.fromisoformat(d["end"])
    start = datetime.fromisoformat(m.group(1))
    if start.tzinfo is None:
        start = start.replace(tzinfo=day_end.tzinfo)
    ends = start + wall
    out = {"expected_start": start.isoformat(timespec="seconds"), "expected_end": ends.isoformat(timespec="seconds"),
           "day_end": d["end"], "time": requested, "time_source": source, "late": ends > day_end,
           "past_end": ends > day_end, "note": "warn-only for --via: the module submitted the job before the check"}
    if out["late"]:
        d = workday.plan_open(site, d)
        rule = workday.past_end_rule(site, wi, d)
        if rule:
            name, reason = rule
            if name == workday.FINISH_RULE and not site.observe:
                workday.note_finish_overtime(site, d, wi)
            out.update(rule=name, allowed=reason)
            print(f"warning: workday: this job ends at {ends:%H:%M}, after today's end {day_end:%H:%M}; allowed as "
                  + ("plan work (Director rule 2026-10-06)" if name == workday.FINISH_RULE else reason), file=sys.stderr)
        else:
            print(f"warning: workday mode: job {payload['job_id']} is expected to start at {out['expected_start']} and, "
                  f"with --time {requested} (from the {source}), end at {out['expected_end']}, after today's end "
                  f"{d['end']}; it was submitted by the module and stays queued: cancel it with `slurm cancel` or let "
                  f"it run. {workday.way_out(site, wi, d, time_hint=f'{day_end:%H:%M}')}", file=sys.stderr)
    return out


def slurm_submit_via(site: Site, actor: str, wi: str, via, record: str | None = None,
                     after: list[str] | None = None, dry_run: bool = False, via_module: str | None = None,
                     extra_payload: dict | None = None, argv_extra: list[str] | None = None) -> dict:
    """`slurm submit --wi WI --via '<module command ...>'`: run the module's own submit command in the WI's job root
    with the site environment, then ingest the submission record it wrote and record the usual `slurm` run so
    status/wait/watch/resubmit-failed/purge and `promote --run` work as for a plain submit. Record path: `--record`,
    else the path the harness passed through `modules[<m>].submit.record_arg`, else the last stdout line
    `submission-record: <path>`. A non-zero exit records only a `slurm.via-failed` event."""
    import shlex
    from datetime import datetime, timezone

    led = Ledger(site)
    rec = _submit_guards(led, actor, wi)
    argv = shlex.split(via) if isinstance(via, str) else [str(w) for w in via]
    if not argv:
        fail("--via needs a command")
    mod_id, sub = via_module_config(site, argv, via_module)
    deps = _after_deps(led, after) if after else []
    if deps and not sub.get("after_arg"):
        fail("--after with --via needs config modules[<m>].submit.after_arg (the option the module takes a job-id "
             "dependency with); without it put the dependency in the module command yourself"
             + (f" (modules[{mod_id}].submit has no after_arg)" if mod_id else " (no modules[] entry matches "
                f"{argv[0]!r}; name one with --via-module)"))
    cfg = site.config.get("slurm") or {}
    root = _job_root(site, rec["payload"].get("target"))
    site_logs = wi_log_dir(site, wi)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    cmd = list(argv) + list(argv_extra or [])
    record_path = _via_path(root, record, site) if record else None
    if sub.get("record_arg"):
        record_path = record_path or str(site_logs / f"via-{stamp}.json")
        cmd += [sub["record_arg"], record_path]
    if sub.get("log_arg"):
        cmd += [sub["log_arg"], str(site_logs)]
    for jid in _dep_job_ids(deps):
        cmd += [sub["after_arg"], jid]
    payload = {"run_type": "slurm", "status": "not-run" if dry_run else "submitted", "command": cmd,
               "chdir": str(root), "logs": str(site_logs), "sbatch_args": [],
               "via": {"command": argv, "text": _via_text(argv), "module": mod_id, "submit": sub,
                       "record": record_path, "added": cmd[len(argv):]},
               **(extra_payload or {})}
    if deps:
        payload["after"] = deps
    if dry_run:
        return led.append("run", "slurm", actor, wi, payload)
    site_logs.mkdir(parents=True, exist_ok=True)
    timeout = cfg.get("via_timeout") or VIA_TIMEOUT_DEFAULT
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        fail(f"config slurm.via_timeout must be a number of seconds: {timeout!r}")
    failure = out = err = None
    try:
        proc = subprocess.run(cmd, cwd=root, env=site.env(), capture_output=True, text=True, timeout=timeout)
        out, err, code = proc.stdout, proc.stderr, proc.returncode
        if code != 0:
            failure = f"exit {code}"
    except FileNotFoundError:
        code, failure = None, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired as exc:
        code, failure = None, f"no exit after {timeout:g} s (config slurm.via_timeout)"
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    if failure:
        tail = ((err or "").strip() or (out or "").strip())[-2000:]
        ev = led.event("slurm.via-failed", actor, wi, {"command": cmd, "chdir": str(root), "exit": code,
                                                       "failure": failure, "stderr_tail": (err or "")[-2000:],
                                                       "stdout_tail": (out or "")[-2000:], "module": mod_id})
        fail(f"--via command failed ({failure}; recorded as event {ev['id']}, no run): {tail or 'no output'}")
    if record_path is None:
        found = VIA_RECORD_LINE.findall(out or "")
        if not found:
            fail("--via: the command exited 0 but named no submission record: pass --record <path>, set config "
                 f"modules[<m>].submit.record_arg, or make it print `submission-record: <path>` as its last line "
                 f"(stdout tail: {(out or '').strip()[-400:]!r})")
        record_path = _via_path(root, found[-1], site)
    if not os.path.isfile(record_path):
        fail(f"--via: submission record {record_path} does not exist after the command exited 0")
    ingested = ingest_submission(Path(record_path), root, site)
    jobs = ingested["jobs"]
    first = jobs[0]
    payload.update(job_id=first["job_id"], jobs=jobs, log_out=first.get("log_out"), log_err=first.get("log_err"))
    if first.get("script"):
        payload["script"] = first["script"]
        if os.path.isfile(first["script"]):
            payload["script_sha256"] = sha256_file(first["script"])
    arrays = [j for j in jobs if j["kind"] == "array"]
    if arrays:  # decision 3: `array` is the first array job; every job is waited for
        a = arrays[0]
        payload["array"] = {"job_id": a["job_id"], "items": list(a["items"]), "count": a["count"], "level": a["level"],
                            "items_file": None}
        if a.get("max_parallel"):
            payload["max_parallel"] = a["max_parallel"]
        if not a.get("level_declared"):  # the module owns the loop: not refused, but its level should be declared
            note = (f"via: the submission record declares no item level for array job {a['job_id']} (recorded as "
                    f"{a['level']}); the module should say `level: file` (workflows loop at file level, Director 2026-10-06)")
            payload.setdefault("warnings", []).append(note)
            print(f"warning: {note}", file=sys.stderr)
    payload["level"] = payload["array"]["level"] if arrays else ("group" if deps else "single")
    payload["via"].update(record=record_path, schema_version=ingested["schema_version"], tool=ingested["tool"],
                          tool_version=ingested["tool_version"], submitted_at=ingested["submitted_at"],
                          cwd=ingested["cwd"], stdout_tail=(out or "")[-1000:])
    for key in ("manifest", "selection", "records"):
        if ingested.get(key) is not None:
            payload["via"][key] = ingested[key]
    if ingested["extra"]:
        payload["via"]["extra"] = ingested["extra"]
    check = _via_day_check(site, payload, cfg, wi=wi)
    if check is not None:
        payload["start_check"] = check
    return led.append("run", "slurm", actor, wi, payload)


def _via_summary(run: dict, lines: dict[str, list[str]]) -> dict:
    """State of a submission that holds several jobs (a via run), from batched `sacct` lines grouped by job id (see
    `sacct_lines`): per-job states, per-item states of the recorded `array` (the first array job), the overall state
    (COMPLETED only when every task of every job completed; FAILED when all ended and any did not complete; else
    RUNNING or PENDING) and `final` once every job has ended."""
    p = run["payload"]
    first_array = str((p.get("array") or {}).get("job_id") or "")
    by_job: dict[str, str] = {}
    items: dict[str, str] = {}
    all_states: list[str] = []
    for job in p.get("jobs") or []:
        jid = str(job["job_id"])
        if job.get("kind") == "array":
            count = int(job.get("count") or len(job.get("items") or []))
            by_idx = _parse_array_lines(jid, lines.get(jid, []), count)
            states = list(by_idx.values())
            if jid == first_array and not items:
                names = p["array"]["items"]
                items = {names[i]: st for i, st in by_idx.items() if i < len(names)}
            by_job[jid] = overall_state(states)
        else:
            own = [ln for ln in lines.get(jid, []) if ln.split("|")[0] == jid] or lines.get(jid, [])
            raw = "|".join(own[0].split("|")[1:]) if own else ""
            states = [(raw.split("|")[0].split() or ["UNKNOWN"])[0] if raw else "UNKNOWN"]
            by_job[jid] = states[0]
        all_states += states
    counts: dict[str, int] = {}
    for st in items.values():
        counts[st] = counts.get(st, 0) + 1
    return {"scheduler_state": overall_state(all_states), "items": items, "counts": counts,
            "final": bool(all_states) and all(st in TERMINAL_STATES for st in all_states), "jobs": by_job}


def _via_status(site: Site, run: dict) -> dict:
    return _via_summary(run, sacct_lines(site, [str(j["job_id"]) for j in run["payload"]["jobs"]]))


def array_states(site: Site, job: str, count: int) -> dict[int, str]:
    """Per-task state of an array job from `sacct -j <job> -X -P -n --format=JobID,State,ExitCode`: task
    lines `<job>_<i>|STATE|x:y`, pending ranges `<job>_[0-3,7%2]|PENDING|0:0`. Missing tasks are UNKNOWN."""
    proc = subprocess.run(["sacct", "-j", job, "-X", "-P", "-n", "--format=JobID,State,ExitCode"],
                          capture_output=True, text=True, timeout=60, env=site.env())
    return _parse_array_lines(job, proc.stdout.splitlines(), count)


def _parse_array_lines(job: str, lines: list[str], count: int) -> dict[int, str]:
    """Per-task states from `sacct` lines whose first two fields are JobID and State (see `array_states`)."""
    states: dict[int, str] = {}
    for line in lines:
        parts = line.strip().split("|")
        if len(parts) < 2 or not parts[0].startswith(f"{job}_") or "." in parts[0]:
            continue
        state = (parts[1].split() or ["UNKNOWN"])[0]
        idx = parts[0][len(job) + 1:]
        if idx.isdigit():
            states[int(idx)] = state
        elif idx.startswith("[") and idx.endswith("]"):
            for rng in idx[1:-1].split("%")[0].split(","):
                a, _, b = rng.partition("-")
                if a.isdigit() and (not b or b.isdigit()):
                    for i in range(int(a), int(b or a) + 1):
                        states.setdefault(i, state)
    return {i: states.get(i, "UNKNOWN") for i in range(count)}


def overall_state(states: list[str]) -> str:
    """COMPLETED only if every task completed; FAILED when any task ended otherwise and none is still
    pending, running or unknown; else RUNNING (any task running) or PENDING."""
    if states and all(s == "COMPLETED" for s in states):
        return "COMPLETED"
    if all(s in TERMINAL_STATES for s in states):
        return "FAILED"
    return "RUNNING" if any(s not in TERMINAL_STATES and s not in ("PENDING", "UNKNOWN") for s in states) \
        else "PENDING"


def _array_status(site: Site, run: dict) -> dict:
    arr = run["payload"]["array"]
    return _array_summary(run, array_states(site, str(arr["job_id"]), arr["count"]))


def _array_summary(run: dict, by_idx: dict[int, str]) -> dict:
    arr = run["payload"]["array"]
    items = {arr["items"][i]: s for i, s in by_idx.items()}
    state = overall_state(list(items.values()))
    counts: dict[str, int] = {}
    for s in items.values():
        counts[s] = counts.get(s, 0) + 1
    return {"scheduler_state": state, "items": items, "counts": counts,
            "final": all(s in TERMINAL_STATES for s in items.values())}


def _record_array(site: Site, led: Ledger, actor: str, run: dict, st: dict, waited: bool, watched: bool = False) -> dict:
    """Record an array's state. Per-item final states are recorded once: when all tasks are terminal and the
    same final states are already recorded, that record is returned instead of a new one."""
    rid = run["id"]
    multi = "jobs" in st  # a via submission: per-job states beside the first array's per-item states
    rec_items = st["items"] if st["items"] or not multi else None
    if st["final"]:
        for e in reversed(led.events(run["wi"])):
            if e["type"] == "run.status" and e["payload"].get("run") == rid and \
                    (e["payload"].get("item_states") or (multi and e["payload"].get("final"))):
                if e["payload"].get("item_states") == rec_items and (not multi or e["payload"].get("jobs") == st["jobs"]):
                    return e
                break
    payload = {"run": rid, "job_id": run["payload"]["job_id"], "scheduler_state": st["scheduler_state"],
               "items": st["items"], "counts": st["counts"], "final": st["final"]}
    if multi:
        payload["jobs"] = st["jobs"]
    if waited:
        payload["waited"] = True
    if watched:
        payload["watched"] = True
    if st["final"]:
        if rec_items is not None:
            payload["item_states"] = rec_items
        failed = [i for i, s in st["items"].items() if s != "COMPLETED"]
        if failed:
            payload["failed_items"] = failed
        if multi:
            bad = [j for j, s in st["jobs"].items() if s != "COMPLETED"]
            if bad:
                payload["failed_jobs"] = bad
        mod = run["payload"].get("module") or {}
        if mod.get("tree"):  # array tasks share one pinned tree: removed once every task has ended
            from . import exectree

            exectree.cleanup(site.relocate(mod["tree"]))  # recorded at submit time; the site may have moved since
    ev = led.event("run.status", actor if actor in ("pipeline-runner", "desk") else "desk", run["wi"], payload, [rid])
    return ev


def job_state(site: Site, job: str) -> tuple[str, str]:
    """(state, raw line) of one job from `sacct -n -P -X -j <job> --format=State,ExitCode,Elapsed`;
    ("UNKNOWN", "") when accounting has nothing yet."""
    proc = subprocess.run(["sacct", "-n", "-P", "-X", "-j", job, "--format=State,ExitCode,Elapsed"],
                          capture_output=True, text=True, timeout=60, env=site.env())
    line = (proc.stdout.strip().splitlines() or [""])[0]
    return (line.split("|")[0] if line else "UNKNOWN"), line


def wait_job(site: Site, job: str, timeout: int, interval: int) -> tuple[str, str, bool]:
    """Poll one job until a terminal state or the timeout: (state, raw line, final)."""
    import time

    deadline = time.monotonic() + max(0, timeout)
    while True:
        state, line = job_state(site, job)
        if state.split()[0] in TERMINAL_STATES or time.monotonic() >= deadline:
            break
        time.sleep(max(1, min(interval, deadline - time.monotonic())))
    return state, line, state.split()[0] in TERMINAL_STATES


def _log_hint(site: Site, run: dict) -> dict:
    """Where the run's scheduler logs are (printed with status/wait; not recorded again): the recorded
    log_out/log_err, under the site's live root when it moved since, with %j/%x (and %A for an array) filled in."""
    from .workflow import _log_name

    p = run["payload"]
    job = str(p.get("job_id") or "").split(";")[0].strip()
    first = (p.get("jobs") or [{}])[0]
    name = first.get("name") or p.get("job_name") or (_scan_opt([str(w) for w in p.get("command") or []], "-J",
                                                                "--job-name") if not p.get("via") else None) \
        or f"dureh-{run['wi']}"  # records before release 44 always named the job dureh-<WI>
    out = {}
    for key in ("log_out", "log_err"):
        if p.get(key):
            rec = str(site.relocate(p[key]))
            pat = rec.replace("%A", "%j") if p.get("array") else rec
            out[key] = (_log_name(pat.replace("%a", "%%a"), job, name) or rec) if job else rec
    return out


def slurm_status(site: Site, actor: str, run_id: str) -> dict:
    led = Ledger(site)
    run = _get_run(led, run_id)
    job = run["payload"].get("job_id")
    if not job:
        fail(f"{run_id} has no scheduler job id")
    if run["payload"].get("jobs"):
        ev = _record_array(site, led, actor, run, _via_status(site, run), waited=False)
    elif run["payload"].get("array"):
        ev = _record_array(site, led, actor, run, _array_status(site, run), waited=False)
    else:
        state, line = job_state(site, job)
        ev = led.event("run.status", actor if actor in ("pipeline-runner", "desk") else "desk", run["wi"],
                       {"run": run_id, "job_id": job, "scheduler_state": state, "raw": line}, [run_id])
    return {**ev, "logs": _log_hint(site, run)}


TERMINAL_STATES = ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED",
                   "BOOT_FAIL", "DEADLINE")


def slurm_wait(site: Site, actor: str, run_id: str, timeout: int = 3600, interval: int = 60) -> dict:
    """Check a job until it reaches a terminal state or the timeout; record only the final state. For an
    array (`--each`): until every task is terminal; the record has per-item states and the overall state."""
    import time

    led = Ledger(site)
    run = _get_run(led, run_id)
    job = run["payload"].get("job_id") or fail(f"{run_id} has no scheduler job id")
    deadline = time.monotonic() + max(0, timeout)
    if run["payload"].get("array") or run["payload"].get("jobs"):
        probe = _via_status if run["payload"].get("jobs") else _array_status
        while True:
            st = probe(site, run)
            if st["final"] or time.monotonic() >= deadline:
                break
            time.sleep(max(1, min(interval, deadline - time.monotonic())))
        ev = _record_array(site, led, actor, run, st, waited=True)
        if not st["final"]:
            ev["note"] = f"still {st['scheduler_state']} ({st['counts']}) after {timeout} s; wait again or check later"
        return {**ev, "logs": _log_hint(site, run)}
    state, line, final = wait_job(site, job, int(max(0, deadline - time.monotonic())), interval)
    ev = led.event("run.status", actor if actor in ("pipeline-runner", "desk") else "desk", run["wi"],
                   {"run": run_id, "job_id": job, "scheduler_state": state, "raw": line,
                    "waited": True, "final": final}, [run_id])
    if not final:
        ev["note"] = f"still {state} after {timeout} s; wait again or check later"
    return {**ev, "logs": _log_hint(site, run)}


def _watch_line(led: Ledger, events: list[dict], run: dict, seen: dict, nxt: str | None = None) -> str:
    """One line per ended job: run, job, WI, owner role and agent, state, and what the desk does next."""
    wi = run["wi"]
    rec = led.get("work-item", wi)
    role = rec["payload"]["role"]
    bound = [e for e in events if e["type"] == "agent.started" and e["wi"] == wi]
    agent = bound[-1]["payload"].get("agent_id") if bound else None
    agent_s = f"{str(agent)[:8]}:{agent_state(led, wi, events)}" if agent else "none"
    st = led.wi_status(wi)["status"]
    parent = rec["payload"].get("parent")
    if nxt is not None:
        pass
    elif run["payload"].get("run_type") == "verify":  # release 37: the checker collects a submitted verify job
        collect = f"checker collects verify run {run['id']}"
        if st != "staged":
            nxt = f"none (WI {st})"
        elif parent and agent_state(led, parent, events) in LIVE_AGENT:
            nxt = f"the manager of bundle {parent} dispatches the checker for {wi} ({collect})"
        elif parent:  # release 45 (6h): the desk dispatches the checker itself; the manager is only for rework
            nxt = f"dispatch brief --wi {wi} --role checker ({collect}; bundle {parent}, its manager is not needed)"
        else:
            nxt = f"dispatch brief --wi {wi} --role checker ({collect})"
    elif agent_s.endswith(":running") or agent_s.endswith(":starting"):
        nxt = "agent still running; nothing to do"
    elif st not in ("dispatched", "rework"):
        nxt = f"none (WI {st})"
    elif parent and agent_state(led, parent, events) in LIVE_AGENT:
        nxt = f"the manager of bundle {parent} re-dispatches {wi}"
    elif parent:
        nxt = f"dispatch brief --wi {parent} (bundle; its manager re-dispatches {wi})"
    else:
        nxt = f"dispatch brief --wi {wi}"
    extra = ""
    if seen.get("array"):
        counts = seen["array"]["counts"]
        extra = " items=" + ",".join(f"{k}:{v}" for k, v in sorted(counts.items()))
    from .util import utc_now

    return (f"{utc_now()[:19]}Z run={run['id']} job={run['payload']['job_id']} wi={wi} role={role} agent={agent_s}"
            f" state={seen['state']}{extra} next={nxt}")


def watch_round(site: Site, actor: str, reported: set[str]) -> list[str]:
    """One pass of `slurm watch`: every tracked run not yet known to have ended is probed with ONE batched
    `sacct` call; a terminal state is recorded once (`run.status` with final and watched; not in observe mode)
    and its line returned. Runs whose final state is already recorded (by `wait`, `status` or an earlier watch)
    are not reported. A run older than `watch_stale_days` for which sacct has no state at all is recorded once
    as stale (UNKNOWN, final) and reported for a check by hand; it is not probed again. A run sacct reports as
    pending, running, requeued, suspended ... is never stale."""
    led = Ledger(site)
    events = led.events()
    recorded = _recorded_states(events)
    todo = []
    for run in _tracked_runs(led):
        if run["id"] in reported:
            continue
        state, final = recorded.get(run["id"], ("UNKNOWN", False))
        if final or state in TERMINAL_STATES:
            reported.add(run["id"])
            continue
        todo.append(run)
    probed = _probe_runs(site, todo)
    days = site.limit("watch_stale_days", 7)
    lines = []
    for run in todo:
        seen = probed.get(run["id"])
        if seen is None or not seen["final"]:
            if not (_no_state(seen) and _stale(site, run)):
                continue  # still pending/running (however old), or the scheduler could not be asked
            if not site.observe:
                led.event("run.status", actor, run["wi"], {"run": run["id"], "job_id": run["payload"]["job_id"],
                                                           "scheduler_state": "UNKNOWN", "final": True, "stale": True,
                                                           "watched": True}, [run["id"]])
            reported.add(run["id"])
            lines.append(_watch_line(led, events, run, {"state": "UNKNOWN"},
                                     nxt=f"check by hand (no scheduler state after {days:g} days)"))
            continue
        if not site.observe:
            if seen.get("multi") or seen.get("array"):
                _record_array(site, led, actor, run, seen.get("multi") or seen["array"], waited=False, watched=True)
            else:
                led.event("run.status", actor, run["wi"], {"run": run["id"], "job_id": run["payload"]["job_id"],
                                                           "scheduler_state": seen["state"], "raw": seen.get("raw", ""),
                                                           "final": True, "watched": True}, [run["id"]])
        reported.add(run["id"])
        lines.append(_watch_line(led, events, run, seen))
    return lines


def slurm_watch(site: Site, actor: str, interval: int = 300, once: bool = False, out=None,
                rounds: int | None = None) -> list[str]:
    """Print one line per tracked job that ends (for the desk's Monitor); poll every `interval` seconds until
    stopped, or one pass with `once`. Idempotent across restarts: what is recorded is not reported again."""
    import time

    if site.org.role(actor).kind != "desk":
        fail("only the desk watches jobs; after `slurm submit` end your turn: the desk re-dispatches you when the job ends")
    out = out or sys.stdout
    reported: set[str] = set()
    printed, n = [], 0
    while True:
        for line in watch_round(site, actor, reported):
            print(line, file=out, flush=True)
            printed.append(line)
        n += 1
        if once or (rounds is not None and n >= rounds):
            return printed
        time.sleep(max(1, interval))


def slurm_resubmit_failed(site: Site, actor: str, run_id: str, script: str | None = None, dry_run: bool = False,
                          extra: list[str] | None = None, module: str | None = None, rev: str | None = None,
                          after: list[str] | None = None, max_parallel: int | None = None,
                          wi: str | None = None) -> dict:
    """A new array over the items of an `--each` run whose task did not complete (same script, sbatch
    arguments and module revision unless overridden); refused while any task is pending or running."""
    led = Ledger(site)
    run = _get_run(led, run_id)
    p = run["payload"]
    if wi is not None and wi != run["wi"]:
        fail(f"{run_id} belongs to {run['wi']}, not {wi}")
    if p.get("via"):
        for name, value in (("--script", script), ("--sbatch-arg", extra), ("--module", module), ("--rev", rev),
                            ("--max-parallel", max_parallel)):
            if value not in (None, []):
                fail(f"{run_id} was submitted --via a module command; {name} does not apply (the module renders the job)")
        return _resubmit_via(site, actor, run, dry_run, after)
    arr = p.get("array")
    if not arr:
        fail(f"{run_id} is not an --each (array) run")
    if not p.get("job_id"):
        fail(f"{run_id} has no scheduler job id")
    by_idx = array_states(site, str(p["job_id"]), arr["count"])
    waiting = {arr["items"][i]: s for i, s in by_idx.items() if s not in TERMINAL_STATES}
    if waiting:
        fail(f"{run_id}: not every task has ended ({', '.join(f'{k} {v}' for k, v in list(waiting.items())[:5])}); "
             f"`slurm wait --run {run_id}` first")
    failed = [arr["items"][i] for i, s in by_idx.items() if s != "COMPLETED"]
    if not failed:
        fail(f"{run_id}: every item completed; nothing to resubmit")
    mod = p.get("module") or {}
    if module is None and mod.get("name"):
        module, rev = mod["name"], rev or mod.get("rev")
    states = {arr["items"][i]: s for i, s in by_idx.items()}
    recorded_script = str(site.relocate(p["script"])) if p.get("script") else None  # as it is now, after any move
    return slurm_submit(site, actor, run["wi"], script or recorded_script, dry_run,
                        extra if extra is not None else p.get("sbatch_args"),
                        {"resubmit_of": run_id, "resubmit_states": {i: states[i] for i in failed}},
                        module=module, rev=rev, after=after, items=failed,
                        max_parallel=max_parallel if max_parallel is not None else p.get("max_parallel"))


def _resubmit_via(site: Site, actor: str, run: dict, dry_run: bool, after: list[str] | None) -> dict:
    """`--resubmit-failed` on a via run: the items of its array job(s) whose task did not complete go to a file under
    the WI's log dir and the recorded module command runs again with `modules[<m>].submit.items_arg @<file>`
    (recorded `resubmit_of`); refused while any job is pending or running. Without a configured items_arg the
    refusal lists the failed items, the file and the command to rerun by hand."""
    from datetime import datetime, timezone

    p, rid = run["payload"], run["id"]
    arrays = [j for j in p.get("jobs") or [] if j.get("kind") == "array"]
    if not arrays:
        fail(f"{rid}: the submission holds no array job, so there are no items to resubmit")
    lines = sacct_lines(site, [str(j["job_id"]) for j in p["jobs"]])
    st = _via_summary(run, lines)
    if not st["final"]:
        waiting = {j: s for j, s in st["jobs"].items() if s not in TERMINAL_STATES}
        fail(f"{rid}: not every job has ended ({', '.join(f'{k} {v}' for k, v in list(waiting.items())[:5])}); "
             f"`slurm wait --run {rid}` first")
    failed: list[str] = []
    states: dict[str, str] = {}
    for j in arrays:
        by_idx = _parse_array_lines(str(j["job_id"]), lines.get(str(j["job_id"]), []), int(j["count"]))
        for i, state in by_idx.items():
            item = j["items"][i]
            states.setdefault(item, state)
            if state != "COMPLETED" and item not in failed:
                failed.append(item)
    if not failed:
        fail(f"{rid}: every item completed; nothing to resubmit")
    via = p["via"]
    mod_id, sub = via_module_config(site, list(via["command"]), via.get("module"))
    site_logs = wi_log_dir(site, run["wi"])
    site_logs.mkdir(parents=True, exist_ok=True)
    items_file = site_logs / f"via-failed-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.items"
    items_file.write_text("".join(f"{i}\n" for i in failed), encoding="utf-8")
    if not sub.get("items_arg"):
        where = f"config modules[{mod_id}].submit" if mod_id else f"no config modules[] entry matches {via['command'][0]!r}"
        fail(f"{rid}: {len(failed)} item(s) did not complete: {', '.join(failed[:20])}{' ...' if len(failed) > 20 else ''} "
             f"(list: {items_file}). {where} has no items_arg, so the harness cannot hand them to the module; rerun by hand: "
             f"slurm submit --wi {run['wi']} --via '{via['text']} <the module's item option> @{items_file}'")
    return slurm_submit_via(site, actor, run["wi"], list(via["command"]), after=after, dry_run=dry_run,
                            via_module=via.get("module"),
                            extra_payload={"resubmit_of": rid, "resubmit_states": {i: states[i] for i in failed},
                                           "resubmit_items_file": str(items_file)},
                            argv_extra=[sub["items_arg"], f"@{items_file}"])


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


def _day_check(site: Site, cmd: list[str], script: Path, extra: list[str], cfg: dict, wi: str | None = None):
    """In workday mode, during an on-duty day: refuse a job whose expected start (`sbatch --test-only`)
    plus its requested --time ends after today's end. None when not checked, "unavailable" when the
    scheduler gave no usable answer (the job is submitted as usual). Finish-plan rule (Director, 2026-10-06):
    a job for a plan item or a fix for one is submitted anyway unless a hard stop holds; the record says
    `past_end: true`, `rule: finish-plan`, and the overtime is recorded as the gate does (`note_finish_overtime`).
    A WI in a Director overtime record of today (`day overtime`, pre-approved on duty or after the report;
    UR-20261006-01) is submitted too, `rule: director-overtime`, with no automatic record and no stop (its
    budget treatment is the gate's). One predicate decides for both, `workday.past_end_rule`."""
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
    array = any(str(w).startswith("--array=") for w in cmd)
    what = "array's first task" if array else "job"
    out = {"expected_start": start.isoformat(timespec="seconds"), "expected_end": ends.isoformat(timespec="seconds"),
           "day_end": d["end"], "time": requested, "time_source": source,
           **({"per_task": True, "note": "one --test-only for the array: its first task's expected start plus the "
               "per-task --time; tasks that wait for resources start later"} if array else {})}
    if ends > day_end:
        head = (f"workday mode: this {what} is expected to start at {start.isoformat(timespec='seconds')} and, with "
                f"--time {requested} (from the {source}{', per task' if array else ''}), end at "
                f"{ends.isoformat(timespec='seconds')}, after today's end {d['end']}.")
        d = workday.plan_open(site, d)
        rule = workday.past_end_rule(site, wi, d)
        if not rule:
            fail(f"{head} Nothing was submitted; {workday.way_out(site, wi, d, time_hint=f'{day_end:%H:%M}')}")
        name, reason = rule
        if name == workday.FINISH_RULE:
            stop = workday.hard_stop(site, d)
            if stop:
                fail(f"{head} It is plan work, but a stop holds ({stop['why']}). Nothing was submitted; finish running "
                     "work and write the end report (`dureh day end`).")
            if not site.observe:
                workday.note_finish_overtime(site, d, wi)  # the harness records the overtime itself, once per item
        print(f"warning: workday: this job ends at {ends:%H:%M}, after today's end {day_end:%H:%M}; allowed as "
              + ("plan work (Director rule 2026-10-06)" if name == workday.FINISH_RULE else reason), file=sys.stderr)
        out.update(past_end=True, rule=name, allowed=reason)
    return out


def day_jobs(site: Site, since: tuple[str, str] | None = None) -> dict:
    """The end report's `jobs` section (Director 2026-10-06: 각 job에 대해 몇 개의 job으로 수행했는지, 각 job ID와 수행 command):
    every scheduler run recorded for the day (after the day plan's record `since` = (ts, id), in ledger order; all
    runs without it): `slurm submit` jobs and arrays, `--via` submissions, `verify --submit` jobs. Per run: WI,
    kind, step (script) or via command, level, tasks and jobs, every job id (an array as its id with the task range
    plus each task id), the exact command recorded at submit time, the state summary from the recorded states
    (`slurm watch|status|wait`; the scheduler is not asked here), log dir. `line` is the Slack summary line."""
    import shlex

    led = Ledger(site)
    runs = [r for r in led.load("run") if r["payload"].get("run_type") in ("slurm", "verify") and r["payload"].get("job_id")
            and (since is None or (r["ts"], r["id"]) > tuple(since))]
    latest: dict[str, dict] = {}
    for e in led.events():
        if e["type"] == "run.status" and e["payload"].get("run"):
            latest[e["payload"]["run"]] = e["payload"]
    out, t_tasks, t_jobs, t_failed = [], 0, 0, 0
    for r in runs:
        p, st = r["payload"], latest.get(r["id"]) or {}
        via, arr, jobs = p.get("via"), p.get("array"), p.get("jobs") or []
        if via:
            kind, step = "via", via.get("text") or " ".join(via.get("command") or [])
        else:
            kind = "verify" if p["run_type"] == "verify" else "slurm"
            step = os.path.basename(p["script"]) if p.get("script") else None
        cmd = p.get("command")
        command = shlex.join(str(c) for c in cmd) if isinstance(cmd, list) else cmd
        ids: list[str] = []
        if jobs:
            for j in jobs:
                jid = str(j["job_id"])
                if j.get("kind") == "array" and j.get("count"):
                    ids += [f"{jid}_[0-{j['count'] - 1}]", *(f"{jid}_{i}" for i in range(j["count"]))]
                else:
                    ids.append(jid)
            n_jobs, tasks = len(jobs), sum(j.get("count") or 1 for j in jobs)
        elif arr:
            jid, n = str(p["job_id"]), int(arr.get("count") or len(arr.get("items") or []))
            ids = [f"{jid}_[0-{n - 1}]", *(f"{jid}_{i}" for i in range(n))]
            n_jobs, tasks = 1, n
        else:
            ids, n_jobs, tasks = [str(p["job_id"])], 1, 1
        counts: dict[str, int] = {}
        if st.get("counts"):
            for k, v in st["counts"].items():
                k = str(k).split()[0] if k else "UNKNOWN"
                counts[k] = counts.get(k, 0) + int(v)
        elif st.get("scheduler_state"):
            counts[str(st["scheduler_state"]).split()[0]] = tasks
        completed, cancelled = counts.get("COMPLETED", 0), counts.get("CANCELLED", 0)
        failed = sum(v for k, v in counts.items() if k in TERMINAL_STATES and k not in ("COMPLETED", "CANCELLED"))
        other = tasks - completed - cancelled - failed
        level = p.get("level") or (arr or {}).get("level") or ("group" if p.get("after") else "single")
        out.append({"run": r["id"], "wi": r["wi"], "kind": kind, "step": step, "level": level, "reason": p.get("reason"),
                    "tasks": tasks, "jobs": n_jobs, "job_ids": ids, "command": command,
                    "state": st.get("scheduler_state") or ("not-run" if p.get("status") == "not-run" else "no state recorded"),
                    "states": {"completed": completed, "failed": failed, "cancelled": cancelled, "other": max(other, 0)},
                    "logs": p.get("logs"), "submitted": r["ts"]})
        t_tasks, t_jobs, t_failed = t_tasks + tasks, t_jobs + n_jobs, t_failed + failed
    return {"runs": out, "count": len(out), "tasks": t_tasks, "jobs": t_jobs, "failed": t_failed,
            "line": f"jobs today: {len(out)} runs, {t_tasks} tasks, {t_failed} failed"}


LOG_DIR_DEFAULT = "logs/slurm"


def log_dir(site: Site) -> Path:
    """Where scheduler stdout/stderr go: config `slurm.log_dir` (default `logs/slurm`, relative to the site root;
    an absolute path only inside the site). Never the company root itself or outside the site."""
    raw = (site.config.get("slurm") or {}).get("log_dir") or LOG_DIR_DEFAULT
    p = Path(site.expand(str(raw)))
    p = p if p.is_absolute() else site.root / p
    real, root = os.path.realpath(p), os.path.realpath(site.root)
    if not real.startswith(root + os.sep):
        fail(f"config slurm.log_dir {raw!r} must name a directory inside the site (below {site.root}), not the site root"
             " or a path outside it")
    return Path(os.path.normpath(p))


def wi_log_dir(site: Site, wi: str) -> Path:
    return log_dir(site) / wi


def _inside(path: str, root: Path) -> bool:
    real, r = os.path.realpath(path), os.path.realpath(root)
    return real == r or real.startswith(r + os.sep)


def _redirect_log(site: Site, folder: Path, value: str, flag: str, source: str, is_array: bool) -> tuple[str, list[str]]:
    """The rule (release 32): an absolute name already inside the site's log dir is kept; any other name (relative,
    absolute elsewhere) keeps only its basename pattern inside `<log_dir>/<WI>/`, with a warning. An array name
    without %a gets `_%a` before its extension and %j becomes %A (warned)."""
    warns = []
    if os.path.isabs(value) and "%" not in os.path.dirname(value) and _inside(os.path.dirname(value), log_dir(site)):
        out = value
    else:
        out = str(folder / (os.path.basename(value.rstrip("/")) or ("%x-%A_%a" if is_array else "%x-%j") + ".log"))
        warns.append(f"{flag} {value} ({source}) is not in the site's log dir; scheduler logs go to {out} instead"
                     " (do not set --output/--error in job scripts)")
    if is_array and "%a" not in os.path.basename(out):
        # a task's %j is its own job id, which no record holds: %A (the array's) + _%a names each task's file
        base = os.path.basename(out)
        stem, dot, ext = base.rpartition(".") if "." in base else (base, "", "")
        new = f"{stem.replace('%j', '%A')}_%a{dot}{ext}"
        out = os.path.join(os.path.dirname(out), new)
        warns.append(f"{flag} name {base} holds no %a: array tasks would share one file; using {new}")
    return out, warns


def job_log_paths(site: Site, folder: Path, script: Path, extra: list[str], is_array: bool) -> tuple[str, str, list[str]]:
    """(stdout path, stderr path, warnings) for a job: always inside `<log_dir>/<WI>/` (see `_redirect_log`).
    Without a name the defaults are `%x-%j` (`%x-%A_%a` for an array); a job that names only --output keeps
    stderr in the same file, as sbatch does."""
    pat = "%x-%A_%a" if is_array else "%x-%j"
    warns: list[str] = []
    paths = {}
    for short, long, ext in (("-o", "--output", "out"), ("-e", "--error", "err")):
        arg = _scan_opt(extra, short, long)
        value, source = (arg, "sbatch argument") if arg is not None else (_script_opt(script, short, long), "#SBATCH directive")
        if value is None:
            paths[long] = None
            continue
        paths[long], w = _redirect_log(site, folder, value, long, source, is_array)
        warns += w
    out = paths["--output"] or str(folder / f"{pat}.out")
    err = paths["--error"] or (out if paths["--output"] else str(folder / f"{pat}.err"))
    return out, err, warns


def slurm_cancel(site: Site, actor: str, run_id: str) -> dict:
    led = Ledger(site)
    run = led.get("run", run_id)
    job = run["payload"].get("job_id") or fail(f"{run_id} has no job id")
    ids = [str(j["job_id"]) for j in run["payload"].get("jobs") or []] or [str(job)]  # a via submission: every job
    proc = subprocess.run(["scancel", *ids], capture_output=True, text=True, timeout=60, env=site.env())
    return led.event("run.status", actor, run["wi"], {"run": run_id, "job_id": job, "scheduler_state": "CANCEL-REQUESTED",
                                                      "scancel_exit": proc.returncode,
                                                      **({"jobs": ids} if len(ids) > 1 else {}),
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
    # after a move or rename: the same path as the site's earlier roots spelled it (records, scripts, notes written
    # before the move) and, for a path given under an old root, as it is now
    given = Path(target.rstrip("/") or target)
    if given.is_absolute():
        now = site.relocate(given)
        if is_within(now, site.root):
            rel = now.relative_to(site.root)
            needles.update(str(r / rel) for r in [site.root, *site.previous_roots()])
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
