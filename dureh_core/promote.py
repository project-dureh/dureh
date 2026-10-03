"""Harness-mediated promotion of verified checkpoints into a derivatives subdataset (DataLad).

The data-steward never writes derivatives/ directly: WI worktrees do not contain subdataset content,
and project main trees are closed to agents. This tool copies into a per-WI clone of the subdataset,
saves there, fast-forwards the subdataset from the clone, fetches the annexed content, records the new
subdataset state in the project, and removes the clone.
"""
from __future__ import annotations

import json
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


METADATA_FILES = ("dataset_description.json", "README", "README.md", "CHANGES", "LICENSE")


def _context(site: Site, actor: str, wi: str, dataset: str | None, agent_id: str | None):
    if actor != "data-steward":
        fail("only data-steward promotes")
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
    if dataset is None:
        return led, p, root, None
    if not NAME_RE.match(dataset):
        fail(f"dataset must be one name under derivatives/: {dataset!r}")
    ds = root / "derivatives" / dataset
    if not (ds / ".datalad").is_dir():
        fail(f"{ds} is not a DataLad dataset")
    return led, p, root, ds


def _apply(site: Site, led: Ledger, wi: str, root: Path, ds: Path, dataset: str,
           pairs: list[tuple[Path, PurePosixPath]], msg: str) -> str:
    """Copy pairs into a per-WI clone of ds, save, bring ds forward, save the project. Caller holds the lock."""
    env = site.env()
    clone = site.worktrees / f"{wi}-promote-{dataset}"
    if clone.exists():
        fail(f"{clone} is left over from an earlier promotion; remove it first")
    remote, uuid = f"dureh-promote-{wi}", ""
    site.worktrees.mkdir(exist_ok=True)
    targets = [str(t) for _, t in pairs]
    try:
        _run(["datalad", "clone", str(ds), str(clone)], site.root, env)
        uuid = _run(["git", "config", "annex.uuid"], clone, env, check=False).strip()
        for f, t in pairs:
            out = clone / t
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.is_symlink() or out.exists():
                out.unlink()  # an annexed file in a fresh clone is a link to absent content
            shutil.copyfile(f, out)
        _run(["datalad", "save", "-d", str(clone), "-m", msg, *targets], clone, env)
        _run(["git", "remote", "add", remote, str(clone)], ds, env)
        _run(["datalad", "update", "-d", str(ds), "-s", remote, "--how", "ff-only"], ds, env)
        _run(["datalad", "get", "-d", str(ds), "-s", remote, *targets], ds, env)
    finally:
        _run(["git", "remote", "remove", remote], ds, env, check=False)
        if uuid:
            _run(["git", "annex", "dead", uuid], ds, env, check=False)
        if clone.exists():
            _rmtree(clone)
    _run(["datalad", "save", "-d", str(root), "-m", msg, str(ds)], root, env)
    return _run(["git", "rev-parse", "HEAD"], ds, env).strip()


def _require_clean(site: Site, ds: Path, dataset: str) -> None:
    if _run(["git", "status", "--porcelain"], ds, site.env()).strip():
        fail(f"derivatives/{dataset} is not clean; save or revert it first")


def promote(site: Site, actor: str, wi: str, run_id: str, source: str, dataset: str, dest: str | None = None,
            agent_id: str | None = None) -> dict:
    led, p, root, ds = _context(site, actor, wi, dataset, agent_id)
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
            fail(f"derivatives/{dataset}/{t} already exists; derivatives are final, promote to a new path"
                 + (" (metadata files: use --metadata)" if str(t) in METADATA_FILES else ""))
    with led.lock():
        _require_clean(site, ds, dataset)
        run = _completed_run(led, run_id, p["target"])
        job = run["payload"].get("job_id")
        msg = f"Promote {source} (WI {wi}, run {run_id}, job {job})"
        commit = _apply(site, led, wi, root, ds, dataset, list(zip(files, targets)), msg)
        return led.append("run", "promotion", actor, wi, {
            "run_type": "promotion", "status": "completed", "source": source, "dataset": f"derivatives/{dataset}",
            "dest": [f"derivatives/{dataset}/{t}" for t in targets],
            "from_run": run_id, "job_id": job, "commit": commit}, [run_id])


