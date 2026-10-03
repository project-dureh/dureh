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
    entry["generated"] = _after_link(Site(site.root), name)
    return entry


def _after_link(site: Site, name: str) -> list[dict]:
    from . import projectgen

    return projectgen.generate_projects(site, only=name)


PACKAGE = re.compile(r"^[a-z_][a-z0-9_]*$")
DEFAULT_ENV_REBUILD = "uv sync --frozen --all-groups --all-extras"


def _skeleton(module_id: str, package: str, python: str, deps: dict[str, tuple[str, str]]) -> dict[str, str]:
    """Files of a new module. deps: {module id: (git url, rev)} of site modules it imports."""
    dep_lines = "".join(f'    "{d}",\n' for d in deps)
    sources = "".join(f'{d} = {{ git = "{url}", rev = "{rev}" }}\n' for d, (url, rev) in deps.items())
    pyproject = (
        "[project]\n"
        f'name = "{module_id}"\n'
        'version = "0.0.1"\n'
        f'description = "{module_id}: created by dureh site new-module"\n'
        'readme = "README.md"\n'
        f'requires-python = ">={python}"\n'
        f"dependencies = [\n{dep_lines}]\n\n"
        "[dependency-groups]\n"
        "dev = []\n\n"
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n\n'
        "[tool.hatch.build.targets.wheel]\n"
        f'packages = ["src/{package}"]\n'
        + (f"\n[tool.uv.sources]\n{sources}" if sources else "")
    )
    return {
        "pyproject.toml": pyproject,
        ".python-version": python + "\n",
        ".gitignore": ".venv/\n__pycache__/\n*.egg-info/\ndist/\n",
        "README.md": f"# {module_id}\n\nPython package `{package}`. Created with `dureh site new-module`; "
                     "changes arrive through work items.\n",
        "docs/index.md": f"# {module_id}\n\nDocumentation starts here.\n",
        f"src/{package}/__init__.py": f'"""{module_id}."""\n\n__version__ = "0.0.1"\n',
        "tests/test_import.py": (f"import {package}\n\n\ndef test_version():\n"
                                 f"    assert {package}.__version__\n"),
    }


def new_module(site: Site, actor: str, module_id: str, package: str, source: str | None = None,
               python: str | None = None, depends: list[str] | None = None,
               env_rebuild: str = DEFAULT_ENV_REBUILD) -> dict:
    """Create a NEW module repository from the standard skeleton and register it (UR-20261003-01).

    With --source the repository is created there (outside the site, like other module sources) and
    linked; without it the repository lives at modules/<id>. Site modules it imports are pinned by git
    revision (a path dependency breaks inside WI worktrees)."""
    if site.org.role(actor).kind != "desk":
        fail("only the desk (or the Director) creates modules")
    if not NAME.match(module_id):
        fail(f"invalid module id: {module_id!r}")
    if not PACKAGE.match(package):
        fail(f"invalid Python package name: {package!r} (lowercase letters, digits, _)")
    dest = site.root / "modules" / module_id
    if dest.exists():
        fail(f"module already exists: {dest}")
    linked = {e["id"]: e for e in site.config.get("modules") or []}
    deps: dict[str, tuple[str, str]] = {}
    for d in depends or []:
        if d not in linked:
            fail(f"{d} is not a module of this site")
        root = site.root / linked[d]["path"]
        deps[d] = (f"file://{root}", git(["rev-parse", "HEAD"], root))
    if not python:
        for e in linked.values():
            pv = site.root / e["path"] / ".python-version"
            if pv.is_file():
                python = pv.read_text().strip()
                break
    python = python or "3.11"
    repo = Path(source).expanduser().resolve() if source else dest
    if repo.exists() and any(repo.iterdir()):
        fail(f"{repo} exists and is not empty")
    repo.mkdir(parents=True, exist_ok=True)
    for rel, text in _skeleton(module_id, package, python, deps).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    env = site.env()
    ident = ["-c", "user.name=DUREH desk", "-c", "user.email=desk@dureh.invalid"]
    git(["init", "-q", "-b", "main"], repo, env=env)
    lock = {"locked": False}
    proc = subprocess.run(["uv", "lock"], cwd=repo, capture_output=True, text=True, env=env, timeout=900) \
        if _has(["uv", "--version"], env) else None
    if proc is not None and proc.returncode == 0:
        lock = {"locked": True}
    elif proc is not None:
        lock = {"locked": False, "lock_error": (proc.stderr or proc.stdout).strip()[-400:]}
    else:
        lock = {"locked": False, "lock_error": "uv not found"}
    git(["add", "-A"], repo, env=env)
    has_ident = subprocess.run(["git", "config", "user.name"], cwd=repo, capture_output=True, text=True, env=env).stdout.strip()
    git(([] if has_ident else ident) + ["commit", "-q", "-m", f"{module_id}: skeleton from dureh site new-module"], repo, env=env)
    sha = git(["rev-parse", "HEAD"], repo)
    if source:
        entry = link_module(site, actor, module_id, str(repo), sha)
        entry = {**entry, "env_rebuild": env_rebuild}
    else:
        entry = {"id": module_id, "path": f"modules/{module_id}", "source": str(repo), "rev": sha, "branch": "main",
                 "env_rebuild": env_rebuild}
    _config_add(site, "modules", entry)
    built = None
    if lock["locked"]:
        p = subprocess.run(env_rebuild, shell=True, cwd=dest, capture_output=True, text=True, env=env, timeout=1800)
        built = p.returncode == 0
        if not built:
            lock["env_error"] = (p.stderr or p.stdout).strip()[-400:]
    out = {**entry, "package": package, "python": python, "depends": {d: rev for d, (_, rev) in deps.items()},
           **lock, "venv_built": built}
    Ledger(site).event("site.module-created", actor, None, out)
    if not lock["locked"]:
        out["next"] = "uv.lock could not be created here; the first WI on this module runs `uv lock` and commits it"
    return out


def _has(cmd: list[str], env: dict) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, env=env, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
