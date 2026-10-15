"""Session runtimes, chosen by config.json `session.runtime`, and the day's session job: planned (printed),
started (submitted and recorded as `session.started`), shown (`status`) and ended (`release`: a close marker
the job script watches, or `scancel` with `--cancel-now`)."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from .. import cluster, workday
from ..records import Ledger
from ..site import Site
from ..util import fail
from .base import PURPOSES, SessionRuntime
from .claude_code import ClaudeCode

RUNTIMES = {ClaudeCode.name: ClaudeCode}
DEFAULT_CPUS, DEFAULT_MEM = 2, "16G"
JOB_ID = re.compile(r"(\d+)\s*(?:;.*)?$")  # `Submitted batch job 123` or --parsable `123[;cluster]`
RELEASED = "session.released"
DEFAULT_CLOSE_GRACE = 90  # seconds the job gives claude to leave after `/exit` before its tmux session is killed
EXIT_HOLD, EXIT_HOLD_CLOSING = 60, 10  # the tmux session's hold after claude exits: normally, and when a close marker exists
LOOKBACK = 3  # `session.started` records (distinct jobs, newest first) asked of squeue to find the running session job


def get(site: Site) -> SessionRuntime:
    name = (site.config.get("session") or {}).get("runtime") or ClaudeCode.name
    if name not in RUNTIMES:
        fail(f"unknown session runtime {name!r} in config.json session.runtime; available: {', '.join(sorted(RUNTIMES))}")
    return RUNTIMES[name]()


def job_name(site: Site, purpose: str = "desk") -> str:
    """The session job's name: `cc-<session name>` for the desk, `cc-<session name>-<purpose>` otherwise."""
    return f"cc-{site.session_name}" + ("" if purpose == "desk" else f"-{purpose}")


def logs_dir(site: Site) -> Path:
    return site.root / "logs" / "session"


def tmux_name(site: Site, job_id: str, purpose: str = "desk") -> str:
    """The tmux session inside job `job_id`: `<job name>-<job id>` (the script's NAME; `local` without Slurm)."""
    return f"{job_name(site, purpose)}-{job_id}"


def close_marker(site: Site, job_id: str, purpose: str = "desk") -> Path:
    """`<logs>/<NAME>.close`, next to the pane log: its presence tells session job `job_id` to end itself (the
    script sends `/exit` to claude, waits the grace, then kills the tmux session and completes; release 39)."""
    return logs_dir(site) / f"{tmux_name(site, job_id, purpose)}.close"


def close_grace(site: Site) -> int:
    """Config `session.close_grace_seconds` (default 90): how long the job waits for claude to leave after `/exit`."""
    raw = (site.config.get("session") or {}).get("close_grace_seconds", DEFAULT_CLOSE_GRACE)
    try:
        grace = int(raw)
    except (TypeError, ValueError):
        grace = -1
    if grace < 1:
        fail(f"config.json session.close_grace_seconds must be a positive number of seconds, not {raw!r}")
    return grace


def _build(site: Site, purpose: str, prompt: str | None = None, no_prompt: bool = False) -> dict:
    """The sbatch script for the working day's session and what it was built from. `plan` prints exactly
    what `start` submits. Defaults work without `session`, `cluster` or `workday` config keys. The first
    prompt is the runtime's default for the purpose (config `session.prompts.<purpose>`; none for desk,
    "<desk name>, 추가근무 출근" for overtime) unless `prompt` replaces it or `no_prompt` drops it."""
    rt = get(site)
    if purpose not in PURPOSES:
        fail(f"unknown session purpose {purpose!r}; available: {', '.join(PURPOSES)}")
    prompt = None if no_prompt else (prompt or rt.first_prompt(site, purpose))
    argv = rt.launch_command(site, purpose, prompt)
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
    logs = logs_dir(site)
    name = job_name(site, purpose)
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
             f"# harness: {site.pinned_harness_root} (config.json harness.root; exported as DUREH_ROOT)",
             f"# first prompt: {prompt!r}" if prompt else "# first prompt: none"]
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
            "logs": logs, "notes": notes, "problems": problems, "text": text, "prompt": prompt}


