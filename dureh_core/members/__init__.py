"""Member runtimes (release 64): roles that do not run as Claude subagents.

A role's `runtime` (startup.yaml) is `claude-code` (today's subagent, spawned with the Agent tool), `codex` (a
read-only verifier) or `local` (a worker on the site's office-hours model server). The non-Claude runtimes run through
one harness command after `dispatch brief`:

    dureh member run --wi W            records the dispatch and submits ONE scheduler job, returns at once
    dureh member exec --run R          (inside that job only) runs the member and records what it did
    dureh member status --wi W         read-only
    dureh member collect --wi W        records the outcome (a checker's verdict, a worker's changes for review)
    dureh member run --wi W --fallback the role's declared fallback: a Claude subagent spawned with the same brief
    dureh member canary --runtime codex

Invariants kept: one desk; briefs and binding (a member run consumes a recorded, unused brief like a spawn; the job
binds `agent.started` to that dispatch); records append-only; Gate 2 stays the desk's; a checker's verdict is recorded
by the same code path as `verify --wi` (verification bound to the verified head). See docs/member-runtimes.md.
"""
from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import time
from pathlib import Path

from ..records import Ledger
from ..site import Site
from ..util import DurehError, dumps, fail, git, utc_now, write_replace

RUN_TYPE = "member"
RUNTIMES = ("codex", "local")
OUTCOMES = ("completed", "not_run")
FALLBACK, NOT_RUN, COLLECTED, DISCARDED, CANARY = ("member.fallback", "member.not-run", "member.collected",
                                                  "member.discarded", "member.canary")
DEFAULTS = {
    "slurm": {"cpus": 2, "mem": "8G"},  # partition, account, qos, gres, time: config `slurm` and computed
    "record_wait_seconds": 300,         # how long the job waits for `member run` to record its run
    "codex": {"bin": "codex", "model": None, "reasoning_effort": "medium", "wait_seconds": 1800,
              "permission_profile": "dureh_member", "sandbox_read": None, "checks_timeout": 1800,
              "env_pass": ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
                           "SSL_CERT_FILE", "CODEX_HOME"]},
    "local": {"wall_seconds": 3600, "quiet_seconds": 300, "grace_windows": 4, "max_turns": 24, "write_cap": 6,
              "think": False, "test_timeout": 900, "retries": 1, "options": {},
              "repetition": {"tail_chars": 600, "max_period": 80, "min_repeats": 6}},
}
JOB_MARGIN_SECONDS = 900  # the job's wall time over the runtime's own limit (checks, evidence, records)


# ------------------------------------------------------------------ config
def settings(site: Site) -> dict:
    """config `members` over DEFAULTS (one level of nesting merged: slurm, codex, local)."""
    cfg = site.config.get("members") or {}
    if not isinstance(cfg, dict):
        fail("config members must be an object (slurm, codex, local, record_wait_seconds)")
    out = {}
    for key, value in DEFAULTS.items():
        given = cfg.get(key)
        if isinstance(value, dict):
            if given is not None and not isinstance(given, dict):
                fail(f"config members.{key} must be an object")
            out[key] = {**value, **(given or {})}
        else:
            out[key] = value if given is None else given
    return out


def config_errors(site: Site) -> list[str]:
    """What is wrong with config `members` (release 64): unknown keys, non-numeric limits."""
    cfg = site.config.get("members")
    if cfg is None:
        return []
    if not isinstance(cfg, dict):
        return ["config members must be an object (slurm, codex, local, record_wait_seconds)"]
    out = [f"config members.{k}: unknown key (release 64: slurm, codex, local, record_wait_seconds)"
           for k in cfg if k not in DEFAULTS]
    for block in ("slurm", "codex", "local"):
        given = cfg.get(block)
        if given is None:
            continue
        if not isinstance(given, dict):
            out.append(f"config members.{block} must be an object")
            continue
        known = set(DEFAULTS[block]) | ({"partition", "account", "qos", "gres", "time"} if block == "slurm" else set())
        out += [f"config members.{block}.{k}: unknown key" for k in given if k not in known]
    for block, keys in (("codex", ("wait_seconds", "checks_timeout")),
                        ("local", ("wall_seconds", "quiet_seconds", "grace_windows", "max_turns", "write_cap",
                                   "test_timeout", "retries"))):
        for k in keys:
            v = (cfg.get(block) or {}).get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                out.append(f"config members.{block}.{k} must be a non-negative number")
    return out


# ------------------------------------------------------------------- paths
def runs_root(site: Site, wi: str) -> Path:
    """`logs/members/<WI>/`: the harness area of an item's member runs (never inside the worktree, whose files are
    the item's outputs)."""
    return site.root / "logs" / "members" / wi


