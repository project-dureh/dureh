"""An installed DUREH site: configuration, paths, and the organization it enables."""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import yamlsub
from .org import HARNESS_ROOT, Org
from .util import DurehError, fail, is_within, read_json

RECORD_KINDS = ("work-item", "run", "decision", "session", "finding", "event")
READ_ONLY_SITE_DIRS = ("references", "startup")
_VAR = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}|\$(\w+)")
TOOL_NAMES = ("wi", "dispatch", "verify", "ledger", "status", "slurm", "container", "path-refs", "purge", "push", "usage", "upstream", "promote", "worktree", "day", "slack", "report")

# Company-specific names the harness used to carry in code (UP-0069): now config keys with generic defaults. A startup
# package supplies its own values through `config_defaults` in startup.yaml (merged at install, docs/config.md).
PROJECT_DEFAULTS = {
    "manifest": "manifest.yaml",             # the one project manifest the harness reads (pins, data flow); null = off
    "pipeline_version_key": "version",       # key of a pipeline entry that names the pipeline's software version
    "derivative_entry": "{family}_{species}_{suffix}",  # derivative name a pipeline entry produces (str.format fields)
}
CONTAINER_DEFAULTS = {
    "cache_env": "DUREH_CONTAINER_CACHE",    # env var that names the container cache when containers.cache is unset
    "manifest_globs": ["*manifest*.yaml", "*manifest*.yml", "*manifest*.json"],  # project files an image may be pinned in
}


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
        self._previous_roots: list[Path] | None = None

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
    def mailbox_name(self) -> str:
        """The site's mailbox name at the upstream exchange (`<exchange>/<mailbox>`, the `site:` header of its
        messages, the usage-budget label): config `upstream.mailbox`, else the site folder name. Pinning it lets
        the folder be renamed without moving the mailbox."""
        return str((self.config.get("upstream") or {}).get("mailbox") or self.root.name)

    @property
    def session_name(self) -> str:
        """The name of the site's sessions (job `cc-<name>[-purpose]`, pane log, Remote Control `{site}`): config
        `session.name`, else the site folder name."""
        return str((self.config.get("session") or {}).get("name") or self.root.name)

    @property
    def observe(self) -> bool:
        return os.environ.get("DUREH_OBSERVE") == "1"

    # --- the site's location over time ----------------------------------------
    @property
    def stored_root(self) -> str | None:
        """config.json `site.root`: where the site was installed, refreshed by `adapter sync` after a move. It is
        informational; the live root (`self.root`, the directory the command runs in) is what every reader uses."""
        return (self.config.get("site") or {}).get("root") or None

    def previous_roots(self) -> list[Path]:
        """Where the site was before it moved, newest first (never the live root): a stored `site.root` that differs
        from the live root (a move `adapter sync` has not seen yet), config `site.previous_roots` when present, and
        the `from` of every `site.moved` event (`adapter sync` records one per move). Read once per process."""
        if self._previous_roots is None:
            raw: list[str] = []
            if self.stored_root:
                raw.append(self.stored_root)
            raw += [str(x) for x in (self.config.get("site") or {}).get("previous_roots") or []]
            try:
                from .records import Ledger

                for e in reversed(Ledger(self).load("event")):
                    if e["type"] == "site.moved" and e["payload"].get("from"):
                        raw.append(str(e["payload"]["from"]))
            except DurehError:  # no record store yet (install in progress): the config is all there is
                pass
            roots: list[Path] = []
            for value in raw:
                p = Path(value)
                if p.is_absolute() and p != self.root and p not in roots:
                    roots.append(p)
            self._previous_roots = roots
        return self._previous_roots

    def relocate(self, path) -> Path:
        """A recorded absolute path as it is now (proposals/site-rename-independence.md row 12): under the live root
        it is returned unchanged; under one of `previous_roots` that prefix is replaced by the live root; anything
        else (a relative path, a path outside the site) is returned unchanged. Records are append-only and never
        rewritten: a reader that opens a path a record names calls this first."""
        p = Path(path)
        if not p.is_absolute() or is_within(p, self.root):
            return p
        for old in self.previous_roots():
            if is_within(p, old):
                return self.root / p.relative_to(old)
        return p

    def index_path(self) -> Path:
        if self.observe:
            # observe keeps its own index outside the site, never the site's shared one; it stays between calls
            # (fresh while the records' fingerprint matches), so a second observe read does not rebuild it
            import hashlib

            tag = hashlib.sha1(str(self.root).encode()).hexdigest()[:8]
            base = os.environ.get("TMPDIR") or "/tmp"
            private = Path(base) / f"dureh-observe-{os.environ.get('USER', 'user')}-{self.site_id}-{tag}"
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

    def _setting(self, section: str, defaults: dict, key: str):
        block = self.config.get(section)
        if isinstance(block, dict) and key in block:
            return block[key]
        return defaults[key]

    def project_setting(self, key: str):
        """config `project.<key>` (manifest, pipeline_version_key, derivative_entry), else PROJECT_DEFAULTS."""
        return self._setting("project", PROJECT_DEFAULTS, key)

    def container_setting(self, key: str):
        """config `containers.<key>` (cache_env, manifest_globs), else CONTAINER_DEFAULTS."""
        return self._setting("containers", CONTAINER_DEFAULTS, key)

    def project_manifest(self) -> str | None:
        """Name of the project manifest file the harness reads; None when `project.manifest` is null or empty."""
        name = self.project_setting("manifest")
        return str(name) if name else None

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
