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
    """Add or replace the `key` entry with this id; keys an existing entry has and the new one lacks are kept
    (for example a module's `submit` block seeded by the package's config_defaults before the module was linked)."""
    cfg = json.loads(site.config_path.read_text(encoding="utf-8"))
    old = next((e for e in cfg.get(key) or [] if e.get("id") == entry["id"]), None)
    items = [e for e in cfg.get(key) or [] if e.get("id") != entry["id"]]
    items.append({**old, **entry} if isinstance(old, dict) else entry)
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


FINAL_STATES = ("promoted", "stopped", "reverted")
STRUCTURAL_SUFFIXES = {".toml", ".lock", ".cfg", ".ini", ".txt", ".json", ".yaml", ".yml"}


def _config_remove(site: Site, key: str, entry_id: str) -> None:
    """Drop the `key` entry with this id; every other key and the writer's formatting stay as `_config_add` leaves them."""
    cfg = json.loads(site.config_path.read_text(encoding="utf-8"))
    cfg[key] = [e for e in cfg.get(key) or [] if e.get("id") != entry_id]
    site.config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _json_mentions(value, needle: str, path: str, skip: str | None) -> list[str]:
    """JSON paths (dotted) whose string value mentions `needle`, except under the `skip` path."""
    if skip and (path == skip or path.startswith(skip + ".")):
        return []
    if isinstance(value, dict):
        return [h for k, v in value.items() for h in _json_mentions(v, needle, f"{path}.{k}" if path else k, skip)]
    if isinstance(value, list):
        return [h for i, v in enumerate(value) for h in _json_mentions(v, needle, f"{path}[{i}]", skip)]
    return [f"{path}: {value}"] if isinstance(value, str) and needle in value else []


def _classify_refs(site: Site, module_id: str, hits: list[str]) -> tuple[list[str], list[str]]:
    """(structural, history) from path-refs hits: structural = other modules' configuration files, project manifests
    and config.json outside the module's own entry; the module's own tree is not a reference to itself."""
    structural, history = [], []
    own = str(site.root / "modules" / module_id) + "/"
    manifest = site.project_manifest()
    cfg_path = str(site.config_path)
    for h in hits:
        file = h.split(":", 1)[0]
        if file == cfg_path or file.startswith(own):
            continue  # config.json is classified structurally below; the module's own files go with it
        p = Path(file)
        rel = p.relative_to(site.root).as_posix() if p.is_relative_to(site.root) else None
        parts = rel.split("/") if rel else []
        if len(parts) >= 3 and parts[0] == "modules" and p.suffix in STRUCTURAL_SUFFIXES:
            structural.append(h)
        elif len(parts) == 3 and parts[0] == "projects" and manifest and parts[2] == manifest:
            structural.append(h)
        else:
            history.append(h)
    cfg = json.loads(site.config_path.read_text(encoding="utf-8"))
    own_idx = next((i for i, e in enumerate(cfg.get("modules") or []) if e.get("id") == module_id), None)
    skip = f"modules[{own_idx}]" if own_idx is not None else None
    structural = [f"{cfg_path}: {m}" for m in _json_mentions(cfg, f"modules/{module_id}", "", skip)] + structural
    return structural, history