def run_dir(site: Site, wi: str, run_id: str) -> Path:
    """`logs/members/<WI>/<run>/`: `dispatch/` (harness-owned: request, job script and logs, run-status.json, the
    runtime's own logs, result.json) and, for a checker, `evidence/` (the bundle the reviewer reads)."""
    return runs_root(site, wi) / run_id


def brief_path(site: Site, wi: str, digest: str) -> Path:
    """Where `dispatch brief` keeps the full text of a member role's brief (the job reads it; its digest is checked)."""
    return runs_root(site, wi) / "briefs" / f"{digest}.md"


def save_brief(site: Site, wi: str, digest: str, text: str) -> None:
    p = brief_path(site, wi, digest)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_replace(p, text)


def read_brief(site: Site, wi: str, digest: str) -> str:
    """The saved brief, checked against its recorded digest (the header names it; the digest covers the body)."""
    import hashlib

    p = brief_path(site, wi, digest)
    if not p.is_file():
        fail(f"{wi}: the text of brief {digest} was not kept ({p}); prepare a new brief with `dispatch brief --wi {wi}`")
    text = p.read_text(encoding="utf-8")
    body = text.split("\n\n", 1)[1] if "\n\n" in text else ""
    if hashlib.sha256(body.encode("utf-8")).hexdigest()[:16] != digest:
        fail(f"{wi}: the kept text of brief {digest} does not match its digest; prepare a new brief")
    return text


def write_status(rdir: Path, state: str, **extra) -> dict:
    """Atomic `dispatch/run-status.json` (state: submitted, running, done; outcome and reason when done)."""
    d = rdir / "dispatch"
    d.mkdir(parents=True, exist_ok=True)
    payload = {"state": state, "updated": utc_now(), **extra}
    write_replace(d / "run-status.json", dumps(payload))
    return payload


def read_status(rdir: Path) -> dict | None:
    p = rdir / "dispatch" / "run-status.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, json.JSONDecodeError):
        return None


def read_result(rdir: Path) -> dict | None:
    p = rdir / "dispatch" / "result.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, json.JSONDecodeError):
        return None


def write_allowlist(site: Site, role, payload: dict) -> list[str]:
    """A local member's exact write allowlist: the item's declared `outputs` (relative to its work root) that lie in
    the role's write scopes (the role ceiling intersected with the WI scope)."""
    out = []
    for o in payload.get("outputs") or []:
        rel = str(o).strip().lstrip("./") if str(o).startswith("./") else str(o).strip()
        parts = rel.split("/")
        if not rel or rel.startswith("/") or any(x in ("", ".", "..") for x in parts) or any(c in rel for c in "*?["):
            continue
        if site.org.scope_allows(role.name, f"{payload['target']}/{rel}"):
            out.append(rel)
    return out


def minimal_env(extra_path: list[str] | None = None, passed: list[str] | None = None) -> dict:
    """The environment a member's processes get: an explicit allowlist, never the job's environment wholesale."""
    env = {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME", "LANG", "TMPDIR") if k in os.environ}
    env.setdefault("LANG", "C.UTF-8")
    path = []
    for d in (extra_path or []) + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]:
        if d and d not in path:
            path.append(d)
    env["PATH"] = os.pathsep.join(path)
    for k in passed or []:
        if k in os.environ:
            env[k] = os.environ[k]
    return env


# ---------------------------------------------------------------- records
def member_runs(led: Ledger, wi: str) -> list[dict]:
    return [r for r in led.load("run") if r["wi"] == wi and r["payload"].get("run_type") == RUN_TYPE]


def _finished(events: list[dict], run_id: str) -> dict | None:
    return next((e for e in events if e["type"] == "agent.finished" and e["payload"].get("run") == run_id), None)


def _collected(events: list[dict], run_id: str) -> dict | None:
    found = [e for e in events if e["type"] in (COLLECTED, DISCARDED) and e["payload"].get("run") == run_id]
    return found[-1] if found else None


def live_runs(led: Ledger, wi: str, events: list[dict] | None = None) -> list[dict]:
    """Member runs of the item whose member has not finished (`agent.finished` not recorded)."""
    events = led.events(wi) if events is None else events
    return [r for r in member_runs(led, wi) if not _finished(events, r["id"])
            and not any(e["type"] == "run.status" and e["payload"].get("run") == r["id"] and e["payload"].get("final")
                        and e["payload"].get("cancelled") for e in events)]


