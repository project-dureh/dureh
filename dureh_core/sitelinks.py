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


BRANCH = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,200}(?<![./])$")


def _origin_branch(dest: Path, branch: str) -> str | None:
    """The sha of `origin/<branch>` in the checkout, or None."""
    proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}^{{commit}}"],
                          cwd=dest, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _checkout_branch(dest: Path, branch: str, sha: str, track: bool) -> bool:
    """`git checkout -B <branch> <sha>` (never forced: git refuses to overwrite local changes), tracking
    origin/<branch> when asked and it exists. Returns whether the branch tracks origin/<branch>."""
    git(["checkout", "--quiet", "-B", branch, sha], dest)
    if track and _origin_branch(dest, branch):
        git(["branch", "--quiet", f"--set-upstream-to=origin/{branch}", branch], dest)
        return True
    return False


def _resolve(dest: Path, rev: str) -> str:
    """The commit `rev` names in the checkout; a branch name of the source resolves through origin/<rev>."""
    for cand in (rev, f"refs/remotes/origin/{rev}"):
        proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}"], cwd=dest,
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout.strip()
    fail(f"{rev} names no commit in {dest} (after fetching the source)")


def _on_branch(dest: Path, sha: str, branch: str) -> bool | None:
    tip = _origin_branch(dest, branch)
    if tip is None:
        return None
    return subprocess.run(["git", "merge-base", "--is-ancestor", sha, tip], cwd=dest, capture_output=True).returncode == 0


def link_module(site: Site, actor: str, module_id: str, source: str | None, rev: str, branch: str | None = None,
                relink: bool = False, force: bool = False) -> dict:
    """`site link-module --id X --source S --rev R [--branch B]`: clone S into modules/X and check out R on the local
    branch `main`, or (release 65, UR-20261015-04) on `--branch B`, created at R and tracking origin/B (B must exist
    at the source); the entry records the branch. `--relink` changes a linked module's rev (and branch) in place."""
    if site.org.role(actor).kind != "desk":
        fail("only the desk links modules")
    if not NAME.match(module_id):
        fail(f"invalid module id: {module_id!r}")
    if branch is not None and not BRANCH.match(branch):
        fail(f"invalid branch name: {branch!r}")
    if relink:
        return relink_module(site, actor, module_id, rev, branch, source, force)
    if force:
        fail("--force is only for --relink")
    if not source:
        fail("--source is required (the module repository to clone)")
    dest = site.root / "modules" / module_id
    if dest.exists():
        fail(f"module already linked: {dest}; to change its revision or branch in place: `dureh site link-module --id"
             f" {module_id} --rev <rev> [--branch <branch>] --relink`")
    git(["clone", "--quiet", source, str(dest)], site.root, env=site.env())
    try:
        sha = _resolve(dest, rev)
        if branch is not None and _origin_branch(dest, branch) is None:
            fail(f"{source} has no branch {branch}")
        on_branch = _on_branch(dest, sha, branch) if branch else None
        tracking = _checkout_branch(dest, branch or "main", sha, track=branch is not None)
    except Exception:
        subprocess.run(["rm", "-rf", str(dest)])
        raise
    entry = {"id": module_id, "path": f"modules/{module_id}", "source": source, "rev": sha, "branch": branch or "main"}
    _config_add(site, "modules", entry)
    Ledger(site).event("site.module-linked", actor, None, {**entry, **({"tracking": f"origin/{branch}" if tracking
                                                                          else None, "rev_on_branch": on_branch}
                                                                         if branch else {})})
    if branch and on_branch is False:
        return {**entry, "note": f"{sha[:12]} is not on origin/{branch}; the local branch {branch} starts there anyway"}
    return entry


def _push_off(site: Site, entry: dict, root: Path) -> bool:
    """A module is push-off when its entry says `push: false`, or when neither its source nor its checkout's origin
    is in config `push.allowed_remotes` (the harness cannot push it)."""
    if entry.get("push") is False:
        return True
    from .ops import push_rule

    origin = git(["remote", "get-url", "origin"], root, check=False) if (root / ".git").exists() else ""
    # release 82: an allowed_remotes entry may be an object; it is compared by its url
    return push_rule(site, str(entry.get("source") or ""), origin, "origin") is None