def retire_module(site: Site, actor: str, module_id: str, dry_run: bool = False, force: bool = False) -> dict:
    """Retire a site module (release 55, UR-20261011-02 option A): the mirror of `link-module`.

    In this order: the id must be registered; no non-final WI, open worktree or lane on `modules/<id>`; the
    checkout is clean and its HEAD is reachable from `source` at `branch` (never overridden by --force); no
    structural reference (path-refs; other modules' configuration, project manifests, config.json) unless
    --force; then one `site.module-retired` event with the compensation command, and the config entry, the
    final WIs' worktrees and branches and the directory are removed; finally the adapter files are regenerated.
    With dry_run every step is listed with its paths and the first refusal it would hit; nothing changes."""
    from .workflow import _own_branch, _registered_worktrees, remove_wi_worktree, wi_branches

    if site.org.role(actor).kind != "desk":
        fail("only the desk retires modules")
    steps: list[dict] = []
    refusal: str | None = None

    def step(name: str, detail, refuse: str | None = None) -> None:
        nonlocal refusal
        steps.append({"step": name, **detail, **({"refusal": refuse} if refuse else {})})
        if refuse and refusal is None:
            refusal = refuse
            if not dry_run:
                fail(refuse)

    # 1. registered module
    entry = next((e for e in site.config.get("modules") or [] if isinstance(e, dict) and e.get("id") == module_id), None)
    if entry is None:
        step("registered", {"id": module_id}, f"{module_id} is not a registered module (config.json modules[].id)")
        return {"id": module_id, "dry_run": dry_run, "steps": steps, "refusal": refusal}
    target = f"modules/{module_id}"
    root = site.root / entry.get("path", target)
    step("registered", {"id": module_id, "path": str(root), "source": entry.get("source"), "branch": entry.get("branch"),
                        "rev": entry.get("rev")})

    # 2. no live WI, worktree or lane on the target
    led = Ledger(site)
    on_target = [w for w in led.work_items() if w["target"] == target]
    live = [w for w in on_target if w["status"] not in FINAL_STATES]
    final = [w for w in on_target if w["status"] in FINAL_STATES]
    from .ops import lane_holders

    lanes = [h for h in lane_holders(site, led, probe=False) if h["wi"] in {w["id"] for w in on_target}]
    final_wts = {w["id"]: site.worktrees / w["id"] for w in final
                 if (site.worktrees / w["id"]).exists() or (site.worktrees / w["id"]).is_symlink()}
    registered = _registered_worktrees(root) if (root / ".git").exists() else []
    foreign = [str(p) for p in registered if p.resolve() not in {w.resolve() for w in final_wts.values()}]
    problems = []
    if live:
        problems.append("live work items on the target: " + ", ".join(f"{w['id']} ({w['status']})" for w in live))
    if lanes:
        problems.append("lanes held on the target: " + ", ".join(f"{h['wi']} ({'; '.join(h['by'])})" for h in lanes))
    if foreign:
        problems.append("open worktrees of the checkout not owned by a final work item: " + ", ".join(foreign))
    step("no-live-work", {"work_items": [w["id"] for w in on_target], "live": [w["id"] for w in live],
                          "final": [w["id"] for w in final], "lanes": [h["wi"] for h in lanes],
                          "open_worktrees": foreign}, "; ".join(problems) if problems else None)

    # 3. the checkout is clean and pushed (never overridden by --force: data at risk)
    head, unpushed = None, None
    if not (root / ".git").exists():
        step("checkout", {"path": str(root)}, f"{root} is not a git checkout; retire it by hand after checking its contents")
    else:
        head = git(["rev-parse", "HEAD"], root, check=False) or None
        dirty = git(["status", "--porcelain"], root, check=False)
        source, branch = str(entry.get("source") or ""), str(entry.get("branch") or "main")
        tip, reach_err = _source_tip(root, source, branch, site.env())
        local = tip and subprocess.run(["git", "cat-file", "-e", f"{tip}^{{commit}}"], cwd=root, capture_output=True).returncode == 0
        if tip and not local:
            # the source moved on since the clone: fetch its branch into the checkout's object store (read-only on the source)
            proc = subprocess.run(["git", "fetch", "--quiet", source, branch], cwd=root, capture_output=True, text=True,
                                  env=site.env(), timeout=600)
            if proc.returncode != 0:
                reach_err = f"fetch of {branch} from {source} failed: {proc.stderr.strip()[-300:]}"
        if tip and not reach_err and head:
            unpushed = subprocess.run(["git", "merge-base", "--is-ancestor", head, tip], cwd=root,
                                      capture_output=True).returncode != 0
        refuse = None
        if dirty:
            refuse = f"{root} has uncommitted changes ({len(dirty.splitlines())} path(s)); commit or discard them first"
        elif reach_err:
            refuse = f"the recorded source is unreachable, so unpushed work cannot be ruled out: {reach_err}"
        elif unpushed:
            refuse = f"{root} HEAD {head[:12]} is not reachable from {source} at {branch} ({tip[:12]}); push it first"
        step("checkout", {"path": str(root), "head": head, "dirty": bool(dirty), "source": source, "branch": branch,
                          "source_tip": tip, "unpushed": unpushed}, refuse)

    # 4. references
    from .ops import path_refs

    refs = path_refs(site, target)
    structural, history = _classify_refs(site, module_id, refs["hits"])
    step("references", {"structural": structural, "history": history, "truncated": refs["truncated"], "force": force},
         None if force or not structural else
         f"{len(structural)} structural reference(s) to {target} (other modules' configuration, project manifests or "
         f"config.json); edit them first or pass --force: " + "; ".join(structural[:5]))

    # 5. the event, with the compensation command
    branches = []
    if (root / ".git").exists():
        for w in final:
            for b in wi_branches(site, w["id"]):
                if git(["branch", "--list", b], root, check=False) and _own_branch(site, led, w["id"], root, b):
                    branches.append(b)
    compensation = f"dureh site link-module --id {module_id} --source {entry.get('source')} --rev {head or entry.get('rev')}"
    payload = {"id": module_id, "path": entry.get("path", target), "source": entry.get("source"), "branch": entry.get("branch"),
               "head": head, "rev": entry.get("rev"),
               "removed": {"config_entry": True, "worktrees": [str(p) for p in final_wts.values()],
                           "branches": branches, "directory": True},
               "compensation": compensation}
    step("record", {"event": "site.module-retired", "payload": payload})
    # 6. removal; 7. adapter files
    step("remove", {"config_entry": str(site.config_path), "worktrees": [str(p) for p in final_wts.values()],
                    "branches": branches, "directory": str(root)})
    step("regenerate", {"adapter": "claude-code", "site": str(site.root)})
    out = {"id": module_id, "dry_run": dry_run, "steps": steps, "refusal": refusal, "compensation": compensation}
    if dry_run or refusal:
        return out
    Ledger(site).event("site.module-retired", actor, None, payload)
    for w in final:
        wt = final_wts.get(w["id"])
        if wt is not None:
            remove_wi_worktree(site, led, w["id"], wt, root, drop_branch=True)
    _config_remove(site, "modules", module_id)
    if root.is_symlink():
        root.unlink()
    elif root.exists():
        from .exectree import _rmtree

        _rmtree(root)
    from .runtimes import claude_code

    fresh = Site(site.root)
    generated = claude_code.generate(fresh)
    out["removed"] = payload["removed"]
    out["generated"] = len(generated["files"])
    out["summary"] = (f"retired {target}: config entry, {len(final_wts)} worktree(s), {len(branches)} branch(es) and "
                      f"{root} removed; {len(generated['files'])} adapter files regenerated. Restore with: {compensation}")
    return out


def _source_tip(root: Path, source: str, branch: str, env: dict) -> tuple[str | None, str | None]:
    """(sha of `branch` at `source`, error). Reads the source only (`git ls-remote`)."""
    if not source:
        return None, "the config entry records no source"
    try:
        proc = subprocess.run(["git", "ls-remote", "--heads", source, branch], cwd=root, capture_output=True, text=True,
                              env=env, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git ls-remote {source} failed to run: {exc}"
    if proc.returncode != 0:
        return None, f"git ls-remote {source} failed: {proc.stderr.strip()[-300:]}"
    line = proc.stdout.strip().splitlines()
    if not line:
        return None, f"{source} has no branch {branch}"
    return line[0].split()[0], None
