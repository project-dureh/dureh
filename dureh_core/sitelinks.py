"""Director-side site setup: link a module repository or a project dataset into the site.

These belong to the installer family, not to the ten agent tools. Sources are
read, never written: a module is a git clone of a pinned revision, a project is a
DataLad clone with subdatasets installed without their data.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .records import Ledger
from .site import Site
from .util import fail, git

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _config_add(site: Site, key: str, entry: dict) -> None:
    cfg = json.loads(site.config_path.read_text(encoding="utf-8"))
    items = [e for e in cfg.get(key) or [] if e.get("id") != entry["id"]]
    items.append(entry)
    cfg[key] = items
    site.config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def link_module(site: Site, actor: str, module_id: str, source: str, rev: str) -> dict:
    if site.org.role(actor).kind != "desk":
        fail("only the desk links modules")
    if not NAME.match(module_id):
        fail(f"invalid module id: {module_id!r}")
    dest = site.root / "modules" / module_id
    if dest.exists():
        fail(f"module already linked: {dest}")
    git(["clone", "--quiet", source, str(dest)], site.root, env=site.env())
    try:
        sha = git(["rev-parse", "--verify", f"{rev}^{{commit}}"], dest)
        git(["checkout", "--quiet", "-B", "main", sha], dest)
    except Exception:
        subprocess.run(["rm", "-rf", str(dest)])
        raise
    entry = {"id": module_id, "path": f"modules/{module_id}", "source": source, "rev": sha, "branch": "main"}
    _config_add(site, "modules", entry)
    Ledger(site).event("site.module-linked", actor, None, entry)
    return entry


def link_project(site: Site, actor: str, name: str, source: str) -> dict:
    if site.org.role(actor).kind != "desk":
        fail("only the desk links projects")
    if not NAME.match(name):
        fail(f"invalid project name: {name!r}")
    dest = site.root / "projects" / name
    if dest.exists():
        fail(f"project already linked: {dest}")
    env = site.env()
    proc = subprocess.run(["datalad", "clone", source, str(dest)], capture_output=True, text=True, env=env, timeout=1800)
    if proc.returncode != 0:
        fail(f"datalad clone failed: {(proc.stderr or proc.stdout).strip()[-800:]}")
    proc = subprocess.run(["datalad", "get", "-n", "-r", "."], cwd=dest, capture_output=True, text=True, env=env, timeout=3600)
    installed = proc.returncode == 0
    sha = git(["rev-parse", "HEAD"], dest)
    subs = [l.split()[-1] for l in git(["submodule", "status", "--recursive"], dest, check=False).splitlines() if l.strip()]
    entry = {"id": name, "path": f"projects/{name}", "source": source, "rev": sha, "subdatasets": subs,
             "subdatasets_installed": installed, "data_fetched": False}
    if not installed:
        entry["install_error"] = (proc.stderr or proc.stdout).strip()[-500:]
    _config_add(site, "projects", entry)
    Ledger(site).event("site.project-linked", actor, None, entry)
    return entry