def cancel_live_runs(site: Site, led: Ledger, wi: str) -> list[dict]:
    """`dispatch stop` of an item with a member run still queued or running: scancel its job (a harness-owned job,
    recorded by `member run`) and record the final state. Returns what was cancelled."""
    out = []
    for run in live_runs(led, wi):
        job = str(run["payload"].get("job_id") or "")
        if not job:
            continue
        try:
            rc = subprocess.run(["scancel", job], capture_output=True, text=True, timeout=60, env=site.env()).returncode
        except (OSError, subprocess.SubprocessError) as exc:
            rc = f"scancel did not run: {exc}"
        led.event("run.status", "desk", wi, {"run": run["id"], "job_id": job, "scheduler_state": "CANCELLED",
                                             "final": True, "cancelled": True, "scancel": rc}, [run["id"]])
        out.append({"run": run["id"], "job_id": job, "scancel": rc})
    return out


def _brief_for(led: Ledger, wi: str, by: str, events: list[dict]) -> dict:
    """The latest brief `by` prepared for the item, unused (no spawn, member run or fallback used it)."""
    prepared = [e for e in events if e["type"] == "dispatch.prepared" and e["payload"].get("by") == by]
    if not prepared:
        fail(f"no brief prepared by {by} for {wi}: run `dureh dispatch brief --wi {wi}` first (the brief names the role)")
    p = prepared[-1]
    digest = p["payload"]["brief"]
    used = [e for e in events if e["type"] in ("wi.dispatched", "checker.dispatched", FALLBACK)
            and e["payload"].get("brief") == digest]
    if used or any(r["payload"].get("brief") == digest for r in member_runs(led, wi)):
        fail(f"brief {digest} for {wi} was already used ({used[-1]['type'] if used else 'a member run'}); prepare a new"
             f" brief with `dureh dispatch brief --wi {wi}`")
    return p


def _model(site: Site, role, cfg: dict) -> str:
    if role.runtime == "codex":
        return str(cfg["codex"].get("model") or "default")
    from .. import office_hours

    entry = office_hours.profile_for(site, role.profile)
    return entry["name"] if entry else f"profile {role.profile}"


def preflight(site: Site, role, rec: dict, cfg: dict) -> str | None:
    """Why the member cannot run now (NOT_RUN before anything is submitted), or None."""
    p = rec["payload"]
    if role.runtime == "codex":
        if site.org.role(p["role"]).is_manager:
            return "a bundle is verified by `verify --wi <bundle>` (a recount of its items), not by a reviewer"
        from . import codex

        if not codex.find_bin(site, cfg):
            return f"the codex command {cfg['codex']['bin']!r} is not on the site's PATH"
        if not codex.passed_canary(Ledger(site)):
            return "no passing codex canary is recorded: run `dureh member canary --runtime codex` on this host first"
        return None
    if not write_allowlist(site, role, p):
        return ("the item declares no output inside the role's write scopes, so the local member would have no file to"
                f" write: declare them with `wi amend --wi {rec['id']} --output <path>` (exact files)")
    from .. import office_hours

    entry = office_hours.profile_for(site, role.profile)
    if entry is None:
        return f"config office_hours.profiles names no profile with file {role.profile} (the role's profile)"
    server = office_hours.server(site)
    if server is None:
        return "no office-hours server is running (start it with `dureh site office-hours start`)"
    if entry["name"] not in (server.get("models") or []):
        return f"the office-hours server does not serve {entry['name']} (it serves {', '.join(server.get('models') or [])})"
    return None


# ---------------------------------------------------------------- run
def _wall_seconds(cfg: dict, runtime: str) -> int:
    if runtime == "codex":
        return int(cfg["codex"]["wait_seconds"]) + int(cfg["codex"]["checks_timeout"]) + JOB_MARGIN_SECONDS
    local = cfg["local"]
    return int(local["wall_seconds"]) * (1 + int(local["retries"])) + JOB_MARGIN_SECONDS


def job_script(site: Site, run_id: str, wi: str, role, rdir: Path, cfg: dict) -> dict:
    """The member job's sbatch script (the session job's pattern: harness-owned, recorded, never an agent's)."""
    from .. import cluster, ops, sessions

    block = dict(cfg["slurm"])
    wall = block.pop("time", None) or cluster.hms(_wall_seconds(cfg, role.runtime) / 3600)
    logs = rdir / "dispatch"
    name = f"{ops.job_prefix(site)}-member-{wi}"
    head = sessions.slurm_head(site, name, block, wall, logs)
    dureh = site.root / "bin" / "dureh"
    body = [f"# dureh member run {run_id}: {role.name} ({role.kind}) on the {role.runtime} runtime for {wi} (generated;"
            " the job runs the member and records what it did)",
            "unset DUREH_ACTOR DUREH_AGENT_ID TMUX TMUX_PANE  # never an agent's identity: the job records as the member",
            f"exec {shlex.quote(str(dureh))} member exec --run {shlex.quote(run_id)}"]
    return {"text": "\n".join(head + body) + "\n", "name": name, "wall": wall, "logs": str(logs)}