def replace_metadata(site: Site, actor: str, wi: str, dataset: str, name: str, input_path: str,
                     agent_id: str | None = None) -> dict:
    """Create or replace one top-level metadata file of a derivatives dataset (e.g. dataset_description.json)."""
    from .paths import classify

    led, p, root, ds = _context(site, actor, wi, dataset, agent_id)
    if name not in METADATA_FILES:
        fail(f"--metadata must be one of {', '.join(METADATA_FILES)}")
    src = Path(input_path).expanduser()
    if src.is_symlink() or not src.is_file():
        fail(f"input must be a regular file: {input_path}")
    zone = classify(site, src.resolve()).zone
    if zone != "scratch" and not is_within(src.resolve(), (root / "checkpoints").resolve()):
        fail("input must be in your scratch space or under the project's checkpoints/")
    if name.endswith(".json"):
        import json

        try:
            if not isinstance(json.loads(src.read_text(encoding="utf-8")), dict):
                fail(f"{name} must be a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            fail(f"{name} is not valid JSON: {exc}")
    if not site.org.scope_allows(actor, f"{p['target']}/derivatives/{dataset}/{name}"):
        fail(f"data-steward may not write derivatives/{dataset}/{name}")
    with led.lock():
        _require_clean(site, ds, dataset)
        old = (ds / name).exists() or (ds / name).is_symlink()
        msg = f"{'Update' if old else 'Add'} {name} (WI {wi})"
        commit = _apply(site, led, wi, root, ds, dataset, [(src, PurePosixPath(name))], msg)
        return led.append("run", "promotion", actor, wi, {
            "run_type": "promotion", "status": "completed", "source": "metadata", "dataset": f"derivatives/{dataset}",
            "dest": [f"derivatives/{dataset}/{name}"], "replaced": old, "commit": commit})



REPORTS = PurePosixPath("reports")


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def promote_report(site: Site, actor: str, wi: str, run_id: str, source: str, dataset: str, name: str,
                   agent_id: str | None = None) -> dict:
    """Land a code-generated report into derivatives/<dataset>/reports/<name>/ (UR-20261003-02).

    Director's decision (2026-10-03): the final report follows BIDS and lives inside the derivatives
    dataset, in a reports folder below it. The harness adds provenance.json: work item, run and job,
    module revisions, containers and inputs recorded in the run, and the sha256 of every file."""
    led, p, root, ds = _context(site, actor, wi, dataset, agent_id)
    if not NAME_RE.match(name):
        fail(f"--name must be one directory name: {name!r}")
    src_rel = safe_relative(source)
    if len(src_rel.parts) < 3 or src_rel.parts[0] != "checkpoints":
        fail("source must be a report directory or file under checkpoints/<step>/")
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
    base = REPORTS / name
    rel = [PurePosixPath(f.name) if src.is_file() else PurePosixPath(f.relative_to(src).as_posix()) for f in files]
    if PurePosixPath("provenance.json") in rel:
        fail("the report may not contain provenance.json; the harness writes it")
    targets = [base / r for r in rel] + [base / "provenance.json"]
    for t in targets:
        if not site.org.scope_allows(actor, f"{p['target']}/derivatives/{dataset}/{t}"):
            fail(f"data-steward may not write derivatives/{dataset}/{t}")
    if (ds / base).exists() or (ds / base).is_symlink():
        fail(f"derivatives/{dataset}/{base} already exists; reports are final, land the next one under a new name")
    env = site.env()
    with led.lock():
        _require_clean(site, ds, dataset)
        run = _completed_run(led, run_id, p["target"])
        modules = {}
        for e in site.config.get("modules") or []:
            mroot = site.root / e["path"]
            if (mroot / ".git").exists():
                modules[e["id"]] = _run(["git", "rev-parse", "HEAD"], mroot, env).strip()
        prov = {"wi": wi, "run": run_id, "job_id": run["payload"].get("job_id"), "source": source,
                "dataset": f"derivatives/{dataset}", "landed_by": actor, "modules": modules,
                "containers": run["payload"].get("containers") or run["payload"].get("container"),
                "inputs": run["payload"].get("inputs"),
                "files": {str(r): _sha256(f.resolve()) for r, f in zip(rel, files)}}
        tmp = site.worktrees / f"{wi}-report-provenance.json"
        site.worktrees.mkdir(exist_ok=True)
        tmp.write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            msg = f"Land report {name} from {source} (WI {wi}, run {run_id}, job {prov['job_id']})"
            commit = _apply(site, led, wi, root, ds, dataset, list(zip(files + [tmp], targets)), msg)
        finally:
            tmp.unlink(missing_ok=True)
        return led.append("run", "promotion", actor, wi, {
            "run_type": "promotion", "kind": "report", "status": "completed", "source": source,
            "dataset": f"derivatives/{dataset}", "dest": [f"derivatives/{dataset}/{t}" for t in targets],
            "from_run": run_id, "job_id": prov["job_id"], "commit": commit, "modules": modules}, [run_id])