def _standing_ff_covers(site: Site, source: str, root: Path, branch: str) -> bool:
    """Would a standing fast-forward push of `branch` to the module's source be allowed by its push rule (release 82)?"""
    from .ops import _globs, push_rule

    origin = git(["remote", "get-url", "origin"], root, check=False) if (root / ".git").exists() else ""
    rule = push_rule(site, source, origin, "origin")
    return bool(rule and rule["standing_ff"] and _globs(rule["branches"], branch))


def relink_module(site: Site, actor: str, module_id: str, rev: str, branch: str | None = None,
                  source: str | None = None, force: bool = False) -> dict:
    """`site link-module --id X --rev R [--branch B] --relink` (release 65, UR-20261015-04): move a linked module's
    checkout to R (on its recorded branch, or B) in place, without retire + link.

    Refused while the checkout has uncommitted changes, or while its HEAD is on no branch of the source (unpushed
    local commits; an unreachable source counts, since unpushed work cannot be ruled out), unless the module is
    push-off (entry `push: false`, or its source not in `push.allowed_remotes`) AND --force is given. The local branch
    is created or reset at R (`git checkout -B`, never forced: git still refuses to overwrite local changes) and
    tracks origin/B when it exists; a previous local branch of another name is kept as it is. Records
    `site.module-relinked` {id, path, source, from, to, forced, push_off, ...}, updates the entry (other keys kept)
    and regenerates the adapter files."""
    from .workflow import CACHE_RE

    entry = next((e for e in site.config.get("modules") or [] if isinstance(e, dict) and e.get("id") == module_id), None)
    if entry is None:
        fail(f"{module_id} is not a registered module (config.json modules[].id); link it without --relink")
    root = site.root / entry.get("path", f"modules/{module_id}")
    if not (root / ".git").exists():
        fail(f"{root} is not a git checkout")
    src = str(entry.get("source") or "")
    if source and source != src:
        fail(f"--relink keeps the source ({src}); a different source is retire-module then link-module")
    old_branch = str(entry.get("branch") or "main")
    new_branch = branch or old_branch
    head = git(["rev-parse", "HEAD"], root)
    current = git(["symbolic-ref", "--quiet", "--short", "HEAD"], root, check=False) or None
    dirty = [ln for ln in git(["status", "--porcelain", "--untracked-files=all"], root, check=False).splitlines()
             if ln.strip() and not CACHE_RE.search(ln[3:])]
    fetch_err = None
    if not src:
        fetch_err = "the config entry records no source"
    else:
        proc = subprocess.run(["git", "fetch", "--quiet", src, "+refs/heads/*:refs/remotes/origin/*"], cwd=root,
                              capture_output=True, text=True, env=site.env(), timeout=600)
        if proc.returncode != 0:
            fetch_err = f"fetch from {src} failed: {proc.stderr.strip()[-300:]}"
    pushed_on = [] if fetch_err else [r for r in git(["for-each-ref", "--contains", head, "--format=%(refname:short)",
                                                      "refs/remotes/origin/"], root, check=False).splitlines() if r]
    push_off = _push_off(site, entry, root)
    problems = []
    if dirty:
        problems.append(f"{root} has uncommitted changes ({len(dirty)} path(s))")
    if fetch_err:
        problems.append(f"the source is unreachable, so unpushed commits cannot be ruled out ({fetch_err})")
    elif not pushed_on:
        problems.append(f"{root} HEAD {head[:12]} is on no branch of {src} (unpushed local commits)")
    if problems and not (force and push_off):
        how = ("pass --force to relink anyway (the module is push-off)" if push_off else
               "--force applies only to a push-off module (entry push: false, or its source not in push.allowed_remotes)")
        fail("; ".join(problems) + f"; nothing was changed. Commit, push or discard first, or {how}")
    sha = _resolve(root, rev)
    if branch is not None and _origin_branch(root, branch) is None:
        fail(f"{src} has no branch {branch}; nothing was changed")
    on_branch = _on_branch(root, sha, new_branch)
    old_tip = git(["rev-parse", "--verify", "--quiet", f"refs/heads/{old_branch}"], root, check=False) or None
    tracking = _checkout_branch(root, new_branch, sha, track=True)
    led = Ledger(site)
    live = [w["id"] for w in led.work_items() if w["target"] == f"modules/{module_id}"
            and w["status"] not in FINAL_STATES]
    _config_add(site, "modules", {**entry, "rev": sha, "branch": new_branch})
    payload = {"id": module_id, "path": entry.get("path", f"modules/{module_id}"), "source": src,
               "from": {"rev": entry.get("rev"), "branch": old_branch, "head": head, "checked_out": current},
               "to": {"rev": sha, "branch": new_branch, "tracking": f"origin/{new_branch}" if tracking else None,
                      "rev_on_branch": on_branch},
               "forced": bool(problems), "problems": problems, "push_off": push_off, "live_work_items": live}
    if new_branch != old_branch and old_tip:
        payload["previous_branch_kept"] = {"branch": old_branch, "head": old_tip}
    ev = led.event("site.module-relinked", actor, None, payload)
    from .runtimes import claude_code

    generated = claude_code.generate(Site(site.root))
    out = {**payload, "event": ev["id"], "generated": len(generated["files"])}
    notes = []
    if on_branch is False:
        notes.append(f"{sha[:12]} is not on origin/{new_branch}")
    if payload.get("previous_branch_kept"):
        notes.append(f"the local branch {old_branch} is kept at {old_tip[:12]} (not moved)")
    if live:
        notes.append(f"live work items on modules/{module_id}: {', '.join(live)} (their worktrees keep their base)")
    if notes:
        out["note"] = "; ".join(notes)
    return out


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


