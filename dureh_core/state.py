"""Site-owned state dataset: persona, memory, records.

Backends: `git` (plain repository) or `datalad` (DataLad dataset, git-annex for
large files). The optional RIA sibling is configured per site. Saving happens at
session end; pushing to RIA is attempted and reported, never silently assumed.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .site import Site
from .util import fail


def _run(cmd: list[str], cwd: Path, env: dict, timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def backend(site: Site) -> str:
    return (site.config.get("state") or {}).get("backend") or "git"


def init_state(site: Site, author: dict | None = None) -> dict:
    env = site.env()
    kind = backend(site)
    state = site.state
    if kind == "datalad":
        if not shutil.which("datalad", path=env.get("PATH")) or not shutil.which("git-annex", path=env.get("PATH")):
            fail("state backend datalad needs datalad and git-annex on PATH (config env.path_prepend)")
        proc = _run(["datalad", "create", "--force", "--description", f"dureh state {site.site_id}", str(state)], site.root, env)
        if proc.returncode != 0:
            fail(f"datalad create failed: {proc.stderr.strip()[-800:]}")
        # records, persona, and memory stay in git; only large attachments go to annex
        attrs = state / ".gitattributes"
        existing = attrs.read_text(encoding="utf-8") if attrs.is_file() else ""
        attrs.write_text(existing + "* annex.largefiles=(largerthan=1mb)\n*.json annex.largefiles=nothing\n"
                         "*.md annex.largefiles=nothing\n*.jsonl annex.largefiles=nothing\n", encoding="utf-8")
    else:
        proc = _run(["git", "init", "-q", str(state)], site.root, env)
        if proc.returncode != 0:
            fail(f"git init of state failed: {proc.stderr.strip()}")
    for keep in (state / "records").iterdir():
        if keep.is_dir() and not any(keep.iterdir()):
            (keep / ".keep").write_text("", encoding="utf-8")
    return save_state(site, "initialize site state", require=True)


def save_state(site: Site, message: str, require: bool = False) -> dict:
    env = site.env()
    state = site.state
    result = {"backend": backend(site), "saved": False, "pushed": None}
    if not (state / ".git").exists():
        if require:
            fail(f"state is not a repository: {state}")
        result["error"] = "state is not a repository"
        return result
    if backend(site) == "datalad":
        proc = _run(["datalad", "save", "-m", message, "."], state, env)
    else:
        _run(["git", "add", "-A"], state, env)
        staged = _run(["git", "diff", "--cached", "--quiet"], state, env)
        proc = staged if staged.returncode == 0 else _run(["git", "commit", "-q", "-m", message], state, env)
    result["saved"] = proc.returncode == 0
    if proc.returncode != 0:
        result["error"] = (proc.stderr or proc.stdout).strip()[-800:]
        if require:
            fail(f"state save failed: {result['error']}")
        return result
    ria = (site.config.get("state") or {}).get("ria")
    if ria and backend(site) == "datalad":
        push = _run(["datalad", "push", "--to", "ria"], state, env, timeout=900)
        result["pushed"] = push.returncode == 0
        if push.returncode != 0:
            result["push_error"] = (push.stderr or push.stdout).strip()[-800:]
    head = _run(["git", "rev-parse", "HEAD"], state, env)
    result["revision"] = head.stdout.strip() if head.returncode == 0 else None
    return result


def unsaved(site: Site) -> list[str]:
    state = site.state
    if not (state / ".git").exists():
        return ["state is not a repository"]
    proc = _run(["git", "status", "--porcelain"], state, site.env())
    return [l for l in proc.stdout.splitlines() if l.strip()]