def run(site: Site, actor: str, wi: str, fallback: bool = False, reason: str | None = None) -> dict:
    """`member run --wi W [--fallback --reason]`: consume the caller's latest unused brief for the item.

    Without `--fallback`: refuse a Claude role; check the gate and the worktree as a spawn does; preflight the runtime
    (a problem is recorded as `member.not-run` and refused: nothing is submitted and the brief stays unused, so the
    desk can choose the fallback); then submit ONE job (`member exec --run <id>`) and record the dispatch
    (`wi.dispatched` or `checker.dispatched` with `runtime` and `member_run`) and the `member` run record. Returns at
    once. With `--fallback`: record `member.fallback` for the brief; the caller then spawns the role as a Claude
    subagent with that brief (the spawn hook admits it)."""
    from ..workday import gate
    from ..workflow import worktree_problem

    led = Ledger(site)
    org = site.org
    events = led.events(wi)
    prepared = _brief_for(led, wi, actor, events)
    pp = prepared["payload"]
    role = org.role(pp["role"])
    digest = pp["brief"]
    if not role.is_member:
        fail(f"{role.name} runs as a Claude subagent: spawn it with the Agent tool and the brief (subagent_type={role.name})")
    why = gate(site, wi, checking=bool(pp.get("checking")))
    if why:
        fail(why)
    if pp.get("worktree"):
        wt = site.worktrees / wi
        err = worktree_problem(wt, wi, site)
        if err:
            fail(f"worktree {wt} is broken ({err}); the desk repairs it with `dureh worktree repair --wi {wi}`")
    live = live_runs(led, wi, events)
    if live:
        fail(f"{wi} already has member run {live[-1]['id']} (job {live[-1]['payload'].get('job_id')}) not finished;"
             f" see `dureh member status --wi {wi}`")
    rec = led.get("work-item", wi)
    if fallback:
        if role.runtime_fallback == "none":
            fail(f"{role.name} declares no fallback (runtime_fallback: none)")
        if not (reason or "").strip():
            fail("--fallback needs --reason: why the role runs as a Claude subagent this time")
        ev = led.event(FALLBACK, actor, wi, {"brief": digest, "role": role.name, "runtime": role.runtime,
                                             "fallback": role.runtime_fallback, "reason": reason.strip(), "by": actor},
                       [prepared["id"]])
        return {"fallback": True, "event": ev["id"], "role": role.name, "brief": digest,
                "next": f"spawn subagent_type={role.name} with brief {digest} (the `dispatch brief` output) as the whole"
                        " prompt; the spawn is recorded with runtime_fallback"}
    cfg = settings(site)
    read_brief(site, wi, digest)  # the job needs its text: refuse now, not in the job
    problem = preflight(site, role, rec, cfg)
    if problem:
        led.event(NOT_RUN, actor, wi, {"brief": digest, "role": role.name, "runtime": role.runtime, "stage": "submit",
                                       "reason": problem, "by": actor}, [prepared["id"]])
        fail(f"{wi}: {role.name} on {role.runtime} is NOT_RUN: {problem}. Nothing was submitted and brief {digest} is"
             f" unused: fix the cause and run again, or choose the fallback with `dureh member run --wi {wi} --fallback"
             " --reason ...`")
    run_id = led._new_id("run")
    rdir = run_dir(site, wi, run_id)
    (rdir / "dispatch").mkdir(parents=True, exist_ok=True)
    js = job_script(site, run_id, wi, role, rdir, cfg)
    script = rdir / "dispatch" / "job.sbatch"
    script.write_text(js["text"], encoding="utf-8")
    model = _model(site, role, cfg)
    request = {"run": run_id, "wi": wi, "role": role.name, "runtime": role.runtime, "brief": digest, "by": actor,
               "prepared": prepared["id"], "model": model, "submitted": utc_now()}
    write_replace(rdir / "dispatch" / "request.json", dumps(request))
    with led.lock():
        _brief_for(led, wi, actor, led.events(wi))  # still unused under the lock
        from .. import sessions

        job_id, cmd = sessions.submit_script(site, script, f"member run of {wi}")
        etype = "checker.dispatched" if pp.get("checking") else "wi.dispatched"
        try:
            disp = led.event(etype, actor, wi, {"role": role.name, "brief": digest, "by": actor, "model": model,
                                                "brief_model": pp.get("model"), "kind": pp.get("kind"),
                                                "kind_reason": pp.get("kind_reason"), "background": True,
                                                "runtime": role.runtime, "member_run": run_id, "job_id": job_id},
                             [prepared["id"]])
            led.append("run", RUN_TYPE, actor, wi, {
                "run_type": RUN_TYPE, "status": "submitted", "runtime": role.runtime, "role": role.name,
                "brief": digest, "model": model, "job_id": job_id, "job_name": js["name"], "time": js["wall"],
                "script": str(script), "run_dir": str(rdir), "log_out": f"{js['logs']}/%x-%j.out",
                "log_err": f"{js['logs']}/%x-%j.err", "command": cmd, "dispatch": disp["id"], "by": actor},
                [disp["id"]], record_id=run_id)
        except DurehError:  # the job must not run unrecorded
            subprocess.run(["scancel", job_id], capture_output=True, text=True, timeout=60, env=site.env())
            raise
    write_status(rdir, "submitted", run=run_id, job_id=job_id)
    from ..ops import job_end_phrase

    return {"run": run_id, "job_id": job_id, "role": role.name, "runtime": role.runtime, "model": model,
            "dispatch": disp["id"], "run_dir": str(rdir), "wall": js["wall"],
            "next": f"end your turn; {job_end_phrase(site)}, run `dureh member collect --wi {wi}`"}


