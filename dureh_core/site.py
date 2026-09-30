"""An installed DUREH site: configuration, paths, and the organization it enables."""
from __future__ import annotations

import os
from pathlib import Path

from . import yamlsub
from .org import HARNESS_ROOT, Org
from .util import fail, read_json

RECORD_KINDS = ("work-item", "run", "decision", "session", "finding", "event")
READ_ONLY_SITE_DIRS = ("references", "startup")
TOOL_NAMES = ("wi", "dispatch", "verify", "ledger", "status", "slurm", "path-refs", "purge", "push", "usage")


class Site:
    def __init__(self, root: Path):
        root = Path(root).expanduser()
        if not root.is_dir():
            fail(f"site directory does not exist: {root}")
        self.root = root.resolve()
        self.config_path = self.root / "config.json"
        self.config = read_json(self.config_path)
        if self.config.get("schema_version") != 1:
            fail(f"unsupported site config schema_version in {self.config_path}")
        self._org: Org | None = None
        self._manifest = None

    # --- paths -----------------------------------------------------------
    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def records(self) -> Path:
        return self.state / "records"

    def record_dir(self, kind: str) -> Path:
        if kind not in RECORD_KINDS:
            fail(f"unknown record kind: {kind!r}")
        return self.records / kind

    @property
    def startup(self) -> Path:
        return self.root / "startup"

    @property
    def worktrees(self) -> Path:
        return self.root / "worktrees"

    @property
    def harness_root(self) -> Path:
        return HARNESS_ROOT

    @property
    def site_id(self) -> str:
        return self.config.get("site", {}).get("id") or "unknown-site"

    def index_path(self) -> Path:
        configured = (self.config.get("index") or {}).get("path")
        if configured:
            return Path(os.path.expandvars(configured)).expanduser()
        base = os.environ.get("TMPDIR") or "/tmp"
        return Path(base) / f"dureh-{os.environ.get('USER', 'user')}-{self.site_id}" / "index.sqlite"

    def reference_paths(self) -> list[Path]:
        out = []
        for ref in self.config.get("references") or []:
            p = ref.get("path") if isinstance(ref, dict) else ref
            if p:
                out.append(Path(p).expanduser())
        link_dir = self.root / "references"
        if link_dir.is_dir():
            for child in link_dir.iterdir():
                out.append(child)
        return out

    def env(self) -> dict:
        env = dict(os.environ)
        prepend = (self.config.get("env") or {}).get("path_prepend") or []
        if prepend:
            env["PATH"] = os.pathsep.join([*prepend, env.get("PATH", "")])
        env["DUREH_SITE"] = str(self.root)
        env["DUREH_ROOT"] = str(HARNESS_ROOT)
        return env

    # --- package and org ---------------------------------------------------
    def package_manifest(self) -> dict:
        if self._manifest is None:
            self._manifest = yamlsub.load_file(self.startup / "startup.yaml")
        return self._manifest

    @property
    def org(self) -> Org:
        if self._org is None:
            manifest = self.package_manifest()
            organization = manifest.get("organization") or {}
            enabled = list(organization.get("managers") or []) + list(organization.get("workers") or [])
            self._org = Org.load(HARNESS_ROOT, enabled or None)
            overrides = self.config.get("limits") or {}
            self._org.limits.update(overrides)
        return self._org

    def limit(self, name: str, default=None):
        return self.org.limits.get(name, default)

    def model_for_tier(self, tier: str) -> dict:
        models = self.config.get("models") or {}
        entry = models.get(tier)
        if not isinstance(entry, dict) or not entry.get("model"):
            fail(f"site config has no model mapping for tier {tier!r}")
        return entry


def find_site(explicit: str | None) -> Site:
    value = explicit or os.environ.get("DUREH_SITE")
    if not value:
        fail("--site is required (or set DUREH_SITE)")
    return Site(Path(value))
