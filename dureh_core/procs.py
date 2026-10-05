"""Leftover processes of a finished agent (release 27).

Evidence: WI-0115 left `find /` and wait loops running for hours after its agent had finished. An agent is
not an operating-system process (every agent runs inside the runtime's one process), so the harness marks
each Bash command an agent runs with `DUREH_AGENT_ID=<agent id>` in its environment (the PreToolUse hook
rewrites the command). When the runtime reports the agent finished, every process of this user that still
carries that marker, and belongs to the same scheduler job as the session (same `SLURM_JOB_ID`, or none),
is terminated: SIGTERM, a short grace period, then SIGKILL. The session's own process tree (this process
and its ancestors), pid 1, and any process without the marker are never touched; processes of a submitted
scheduler job carry another `SLURM_JOB_ID` and are skipped. Linux `/proc` only; elsewhere nothing is killed
and the result says so.
"""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path

PROC = Path("/proc")
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "python", "python3", "dureh", "env", "timeout"}


def _read(pid: int, name: str) -> bytes | None:
    try:
        return (PROC / str(pid) / name).read_bytes()
    except OSError:
        return None


def _stat(pid: int) -> tuple[str, int] | None:
    """(comm, ppid) from /proc/<pid>/stat; None when the process is gone or unreadable."""
    raw = _read(pid, "stat")
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        comm = text[text.index("(") + 1:text.rindex(")")]
        fields = text[text.rindex(")") + 2:].split()
        return comm, int(fields[1])
    except (ValueError, IndexError):
        return None


def ancestors(pid: int | None = None) -> list[int]:
    """This process (or `pid`) and its ancestors up to pid 1, nearest first."""
    out, cur, seen = [], pid or os.getpid(), set()
    while cur > 0 and cur not in seen:
        seen.add(cur)
        out.append(cur)
        st = _stat(cur)
        if st is None:
            break
        cur = st[1]
    return out


def runtime_pid(pid: int | None = None) -> int | None:
    """The runtime's process the hook runs under: the nearest ancestor that is not a shell or the harness."""
    for p in ancestors(pid)[1:]:
        st = _stat(p)
        if st is None:
            return None
        if st[0] not in SHELLS and not st[0].startswith("python"):
            return p
    return None


def _environ(pid: int) -> dict[str, str] | None:
    raw = _read(pid, "environ")
    if raw is None:
        return None
    out = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            k, _, v = item.partition(b"=")
            out[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return out


def _cmdline(pid: int) -> str:
    raw = _read(pid, "cmdline") or b""
    return " ".join(x.decode("utf-8", "replace") for x in raw.split(b"\0") if x)[:300]


def marked_processes(agent_id: str, job: str | None, protect: set[int]) -> list[dict]:
    """Live processes of this user whose environment carries DUREH_AGENT_ID=<agent_id> and the same
    SLURM_JOB_ID as the caller (`job`; None means none), excluding `protect`."""
    if not agent_id or not PROC.is_dir():
        return []
    uid = os.getuid()
    found = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid <= 1 or pid in protect:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        env = _environ(pid)
        if not env or env.get("DUREH_AGENT_ID") != agent_id or env.get("SLURM_JOB_ID") != job:
            continue
        st = _stat(pid)
        found.append({"pid": pid, "ppid": st[1] if st else None, "comm": st[0] if st else None, "cmd": _cmdline(pid)})
    return sorted(found, key=lambda p: p["pid"])


def _alive(pid: int) -> bool:
    """Present and not a zombie."""
    raw = _read(pid, "stat")
    if not raw:
        return False
    text = raw.decode("utf-8", "replace")
    try:
        return text[text.rindex(")") + 2:].split()[0] != "Z"
    except (ValueError, IndexError):
        return False


def kill_leftovers(agent_id: str, protect: set[int] | None = None, grace: float = 2.0,
                   job: str | None = None) -> dict:
    """Terminate the marked processes of a finished agent. Returns what was found and killed."""
    if not PROC.is_dir():
        return {"method": "none", "note": "no /proc on this platform; nothing was scanned", "scanned": 0, "killed": []}
    safe = set(protect or ()) | set(ancestors())
    job = os.environ.get("SLURM_JOB_ID") if job is None else job
    procs = marked_processes(agent_id, job, safe)
    killed, errors = [], []
    for p in procs:
        try:
            os.kill(p["pid"], signal.SIGTERM)
            killed.append({**p, "signal": "TERM"})
        except ProcessLookupError:
            continue
        except OSError as exc:
            errors.append({"pid": p["pid"], "error": str(exc)})
    deadline = time.monotonic() + max(0.0, grace)
    pending = [k for k in killed]
    while pending and time.monotonic() < deadline:
        pending = [k for k in pending if _alive(k["pid"])]
        if pending:
            time.sleep(0.05)
    for k in pending:
        try:
            os.kill(k["pid"], signal.SIGKILL)
            k["signal"] = "KILL"
        except ProcessLookupError:
            pass
        except OSError as exc:
            errors.append({"pid": k["pid"], "error": str(exc)})
    out = {"method": "environ-marker", "agent_id": agent_id, "job": job, "scanned": len(procs), "killed": killed}
    if errors:
        out["errors"] = errors
    return out