# ---------------------------------------------------------------- exec (job side)
def _wait_run(led: Ledger, run_id: str, seconds: float) -> dict:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        found = next((r for r in led.load("run") if r["id"] == run_id), None)
        if found is not None or time.monotonic() >= deadline:
            break
        time.sleep(2)
    if found is None or found["payload"].get("run_type") != RUN_TYPE:
        fail(f"no member run {run_id} is recorded (waited {seconds:g} s)")
    return found


def execute(site: Site, run_id: str) -> dict:
    """`member exec --run R`, inside the job `member run` submitted (and only there): bind `agent.started` to the run's
    dispatch, run the runtime, record `agent.finished` and one usage row (`run` type agent, `runtime`), write
    `dispatch/result.json` and the final run-status. Any harness error becomes a NOT_RUN outcome with its reason."""
    if os.environ.get("DUREH_ACTOR"):
        fail("member exec runs only in the job `member run` submitted, never from an agent")
    led = Ledger(site)
    cfg = settings(site)
    run_rec = _wait_run(led, run_id, float(cfg["record_wait_seconds"]))
    p = run_rec["payload"]
    if str(os.environ.get("SLURM_JOB_ID") or "") != str(p.get("job_id")):
        fail(f"member exec --run {run_id} runs only inside its own job {p.get('job_id')}"
             f" (SLURM_JOB_ID={os.environ.get('SLURM_JOB_ID')})")
    wi = run_rec["wi"]
    rdir = Path(site.relocate(p["run_dir"]))
    st = read_status(rdir)
    if st and st.get("state") in ("running", "done"):
        fail(f"member run {run_id} already {st['state']}; one exec per run")
    role = site.org.role(p["role"])
    agent_id = f"member-{run_id}"
    host = socket.gethostname()
    write_status(rdir, "running", run=run_id, job_id=p.get("job_id"), host=host)
    led.event("agent.started", role.name, wi, {"agent_id": agent_id, "brief": p.get("brief"), "bound_by": "member-run",
                                               "bound_at": "start", "runtime": role.runtime, "run": run_id,
                                               "host": host, "job_id": p.get("job_id")}, [p["dispatch"]])
    t0 = time.monotonic()
    started = utc_now()
    ctx = {"site": site, "led": led, "run": run_rec, "role": role, "wi": wi, "rdir": rdir, "cfg": cfg,
           "brief": None, "model": p.get("model"), "agent_id": agent_id}
    try:
        ctx["brief"] = read_brief(site, wi, p["brief"])
        if role.runtime == "codex":
            from . import codex

            result = codex.execute(ctx)
        else:
            from . import local

            result = local.execute(ctx)
    except (DurehError, OSError, ValueError, subprocess.SubprocessError) as exc:
        result = {"outcome": "not_run", "reason": f"harness error: {exc}"}
    result = {"run": run_id, "wi": wi, "role": role.name, "runtime": role.runtime, "started": started,
              "finished": utc_now(), "seconds": round(time.monotonic() - t0, 1), **result}
    if result.get("outcome") not in OUTCOMES:
        result["outcome"], result["reason"] = "not_run", f"harness error: unknown outcome {result.get('outcome')!r}"
    write_replace(rdir / "dispatch" / "result.json", dumps(result))
    usage = result.get("usage") or {"coverage": "none", "reason": "the runtime reported no usage"}
    led.event("agent.finished", role.name, wi, {"agent_id": agent_id, "runtime": role.runtime, "run": run_id,
                                                "outcome": result["outcome"], "reason": result.get("reason"),
                                                "leftovers_killed": 0}, [run_id])
    led.append("run", "agent", role.name, wi, {"run_type": "agent", "status": "completed" if result["outcome"] ==
                                               "completed" else "not-run", "agent_id": agent_id,
                                               "runtime": role.runtime, "model": p.get("model"), "member_run": run_id,
                                               "outcome": result["outcome"], "reason": result.get("reason"),
                                               "seconds": result["seconds"], "usage": usage}, [run_id])
    write_status(rdir, "done", run=run_id, outcome=result["outcome"], reason=result.get("reason"))
    return result


