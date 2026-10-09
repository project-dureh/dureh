"""UR-20261003-01: `dureh site new-module` creates a NEW module from the standard skeleton and registers it."""
import json
import shutil
import subprocess
import unittest

from helpers import SiteCase

from dureh_core import sitelinks
from dureh_core import workflow as wf
from dureh_core.util import DurehError


class NewModule(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()

    def config(self):
        return json.loads((self.site_root / "config.json").read_text())

    def test_creates_skeleton_registers_and_is_a_wi_target(self):
        out = sitelinks.new_module(self.s, "desk", "exmod-vis", "exmod_vis", python="3.11")
        mod = self.site_root / "modules" / "exmod-vis"
        for rel in ("pyproject.toml", ".python-version", "README.md", "docs/index.md",
                    "src/exmod_vis/__init__.py", "tests/test_import.py"):
            self.assertTrue((mod / rel).is_file(), rel)
        self.assertIn('requires-python = ">=3.11"', (mod / "pyproject.toml").read_text())
        self.assertEqual(subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=mod, capture_output=True,
                                        text=True).stdout.strip(), "main")
        self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=mod, capture_output=True,
                                        text=True).stdout.strip(), "", "everything committed")
        entry = next(e for e in self.config()["modules"] if e["id"] == "exmod-vis")
        self.assertEqual(entry["path"], "modules/exmod-vis")
        self.assertEqual(entry["env_rebuild"], sitelinks.DEFAULT_ENV_REBUILD)
        self.assertEqual(entry["rev"], out["rev"])
        if shutil.which("uv"):
            self.assertTrue(out["locked"], out)
            self.assertTrue((mod / "uv.lock").is_file())
        self.s = self.site()
        wi = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/exmod-vis", ["ok"])
        self.assertEqual(wi["payload"]["target"], "modules/exmod-vis")

    def test_external_source_and_dependency_pinned_by_revision(self):
        base = self.make_module("exmod")
        cfg = self.config()
        cfg["modules"] = [{"id": "exmod", "path": "modules/exmod", "source": str(base), "rev": "x", "branch": "main"}]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        src = self.tmp / "sources" / "exmod-vis"
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=base, capture_output=True, text=True).stdout.strip()
        out = sitelinks.new_module(self.s, "desk", "exmod-vis", "exmod_vis", source=str(src), depends=["exmod"],
                                   python="3.11")
        self.assertTrue((src / ".git").exists(), "repository created at the source")
        mod = self.site_root / "modules" / "exmod-vis"
        self.assertTrue((mod / ".git").exists(), "and linked into the site")
        text = (mod / "pyproject.toml").read_text()
        self.assertIn('"exmod",', text)
        self.assertIn(f'exmod = {{ git = "file://{base}", rev = "{head}" }}', text)
        self.assertEqual(out["depends"], {"exmod": head})

    def test_refusals(self):
        with self.assertRaises(DurehError):
            sitelinks.new_module(self.s, "py-writer", "x", "x")
        with self.assertRaises(DurehError):
            sitelinks.new_module(self.s, "desk", "x", "Bad-Name")
        with self.assertRaises(DurehError):
            sitelinks.new_module(self.s, "desk", "x", "x", depends=["nope"])
        (self.site_root / "modules" / "taken").mkdir(parents=True)
        with self.assertRaises(DurehError):
            sitelinks.new_module(self.s, "desk", "taken", "taken")


if __name__ == "__main__":
    unittest.main()
