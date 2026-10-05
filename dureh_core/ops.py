"""Operational tools: status, slurm, push, path-refs, usage."""
from __future__ import annotations

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
from .util import DurehError, fail, git, sha256_file

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
            action = (f"if no agent is starting, re-dispatch with `dispatch brief --wi {wi}` (allowed once the dispatch is"
                      f" {int(led.site.limit('dispatch_start_grace_seconds', 120))} s old; it supersedes the one that never"
                      f" started) or release with `dispatch stop --wi {wi} --reason ...`")
        elif running:
            action = f"wait for {', '.join(running)}, or re-adopt with `dispatch brief --wi {wi}`"
        else:
            action = f"re-adopt with `dispatch brief --wi {wi}`, or release with `dispatch stop --wi {wi} --reason ...`"
        out.append({"wi": wi, "role": led.get("work-item", wi)["payload"]["role"], "agent": state[wi],
                    "children_running": running, "action": action})
    return out


LIVE_AGENT = ("running", "starting")


def _recorded_states(events: list[dict]) -> dict[str, tuple[str, bool]]:
    """Latest recorded scheduler state per run id from run.status events: {run: (STATE, final)}."""
    out: dict[str, tuple[str, bool]] = {}
    for e in events:
        if e["type"] == "run.status" and e["payload"].get("run"):
            state = (e["payload"].get("scheduler_state") or "").split()
            out[e["payload"]["run"]] = (state[0] if state else "UNKNOWN", bool(e["payload"].get("final")))
    return out


def _tracked_runs(led: Ledger) -> list[dict]:
    """Runs submitted through `slurm submit` (single jobs and `--each` arrays) with a scheduler job id."""
    return [r for r in led.load("run") if r["payload"].get("run_type") == "slurm" and r["payload"].get("job_id")
            and r["payload"].get("status") == "submitted"]


def _probe_run(site: Site, run: dict) -> dict | None:
    """Ask the scheduler for a run's state: {state, final, raw | items, counts}; None when it cannot be asked."""
    try:
        if run["payload"].get("array"):
            st = _array_status(site, run)
            return {"state": st["scheduler_state"], "final": st["final"], "array": st}
        state, line = job_state(site, str(run["payload"]["job_id"]))
        return {"state": state.split()[0] if state else "UNKNOWN", "final": state.split()[0] in TERMINAL_STATES, "raw": line}
    except (OSError, subprocess.SubprocessError):
        return None


def live_job_runs(site: Site, led: Ledger, wis: set[str], events: list[dict], probe: bool = True) -> list[dict]:
    """Tracked runs of these WIs whose job is not known to have ended: [{run, job_id, wi, state}]. A run with a
    recorded terminal state has ended; otherwise the scheduler is asked (`probe`), and a run it cannot report
    counts as live until a state is recorded."""
    recorded = _recorded_states(events)
    out = []
    for run in _tracked_runs(led):
        if run["wi"] not in wis:
            continue
        state, final = recorded.get(run["id"], ("UNKNOWN", False))
        if final or state in TERMINAL_STATES:
            continue
        if probe:
            seen = _probe_run(site, run)
            if seen is not None and seen["final"]:
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
    parent_of = {w["id"]: led.get("work-item", w["id"])["payload"].get("parent") for w in wis}
    role_of = {w["id"]: w["role"] for w in wis}
    top = [w for w in wis if w["status"] in ACTIVE and not parent_of[w["id"]] and w["id"] != exclude]
    if not top:
        return []
    events = led.events()
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
        "active_lanes": f"{len(lanes)}/{site.limit('max_concurrent_lanes', 2)}",
        "lanes": lanes,
        "lane_refusals": lane_refusals(led),
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
    for k, v in st["by_status"].items():
        lines.append(f"[{k}]")
        lines += [f"  {x}" for x in v]
    return "\n".join(lines)


# ------------------------------------------------------------------ slurm
ITEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_items(items: list[str]) -> list[str]:
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
    items_level(items)
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