# ---------------------------------------------------------------- status / collect
def _job(site: Site, run: dict, events: list[dict]) -> dict:
    from .. import ops

    recorded = ops._recorded_states(events).get(run["id"])
    if recorded and (recorded[1] or recorded[0] in ops.TERMINAL_STATES):
        return {"state": recorded[0], "final": True}
    try:
        state, _ = ops.job_state(site, str(run["payload"]["job_id"]))
    except (OSError, subprocess.SubprocessError) as exc:
        return {"state": "UNKNOWN", "final": False, "error": str(exc)}
    first = (state.split() or ["UNKNOWN"])[0]
    return {"state": first, "final": first in ops.TERMINAL_STATES}


def status(site: Site, wi: str) -> dict:
    """Read-only: the item's member runs, newest last, with run-status, job state, outcome and collection."""
    led = Ledger(site)
    events = led.events(wi)
    rows = []
    for r in member_runs(led, wi):
        rdir = Path(site.relocate(r["payload"]["run_dir"]))
        res = read_result(rdir) or {}
        col = _collected(events, r["id"])
        rows.append({"run": r["id"], "role": r["payload"]["role"], "runtime": r["payload"]["runtime"],
                     "model": r["payload"].get("model"), "job_id": r["payload"].get("job_id"), "by": r["actor"],
                     "submitted": r["ts"], "run_status": read_status(rdir), "job": _job(site, r, events),
                     "outcome": res.get("outcome"), "reason": res.get("reason"), "verdict": res.get("verdict"),
                     "notes": res.get("notes") or [],
                     "collected": ({"event": col["id"], "type": col["type"], "by": col["actor"]} if col else None),
                     "run_dir": str(rdir)})
    others = [{"type": e["type"], "id": e["id"], "brief": e["payload"].get("brief"), "reason": e["payload"].get("reason")}
              for e in events if e["type"] in (FALLBACK, NOT_RUN)]
    return {"wi": wi, "runs": rows, "fallbacks_and_not_run": others}


def render_status(st: dict) -> str:
    if not st["runs"] and not st["fallbacks_and_not_run"]:
        return f"{st['wi']}: no member runs"
    lines = []
    for r in st["runs"]:
        line = (f"{r['run']} {r['role']} ({r['runtime']}, {r['model']}) job {r['job_id']} {r['job']['state']};"
                f" run {((r['run_status'] or {}).get('state') or 'unknown')}")
        if r["outcome"]:
            line += f", outcome {r['outcome']}" + (f" ({r['reason']})" if r.get("reason") else "")
        if r.get("verdict"):
            line += f", verdict {r['verdict']}"
        if r.get("notes"):
            line += f", notes {', '.join(r['notes'])}"
        line += f"; collected {r['collected']['event']}" if r["collected"] else "; not collected"
        lines.append(line)
    for o in st["fallbacks_and_not_run"]:
        lines.append(f"{o['type']} {o['id']} brief {o['brief']}: {o['reason']}")
    return "\n".join(lines)


def _may_collect(site: Site, actor: str, run: dict) -> None:
    if actor != "desk" and actor != run["payload"].get("by"):
        fail(f"member run {run['id']} was submitted by {run['payload'].get('by')}; only it or the desk collects it")


def _worktree_changes(wt: Path) -> list[str]:
    from ..workflow import CACHE_RE, _git_z

    names = []
    entries = _git_z(["status", "--porcelain", "-z", "--untracked-files=all"], wt)
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        if entry[0] in "RC":
            i += 1
        if not CACHE_RE.search(entry[3:]):
            names.append(entry[3:])
    return sorted(set(names))


