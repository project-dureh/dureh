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
GIT_ENV = {"GIT_AUTHOR_NAME": "DUREH Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "DUREH Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


class SiteCase(unittest.TestCase):
    """Creates tmp/{package, site, ref}. The package is a git repo so installs record a revision."""

    def setUp(self):
        self._env = dict(os.environ)
        os.environ.update(GIT_ENV)
        os.environ.pop("DUREH_ACTOR", None)
        os.environ.pop("DUREH_SITE", None)
        self.tmp = Path(tempfile.mkdtemp(prefix="dureh-test-")).resolve()
        os.environ["TMPDIR"] = str(self.tmp / "nodelocal")
        (self.tmp / "nodelocal").mkdir()
        self.package = self.tmp / "package"
        shutil.copytree(FIXTURE_PACKAGE, self.package)
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
                "slurm": {"account": "acct-x", "partition": "general"}}
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


def hook(site, **data):
    from dureh_core.runtimes.claude_code import handle

    return handle(site, data)


def denied(out) -> bool:
    return bool(out) and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