def items_from_bids(site: Site, dataset: str, level: str = "subject") -> tuple[list[str], str]:
    """Items from a BIDS dataset's directory names (inside the site; directory listing only): `sub-*` for
    level subject, `sub-*/ses-*` as `sub-X_ses-Y` for level session. Returns (items, resolved dataset)."""
    if level not in ("subject", "session"):
        fail("--level must be subject or session")
    root = _inside_site(site, dataset, "--each-from-bids")
    if not root.is_dir():
        fail(f"--each-from-bids: not a directory: {root}")
    subs = sorted(d.name for d in root.iterdir() if d.name.startswith("sub-") and d.is_dir())
    items = subs if level == "subject" else [f"{s}_{d.name}" for s in subs for d in sorted((root / s).iterdir())
                                             if d.name.startswith("ses-") and d.is_dir()]
    if not items:
        fail(f"--each-from-bids: no {'sub-*' if level == 'subject' else 'sub-*/ses-*'} directories in {root}")
    return validate_items(items), str(root)


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
                out.append({"run": rid, "job_id": str(p["job_id"]).split(";")[0].strip()})
    if not out:
        fail("--after needs at least one run id")
    return out


DEFAULT_ITEM_ENV = {"item": "DUREH_ITER", "by": "DUREH_ITER_BY", "index": "DUREH_ITER_INDEX",
                    "sub": "DUREH_ITER_SUB", "ses": "DUREH_ITER_SES"}
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


def items_level(items: list[str]) -> str:
    """subject, session or plain; one array holds one level only."""
    levels = {item_entities(i)[0] for i in items}
    if len(levels) > 1:
        fail(f"--each mixes {' and '.join(sorted(levels))} items in one array; submit one level per array")
    return levels.pop()


def item_env(site: Site) -> dict:
    """Names the --each job gets the item, its index and its subject/session labels under: config
    `slurm.each_env` {"item", "index", ["by"], ["sub"], ["ses"]} (the company's own names, e.g. CAMRI_SUBJECT;
    an unmapped by/sub/ses is not exported), else DUREH_ITER, DUREH_ITER_BY, DUREH_ITER_INDEX, DUREH_ITER_SUB,
    DUREH_ITER_SES (Director 2026-10-04). `by` is the array level: subject, session or plain."""
    cfg = (site.config.get("slurm") or {}).get("each_env")
    if not cfg:
        return dict(DEFAULT_ITEM_ENV)
    ok = (isinstance(cfg, dict) and {"item", "index"} <= set(cfg) <= set(DEFAULT_ITEM_ENV)
          and all(isinstance(v, str) and VAR_RE.match(v) for v in cfg.values())
          and len(set(cfg.values())) == len(cfg))
    if not ok:
        fail('config slurm.each_env must be {"item": <NAME>, "by": <NAME>, "index": <NAME>, "sub": <NAME>, '
             f'"ses": <NAME>}} (by, sub and ses optional) with different variable names: {cfg!r}')
    return {k: cfg[k] for k in DEFAULT_ITEM_ENV if k in cfg}


def item_lines(items: list[str], items_file: str | None, env: dict | None = None) -> list[str]:
    """Shell lines that map $SLURM_ARRAY_TASK_ID to the item and export the item, the array level (`by`), its
    index and its subject and session labels (empty when the item has none) under the configured names."""
    import shlex

    env = env or DEFAULT_ITEM_ENV
    ents = [item_entities(i)[1] for i in items]
    it, ix = env["item"], env["index"]
    arr = lambda vals: "(" + " ".join(shlex.quote(v) if v else "''" for v in vals) + ")"  # noqa: E731
    out = [f"# --each: {len(items)} item(s), array task i runs item i" + (f" (list: {items_file})" if items_file else ""),
           "_DUREH_ITERS=" + arr(items),
           ': "${SLURM_ARRAY_TASK_ID:?dureh: not a Slurm array task}"',
           f'export {ix}="$SLURM_ARRAY_TASK_ID"',
           f'export {it}="${{_DUREH_ITERS[$SLURM_ARRAY_TASK_ID]:-}}"',
           f'[ -n "${it}" ] || {{ echo "dureh: no item for array task $SLURM_ARRAY_TASK_ID" >&2; exit 98; }}']
    if env.get("by"):
        out.append(f"export {env['by']}={items_level(items)}")
    for key in ("sub", "ses"):
        if env.get(key):
            out += [f"_DUREH_{key.upper()}=" + arr([e.get(key, "") for e in ents]),
                    f'export {env[key]}="${{_DUREH_{key.upper()}[$SLURM_ARRAY_TASK_ID]}}"']
    names = ", ".join(f"{env[k]}=${env[k]}" for k in DEFAULT_ITEM_ENV if env.get(k))
    out.append(f'echo "dureh: array task: {names}" >&2')
    return out