RECORD_DIRS = ("research-notes", "reports", "runs")


def records_projects(site: Site) -> bool:
    """The package allows records projects (startup.yaml `projects: {records: true}`; UR-20261027-01)."""
    projects = site.package_manifest().get("projects")
    return isinstance(projects, dict) and projects.get("records") is True


def new_project(site: Site, actor: str, name: str, purpose: str | None = None) -> dict:
    """Create a records project projects/<name>: a git repository with research-notes/, reports/ and runs/, no data
    and no DataLad source, registered in config `projects` with kind `records` (UR-20261027-01, Director 2026-10-28:
    the projects-only-through-link-project rule was one company's data rule in the shared harness). Data projects keep
    `site link-project`. A plain directory already at projects/<name> (no .git, no .datalad) is adopted: its files
    are kept and go into the first commit."""
    if site.org.role(actor).kind != "desk":
        fail("only the desk (or the Director) creates projects")
    if not records_projects(site):
        fail("this package does not allow records projects (startup.yaml `projects: {records: true}`); a data"
             " project comes in with `dureh site link-project`")
    if not NAME.match(name):
        fail(f"invalid project name: {name!r}")
    if any(e.get("id") == name for e in site.config.get("projects") or []):
        fail(f"project already registered: {name}")
    dest = site.root / "projects" / name
    adopted = dest.exists()
    if adopted and (not dest.is_dir() or dest.is_symlink() or (dest / ".git").exists()
                    or (dest / ".datalad").exists()):
        fail(f"{dest} exists and is not a plain directory (only a plain directory without .git or .datalad is"
             " adopted)")
    dest.mkdir(parents=True, exist_ok=True)
    readme = dest / "README.md"
    if not readme.exists():
        readme.write_text(f"# {name}\n\n{purpose or 'Records project.'}\n\nCreated by `dureh site new-project`:"
                          " research-notes/ (design and theory notes), reports/ (comparison and run reports), runs/"
                          " (run records). No data lives here.\n", encoding="utf-8")
    for d in RECORD_DIRS:
        (dest / d).mkdir(exist_ok=True)
        if not any((dest / d).iterdir()):
            (dest / d / ".gitkeep").write_text("", encoding="utf-8")
    env = site.env()
    ident = ["-c", "user.name=DUREH desk", "-c", "user.email=desk@dureh.invalid"]
    git(["init", "-q", "-b", "main"], dest, env=env)
    git(["add", "-A"], dest, env=env)
    has_ident = subprocess.run(["git", "config", "user.name"], cwd=dest, capture_output=True, text=True,
                               env=env).stdout.strip()
    git(([] if has_ident else ident) + ["commit", "-q", "-m", f"{name}: records project from dureh site new-project"],
        dest, env=env)
    sha = git(["rev-parse", "HEAD"], dest)
    entry = {"id": name, "path": f"projects/{name}", "kind": "records", "rev": sha, "data_fetched": False}
    if purpose:
        entry["purpose"] = purpose
    _config_add(site, "projects", entry)
    Ledger(site).event("site.project-created", actor, None, {**entry, "adopted": adopted})
    entry = {**entry, "adopted": adopted}
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
    --force; then the final WIs' worktrees and branches, the config entry and the directory are removed, in that
    order, and one `site.module-retired` event records what was removed with the compensation command (release 57:
    the event follows the removal; a failure mid-way records `site.module-retire-failed` with the failing step and
    what was removed so far, and leaves the module registered); finally the adapter files are regenerated.
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
            refuse = f"{root} HEAD {head[:12]} is not reachable from {source} at {branch} ({tip[:12]}); push it first" + (
                f" (standing fast-forward push: `dureh push --repo {root} --remote origin --ref {branch}`)"
                if _standing_ff_covers(site, source, root, branch) else
                " (ask the Director: `dureh push --approve ...` then `dureh push --approval <decision>`)")  # release 58
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

    # 5. removal (worktrees and branches, then the config entry and the directory last); 6. the event, recorded
    # only after every removal step succeeded (release 57); 7. adapter files
    branches = []
    if (root / ".git").exists():
        for w in final:
            for b in wi_branches(site, w["id"]):
                if git(["branch", "--list", b], root, check=False) and _own_branch(site, led, w["id"], root, b):
                    branches.append(b)
    compensation = f"dureh site link-module --id {module_id} --source {entry.get('source')} --rev {head or entry.get('rev')}"
    if entry.get("branch") and entry.get("branch") != "main":
        compensation += f" --branch {entry['branch']}"
    payload = {"id": module_id, "path": entry.get("path", target), "source": entry.get("source"), "branch": entry.get("branch"),
               "head": head, "rev": entry.get("rev"),
               "removed": {"config_entry": True, "worktrees": [str(p) for p in final_wts.values()],
                           "branches": branches, "directory": True},
               "compensation": compensation}
    step("remove", {"config_entry": str(site.config_path), "worktrees": [str(p) for p in final_wts.values()],
                    "branches": branches, "directory": str(root)})
    step("record", {"event": "site.module-retired", "payload": payload})
    step("regenerate", {"adapter": "claude-code", "site": str(site.root)})
    out = {"id": module_id, "dry_run": dry_run, "steps": steps, "refusal": refusal, "compensation": compensation}
    if dry_run or refusal:
        return out
    removed = {"config_entry": False, "worktrees": [], "branches": [], "directory": False}
    current = "remove"

    def _failed(exc: BaseException) -> None:
        err = str(exc).strip() or exc.__class__.__name__
        Ledger(site).event("site.module-retire-failed", actor, None,
                           {"id": module_id, "path": entry.get("path", target), "step": current, "error": err,
                            "removed": removed, "compensation": compensation})
        done = (f"config entry {'removed' if removed['config_entry'] else 'kept'}, "
                f"{len(removed['worktrees'])} of {len(final_wts)} worktree(s) and {len(removed['branches'])} branch(es) removed, "
                f"directory {'removed' if removed['directory'] else 'kept'}")
        fail(f"retire-module {module_id} failed at step '{current}': {err}. Removed so far: {done}; "
             f"recorded as site.module-retire-failed. Fix the cause and run the command again")

    try:
        for w in final:
            wt = final_wts.get(w["id"])
            if wt is None:
                continue
            current = f"worktree {w['id']}"
            dropped = remove_wi_worktree(site, led, w["id"], wt, root, drop_branch=True)
            removed["worktrees"].append(str(wt))
            if dropped:
                removed["branches"].append(dropped)
        current = "config-entry"
        _config_remove(site, "modules", module_id)
        removed["config_entry"] = True
        current = "directory"
        if root.is_symlink():
            root.unlink()
        elif root.exists():
            from .exectree import _rmtree

            _rmtree(root)
            if root.exists():
                fail(f"{root} is still there after removal; check what is left and remove it by hand")
        removed["directory"] = True
    except Exception as exc:  # DurehError, git/subprocess and OS errors alike: record what happened, then fail
        _failed(exc)
    payload["removed"] = removed
    Ledger(site).event("site.module-retired", actor, None, payload)
    from .runtimes import claude_code

    fresh = Site(site.root)
    generated = claude_code.generate(fresh)
    out["removed"] = removed
    out["generated"] = len(generated["files"])
    out["summary"] = (f"retired {target}: config entry, {len(removed['worktrees'])} worktree(s), "
                      f"{len(removed['branches'])} branch(es) and {root} removed; {len(generated['files'])} adapter files "
                      f"regenerated. Restore with: {compensation}")
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
