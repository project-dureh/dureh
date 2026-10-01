"""Harness-mediated promotion of verified checkpoints into a derivatives subdataset (DataLad).

The data-steward never writes derivatives/ directly: WI worktrees do not contain subdataset content,
and project main trees are closed to agents. This tool copies into a per-WI clone of the subdataset,
saves there, fast-forwards the subdataset from the clone, fetches the annexed content, records the new
subdataset state in the project, and removes the clone.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath

from .paths import target_root
from .records import Ledger
from .site import Site
from .util import fail, is_within, safe_relative

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _run(cmd: list[str], cwd: Path, env: dict, check: bool = True) -> str:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=3600)
    if check and proc.returncode != 0:
        fail(f"`{' '.join(cmd[:3])}` failed: {(proc.stderr or proc.stdout).strip()[-800:]}")
    return proc.stdout


def _completed_run(led: Ledger, run_id: str, target: str) -> dict:
    run = led.get("run", run_id)
    if run["payload"].get("run_type") != "slurm":
        fail(f"{run_id} is not a scheduler run")
    if not run["wi"] or led.get("work-item", run["wi"])["payload"]["target"] != target:
        fail(f"{run_id} did not run for {target}")
    states = [e["payload"].get("scheduler_state") or "" for e in led.events(run["wi"])
              if e["type"] == "run.status" and e["payload"].get("run") == run_id]
    if not states or not states[-1].startswith("COMPLETED"):
        fail(f"{run_id} has no completed scheduler state (latest: {states[-1] if states else 'none'}); "
             "run `dureh slurm status` after the job finished")
    return run


def _rmtree(path: Path) -> None:
    def unlock(func, p, _exc):
        os.chmod(os.path.dirname(p), stat.S_IRWXU)
        os.chmod(p, stat.S_IRWXU)
        func(p)

    shutil.rmtree(path, onerror=unlock)


def promote(site: Site, actor: str, wi: str, run_id: str, source: str, dataset: str, dest: str | None = None,
            agent_id: str | None = None) -> dict:
    if actor != "data-steward":
        fail("only data-steward promotes into derivatives")
    led = Ledger(site)
    if agent_id:
        bound = [e["wi"] for e in led.events() if e["type"] == "agent.started" and e["payload"].get("agent_id") == agent_id]
        if not bound or bound[-1] != wi:
            fail(f"this agent is bound to {bound[-1] if bound else 'no work item'}, not {wi}")
    p = led.get("work-item", wi)["payload"]
    if p["role"] != actor:
        fail(f"{wi} is a {p['role']} work item, not data-steward")
    if led.wi_status(wi)["status"] != "dispatched":
        fail(f"{wi} is not dispatched")
    root = target_root(site, p["target"])
    if not p["target"].startswith("projects/") or root is None or not root.is_dir():
        fail(f"{wi} does not target a project")
    if not NAME_RE.match(dataset):
        fail(f"dataset must be one name under derivatives/: {dataset!r}")
    ds = root / "derivatives" / dataset
    if not (ds / ".datalad").is_dir():
        fail(f"{ds} is not a DataLad dataset")

    src_rel = safe_relative(source)
    if len(src_rel.parts) < 3 or src_rel.parts[0] != "checkpoints":
        fail("source must be a file or directory under checkpoints/<step>/")
    checkpoints = (root / "checkpoints").resolve()
    src = root / src_rel.as_posix()
    if not src.exists():
        fail(f"source does not exist: {source}")
    files = [src] if src.is_file() else sorted(x for x in src.rglob("*") if x.is_file() or x.is_symlink())
    if not files:
        fail(f"source has no files: {source}")
    for f in files:
        if not is_within(f.resolve(), checkpoints) or not f.resolve().is_file():
            fail(f"{f.relative_to(root)} resolves outside checkpoints/ or is not a file")
    base = safe_relative(dest) if dest else PurePosixPath(*src_rel.parts[2:])
    if base.parts[0] in (".git", ".datalad", ".gitattributes", ".gitmodules"):
        fail(f"dest may not touch dataset metadata: {base}")
    targets = [base if src.is_file() else base / f.relative_to(src).as_posix() for f in files]
    for t in targets:
        if not site.org.scope_allows(actor, f"{p['target']}/derivatives/{dataset}/{t}"):
            fail(f"data-steward may not write derivatives/{dataset}/{t}")
        if (ds / t).exists() or (ds / t).is_symlink():
            fail(f"derivatives/{dataset}/{t} already exists; derivatives are final, promote to a new path")

    env = site.env()
    with led.lock():
        if _run(["git", "status", "--porcelain"], ds, env).strip():
            fail(f"derivatives/{dataset} is not clean; save or revert it first")
        run = _completed_run(led, run_id, p["target"])
        job = run["payload"].get("job_id")
        msg = f"Promote {source} (WI {wi}, run {run_id}, job {job})"
        clone = site.worktrees / f"{wi}-promote-{dataset}"
        if clone.exists():
            fail(f"{clone} is left over from an earlier promotion; remove it first")
        remote, uuid = f"dureh-promote-{wi}", ""
        site.worktrees.mkdir(exist_ok=True)
        try:
            _run(["datalad", "clone", str(ds), str(clone)], site.root, env)
            uuid = _run(["git", "config", "annex.uuid"], clone, env, check=False).strip()
            for f, t in zip(files, targets):
                (clone / t).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, clone / t)
            _run(["datalad", "save", "-d", str(clone), "-m", msg, *[str(t) for t in targets]], clone, env)
            _run(["git", "remote", "add", remote, str(clone)], ds, env)
            _run(["datalad", "update", "-d", str(ds), "-s", remote, "--how", "ff-only"], ds, env)
            _run(["datalad", "get", "-d", str(ds), "-s", remote, *[str(t) for t in targets]], ds, env)
        finally:
            _run(["git", "remote", "remove", remote], ds, env, check=False)
            if uuid:
                _run(["git", "annex", "dead", uuid], ds, env, check=False)
            if clone.exists():
                _rmtree(clone)
        _run(["datalad", "save", "-d", str(root), "-m", msg, str(ds)], root, env)
        commit = _run(["git", "rev-parse", "HEAD"], ds, env).strip()
        return led.append("run", "promotion", actor, wi, {
            "run_type": "promotion", "status": "completed", "source": source, "dataset": f"derivatives/{dataset}",
            "dest": [f"derivatives/{dataset}/{t}" for t in targets],
            "from_run": run_id, "job_id": job, "commit": commit}, [run_id])