def collect(site: Site, actor: str, wi: str, run_id: str | None = None, stage: bool = False, summary: str | None = None,
            discard: bool = False, reason: str | None = None) -> dict:
    """`member collect --wi W [--run R] [--stage --summary S | --discard --reason R]`.

    Records the outcome of an ended member run once (`member.collected`). A run whose job ended without a result
    (cancelled, out of time, node failure) is recorded as NOT_RUN and its `agent.finished` written for it. A codex
    checker's completed review goes through `workflow._record_verification`, the same path as `verify --wi`: the
    declared check results the job measured plus the reviewer's verdict, ownership and outputs from the current tree,
    bound to the head the job reviewed (a moved head records nothing but `member.collected` with `superseded`).
    A local worker's changes stay uncommitted in the worktree for review; `--stage` (the dispatcher or the desk, after
    review) commits exactly the member's changed files on the WI branch and records `wi.staged` as the member;
    `--discard` restores them and records `member.discarded`."""
    led = Ledger(site)
    runs = member_runs(led, wi)
    if not runs:
        fail(f"{wi} has no member run")
    run = next((r for r in runs if r["id"] == run_id), None) if run_id else runs[-1]
    if run is None:
        fail(f"{run_id} is not a member run of {wi}")
    _may_collect(site, actor, run)
    p = run["payload"]
    rdir = Path(site.relocate(p["run_dir"]))
    events = led.events(wi)
    result = read_result(rdir)
    if result is None:
        job = _job(site, run, events)
        if not job["final"]:
            fail(f"member run {run['id']}: job {p.get('job_id')} is still {job['state']}; nothing was recorded. End your"
                 " turn: you are re-dispatched when the job ends")
        result = {"run": run["id"], "outcome": "not_run",
                  "reason": f"the job ended ({job['state']}) without a result from the member"}
        if not _finished(events, run["id"]):
            led.event("agent.finished", p["role"], wi, {"agent_id": f"member-{run['id']}", "runtime": p["runtime"],
                                                        "run": run["id"], "outcome": "not_run",
                                                        "reason": result["reason"], "recorded_by": actor}, [run["id"]])
    done = _collected(events, run["id"])
    role = site.org.role(p["role"])
    if p["runtime"] == "local" and (stage or discard):
        return _review(site, led, actor, wi, run, result, role, stage, summary, discard, reason, done)
    if stage or discard:
        fail("--stage and --discard are for a local worker's changes")
    if done:
        return {"run": run["id"], "already": done["id"], "outcome": result.get("outcome"),
                "note": f"member run {run['id']} was already collected ({done['id']})"}
    base = {"run": run["id"], "role": role.name, "runtime": p["runtime"], "outcome": result["outcome"],
            "reason": result.get("reason"), "collected_by": actor,
            **({"notes": result["notes"]} if result.get("notes") else {})}
    if result["outcome"] != "completed":
        ev = led.event(COLLECTED, actor, wi, base, [run["id"]])
        return {**base, "event": ev["id"],
                "next": (f"NOT_RUN: {result.get('reason')}. Fix the cause and run again (a new brief), or choose the"
                         f" fallback: `dureh dispatch brief --wi {wi}{' --role ' + role.name if role.has('verify') else ''}`"
                         f" then `dureh member run --wi {wi} --fallback --reason ...`")}
    if p["runtime"] == "codex":
        return _collect_verdict(site, led, actor, wi, run, result, role, base)
    wt = site.worktrees / wi  # a local worker: the changes stay in the worktree for review
    changed = _worktree_changes(wt) if wt.is_dir() else []
    payload = {**base, "changed": changed, "writes": result.get("writes"), "tests": result.get("tests"),
               "summary": (result.get("output") or "")[-2000:]}
    if result.get("tool_results"):  # release 65: allowed / denied / malformed tool calls
        payload["tool_results"] = result["tool_results"]
    ev = led.event(COLLECTED, actor, wi, payload, [run["id"]])
    diff = git(["diff", "--stat"], wt, check=False) if wt.is_dir() else ""
    return {**payload, "event": ev["id"], "diffstat": diff,
            "next": f"review the changes in {wt} (`git diff`), then `dureh member collect --wi {wi} --stage --summary"
                    f" \"...\"` (commits them and stages the item for the checker) or `--discard --reason \"...\"`"}