def _each_wrapper(script: Path, items: list[str], items_file: str, env: dict | None = None) -> str:
    import shlex

    lines = script.read_text(encoding="utf-8", errors="replace").splitlines()
    first = lines[0] if lines and lines[0].startswith("#!") else ""
    interp = shlex.split(first[2:]) if first else ["/bin/bash"]
    directives = [ln for ln in lines if ln.strip().startswith("#SBATCH")]
    out = ["#!/bin/bash", *directives,
           f"# Generated by `dureh slurm submit --each`: runs {script} once per item (one array task each).",
           *item_lines(items, items_file, env),
           # absolute: the job runs in its --chdir, not where the script path was given
           "exec " + " ".join(shlex.quote(w) for w in interp) + f" {shlex.quote(os.path.abspath(script))} \"$@\""]
    return "\n".join(out) + "\n"


def slurm_submit(site: Site, actor: str, wi: str, script: str | None, dry_run: bool = False,
                 extra: list[str] | None = None, extra_payload: dict | None = None,
                 module: str | None = None, rev: str | None = None, each: str | None = None,
                 after: list[str] | None = None, max_parallel: int | None = None,
                 items: list[str] | None = None, each_from_bids: str | None = None,
                 level: str | None = None) -> dict:
    from datetime import datetime, timezone

    led = Ledger(site)
    rec = led.get("work-item", wi)
    if actor not in ("pipeline-runner", "desk"):
        fail("only pipeline-runner (or the desk) submits scheduler jobs")
    if actor != "desk" and rec["payload"]["role"] != actor:
        fail(f"{wi} is not a {actor} work item")
    if led.wi_status(wi)["status"] not in ("dispatched", "rework"):
        fail(f"{wi} is not dispatched")
    if not script:
        fail("--script is required")
    path = Path(script)
    if not path.is_file():
        fail(f"required job script is missing: {script}")
    if rev is not None and module is None:
        fail("--rev needs --module")
    extra = list(extra or [])
    each_file = bids = None
    if each is not None and each_from_bids is not None:
        fail("--each and --each-from-bids exclude each other")
    if level is not None and each_from_bids is None:
        fail("--level goes with --each-from-bids")
    if each is not None:
        items, each_file = parse_items(site, each)
    elif each_from_bids is not None:
        items, bids = items_from_bids(site, each_from_bids, level or "subject")
    elif items is not None:
        validate_items(items)
    if max_parallel is not None:
        if items is None:
            fail("--max-parallel needs --each")
        if max_parallel < 1:
            fail("--max-parallel must be a positive number")
    if items is not None and _sbatch_opt(path, extra, "-a", "--array") is not None:
        fail("--each makes the job array itself; remove --array from the script and the sbatch arguments")
    deps = _after_deps(led, after) if after else []
    cfg = site.config.get("slurm") or {}
    cmd = ["sbatch", "--parsable", f"--job-name=dureh-{wi}"]
    for key, flag in (("account", "--account"), ("partition", "--partition")):
        if cfg.get(key):
            cmd.append(f"{flag}={cfg[key]}")
    wall, wall_source = _requested_time(path, extra, cfg)
    if wall_source == "config":
        cmd.append(f"--time={wall}")  # config slurm.time is the fallback: a script or argument --time wins (UR-20261003-09)
    if items is not None:
        cmd.append(f"--array=0-{len(items) - 1}" + (f"%{max_parallel}" if max_parallel else ""))
    if deps:
        cmd.append("--dependency=afterok:" + ":".join(d["job_id"] for d in deps))
    # relative #SBATCH -o/-e resolve against the job's directory: the WI target, not the agent's shell cwd
    chdir = _sbatch_opt(path, extra, "-D", "--chdir")
    if chdir is None:
        chdir = str(_job_root(site, rec["payload"].get("target")))
        cmd.append(f"--chdir={chdir}")
    else:
        chdir = os.path.join(os.getcwd(), site.expand(chdir))
    logs = None
    site_logs = site.root / "logs" / "slurm" / wi
    pat = "%x-%A_%a" if items is not None else "%x-%j"
    warnings = []
    if not _sets_output(path, extra):
        # sbatch writes slurm-<job>.out into the submitting directory (a project tree) unless told otherwise
        logs = site_logs
        cmd += [f"--output={logs}/{pat}.out", f"--error={logs}/{pat}.err"]
        log_out, log_err = f"{logs}/{pat}.out", f"{logs}/{pat}.err"
    else:
        out = _sbatch_opt(path, extra, "-o", "--output") or "slurm-%j.out"
        log_out = os.path.join(chdir, out)
        log_err = os.path.join(chdir, _sbatch_opt(path, extra, "-e", "--error") or out)
        if items is not None and not any(("%a" in x or "%j" in x) for x in (log_out, log_err)):
            warnings.append(f"the script's own log names ({log_out}) hold neither %a nor %j: array tasks share one file")
    array = None
    if items is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        items_file = site_logs / f"each-{stamp}.items"
        array = {"job_id": None, "items": list(items), "count": len(items), "items_file": str(items_file),
                 "env": item_env(site), "level": items_level(items)}
    mod = None
    if module is not None:  # UR-20261004-04: the job runs against a pinned, read-only tree of modules/<m>
        from . import exectree

        mod = exectree.prepare(site, wi, rec["payload"].get("target"), module, rev, path, dry_run,
                               items=items, items_file=array["items_file"] if array else None,
                               item_env=array["env"] if array else None)
        warnings += mod["warnings"]
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    job_file = mod["wrapper"] if mod else str(path)
    if array is not None and not mod:
        job_file = str(site_logs / (Path(array["items_file"]).stem + ".sbatch"))
        array["wrapper"] = job_file
    cmd += extra + [job_file]
    payload = {"run_type": "slurm", "status": "not-run" if dry_run else "submitted", "command": cmd,
               "script_sha256": sha256_file(path), "script": str(path), "logs": str(logs) if logs else None,
               "chdir": chdir, "log_out": log_out, "log_err": log_err, "sbatch_args": extra, **(extra_payload or {})}
    if array is not None:
        payload["array"] = array
        if max_parallel:
            payload["max_parallel"] = max_parallel
        if each_file:
            payload["each_file"] = each_file
        if bids:
            payload["each_from_bids"] = {"dataset": bids, "level": level or "subject"}
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
            for f in (array["items_file"], array.get("wrapper")):
                if f and os.path.isfile(f):
                    os.unlink(f)

    if not dry_run:
        if array is not None:
            site_logs.mkdir(parents=True, exist_ok=True)
            Path(array["items_file"]).write_text("".join(f"{i}\n" for i in items), encoding="utf-8")
            if array.get("wrapper"):
                Path(array["wrapper"]).write_text(_each_wrapper(path, items, array["items_file"], array["env"]))
                Path(array["wrapper"]).chmod(0o755)
        try:
            check = _day_check(site, cmd, path, extra, cfg)
        except DurehError:
            undo()
            raise
        if check is not None:
            payload["start_check"] = check
        if logs:
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


