"""Install a reviewed startup package into a new, empty site."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from . import SCHEMA_VERSION, yamlsub
from .org import HARNESS_ROOT, Org
from .package import check as check_package, role_scope_conflicts
from .site import RECORD_KINDS, Site
from .util import DurehError, dumps, fail, git_revision, read_json, utc_now, write_replace

FORBIDDEN_PACKAGE_DIRS = {"state", "records", "persona", "memory"}
DEFAULT_MODELS = {
    "desk": "anthropic:claude-opus-5-5",
    "pm": "anthropic:claude-opus-5-5",
    "writer-academic": "anthropic:claude-opus-5-5",
    "worker": "anthropic:claude-sonnet-5",
    "recount": "anthropic:claude-haiku-4-5-20251001",
}


def package_warnings(startup: Path) -> list[str]:
    """Warnings of a valid package (protected rules that lift nothing); [] when it does not load."""
    try:
        m = yamlsub.load_file(startup / "startup.yaml")
        org = Org.load(HARNESS_ROOT, manifest=m)
        return check_package(m, startup) + org.warnings
    except DurehError:
        return []


def validate_package(startup: Path) -> dict:
    """Install, `adapter sync` and `verify --self` (proposals/boundary-design-2026-10-08.md 3.4): the manifest
    (schema 2; schema 1, `organization` and unknown top-level keys are refused since release 49), the roles it
    builds with the harness's generic roles, protected paths, labels, files it names, no symlinks, no state. Fails on the first error; warnings come from `package_warnings`."""
    if startup.is_symlink() or not startup.is_dir():
        fail(f"startup package must be a directory: {startup}")
    manifest_path = startup / "startup.yaml"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        fail(f"required startup manifest is missing: {manifest_path}")
    m = yamlsub.load_file(manifest_path)
    pkg = m.get("package") if isinstance(m, dict) else None
    if not isinstance(pkg, dict) or not pkg.get("id") or not pkg.get("name"):
        fail(f"startup.yaml must declare package.id and package.name: {manifest_path}")
    for base, dirs, files in os.walk(startup, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in dirs + files:
            if (Path(base) / name).is_symlink():
                fail(f"startup package may not contain symlinks: {Path(base) / name}")
    check_package(m, startup)
    for d in FORBIDDEN_PACKAGE_DIRS:
        if (startup / d).exists():
            fail(f"startup package may not contain site state ({d}/); state is site-owned")
    if (startup / ".gitmodules").is_file() and "state" in (startup / ".gitmodules").read_text(encoding="utf-8"):
        fail("startup package may not carry a state submodule or recovery pointer")
    if (m.get("state") or {}).get("package_contains_state") not in (False, None):
        fail("startup.yaml declares package state; packages must not contain state")
    org = Org.load(HARNESS_ROOT, manifest=m)
    conflicts = role_scope_conflicts(m, org.roles.values())
    if conflicts:
        fail("startup.yaml: a write scope may not cover a protected path: " + "; ".join(conflicts))
    for v in m.get("values") or []:
        if v.get("manager") and v["manager"] not in org.roles:
            fail(f"value {v.get('id')} names manager {v['manager']!r}, which is not a role of this organization")
    boot = m.get("bootstrap") or {}
    for rel in list(boot.get("work_items") or []) + ([boot["persona_questions"]] if boot.get("persona_questions") else []):
        p = startup / rel
        if not p.is_file():
            fail(f"startup.yaml names a missing bootstrap file: {rel}")
    for v in m.get("values") or []:
        if v.get("skill") and not (startup / v["skill"]).is_file():
            fail(f"value {v.get('id')} names a missing skill: {v['skill']}")
    package_config_defaults(m)
    return m


def copy_package(src: Path, dst: Path) -> None:
    for base, dirs, files in os.walk(src, followlinks=False):
        base_path = Path(base)
        rel = base_path.relative_to(src)
        out = dst / rel
        out.mkdir(parents=True, exist_ok=True)
        for directory in list(dirs):
            if (base_path / directory).is_symlink():
                fail(f"startup package may not contain symlinks: {base_path / directory}")
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for filename in sorted(files):
            source = base_path / filename
            if source.is_symlink():
                fail(f"startup package may not contain symlinks: {source}")
            if filename in (".DS_Store",):
                continue
            shutil.copy2(source, out / filename)


# ------------------------------------------------------------ single package source (release 46, option A)
def tree_digest(root: Path) -> str:
    """A content hash of a package tree (relative paths and bytes, .git excluded): the revision of a source
    without git, and what `adapter sync` compares to list changed files."""
    import hashlib

    h = hashlib.sha256()
    for rel, digest in sorted(_file_digests(root).items()):
        h.update(rel.encode() + b"\0" + digest.encode() + b"\n")
    return "sha256:" + h.hexdigest()


def _file_digests(root: Path) -> dict[str, str]:
    import hashlib

    out = {}
    if not root.is_dir():
        return out
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for f in files:
            p = Path(base) / f
            if p.is_file() and not p.is_symlink():
                out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _git_source(src: Path) -> bool:
    from .util import git

    return git(["rev-parse", "--is-inside-work-tree"], src, check=False) == "true"


def fetch_package(src: Path, dst: Path, rev: str | None = None) -> str:
    """Put the package at `src` into the new directory `dst` and return its revision. A git source is exported with
    `git archive <rev>` (default HEAD) and refused while it has uncommitted or untracked changes; a source without
    git is copied (no symlinks) and its revision is a content hash. Option A of the boundary design, section 4."""
    import io
    import tarfile

    from .util import git

    if src.is_symlink() or not src.is_dir():
        fail(f"startup package source must be a directory: {src}")
    if not _git_source(src):
        if rev:
            fail(f"--package-rev needs a git package source; {src} is not a git work tree")
        copy_package(src, dst)
        return tree_digest(dst)
    dirty = git(["status", "--porcelain", "--untracked-files=normal", "--", "."], src, check=False)
    if dirty:
        fail(f"the package source {src} has uncommitted changes; commit them first (the site installs only a committed"
             f" revision):\n{dirty[:800]}")
    full = git(["rev-parse", "--verify", f"{rev or 'HEAD'}^{{commit}}"], src, check=False)
    if not full:
        fail(f"package source {src}: unknown revision {rev or 'HEAD'!r}")
    prefix = git(["rev-parse", "--show-prefix"], src)
    proc = subprocess.run(["git", "archive", "--format=tar", f"{full}:{prefix}" if prefix else full], cwd=src,
                          capture_output=True, timeout=300)
    if proc.returncode != 0:
        fail(f"git archive of {src} at {full[:12]} failed: {proc.stderr.decode(errors='replace').strip()}")
    dst.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        for m in tar.getmembers():
            name = m.name.lstrip("./")
            if not name:
                continue
            if m.issym() or m.islnk():
                fail(f"startup package may not contain symlinks: {m.name} at {full[:12]}")
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                fail(f"package archive member outside the package: {m.name}")
            out = dst / name
            if m.isdir():
                out.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(tar.extractfile(m).read())
                out.chmod(0o755 if m.mode & 0o111 else 0o644)
    if not (dst / "startup.yaml").is_file():
        fail(f"{src} at {full[:12]} has no committed startup.yaml")
    return full


def _rmtree_writable(path: Path) -> None:
    def unlock(func, p, _exc):
        os.chmod(os.path.dirname(p), 0o700)
        if not os.path.islink(p):
            os.chmod(p, 0o700)
        func(p)

    if path.exists() or path.is_symlink():
        shutil.rmtree(path, onerror=unlock)


def sync_package(site, rev: str | None = None) -> dict:
    """`adapter sync [--package-rev REV]`: re-copy startup/ from config `startup.source` at a committed revision
    (default its HEAD), validate the copy, swap it in, and record `package.synced` {from, to, changed} when the
    revision or the files changed; config `startup.rev` and `startup.version` follow. Without a configured or
    existing source, startup/ is kept as installed (the site stays self-contained)."""
    from .records import Ledger

    src = site.package_source()
    cfg = dict(site.config.get("startup") or {})
    if src is None or not src.is_dir():
        if rev:
            fail("--package-rev needs config startup.source naming an existing package source")
        why = "no startup.source in config.json" if src is None else f"startup.source {src} does not exist"
        return {"skipped": f"{why}; startup/ kept as installed", "warnings": package_warnings(site.startup)}
    new = site.root / "startup.new"
    old = site.root / "startup.old"
    for leftover in (new, old):
        _rmtree_writable(leftover)
    try:
        to = fetch_package(src, new, rev)
        manifest = validate_package(new)
        warnings = package_warnings(new)
    except BaseException:
        _rmtree_writable(new)
        raise
    if manifest["package"]["id"] != cfg.get("id", manifest["package"]["id"]):
        _rmtree_writable(new)
        fail(f"the package source holds {manifest['package']['id']!r}, but this site runs {cfg.get('id')!r}")
    before, after = _file_digests(site.startup), _file_digests(new)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    frm = cfg.get("rev")
    if changed:
        if site.startup.exists():
            site.startup.rename(old)
        new.rename(site.startup)
        _rmtree_writable(old)
    else:
        _rmtree_writable(new)
    if frm != to or changed:
        cfg.update(rev=to, version=manifest["package"].get("version"))
        conf = dict(site.config)
        conf["startup"] = cfg
        write_replace(site.config_path, json.dumps(conf, indent=2, ensure_ascii=False) + "\n",
                      site.config_path.stat().st_mode & 0o777)
        site.config = conf
        site._manifest = None
        site._org = None
        Ledger(site).event("package.synced", "desk", None, {"package": manifest["package"]["id"], "from": frm, "to": to,
                                                            "version": manifest["package"].get("version"),
                                                            "changed": changed[:200], "changed_count": len(changed)})
    return {"id": manifest["package"]["id"], "from": frm, "to": to, "changed": changed, "warnings": warnings}


def _answers(questions: list[dict], supplied: dict, interactive: bool) -> dict:
    out = {}
    for q in questions:
        key = q["key"]
        if key in supplied and supplied[key] not in (None, ""):
            out[key] = supplied[key]
        elif interactive:
            default = q.get("default")
            raw = input(f"{q.get('prompt', key)}" + (f" [{default}]" if default else "") + ": ").strip()
            out[key] = raw or default
        elif q.get("default") is not None:
            out[key] = q["default"]
        if q.get("required") and not out.get(key):
            fail(f"persona question {key!r} needs an answer (preset persona.{key} or --interactive)")
    return out


# config.json keys a startup package may seed through `config_defaults` (startup.yaml); never the site identity,
# harness root, models or state backend, which the install and the preset own.
CONFIG_DEFAULT_KEYS = ("project", "containers", "slurm", "checks", "env", "limits", "guards", "modules", "index",
                       "members")  # release 64: member runtimes


def package_config_defaults(manifest: dict) -> dict:
    """The `config_defaults` block of a startup manifest, checked: a mapping of known top-level keys to mappings
    (`modules`: a list of {id, ...} entries). UP-0069: company values (manifest name, env names) live here, not in code."""
    block = manifest.get("config_defaults")
    if block in (None, {}):
        return {}
    if not isinstance(block, dict):
        fail("startup.yaml config_defaults must be a mapping")
    for key, value in block.items():
        if key not in CONFIG_DEFAULT_KEYS:
            fail(f"startup.yaml config_defaults.{key}: not a key a package may seed (allowed: {', '.join(CONFIG_DEFAULT_KEYS)})")
        if key == "modules":
            if not isinstance(value, list) or not all(isinstance(e, dict) and e.get("id") for e in value):
                fail("startup.yaml config_defaults.modules must be a list of {id: <module>, ...} entries")
        elif not isinstance(value, dict):
            fail(f"startup.yaml config_defaults.{key} must be a mapping")
    return block


def merge_config_defaults(config: dict, defaults: dict) -> tuple[dict, list[str]]:
    """`defaults` under `config`: a key the config lacks is added; a mapping is merged key by key (the config's
    value wins, recursively); `modules` entries are matched by id and merged the same way (new ids are appended).
    Returns (merged config, dotted paths that were added)."""
    out = json.loads(json.dumps(config))
    added: list[str] = []

    def merge(dst: dict, src: dict, path: str) -> None:
        for k, v in src.items():
            here = f"{path}.{k}" if path else k
            if k not in dst or dst[k] is None:
                dst[k] = json.loads(json.dumps(v))
                added.append(here)
            elif isinstance(dst[k], dict) and isinstance(v, dict):
                merge(dst[k], v, here)
            # anything else: the site's value stands

    for key, value in defaults.items():
        if key == "modules":
            mods = list(out.get("modules") or [])
            for entry in value:
                hit = next((m for m in mods if isinstance(m, dict) and m.get("id") == entry["id"]), None)
                if hit is None:
                    mods.append(json.loads(json.dumps(entry)))
                    added.append(f"modules[{entry['id']}]")
                else:
                    merge(hit, entry, f"modules[{entry['id']}]")
            out["modules"] = mods
        else:
            merge(out, {key: value}, "")
    return out, added


def apply_config_defaults(site, manifest: dict | None = None) -> list[str]:
    """For an existing site (`adapter sync`): add the package's `config_defaults` the site's config.json lacks
    (never changing a value the site has). Reads the installed package (`startup/startup.yaml`) only: `adapter sync`
    re-copies it from the committed source first (release 46, single package source). Returns the dotted keys added;
    config.json is rewritten only when something was added."""
    if manifest is None:
        path = site.startup / "startup.yaml"
        if not path.is_file():
            return []
        manifest = yamlsub.load_file(path) or {}
    defaults = package_config_defaults(manifest)
    if not defaults:
        return []
    merged, added = merge_config_defaults(site.config, defaults)
    if added:
        write_replace(site.config_path, json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
                      site.config_path.stat().st_mode & 0o777)
        site.config = merged
    return added


def _config(site: Path, manifest: dict, startup: Path, preset: dict, rev: str | None = None) -> dict:
    models = {}
    waiver = (date.today() + timedelta(days=int(preset.get("waiver_days", 31)))).isoformat()
    for tier, model in {**DEFAULT_MODELS, **(preset.get("models") or {})}.items():
        entry = model if isinstance(model, dict) else {"model": model}
        entry.setdefault("qualification", "waived-bootstrap")
        if entry["qualification"] == "waived-bootstrap":
            entry.setdefault("waiver_expires", waiver)
            entry.setdefault("note", "bootstrap waiver: no qualification exam has been run for this tier")
        models[tier] = entry
    harness_defaults = {"slurm": {"account": None, "partition": None, "time": None}, "checks": {},
                        "push": {"allowed_remotes": []}, "state": {"backend": "git", "ria": None},
                        "console": {"revision": None}, "env": {}, "limits": {}, "index": {}}
    config = {
        "schema_version": SCHEMA_VERSION,
        "site": {"id": preset.get("site_id") or f"s-{secrets.token_hex(4)}", "name": preset.get("name") or site.name,
                 "root": str(site)},
        "harness": {"root": str(HARNESS_ROOT)},
        "startup": {"id": manifest["package"]["id"], "version": manifest["package"].get("version"),
                    "source": str(startup), "rev": rev},
        "references": preset.get("references") or [],
        "runtime": {"adapter": preset.get("adapter", "claude-code")},
        "models": models,
    }
    # precedence: the preset, then the package's config_defaults, then the harness defaults
    for key in ("index", "slurm", "checks", "push", "state", "console", "env", "limits", "members"):
        if preset.get(key):
            config[key] = preset[key]
    for key in ("project", "containers", "guards", "modules"):
        if key in preset:
            config[key] = preset[key]
    config, _ = merge_config_defaults(config, package_config_defaults(manifest))
    for key, value in harness_defaults.items():
        config.setdefault(key, value)
    # release 46 (O6): a new site learns of job ends on demand (no standing watch Monitor); an existing site without
    # the key keeps `monitor` (ops.watch_mode). A preset or the package's config_defaults may set another mode.
    if isinstance(config.get("slurm"), dict):
        config["slurm"] = {**config["slurm"], "watch": config["slurm"].get("watch") or "on-demand"}
    return config


def install(startup_arg: str, site_arg: str, preset_path: str | None = None, persona_path: str | None = None,
            interactive: bool = False, run_unit: bool = True) -> dict:
    startup_p = Path(startup_arg).expanduser()
    if startup_p.is_symlink():
        fail(f"startup package path may not be a symlink: {startup_p}")
    startup = startup_p.resolve()
    site_p = Path(site_arg).expanduser()
    if site_p.is_symlink():
        fail(f"site path may not be a symlink: {site_p}")
    site_root = site_p.resolve()
    manifest = validate_package(startup)  # the source as given; the installed copy is validated again below
    preset = read_json(Path(preset_path)) if preset_path else {}
    supplied = dict(preset.get("persona") or {})
    if persona_path:
        supplied.update(read_json(Path(persona_path)))
    questions = []
    pq = (manifest.get("bootstrap") or {}).get("persona_questions")
    if pq:
        questions = (yamlsub.load_file(startup / pq) or {}).get("questions") or []
    persona = _answers(questions, supplied, interactive and sys.stdin.isatty())
    # Director, 2026-10-04: the conversation language is recorded in the persona (English by default; the desk
    # confirms it at first contact); a language the package questions or the preset supplied is kept
    if not persona.get("language"):
        persona["language"] = "English"
    if site_root.exists() and (not site_root.is_dir() or any(site_root.iterdir())):
        fail(f"site destination must be absent or empty: {site_root}")
    created_root = not site_root.exists()
    site_root.mkdir(parents=True, exist_ok=True)
    try:
        # release 46 (single package source): the committed revision of a git source, else a copy with a content hash
        package_rev = fetch_package(startup, site_root / "startup")
        manifest = validate_package(site_root / "startup")
        for kind in RECORD_KINDS:
            (site_root / "state" / "records" / kind).mkdir(parents=True)
        for d in ("state/persona", "state/memory", "projects", "modules", "references", "inbox", "worktrees", "notes"):
            (site_root / d).mkdir(parents=True, exist_ok=True)
        config = _config(site_root, manifest, startup, preset, package_rev)
        (site_root / "config.json").write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        for ref in config["references"]:
            src = Path(ref["path"]).expanduser()
            name = ref.get("name") or src.name
            if not src.exists():
                fail(f"reference path does not exist: {src}")
            (site_root / "references" / name).symlink_to(src)
        (site_root / ".gitignore").write_text("worktrees/\n.claude/\nbin/\nCLAUDE.md\n", encoding="utf-8")
        (site_root / "state" / "persona" / "persona.json").write_text(
            json.dumps({**persona, "recorded_at": utc_now(), "source": "bootstrap questions"}, indent=2,
                       ensure_ascii=False) + "\n", encoding="utf-8")
        (site_root / "state" / "memory" / "handoff.md").write_text(
            "# Desk handoff\n\nNew site. No Director decisions yet. Bootstrap work items await Gate 1.\n", encoding="utf-8")
        site = Site(site_root)
        from .records import Ledger
        from .state import init_state, save_state
        from .workflow import wi_new

        led = Ledger(site)
        led.event("site.installed", "desk", None, {"package": manifest["package"]["id"],
                                                   "package_version": manifest["package"].get("version")})
        created = []
        for rel in (manifest.get("bootstrap") or {}).get("work_items") or []:
            spec = yamlsub.load_file(site_root / "startup" / rel)
            rec = wi_new(site, "desk", title=spec["title"], goal=spec["goal"], role=spec.get("role") or _role_for(manifest, spec, site.org),
                         target=spec.get("target") or "site", acceptance=list(spec.get("acceptance") or []),
                         lane="work", value=spec.get("value"), constraints=list(spec.get("constraints") or []),
                         source=f"startup/{rel}")
            created.append(rec["id"])
        from .runtimes import claude_code

        if config["runtime"]["adapter"] != "claude-code":
            fail(f"runtime adapter {config['runtime']['adapter']!r} is not implemented")
        claude_code.generate(site)
        state_result = init_state(site)
        from .selftest import run_self

        self_result = run_self(site, actor="desk", unit=run_unit)
        state_result = save_state(site, "record install self-test")
        deployment = {
            "schema_version": SCHEMA_VERSION,
            "harness": {"url": _remote(HARNESS_ROOT), "rev": git_revision(HARNESS_ROOT)},
            "package": {"id": manifest["package"]["id"], "name": manifest["package"]["name"],
                        "url": _remote(startup), "rev": package_rev,
                        "version": manifest["package"].get("version")},
            "state": {"backend": config["state"]["backend"], "url": config["state"].get("ria"),
                      "rev": state_result.get("revision"), "schema_version": 1},
            "console": {"rev": config["console"].get("revision")},
            "recorded": utc_now(),
        }
        dep = site_root / "deployment.json"
        dep.write_text(json.dumps(deployment, indent=2) + "\n", encoding="utf-8")
        dep.chmod(0o600)
    except BaseException:
        if created_root:
            shutil.rmtree(site_root, ignore_errors=True)
        else:
            for child in list(site_root.iterdir()):
                if child.is_symlink() or child.is_file():
                    child.unlink()
                else:
                    shutil.rmtree(child, ignore_errors=True)
        raise
    return {"site": str(site_root), "package": manifest["package"]["id"], "work_items": created,
            "selftest": self_result, "state": state_result, "deployment": deployment}


def _role_for(manifest: dict, spec: dict, org: Org | None = None) -> str:
    """The role of a bootstrap work item without one: the manager its value names (package `values[].manager`, when
    the organization has it), else the manager whose role declares that value, else the generic `manager` (or, when
    the package's own managers replace it, the first of them)."""
    value = spec.get("value")
    for v in manifest.get("values") or []:
        if value and v.get("id") == value and v.get("manager") and (org is None or v["manager"] in org.roles):
            return v["manager"]
    if org is not None:
        managers = [r for r in org.roles.values() if r.is_manager]
        for r in managers:
            if value and r.value == value:
                return r.name
        if "manager" not in org.roles and managers:
            return managers[0].name  # package managers replace the generic one
    return "manager"


def _remote(path: Path) -> str | None:
    from .util import git

    if not (Path(path) / ".git").exists():
        return None
    return git(["remote", "get-url", "origin"], Path(path), check=False) or None


def refresh_deployment(site: Site) -> dict:
    """Re-pin harness and package revisions after an update; the previous pin stays in the event record."""
    from .records import Ledger

    path = site.root / "deployment.json"
    dep = read_json(path)
    new = dict(dep)
    new["harness"] = {"url": _remote(HARNESS_ROOT), "rev": git_revision(HARNESS_ROOT)}
    # the installed package is the pin (release 46): config startup.rev, which `adapter sync` sets from the revision
    # it copied, else the content hash of startup/ (a site not synced since release 46)
    rev = (site.config.get("startup") or {}).get("rev") or tree_digest(site.startup)
    new["package"] = {**dep.get("package", {}), "rev": rev,
                      "version": (site.config.get("startup") or {}).get("version") or dep.get("package", {}).get("version")}
    old_root = (site.config.get("harness") or {}).get("root")
    root_changed = old_root != str(HARNESS_ROOT)
    stored_site_root, live_root = site.stored_root, str(site.root)
    site_moved = stored_site_root != live_root
    if site_moved and stored_site_root:
        # rename independence step 4: the site is not where config.json says; one `site.moved` per move, recorded
        # before the refresh so a reader can map paths recorded under the old root (Site.relocate) from now on
        Ledger(site).event("site.moved", "desk", None, {"from": stored_site_root, "to": live_root})
        site._previous_roots = None
    if root_changed or site_moved:
        cfg = dict(site.config)
        if root_changed:
            # D1: the site now runs this checkout (bin/dureh and the hooks were just regenerated to exec it)
            cfg["harness"] = {**(cfg.get("harness") or {}), "root": str(HARNESS_ROOT)}
        if site_moved:
            cfg["site"] = {**(cfg.get("site") or {}), "root": live_root}  # informational: readers use the live root
        write_replace(site.config_path, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", site.config_path.stat().st_mode & 0o777)
        site.config = cfg
    if new["harness"] != dep.get("harness") or new["package"] != dep.get("package") or root_changed:
        new["recorded"] = utc_now()
        Ledger(site).event("deployment.updated", "desk", None, {"previous": {"harness": dep.get("harness"),
                                                                             "package": dep.get("package"),
                                                                             "harness_root": old_root},
                                                                "current": {"harness": new["harness"], "package": new["package"],
                                                                            "harness_root": str(HARNESS_ROOT)}})
        tmp = path.with_name(".deployment.json.tmp")
        tmp.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    return new
