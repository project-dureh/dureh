"""Test helpers. Every test gets its own temporary site; nothing touches a live site."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parent.parent
FIXTURE_PACKAGE = HARNESS / "tests" / "fixtures" / "minimal-package"
# the neutral schema-2 example package (release 46): package roles with capabilities, labels, protected paths
EXAMPLE_PACKAGE = HARNESS / "tests" / "fixtures" / "example-org"


# datalad and git-annex for the suites that need them (where git-annex lives when it is not on PATH; override with
# DUREH_TEST_ANNEX_BIN); None when unavailable, and those tests are not run
ANNEX_BIN = Path(os.environ.get("DUREH_TEST_ANNEX_BIN") or Path.home() / "micromamba" / "envs" / "annex" / "bin")


def _tools_path():
    path = os.environ.get("PATH", "")
    if ANNEX_BIN.is_dir():
        path = f"{ANNEX_BIN}{os.pathsep}{path}"
    return path if shutil.which("datalad", path=path) and shutil.which("git-annex", path=path) else None


TOOLS = _tools_path()


def foreign_git_above(path) -> Path | None:
    """The nearest `.git` in `path` or one of its ancestors, else None. datalad looks for a superdataset this way, in
    Python, so GIT_CEILING_DIRECTORIES does not stop it (UR-20261030-01: a compute node's /tmp/.git that is not ours
    made `datalad create` run `git -C /tmp init`)."""
    for d in [Path(path).resolve(), *Path(path).resolve().parents]:
        if (d / ".git").exists():
            return d / ".git"
    return None


def _test_tmp_root() -> str | None:
    """Where each test's temporary site is made: the default temporary directory, or (when a `.git` sits in it or
    above it) the first of the fallbacks without one; None (the default) when none is clean."""
    for cand in (tempfile.gettempdir(), "/dev/shm"):
        if os.path.isdir(cand) and os.access(cand, os.W_OK) and foreign_git_above(cand) is None:
            return cand
    return None


TEST_TMP_ROOT = _test_tmp_root()


class private_tmp:
    """`with private_tmp(base):` datalad and git-annex subprocesses (and the harness commands that start them) see a
    TMPDIR of their own, a new directory under `base`, and git never looks above `base` for a repository
    (GIT_CEILING_DIRECTORIES). Company finding 68757440 (2026-10-26): on a compute node datalad's probe met a
    `/tmp/.git` that was not ours (Permission denied). The environment is restored on exit."""

    def __init__(self, base):
        self.base = Path(base)

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in ("TMPDIR", "GIT_CEILING_DIRECTORIES")}
        os.environ["TMPDIR"] = tempfile.mkdtemp(prefix="tool-tmp-", dir=self.base)
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.base.parent)
        return os.environ["TMPDIR"]

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def run(cmd, cwd=None):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-1500:]}")
    return proc.stdout


def minimal_org():
    """The organization the minimal package (schema 2: pm-modules, py-writer, doc-writer, researcher) builds."""
    from dureh_core import yamlsub
    from dureh_core.org import Org

    return Org.load(HARNESS, manifest=yamlsub.load_file(FIXTURE_PACKAGE / "startup.yaml"))


def example_org():
    """The organization the example package builds on this harness."""
    from dureh_core import yamlsub
    from dureh_core.org import Org

    return Org.load(HARNESS, manifest=yamlsub.load_file(EXAMPLE_PACKAGE / "startup.yaml"))
GIT_ENV = {"GIT_AUTHOR_NAME": "DUREH Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "DUREH Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


def copy_writable(src, dst, **kw) -> None:
    """copytree, then make the copy writable: release checkouts are read-only (D1) and copytree keeps modes."""
    shutil.copytree(src, dst, **kw)
    for root, dirs, files in os.walk(dst):
        for name in dirs + files:
            p = Path(root) / name
            if not p.is_symlink():
                p.chmod(p.stat().st_mode | 0o200)
    Path(dst).chmod(Path(dst).stat().st_mode | 0o200)


class SiteCase(unittest.TestCase):
    """Creates tmp/{package, site, ref}. The package is a copy of PACKAGE (the minimal package unless a subclass
    names another fixture)."""

    PACKAGE = FIXTURE_PACKAGE

    def setUp(self):
        self._env = dict(os.environ)
        os.environ.update(GIT_ENV)
        os.environ.pop("DUREH_ACTOR", None)
        os.environ.pop("DUREH_SITE", None)
        os.environ.pop("SLURM_JOB_ID", None)  # never the real session job: 2026-10-03 a close() test cancelled it
        self.tmp = Path(tempfile.mkdtemp(prefix="dureh-test-", dir=TEST_TMP_ROOT)).resolve()
        os.environ["TMPDIR"] = str(self.tmp / "nodelocal")
        (self.tmp / "nodelocal").mkdir()
        self.package = self.tmp / "package"
        copy_writable(self.PACKAGE, self.package)
        self.ref = self.tmp / "reference-repo"
        (self.ref / "docs").mkdir(parents=True)
        (self.ref / "docs" / "old.md").write_text("old rules; do not follow\n")
        self.site_root = self.tmp / "site"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def preset(self, **extra) -> Path:
        data = {"name": "test-site", "persona": {"name": "Tester"},
                "references": [{"path": str(self.ref), "name": "old-org"}],
                # each_level any: the suites submit single plumbing jobs; the file-level loop rule has its own tests
                # external_jobs off: status must not list a login node's real squeue jobs (test_external_jobs fakes it)
                "slurm": {"account": "acct-x", "partition": "general", "each_level": "any", "external_jobs": False},
                "checks": {"runner": "local"}}  # never a real sbatch from a test; test_verify_slurm fakes it
        data.update(extra)
        p = self.tmp / "preset.json"
        p.write_text(json.dumps(data))
        return p

    def install(self, **extra):
        from dureh_core.install import install

        return install(str(self.package), str(self.site_root), str(self.preset(**extra)), run_unit=False)

    def site(self):
        from dureh_core.site import Site

        return Site(self.site_root)

    def make_module(self, name="demo") -> Path:
        mod = self.site_root / "modules" / name
        (mod / "src").mkdir(parents=True)
        (mod / "src" / "demo.py").write_text("def f():\n    return 1\n")
        (mod / "tests").mkdir()
        (mod / "tests" / "test_demo.py").write_text(
            "import sys, os, unittest\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
            "import demo\n\nclass T(unittest.TestCase):\n    def test_f(self):\n        self.assertEqual(demo.f(), 1)\n")
        subprocess.run(["git", "init", "-q", "-b", "main", str(mod)], check=True)
        subprocess.run(["git", "add", "-A"], cwd=mod, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=mod, check=True)
        return mod


RUNNER_ROLES = """  coordinator:
    kind: manager
    tier: pm
    description: Manager for batch work on projects.
    may_dispatch: [runner, py-writer, doc-writer, researcher, checker]
  runner:
    kind: worker
    tier: worker
    isolation: scratch
    output: Scheduler jobs submitted and recorded through the slurm tool.
    never: Code changes.
    capabilities: [submit_jobs]
    write_scopes:
      - projects/*/checkpoints/**
      - projects/*/code/notebooks/**
      - projects/*/README.md
"""


def add_runner_roles(package: Path) -> None:
    """Add a manager `coordinator` and a scratch `runner` (submit_jobs) to a copy of the minimal package: the suites
    that exercise scheduler plumbing need a role with the submit_jobs capability (release 46: no harness role has it)."""
    m = Path(package) / "startup.yaml"
    text = m.read_text()
    m.write_text(text.replace("\nroles:\n", "\nroles:\n" + RUNNER_ROLES, 1))


class ExampleSiteCase(SiteCase):
    """A site of the neutral example package (example-org, schema 2)."""

    PACKAGE = EXAMPLE_PACKAGE


def hook(site, **data):
    from dureh_core.runtimes.claude_code import handle

    return handle(site, data)


def denied(out) -> bool:
    return bool(out) and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def commit_work(site, wi, message="work") -> str:
    """An empty commit on the WI branch, so the head is past its dispatch base (release 81: `wi stage` refuses a
    head equal to the base); returns the new head."""
    wt = site.worktrees / wi
    subprocess.run(["git", "commit", "--allow-empty", "-qm", message], cwd=wt, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, check=True, capture_output=True, text=True).stdout.strip()