def array_states(site: Site, job: str, count: int) -> dict[int, str]:
    """Per-task state of an array job from `sacct -j <job> -X -P -n --format=JobID,State,ExitCode`: task
    lines `<job>_<i>|STATE|x:y`, pending ranges `<job>_[0-3,7%2]|PENDING|0:0`. Missing tasks are UNKNOWN."""
    proc = subprocess.run(["sacct", "-j", job, "-X", "-P", "-n", "--format=JobID,State,ExitCode"],
                          capture_output=True, text=True, timeout=60, env=site.env())
    states: dict[int, str] = {}
    for line in proc.stdout.splitlines():
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
    by_idx = array_states(site, str(arr["job_id"]), arr["count"])
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
    if st["final"]:
        for e in reversed(led.events(run["wi"])):
            if e["type"] == "run.status" and e["payload"].get("run") == rid and e["payload"].get("item_states"):
                if e["payload"]["item_states"] == st["items"]:
                    return e
                break
    payload = {"run": rid, "job_id": run["payload"]["job_id"], "scheduler_state": st["scheduler_state"],
               "items": st["items"], "counts": st["counts"], "final": st["final"]}
    if waited:
        payload["waited"] = True
    if watched:
        payload["watched"] = True
    if st["final"]:
        payload["item_states"] = st["items"]
        failed = [i for i, s in st["items"].items() if s != "COMPLETED"]
        if failed:
            payload["failed_items"] = failed
        mod = run["payload"].get("module") or {}
        if mod.get("tree"):  # array tasks share one pinned tree: removed once every task has ended
            from . import exectree

            exectree.cleanup(Path(mod["tree"]))
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


