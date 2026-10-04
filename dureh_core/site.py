"""An installed DUREH site: configuration, paths, and the organization it enables."""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import yamlsub
from .org import HARNESS_ROOT, Org
from .util import fail, read_json

RECORD_KINDS = ("work-item", "run", "decision", "session", "finding", "event")
READ_ONLY_SITE_DIRS = ("references", "startup")
_OBSERVE_DIRS: set = set()
_VAR = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}|\$(\w+)")
TOOL_NAMES = ("wi", "dispatch", "verify", "ledger", "status", "slurm", "container", "path-refs", "purge", "push", "usage", "upstream", "promote", "worktree", "day")


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
        """The harness tree this command runs from."""
        return HARNESS_ROOT

    @property
    def pinned_harness_root(self) -> Path:
        """The harness the site is installed on: config.json `harness.root` when set (D1, `adapter sync`), else
        the running tree. What a session job must export as DUREH_ROOT, whichever tree runs `session start`."""
        named = (self.config.get("harness") or {}).get("root")
        if not named:
            return HARNESS_ROOT
        p = Path(self.expand(named))  # as written: a release checkout path stays the path the site was synced to
        return p if p.is_absolute() else self.root / p

    @property
    def site_id(self) -> str:
        return self.config.get("site", {}).get("id") or "unknown-site"

    @property
    def observe(self) -> bool:
        return os.environ.get("DUREH_OBSERVE") == "1"

    def index_path(self) -> Path:
        if self.observe:
            import atexit
            import shutil
            import tempfile

            private = Path(tempfile.gettempdir()) / f"dureh-observe-{os.getpid()}-{self.site_id}"
            if private not in _OBSERVE_DIRS:
                _OBSERVE_DIRS.add(private)
                atexit.register(shutil.rmtree, private, True)
            return private / "index.sqlite"
        configured = (self.config.get("index") or {}).get("path")
        if configured:
            return Path(self.expand(configured))
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

    def expand(self, value: str, env: dict | None = None) -> str:
        """Expand ~, $VAR, ${VAR} and ${VAR:-default}; DUREH_SITE and DUREH_ROOT always name this site and harness."""
        base = dict(os.environ if env is None else env, DUREH_SITE=str(self.root), DUREH_ROOT=str(HARNESS_ROOT))

        def sub(m):
            name = m.group(1) or m.group(3)
            if name in base:
                return base[name]
            return m.group(2) if m.group(2) is not None else m.group(0)

        return os.path.expanduser(_VAR.sub(sub, str(value)))

    def env(self) -> dict:
        env = dict(os.environ)
        env["DUREH_SITE"] = str(self.root)
        env["DUREH_ROOT"] = str(HARNESS_ROOT)
        prepend = [self.expand(x, env) for x in (self.config.get("env") or {}).get("path_prepend") or []]
        if prepend:
            env["PATH"] = os.pathsep.join([*prepend, env.get("PATH", "")])
        for key, value in ((self.config.get("env") or {}).get("vars") or {}).items():
            env[str(key)] = self.expand(value, env)
        return env

    def package_source(self) -> Path | None:
        """The startup package source tree: absolute, relative to the site, or built from env vars."""
        src = (self.config.get("startup") or {}).get("source")
        if not src:
            return None
        p = Path(self.expand(src))
        return p if p.is_absolute() else (self.root / p).resolve()

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
