"""Install a reviewed startup package into a new, empty site."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

from . import SCHEMA_VERSION, yamlsub
from .org import HARNESS_ROOT, Org
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


def validate_package(startup: Path) -> dict:
    if startup.is_symlink() or not startup.is_dir():
        fail(f"startup package must be a directory: {startup}")
    manifest_path = startup / "startup.yaml"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        fail(f"required startup manifest is missing: {manifest_path}")
    m = yamlsub.load_file(manifest_path)
    pkg = m.get("package") if isinstance(m, dict) else None
    if not isinstance(pkg, dict) or not pkg.get("id") or not pkg.get("name"):
        fail(f"startup.yaml must declare package.id and package.name: {manifest_path}")
    for d in FORBIDDEN_PACKAGE_DIRS:
        if (startup / d).exists():
            fail(f"startup package may not contain site state ({d}/); state is site-owned")
    if (startup / ".gitmodules").is_file() and "state" in (startup / ".gitmodules").read_text(encoding="utf-8"):
        fail("startup package may not carry a state submodule or recovery pointer")
    if (m.get("state") or {}).get("package_contains_state") not in (False, None):
        fail("startup.yaml declares package state; packages must not contain state")
    org = m.get("organization") or {}
    Org.load(HARNESS_ROOT, list(org.get("managers") or []) + list(org.get("workers") or []) or None)
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
CONFIG_DEFAULT_KEYS = ("project", "containers", "slurm", "checks", "env", "limits", "guards", "modules", "index")


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
    (never changing a value the site has). Reads the package source tree's startup.yaml when it exists, else the
    site's copy. Returns the dotted keys added; config.json is rewritten only when something was added."""
    if manifest is None:
        src = site.package_source() or site.startup
        path = src / "startup.yaml"
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


def _config(site: Path, manifest: dict, startup: Path, preset: dict) -> dict:
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
                    "source": str(startup)},
        "references": preset.get("references") or [],
        "runtime": {"adapter": preset.get("adapter", "claude-code")},
        "models": models,
    }
    # precedence: the preset, then the package's config_defaults, then the harness defaults
    for key in ("index", "slurm", "checks", "push", "state", "console", "env", "limits"):
        if preset.get(key):
            config[key] = preset[key]
    for key in ("project", "containers", "guards", "modules"):
        if key in preset:
            config[key] = preset[key]
    config, _ = merge_config_defaults(config, package_config_defaults(manifest))
    for key, value in harness_defaults.items():
        config.setdefault(key, value)
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
    manifest = validate_package(startup)
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
        copy_package(startup, site_root / "startup")
        for kind in RECORD_KINDS:
            (site_root / "state" / "records" / kind).mkdir(parents=True)
        for d in ("state/persona", "state/memory", "projects", "modules", "references", "inbox", "worktrees", "notes"):
            (site_root / d).mkdir(parents=True, exist_ok=True)
        config = _config(site_root, manifest, startup, preset)
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
            spec = yamlsub.load_file(startup / rel)
            rec = wi_new(site, "desk", title=spec["title"], goal=spec["goal"], role=spec.get("role") or _role_for(manifest, spec),
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
                        "url": _remote(startup), "rev": git_revision(startup),
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


def _role_for(manifest: dict, spec: dict) -> str:
    value = spec.get("value")
    mapping = {"refactor-to-modules": "pm-modules", "reproducible-projects": "pm-projects",
               "evidence-based-interpretation": "pm-projects", "harness": "pm-harness"}
    return mapping.get(value, "pm-projects")


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
    src = site.package_source() or site.startup
    new = dict(dep)
    new["harness"] = {"url": _remote(HARNESS_ROOT), "rev": git_revision(HARNESS_ROOT)}
    new["package"] = {**dep.get("package", {}), "rev": git_revision(src) if src.exists() else dep.get("package", {}).get("rev")}
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
