"""Session runtimes, chosen by config.json `session.runtime`, and the day job plan (printed, never submitted)."""
from __future__ import annotations

import shlex

from .. import cluster, workday
from ..site import Site
from ..util import fail
from .base import PURPOSES, SessionRuntime
from .claude_code import ClaudeCode

RUNTIMES = {ClaudeCode.name: ClaudeCode}


def get(site: Site) -> SessionRuntime:
    name = (site.config.get("session") or {}).get("runtime") or ClaudeCode.name
    if name not in RUNTIMES:
        fail(f"unknown session runtime {name!r} in config.json session.runtime; available: {', '.join(sorted(RUNTIMES))}")
    return RUNTIMES[name]()


def plan(site: Site, purpose: str = "desk") -> str:
    """The sbatch script that would start the session for the working day. Nothing is submitted."""
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
    logs = site.root / "logs" / "sessions"
    name = f"dureh-{site.root.name}-{purpose}"
    lines = ["#!/bin/bash", f"#SBATCH --job-name={name}"]
    for flag, value in (("--account", account), ("--partition", partition), ("--qos", ss.get("qos")),
                        ("--cpus-per-task", ss.get("cpus")), ("--mem", ss.get("mem")), ("--gres", ss.get("gres"))):
        if value:
            lines.append(f"#SBATCH {flag}={value}")
    lines += [f"#SBATCH --time={cluster.hms(wall)}", f"#SBATCH --chdir={site.root}",
              f"#SBATCH --output={logs}/%x-%j.out", f"#SBATCH --error={logs}/%x-%j.err",
              f"# plan only (dureh site session plan): {rt.name} session, purpose {purpose}, "
              f"workday {hours:g} h + overtime up to {max(0.0, wall - hours):g} h; nothing was submitted"]
    notes = list(rt.check(site))
    part = prof["partitions"].get(partition) if partition else None
    if prof["partitions"] and partition and part is None:
        notes.append(f"partition {partition!r} is not in the cluster profile")
    limit = cluster.parse_time((part or {}).get("time_limit"))
    if limit is not None and limit.total_seconds() < wall * 3600:
        notes.append(f"partition {partition!r} time limit {part['time_limit']} is shorter than the requested --time")
    lines += [f"# note: {n}" for n in notes]
    lines += [f"export {k}={shlex.quote(v)}" for k, v in sorted(rt.env(site).items())]
    lines.append(f"# before submitting: mkdir -p {shlex.quote(str(logs))} (Slurm opens the logs before the job runs)")
    lines.append("exec " + shlex.join(argv))
    return "\n".join(lines) + "\n"