HOLD_FUNCTION = f"""# Hold the job while the tmux session lives, polling every 20 s. A close marker ($CLOSE, written by `dureh day
# close` or `dureh site session release`) ends it gracefully: Escape clears any input, `/exit` asks claude to leave,
# the session gets GRACE seconds to end by itself (claude exits -> CLAUDE_REMOTE_EXIT -> {EXIT_HOLD_CLOSING} s hold), then it
# is killed; the script returns and the job completes on its own, no scancel. Uses SOCK NAME PANE CLOSE GRACE.
dureh_session_hold() {{
  local waited=0 seen=
  while tmux -L "$SOCK" has-session -t "$NAME" 2>/dev/null; do
    if [ -z "$seen" ] && [ -e "$CLOSE" ]; then
      seen=1
      dureh_session_say "close marker seen at $(date '+%Y-%m-%dT%H:%M:%S%z'); sending /exit, grace ${{GRACE}} s"
      tmux -L "$SOCK" send-keys -t "$NAME" Escape
      sleep 1
      tmux -L "$SOCK" send-keys -t "$NAME" "/exit" Enter
      continue
    fi
    if [ -n "$seen" ]; then
      if [ "$waited" -ge "$GRACE" ]; then
        tmux -L "$SOCK" kill-session -t "$NAME" 2>/dev/null
        dureh_session_say "killed after grace ${{GRACE}} s; claude had not left after /exit"
        return 0
      fi
      sleep 1
      waited=$((waited + 1))
    else
      sleep 20
    fi
  done
  if [ -n "$seen" ]; then dureh_session_say "graceful exit after ${{waited}} s"; fi
  return 0
}}
dureh_session_say() {{ echo "dureh session: $*"; echo "dureh session: $*" >> "$PANE" 2>/dev/null; }}"""


def _tmux_body(site: Site, name: str, logs, argv: list[str]) -> list[str]:
    """Measured 2026-10-04 (labs/session-test, job 3735671): a batch job keeps `claude --remote-control` alive only
    inside a detached tmux on a private socket, with the submitter's TMUX scrubbed (sbatch exports it; a stale
    socket path kills the new server at once) and a sane TERM; the job then holds while the tmux session lives.
    Release 39 (Director, 2026-10-06, after the 10-05 close killed the desk mid-handoff): the hold loop watches
    the close marker `close_marker()` and ends the session gracefully instead of being scancelled."""
    return ["unset TMUX TMUX_PANE  # sbatch exports the submitter's environment; a stale tmux socket kills the server",
            'export TERM="${TERM:-xterm-256color}"; case "$TERM" in screen*) export TERM=xterm-256color;; esac',
            f"SOCK={shlex.quote(f'dureh-{site.site_id}')}  # private tmux socket for this site",
            f'NAME={shlex.quote(name)}-"${{SLURM_JOB_ID:-local}}"',
            f"PANE_DIR={shlex.quote(str(logs))}",
            'PANE="$PANE_DIR/$NAME.pane.log"',
            'CLOSE="$PANE_DIR/$NAME.close"  # close marker: dureh day close / dureh site session release (sessions.close_marker)',
            f"GRACE={close_grace(site)}  # seconds claude gets to leave after /exit (config session.close_grace_seconds)",
            'rm -f "$CLOSE"  # never start under a stale marker',
            f"CMD={shlex.quote(shlex.join(argv))}",
            f'tmux -L "$SOCK" new-session -d -s "$NAME" -x 200 -y 50 -c {shlex.quote(str(site.root))} '
            f'"$CMD; echo CLAUDE_REMOTE_EXIT \\$?; if [ -e \\"$CLOSE\\" ]; then sleep {EXIT_HOLD_CLOSING}; else sleep {EXIT_HOLD}; fi" || exit $?',
            'tmux -L "$SOCK" pipe-pane -o -t "$NAME" "cat >> $(printf %q "$PANE")"',
            HOLD_FUNCTION,
            "dureh_session_hold"]


