"""Classify a filesystem path for write authorization.

This decides what a harness-mediated or hook-inspected write may touch. It is a
workflow control, not an operating-system sandbox: a process that writes
without passing through the CLI or the runtime hook is not constrained here.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .org import HARNESS_ROOT
from .site import Site
from .util import is_within, read_json


@dataclass
class Target:
    zone: str            # see classify()
    logical: str | None  # <target>/<rest> for scoped matching
    site_rel: str | None
    wi: str | None = None


def _resolve(path: Path) -> Path:
    # Resolve symlinks in existing parents; keep a nonexistent leaf as written.
    path = Path(os.path.abspath(path))
    parent, leaf = path.parent, path.name
    try:
        return parent.resolve(strict=False) / leaf if not path.is_symlink() else path.resolve(strict=False)
    except OSError:
        return path


def target_root(site: Site, target: str) -> Path | None:
    if target == "harness":
        return HARNESS_ROOT
    if target == "package":
        return site.package_source()
    parts = target.split("/")
    if len(parts) == 2 and parts[0] in ("modules", "projects") and parts[1]:
        return site.root / target
    return None


def wi_target(site: Site, wi: str) -> str | None:
    p = site.record_dir("work-item") / f"{wi}.json"
    if not p.is_file():
        return None
    return read_json(p)["payload"].get("target")


def scratch_roots() -> list[Path]:
    roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}
    for var in ("TMPDIR", "CLAUDE_SCRATCHPAD"):
        if os.environ.get(var):
            roots.add(Path(os.environ[var]).resolve())
    return sorted(roots)


def _container_cache(site: Site) -> Path | None:
    from .containers import cache_dir

    try:
        c = cache_dir(site)
    except Exception:  # noqa: BLE001 - a broken setting must not break every write decision
        return None
    if c is None:
        return None
    return Path(os.path.abspath(c)).resolve(strict=False)


def classify(site: Site, raw: str | Path, cwd: Path | None = None) -> Target:
    """Zones: reference, container-cache, records, desk-state, installed-package, generated, deployment,
    config, worktree, target, harness-main, site-other, scratch, outside."""
    p = Path(raw)
    if not p.is_absolute():
        p = (cwd or site.root) / p
    real = _resolve(p)
    cache = _container_cache(site)
    if cache is not None and (is_within(real, cache) or is_within(Path(os.path.abspath(p)), cache)):
        return Target("container-cache", None, None)
    for ref in site.reference_paths():
        for candidate in {Path(os.path.abspath(ref)), ref.resolve(strict=False)}:
            if is_within(real, candidate) or is_within(Path(os.path.abspath(p)), candidate):
                return Target("reference", None, None)
    root = site.root
    if is_within(real, root):
        rel = real.relative_to(root).as_posix()
        parts = rel.split("/")
        top = parts[0]
        if rel.startswith("state/records"):
            return Target("records", None, rel)
        if rel.startswith("state/persona") or rel.startswith("state/memory"):
            return Target("desk-state", None, rel)
        if top == "startup":
            return Target("installed-package", None, rel)
        if top == "references":
            return Target("reference", None, rel)
        if top in (".claude", "CLAUDE.md", "bin") or rel == ".mcp.json":
            return Target("generated", None, rel)
        if rel == "deployment.json":
            return Target("deployment", None, rel)
        if rel == "config.json":
            return Target("config", None, rel)
        if top == "worktrees" and len(parts) >= 2:
            wi = parts[1]
            target = wi_target(site, wi)
            rest = "/".join(parts[2:])
            logical = f"{target}/{rest}" if target and rest else None
            return Target("worktree", logical, rel, wi)
        if top in ("modules", "projects") and len(parts) >= 2:
            return Target("target", rel, rel)
        return Target("site-other", None, rel)
    if is_within(real, HARNESS_ROOT):
        return Target("harness-main", "harness/" + real.relative_to(HARNESS_ROOT).as_posix(), None)
    src = site.package_source()
    if src and is_within(real, src.resolve()):
        return Target("package-main", "package/" + real.relative_to(src.resolve()).as_posix(), None)
    for s in scratch_roots():
        if is_within(real, s):
            return Target("scratch", None, None)
    return Target("outside", None, None)


def write_decision(site: Site, role: str, raw: str | Path, cwd: Path | None = None,
                   agent_wi: str | None = None) -> tuple[bool, str]:
    """Return (allowed, reason) for a write by `role` to `raw`."""
    org = site.org
    r = org.role(role)
    t = classify(site, raw, cwd)
    if t.zone == "reference":
        return False, "reference material is read-only for every role"
    if t.zone == "container-cache":
        return False, "the container cache is written only by `dureh container build` jobs; no role writes it directly"
    if t.zone == "records":
        return False, "records are append-only and written only through the dureh ledger tools"
    if t.zone in ("generated", "deployment"):
        return False, f"{t.site_rel} is harness-generated; change the source and run 'dureh adapter sync'"
    if t.zone == "installed-package":
        return False, "the installed startup package is read-only; change the package through a WI worktree"
    if t.zone in ("harness-main", "package-main"):
        return False, "shared harness/package main trees change only through a WI worktree and Gate 2"
    if t.zone == "scratch":
        return True, "scratch space"
    if t.zone == "outside":
        return False, "path is outside the site and scratch space"
    if r.kind == "desk":
        if t.zone == "worktree":
            return False, "the desk does not edit a worker's WI worktree; stop or rework the WI instead"
        if t.zone in ("desk-state", "config", "site-other"):
            return True, "desk-owned site path"
        if t.zone == "target":
            if org.scope_allows("desk", t.logical):
                return True, "desk-owned project file"
            return False, "project and module content is produced by workers through WIs"
        return False, "not a desk-owned path"
    # managers and workers
    if t.zone == "worktree":
        if agent_wi and t.wi != agent_wi:
            return False, f"this agent is dispatched for {agent_wi}, not {t.wi}"
        if not t.logical:
            return False, "cannot map the worktree path to its WI target"
        if org.scope_allows(role, t.logical):
            return True, f"within {role} scope ({t.logical})"
        return False, f"{role} may not write {t.logical}"
    if t.zone == "target":
        root = target_root(site, "/".join(t.logical.split("/")[:2]))
        # a role without a WI worktree (isolation scratch) writes its scope in the target's main tree, tracked files
        # included (review 2026-10-07 #8: the runner's regenerated notebooks and README); every other role writes a
        # git-tracked target only through its WI worktree
        if root is not None and (root / ".git").exists() and r.isolation != "scratch":
            return False, "git-tracked targets are written only inside the WI worktree"
        if org.scope_allows(role, t.logical):
            return True, f"within {role} scope ({t.logical})"
        return False, f"{role} may not write {t.logical}"
    return False, f"{role} may not write {t.site_rel or raw}"
