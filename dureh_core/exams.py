"""Qualification exam scaffold.

An exam task lives in org/exams/<role>/<task-id>/task.yaml with an optional
fixture/ directory. A candidate works on a copy of the fixture; `verify --exam`
scores the candidate's workspace with the task's executable checks and treats
forbidden writes as hard failures. Tasks marked split: example may be used to
tune prompts or skills; split: holdout must not be. Phase 1 ships one example
task per role. It is not a qualification of any model.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
import time
from pathlib import Path

from . import yamlsub
from .org import HARNESS_ROOT
from .records import Ledger
from .site import Site
from .util import fail


def _tree_hashes(root: Path) -> dict[str, str]:
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            p = Path(base) / f
            if p.is_file() and not p.is_symlink():
                out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def tasks(role: str, site: Site | None = None) -> list[Path]:
    """Harness exam tasks for a role plus the installed package's evaluation cases."""
    dirs = [HARNESS_ROOT / "org" / "exams" / role]
    if site is not None:
        evals = (site.package_manifest().get("lessons") or {}).get("evals")
        if evals:
            dirs.append(site.startup / evals / role)
    return sorted(p.parent for d in dirs for p in d.glob("*/task.yaml"))


def score_workspace(task_dir: Path, workspace: Path) -> dict:
    spec = yamlsub.load_file(task_dir / "task.yaml")
    fixture = task_dir / "fixture"
    before = _tree_hashes(fixture) if fixture.is_dir() else {}
    after = _tree_hashes(workspace)
    changed = sorted({k for k in set(before) | set(after) if before.get(k) != after.get(k)})
    forbidden = [c for c in changed if any(fnmatch.fnmatchcase(c, g) for g in spec.get("forbidden_writes") or [])]
    missing = [o for o in spec.get("required_outputs") or [] if not (workspace / o).exists()]
    checks = []
    for cmd in spec.get("checks") or []:
        t0 = time.monotonic()
        proc = subprocess.run(cmd, shell=True, cwd=workspace, capture_output=True, text=True, timeout=600)
        checks.append({"command": cmd, "exit": proc.returncode, "seconds": round(time.monotonic() - t0, 2)})
    passed = not forbidden and not missing and all(c["exit"] == 0 for c in checks)
    return {"task": spec["id"], "split": spec.get("split"), "result": "pass" if passed else "fail",
            "changed": changed, "forbidden_writes": forbidden, "missing_outputs": missing, "checks": checks,
            "hard_failure": bool(forbidden)}


def score(site: Site, actor: str, role: str, answers: str | None, task: str | None) -> dict:
    if not answers:
        fail("--answers WORKSPACE is required (the candidate's copy of the task fixture)")
    candidates = [t for t in tasks(role, site) if task is None or t.name == task]
    if not candidates:
        fail(f"no exam tasks for role {role}" + (f" named {task}" if task else ""))
    if len(candidates) > 1:
        fail("several tasks exist; pass --task")
    result = score_workspace(candidates[0], Path(answers))
    Ledger(site).append("run", "exam", actor, None, {"run_type": "exam", "status": result["result"], "role": role, **result})
    return result