def plan(site: Site, purpose: str = "desk", prompt: str | None = None, no_prompt: bool = False) -> str:
    """The sbatch script `start` would submit. Nothing is submitted, nothing is created."""
    return _build(site, purpose, prompt, no_prompt)["text"]


def start(site: Site, actor: str, purpose: str = "desk", force: bool = False, memo: str | None = None,
          prompt: str | None = None, no_prompt: bool = False, apply_landed: bool = True) -> dict:
    """Submit the planned session job and record `session.started` (job_id, submitted_at, wall_time, purpose,
    prompt): the company's working day runs 8 hours from this moment (Director, 2026-10-04); `day close`
    releases only a job with this record. One session per purpose a day: a second start on the same local day
    is refused while today's job of that purpose is still in squeue (`force` with a `memo` overrides and
    records why); when that job is gone the start is a restart (`restart: true`). An `overtime` session after
    the day's session is simply a second session. Either way the day's clock keeps the first session's start.
    A pending landed release (the newest `landed` mailbox message names a checkout the site does not run yet)
    is applied first unless `apply_landed` is False: that checkout's own `dureh --site <root> adapter sync`
    runs as a subprocess, `harness.switched` is recorded and the job is built from the re-read site, so the
    session boots on the new release (Director, 2026-10-06: the release goes on before the start, not by the
    desk after it). A failed switch stops the start; nothing is submitted."""
    b = _build(site, purpose, prompt, no_prompt)
    if b["problems"] and workday.device_on(site, "session"):
        fail("the session cannot start here: " + "; ".join(b["problems"]))
    again = _second_start(site, workday.todays_session(Ledger(site).events()), purpose, force, memo)
    switched = _apply_landed(site, actor) if apply_landed else None
    if switched:
        site = Site(site.root)  # config.json harness.root changed: the script must export the new checkout
        b = _build(site, purpose, prompt, no_prompt)
        again = {**again, "harness_switch": switched}
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
               "runtime": b["runtime"], "script": str(script), "command": cmd, "notes": b["notes"],
               "prompt": b["prompt"], **again}
    return Ledger(site).event(workday.SESSION, actor, None, payload)


def submit_script(site: Site, script: Path, what: str) -> tuple[str, list[str]]:
    """`sbatch --parsable <script>` for a harness-owned job (release 64: the member-run and office-hours jobs, built
    like the session job): (job id, command). Fails with the scheduler's words; the caller records nothing then."""
    cmd = ["sbatch", "--parsable", str(script)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=site.env())
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"sbatch did not run for the {what}: {exc}; nothing was recorded")
    out = (proc.stdout or "").strip()
    m = JOB_ID.search(out.splitlines()[-1] if out else "")
    if proc.returncode != 0 or not m:
        fail(f"sbatch failed for the {what} (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[-500:]};"
             " nothing was recorded")
    return m.group(1), cmd


def slurm_head(site: Site, name: str, block: dict, wall: str, logs: Path, chdir: Path | None = None) -> list[str]:
    """The `#SBATCH` header of a harness-owned job: name, account, partition, qos, cpus, mem, gres from `block` (a
    config block such as `members.slurm` or `office_hours`) over config `slurm`, the wall time, the work dir and the
    logs (`<logs>/%x-%j.out|err`)."""
    slurm = site.config.get("slurm") or {}
    prof = cluster.profile(site)
    partition = block.get("partition") or slurm.get("partition") \
        or next((n for n, p in prof["partitions"].items() if p.get("default")), None)
    head = ["#!/bin/bash", f"#SBATCH --job-name={name}"]
    for flag, value in (("--account", block.get("account") or slurm.get("account") or prof.get("account")),
                        ("--partition", partition), ("--qos", block.get("qos")),
                        ("--cpus-per-task", block.get("cpus")), ("--mem", block.get("mem")), ("--gres", block.get("gres"))):
        if value:
            head.append(f"#SBATCH {flag}={value}")
    head += [f"#SBATCH --time={wall}", f"#SBATCH --chdir={chdir or site.root}",
             f"#SBATCH --output={logs}/%x-%j.out", f"#SBATCH --error={logs}/%x-%j.err"]
    return head


