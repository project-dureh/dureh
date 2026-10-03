"""Cluster profile: what the scheduler offers this site (partitions, GPUs, time limits, account, QoS).

Cluster specifics live in config.json `cluster`, not in code. `dureh site cluster detect` fills the
profile from `sinfo` and `sacctmgr` on Slurm clusters; other clusters edit the config by hand.
"""
from __future__ import annotations

import getpass
import json
import re
import subprocess
from datetime import timedelta

from .site import Site
from .util import fail

DEFAULTS = {"scheduler": "slurm", "partitions": {}}
SINFO_FORMAT = "%P|%G|%D|%l"


def profile(site: Site) -> dict:
    """config.json `cluster` over the defaults."""
    return {**DEFAULTS, **(site.config.get("cluster") or {})}


def _run(site: Site, cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=site.env())
    except (OSError, subprocess.SubprocessError):
        return None


def _gpus(gres: str) -> list[tuple[str | None, int]]:
    """`gpu:a100:4(S:0-1),gpu:2` -> [("a100", 4), (None, 2)]; other GRES are ignored."""
    out = []
    for item in re.sub(r"\([^)]*\)", "", gres or "").split(","):
        parts = item.strip().split(":")
        if parts[0] != "gpu" or len(parts) < 2:
            continue
        count = parts[-1] if parts[-1].isdigit() else "1"
        kind = parts[1] if len(parts) > 2 or not parts[1].isdigit() else None
        out.append((kind, int(count)))
    return out


def parse_sinfo(text: str) -> dict:
    """Lines of `sinfo -h -o "%P|%G|%D|%l"` -> partitions {name: {nodes, time_limit, default, gpus}}."""
    parts: dict = {}
    for line in text.splitlines():
        fields = line.strip().split("|")
        if len(fields) != 4 or not fields[0]:
            continue
        name, gres, nodes, limit = fields
        default = name.endswith("*")
        name = name.rstrip("*")
        p = parts.setdefault(name, {"nodes": 0, "time_limit": limit, "default": False, "gpus": []})
        p["nodes"] += int(nodes) if nodes.isdigit() else 0
        p["default"] = p["default"] or default
        for kind, per_node in _gpus(gres):
            p["gpus"].append({"type": kind, "per_node": per_node, "nodes": int(nodes) if nodes.isdigit() else 0})
    return parts


def parse_assoc(text: str) -> dict:
    """Lines of `sacctmgr -n -P show assoc user=$USER format=account,qos` -> account (first), accounts, qos."""
    accounts, qos = [], []
    for line in text.splitlines():
        fields = line.strip().split("|")
        if not fields or not fields[0]:
            continue
        if fields[0] not in accounts:
            accounts.append(fields[0])
        for q in (fields[1] if len(fields) > 1 else "").split(","):
            if q and q not in qos:
                qos.append(q)
    out = {"account": accounts[0] if accounts else None, "qos": qos}
    if len(accounts) > 1:
        out["accounts"] = accounts
    return out


def detect(site: Site) -> dict:
    """Ask the scheduler (Slurm) for the profile; nothing is written."""
    proc = _run(site, ["sinfo", "-h", "-o", SINFO_FORMAT])
    if proc is None or proc.returncode != 0:
        err = (proc.stderr.strip() if proc else "") or "sinfo is not available"
        fail(f"cannot detect the cluster: {err}. Edit config.json `cluster` by hand on non-Slurm clusters")
    out = {"scheduler": "slurm", "partitions": parse_sinfo(proc.stdout)}
    user = site.env().get("USER") or getpass.getuser()
    acc = _run(site, ["sacctmgr", "-n", "-P", "show", "assoc", f"user={user}", "format=account,qos"])
    out.update(parse_assoc(acc.stdout) if acc is not None and acc.returncode == 0 else {"account": None, "qos": []})
    return out


def write(site: Site, actor: str, detected: dict) -> dict:
    """Save a detected profile into config.json `cluster`; keys the scheduler cannot tell (for example a
    container runtime) are kept, and the order of config keys is unchanged."""
    if site.org.role(actor).kind != "desk":
        fail("only the desk (or the Director) writes the cluster profile")
    cfg = json.loads(site.config_path.read_text(encoding="utf-8"))
    cfg["cluster"] = {**(cfg.get("cluster") or {}), **detected}
    site.config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"written": str(site.config_path), "cluster": cfg["cluster"]}


_TIME = re.compile(r"^(?:(\d+)-)?(\d+)(?::(\d+))?(?::(\d+))?$")


def parse_time(value: str | None) -> timedelta | None:
    """Slurm time: M, M:S, H:M:S, D-H, D-H:M, D-H:M:S; None when unknown or unlimited."""
    m = _TIME.match((value or "").strip())
    if not m:
        return None
    days, a, b, c = m.groups()
    a, b, c = int(a), int(b or 0), int(c or 0)
    if days is not None:  # D-H[:M[:S]]
        return timedelta(days=int(days), hours=a, minutes=b, seconds=c)
    if m.group(4) is not None:  # H:M:S
        return timedelta(hours=a, minutes=b, seconds=c)
    return timedelta(minutes=a, seconds=b)  # M or M:S


def hms(hours: float) -> str:
    """8 -> 08:00:00, 7.5 -> 07:30:00 (sbatch --time)."""
    total = int(round(hours * 3600))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
