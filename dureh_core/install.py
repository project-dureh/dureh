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
from .util import DurehError, dumps, fail, git_revision, read_json, utc_now

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
    return {
        "schema_version": SCHEMA_VERSION,
        "site": {"id": preset.get("site_id") or f"s-{secrets.token_hex(4)}", "name": preset.get("name") or site.name,
                 "root": str(site)},
        "harness": {"root": str(HARNESS_ROOT)},
        "startup": {"id": manifest["package"]["id"], "version": manifest["package"].get("version"),
                    "source": str(startup)},
        "references": preset.get("references") or [],
        "runtime": {"adapter": preset.get("adapter", "claude-code")},
        "models": models,
        "index": preset.get("index") or {},
        "slurm": preset.get("slurm") or {"account": None, "partition": None, "time": None},
        "push": preset.get("push") or {"allowed_remotes": []},
        "state": preset.get("state") or {"backend": "git", "ria": None},
        "console": preset.get("console") or {"revision": None},
        "env": preset.get("env") or {},
        "limits": preset.get("limits") or {},
    }


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
    src = Path((site.config.get("startup") or {}).get("source") or site.startup)
    new = dict(dep)
    new["harness"] = {"url": _remote(HARNESS_ROOT), "rev": git_revision(HARNESS_ROOT)}
    new["package"] = {**dep.get("package", {}), "rev": git_revision(src) if src.exists() else dep.get("package", {}).get("rev")}
    if new["harness"] != dep.get("harness") or new["package"] != dep.get("package"):
        new["recorded"] = utc_now()
        Ledger(site).event("deployment.updated", "desk", None, {"previous": {"harness": dep.get("harness"),
                                                                             "package": dep.get("package")},
                                                                "current": {"harness": new["harness"], "package": new["package"]}})
        tmp = path.with_name(".deployment.json.tmp")
        tmp.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    return new