SWITCHED = "harness.switched"
SWITCH_TIMEOUT = 900  # adapter sync regenerates the adapter files and project guidance; measured minutes, not hours


def _apply_landed(site: Site, actor: str) -> dict | None:
    """Switch the site to the pending landed checkout (upstream.pending_landed_checkout) by running that
    checkout's own `dureh --site <root> adapter sync` (exactly what `bin/dureh adapter sync --harness <checkout>`
    execs), then record `harness.switched`. Returns the switch for the session record, None when nothing is
    pending. A non-zero exit fails with the output; the start stops there."""
    from .. import upstream
    from ..util import harness_label

    pending = upstream.pending_landed_checkout(site)
    if not pending:
        return None
    old_root = site.pinned_harness_root
    checkout = pending["checkout"]
    cmd = [str(checkout / "dureh"), "--site", str(site.root), "adapter", "sync"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SWITCH_TIMEOUT, env=site.env())
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"the landed release {checkout} (landed {pending['id']}) could not be applied: {' '.join(cmd)} did not run: "
             f"{exc}; nothing was submitted")
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        fail(f"the landed release {checkout} (landed {pending['id']}) could not be applied: {' '.join(cmd)} exited "
             f"{proc.returncode}:\n{output[-2000:]}\nnothing was submitted; fix the switch (or start with "
             f"--no-apply-landed) and start again")
    tail = output.splitlines()[-5:]
    payload = {"from": str(old_root), "to": str(checkout), "via": "session start", "landed": pending["id"],
               "landed_file": pending["file"], "command": cmd, "output_tail": tail}
    rec = Ledger(site).event(SWITCHED, actor, None, payload)
    return {"from": str(old_root), "from_label": harness_label(old_root), "to": str(checkout),
            "to_label": harness_label(checkout), "landed": pending["id"], "record": rec["id"]}


def _second_start(site: Site, prev: dict | None, purpose: str, force: bool, memo: str | None) -> dict:
    """What a second `session start` on the same local day adds to its record, or why it is refused.
    `previous` names today's last session and `day_start` keeps the day's clock at the first start. A
    different purpose (overtime after the day's session) is simply allowed. Same purpose: the previous job
    still in squeue (or squeue unavailable, so possibly still running) is refused unless `force`, which
    needs a `memo` and is recorded as `force`; the job gone makes it a restart (`restart: true`)."""
    if prev is None:
        return {}
    pp = prev.get("payload") or {}
    job = pp.get("job_id")
    previous = {"record": prev["id"], "job_id": job, "submitted_at": pp.get("submitted_at") or prev["ts"],
                "purpose": pp.get("purpose") or "desk"}
    out = {"previous": previous, "day_start": pp.get("day_start") or previous["submitted_at"]}
    if previous["purpose"] != purpose:
        return out
    q = _squeue(site, job) if job else {"queued": False}
    if q.get("queued") is False:
        return {**out, "restart": True}
    if q.get("queued"):
        why = f"today's {purpose} session job {job} ({prev['id']}, submitted {previous['submitted_at']}) is still in squeue: " \
              f"{q['state']}, time left {q['time_left']}"
    else:
        why = f"today's {purpose} session job {job} ({prev['id']}, submitted {previous['submitted_at']}) may still be running: " \
              f"squeue unavailable ({q.get('error')})"
    if not force:
        fail(f"{why}. A company has one {purpose} session a day; `dureh site session status` shows it. To start another "
             f"anyway: `dureh site session start --purpose {purpose} --force --memo \"why\"`")
    if not (memo or "").strip():
        fail(f"{why}; --force needs --memo: why a second session starts today")
    return {**out, "force": {"memo": memo.strip(), "why": why}}


def last_started(site: Site) -> dict | None:
    found = [e for e in Ledger(site).events() if e["type"] == workday.SESSION]
    return found[-1] if found else None