def slurm_status(site: Site, actor: str, run_id: str) -> dict:
    led = Ledger(site)
    run = _get_run(led, run_id)
    job = run["payload"].get("job_id")
    if not job:
        fail(f"{run_id} has no scheduler job id")
    if run["payload"].get("array"):
        return _record_array(site, led, actor, run, _array_status(site, run), waited=False)
    state, line = job_state(site, job)
    ev = led.event("run.status", actor if actor in ("pipeline-runner", "desk") else "desk", run["wi"],
                   {"run": run_id, "job_id": job, "scheduler_state": state, "raw": line}, [run_id])
    return ev


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
    if run["payload"].get("array"):
        while True:
            st = _array_status(site, run)
            if st["final"] or time.monotonic() >= deadline:
                break
            time.sleep(max(1, min(interval, deadline - time.monotonic())))
        ev = _record_array(site, led, actor, run, st, waited=True)
        if not st["final"]:
            ev["note"] = f"still {st['scheduler_state']} ({st['counts']}) after {timeout} s; wait again or check later"
        return ev
    state, line, final = wait_job(site, job, int(max(0, deadline - time.monotonic())), interval)
    ev = led.event("run.status", actor if actor in ("pipeline-runner", "desk") else "desk", run["wi"],
                   {"run": run_id, "job_id": job, "scheduler_state": state, "raw": line,
                    "waited": True, "final": final}, [run_id])
    if not final:
        ev["note"] = f"still {state} after {timeout} s; wait again or check later"
    return ev


def _watch_line(led: Ledger, events: list[dict], run: dict, seen: dict) -> str:
    """One line per ended job: run, job, WI, owner role and agent, state, and what the desk does next."""
    wi = run["wi"]
    rec = led.get("work-item", wi)
    role = rec["payload"]["role"]
    bound = [e for e in events if e["type"] == "agent.started" and e["wi"] == wi]
    agent = bound[-1]["payload"].get("agent_id") if bound else None
    agent_s = f"{str(agent)[:8]}:{agent_state(led, wi, events)}" if agent else "none"
    st = led.wi_status(wi)["status"]
    parent = rec["payload"].get("parent")
    if agent_s.endswith(":running") or agent_s.endswith(":starting"):
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
    """One pass of `slurm watch`: for every tracked run not yet known to have ended, ask the scheduler; record
    a terminal state once (`run.status` with final and watched; not in observe mode) and return its line.
    Runs whose final state is already recorded (by `wait`, `status` or an earlier watch) are not reported."""
    led = Ledger(site)
    events = led.events()
    recorded = _recorded_states(events)
    lines = []
    for run in _tracked_runs(led):
        if run["id"] in reported:
            continue
        state, final = recorded.get(run["id"], ("UNKNOWN", False))
        if final or state in TERMINAL_STATES:
            reported.add(run["id"])
            continue
        seen = _probe_run(site, run)
        if seen is None or not seen["final"]:
            continue
        if not site.observe:
            if seen.get("array"):
                _record_array(site, led, actor, run, seen["array"], waited=False, watched=True)
            else:
                led.event("run.status", actor, run["wi"], {"run": run["id"], "job_id": run["payload"]["job_id"],
                                                           "scheduler_state": seen["state"], "raw": seen.get("raw", ""),
                                                           "final": True, "watched": True}, [run["id"]])
        reported.add(run["id"])
        lines.append(_watch_line(led, events, run, seen))
    return lines


def slurm_watch(site: Site, actor: str, interval: int = 60, once: bool = False, out=None,
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
    arr = p.get("array")
    if not arr:
        fail(f"{run_id} is not an --each (array) run")
    if not p.get("job_id"):
        fail(f"{run_id} has no scheduler job id")
    if wi is not None and wi != run["wi"]:
        fail(f"{run_id} belongs to {run['wi']}, not {wi}")
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
    return slurm_submit(site, actor, run["wi"], script or p.get("script"), dry_run,
                        extra if extra is not None else p.get("sbatch_args"),
                        {"resubmit_of": run_id, "resubmit_states": {i: states[i] for i in failed}},
                        module=module, rev=rev, after=after, items=failed,
                        max_parallel=max_parallel if max_parallel is not None else p.get("max_parallel"))


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
    array = any(str(w).startswith("--array=") for w in cmd)
    what = "array's first task" if array else "job"
    if ends > day_end:
        fail(f"workday mode: this {what} is expected to start at {start.isoformat(timespec='seconds')} and, with "
             f"--time {requested} (from the {source}{', per task' if array else ''}), end at {ends.isoformat(timespec='seconds')}, after "
             f"today's end {d['end']}. Nothing was submitted; ask for a shorter --time, or submit it in the next "
             "approved day.")
    return {"expected_start": start.isoformat(timespec="seconds"), "expected_end": ends.isoformat(timespec="seconds"),
            "day_end": d["end"], "time": requested, "time_source": source,
            **({"per_task": True, "note": "one --test-only for the array: its first task's expected start plus the "
                "per-task --time; tasks that wait for resources start later"} if array else {})}


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