def _collect_verdict(site: Site, led: Ledger, actor: str, wi: str, run: dict, result: dict, role, base: dict) -> dict:
    from .. import workflow as wf

    ctx = wf._verify_context(site, role.name, wi)
    paths, head = wf.changed_paths(site, wi)
    if head == result.get("head") and isinstance(result.get("owned_paths"), list):
        paths = result["owned_paths"]  # release 65: listed in the job before its checks ran (as `verify --wi` does)
    if head != result.get("head"):
        ev = led.event(COLLECTED, actor, wi, {**base, "superseded": True, "reviewed_head": result.get("head"),
                                              "head": head}, [run["id"]])
        return {**base, "event": ev["id"], "superseded": True,
                "note": f"{wi} moved from {str(result.get('head'))[:12]} to {str(head)[:12]} after the review; nothing"
                        f" was verified. Prepare a new checker brief and run again"}
    verdict = result.get("verdict")
    review = {"command": f"{role.runtime} review (member run {run['id']}, model {run['payload'].get('model')})",
              "exit": 0 if verdict == "PASS" else 1, "verdict": verdict, "seconds": result.get("review_seconds"),
              "tail": (result.get("findings") or "")[-2000:], "log": str(Path(site.relocate(run["payload"]["run_dir"]))
                                                                           / "dispatch" / "codex-events.jsonl")}
    checks = list(result.get("checks") or [])
    led_ev = led.event(COLLECTED, actor, wi, {**base, "verdict": verdict, "head": head}, [run["id"]])
    r = wf._record_verification(site, ctx, role.name, wi, paths, head, checks, "member", None, time.monotonic(),
                                extra={"member_run": run["id"], "runtime": role.runtime, "verdict": verdict,
                                       "collected_by": actor, "collected_event": led_ev["id"], "runner_source": "member"},
                                review=review)
    return {**base, "event": led_ev["id"], "verdict": verdict, "verification": r["run"], "result": r["result"],
            "note": r["note"]}


def _review(site: Site, led: Ledger, actor: str, wi: str, run: dict, result: dict, role, stage: bool,
            summary: str | None, discard: bool, reason: str | None, done: dict | None) -> dict:
    if stage == discard:
        fail("choose one: --stage --summary \"...\" or --discard --reason \"...\"")
    if done and done["type"] == DISCARDED:
        fail(f"member run {run['id']} was discarded ({done['id']})")
    later = [r for r in member_runs(led, wi) if r["ts"] > run["ts"]]
    if later:
        fail(f"member run {run['id']} is not the item's latest ({later[-1]['id']})")
    wt = site.worktrees / wi
    if not wt.is_dir():
        fail(f"{wi} has no worktree")
    changed = _worktree_changes(wt)
    allow = set(write_allowlist(site, role, led.get("work-item", wi)["payload"]))
    if discard:
        if not (reason or "").strip():
            fail("--discard needs --reason")
        tracked = set(_tracked(wt, changed))
        if tracked:
            git(["checkout", "--", *sorted(tracked)], wt)
        for name in changed:
            if name not in tracked and name in allow and (wt / name).is_file():
                (wt / name).unlink()
        ev = led.event(DISCARDED, actor, wi, {"run": run["id"], "reason": reason.strip(), "restored": sorted(changed),
                                              "role": role.name}, [run["id"]])
        return {"run": run["id"], "event": ev["id"], "discarded": changed,
                "next": f"the item stays dispatched; prepare a new brief (`dispatch brief --wi {wi}`) and run again"}
    if not (summary or "").strip():
        fail("--stage needs --summary")
    if led.wi_status(wi)["status"] != "dispatched":
        fail(f"{wi} is {led.wi_status(wi)['status']}; only a dispatched item is staged")
    outside = [c for c in changed if c not in allow]
    if outside:
        fail(f"{wi}: changes outside the member's write allowlist: {', '.join(outside)}; nothing was staged (discard"
             " them or let the desk decide)")
    if not changed:
        fail(f"{wi}: the member changed nothing; nothing to stage")
    git(["add", "--", *changed], wt)
    msg = f"{wi}: {summary.strip()} (member run {run['id']}, runtime {role.runtime}; reviewed by {actor})"
    git(["commit", "-q", "-m", msg], wt)
    head = git(["rev-parse", "HEAD"], wt)
    if not done:
        led.event(COLLECTED, actor, wi, {"run": run["id"], "role": role.name, "runtime": role.runtime,
                                         "outcome": result.get("outcome"), "reason": result.get("reason"),
                                         "collected_by": actor, "changed": changed}, [run["id"]])
    ev = led.event("wi.staged", role.name, wi, {"summary": summary.strip()[:4000], "head": head, "member_run": run["id"],
                                                "reviewed_by": actor, "files": changed}, [run["id"]])
    return {"run": run["id"], "event": ev["id"], "head": head, "files": changed,
            "next": f"verify through the checker: `dureh dispatch brief --wi {wi} --role <checker>`"}


def _tracked(wt: Path, names: list[str]) -> list[str]:
    if not names:
        return []
    out = subprocess.run(["git", "ls-files", "-z", "--", *names], cwd=wt, capture_output=True, text=True)
    return [n for n in out.stdout.split("\0") if n]

