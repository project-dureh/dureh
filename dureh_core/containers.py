"""The site's container cache location.

Release 46 removed `dureh container build/status/retire` (images are built by the modules that need them, outside the
harness; proposals/boundary-design-2026-10-08.md 8.1). What stays is the cache location: config `containers.cache`
(else the env var named by `containers.cache_env`). No role writes the cache through the hook (paths.classify zone
`container-cache`, a write-guard invariant).
"""
from __future__ import annotations

import os
from pathlib import Path

from .site import Site


def _cfg(site: Site) -> dict:
    return site.config.get("containers") or {}


def cache_env(site: Site) -> str | None:
    """The env var that names the cache when `containers.cache` is unset: config `containers.cache_env`
    (default DUREH_CONTAINER_CACHE; null = no env lookup). UP-0069: the company's name is config, not code."""
    name = site.container_setting("cache_env")
    return str(name) if name else None


def cache_dir(site: Site) -> Path | None:
    """The container cache: config `containers.cache`, else the site env var named by `containers.cache_env`
    (config `env.vars` or the process environment); relative paths are under the site. None when unset."""
    raw = _cfg(site).get("cache")
    var = cache_env(site)
    if not raw and var:
        raw = ((site.config.get("env") or {}).get("vars") or {}).get(var) or os.environ.get(var)
    if not raw:
        return None
    p = Path(site.expand(str(raw)))
    return p if p.is_absolute() else site.root / p