def running_session(site: Site, events: list[dict] | None = None) -> tuple[dict | None, list[dict]]:
    """The session job dureh may end: the newest `session.started` record whose job squeue still lists (the
    last LOOKBACK distinct jobs are asked, newest first), as {record, job_id, purpose, queue}; None when none
    runs. Squeue unavailable counts the newest job as possibly running (`queue.queued` None) and stops the
    search. The second value lists every job asked and its answer. Only a recorded job is ever named
    (Director, 2026-10-03: dureh ends only session jobs it started)."""
    evs = events if events is not None else Ledger(site).events()
    asked, seen = [], set()
    for e in reversed([x for x in evs if x["type"] == workday.SESSION]):
        p = e.get("payload") or {}
        job = str(p.get("job_id") or "")
        if not job or job in seen:
            continue
        seen.add(job)
        q = _squeue(site, job)
        asked.append({"job_id": job, "record": e["id"], **q})
        if q.get("queued") or q.get("queued") is None:
            return {"record": e["id"], "job_id": job, "purpose": p.get("purpose") or "desk", "queue": q}, asked
        if len(seen) >= LOOKBACK:
            break
    return None, asked


def release(site: Site, actor: str, cancel_now: bool = False, parent: str | None = None, must_run: bool = False) -> dict:
    """End the site's running session job and record `session.released`. Default (`mode: marker`): write the
    close marker next to the job's pane log; the job script sends `/exit` to claude, waits the grace and
    completes by itself, so a desk that runs `day close` inside its own job is not killed mid-turn and the
    upstream team can close from outside the job (10-05: an outside close recorded day.closed, could not
    release, and the job was scancelled by hand while the desk was still writing). `cancel_now` is the old
    immediate scancel (`mode: scancel`), the fallback when a marker was ignored. Either way only the job of a
    `session.started` record in this site's ledger is touched, found by `running_session`, whether the
    caller runs inside that job, in another job (never touched) or in none. `parent` (a day.closed id) is
    referenced by the record and written into the marker. Returns the recorded payload. `must_run` (the
    `session release` command) refuses instead of recording `released: False` when no recorded job runs."""
    led = Ledger(site)
    found, asked = running_session(site, led.events())
    caller = os.environ.get("SLURM_JOB_ID")
    if not found:
        if must_run:
            fail("no running session job to release: " + ("; ".join(
                f"job {a['job_id']} ({a['record']}): " + ("not in squeue" if a.get("queued") is False else a.get("error", "?"))
                for a in asked) or "no session.started record") + "; `dureh site session status` shows the last session")
        out = {"released": False, "reason": "no running session job", "job_id": None, "asked": asked, "caller_job": caller}
        led.event(RELEASED, actor, None, out, [parent] if parent else None)
        return out
    job, purpose = found["job_id"], found["purpose"]
    base = {"job_id": job, "session": found["record"], "purpose": purpose, "caller_job": caller,
            "from": "inside" if caller == job else "outside"}
    if found["queue"].get("queued") is None:
        base["squeue"] = f"unavailable ({found['queue'].get('error')}); the job may still run"
    if cancel_now:
        out = {"released": True, "mode": "scancel", **base, **get(site).release(site, job)}
    else:
        grace = close_grace(site)
        marker = close_marker(site, job, purpose)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"actor": actor, "closed_at": workday._ts(workday._now()), "record": parent,
                                      "job_id": job}, ensure_ascii=False) + "\n", encoding="utf-8")
        out = {"released": True, "mode": "marker", **base, "marker": str(marker), "grace": grace}
    led.event(RELEASED, actor, None, out, [parent or found["record"]])
    return out


def render_release(out: dict) -> str:
    """One line for the terminal after `day close` or `session release`."""
    if not out.get("released"):
        return f"session job: nothing released ({out.get('reason')})"
    if out.get("mode") == "scancel":
        rc = out.get("exit")
        return (f"session job {out['job_id']} cancelled now (scancel exit {rc}" + (f": {out['stderr']}" if out.get("stderr") else "")
                + ")")
    return (f"session job {out['job_id']} ends by itself within ~{out['grace'] + EXIT_HOLD_CLOSING + 20} s (close marker "
            f"written: {out['marker']}); do nothing more in the session")


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
