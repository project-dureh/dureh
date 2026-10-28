"""UR-20261027-01 (Director 2026-10-28): `dureh site new-project` creates a records project (git, no data, no DataLad)
where the package allows it (startup.yaml `projects: {records: true}`); data projects keep `site link-project`."""
import json
import subprocess
import unittest

from helpers import SiteCase

from dureh_core import sitelinks
from dureh_core import workflow as wf
from dureh_core.records import FAIL_CLOSED_RULE, Ledger
from dureh_core.util import DurehError


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class NewProject(SiteCase):
    def setUp(self):
        super().setUp()
        m = self.package / "startup.yaml"
        m.write_text(m.read_text() + "projects:\n  records: true\n")
        self.install()
        self.s = self.site()

    def config(self):
        return json.loads((self.site_root / "config.json").read_text())

    def test_creates_registers_and_is_a_wi_target(self):
        out = sitelinks.new_project(self.s, "desk", "golden", purpose="rep-pattern notes")
        root = self.site_root / "projects" / "golden"
        for d in ("research-notes", "reports", "runs"):
            self.assertTrue((root / d).is_dir(), d)
        self.assertIn("rep-pattern notes", (root / "README.md").read_text())
        self.assertEqual(git(root, "status", "--porcelain"), "")
        self.assertEqual(out["rev"], git(root, "rev-parse", "HEAD"))
        entry = next(e for e in self.config()["projects"] if e["id"] == "golden")
        self.assertEqual((entry["kind"], entry["path"], entry["data_fetched"]), ("records", "projects/golden", False))
        self.assertNotIn("source", entry)
        ev = [e for e in Ledger(self.site()).events() if e["type"] == "site.project-created"]
        self.assertEqual(len(ev), 1)
        self.assertFalse(ev[0]["payload"]["adopted"])
        wi = wf.wi_new(self.site(), "desk", "notes", "write notes", "researcher", "projects/golden", ["a note"])
        self.assertEqual(wi["payload"]["target"], "projects/golden")

    def test_adopts_a_plain_directory(self):
        root = self.site_root / "projects" / "golden"
        (root / "research-notes").mkdir(parents=True)
        (root / "README.md").write_text("# golden\nkept\n")
        (root / "research-notes" / "note.md").write_text("a note\n")
        out = sitelinks.new_project(self.s, "desk", "golden")
        self.assertTrue(out["adopted"])
        self.assertEqual((root / "README.md").read_text(), "# golden\nkept\n")
        self.assertIn("research-notes/note.md", git(root, "ls-files").splitlines())
        self.assertFalse((root / "research-notes" / ".gitkeep").exists())

    def test_refusals(self):
        with self.assertRaises(DurehError):
            sitelinks.new_project(self.s, "py-writer", "x")
        with self.assertRaises(DurehError):
            sitelinks.new_project(self.s, "desk", "-bad")
        repo = self.site_root / "projects" / "repo"
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        with self.assertRaisesRegex(DurehError, "not a plain directory"):
            sitelinks.new_project(self.s, "desk", "repo")
        sitelinks.new_project(self.s, "desk", "once")
        with self.assertRaisesRegex(DurehError, "already registered"):
            sitelinks.new_project(self.site(), "desk", "once")

    def test_fail_closed_rule_names_only_data_project_links(self):
        self.assertIn("a dataset or data project link (`site link-project`", FAIL_CLOSED_RULE)
        self.assertIn("`site new-project`", FAIL_CLOSED_RULE)


class NewProjectOff(SiteCase):
    def test_refused_without_the_package_switch(self):
        self.install()
        with self.assertRaisesRegex(DurehError, "does not allow records projects"):
            sitelinks.new_project(self.site(), "desk", "golden")
        self.assertFalse((self.site_root / "projects" / "golden").exists())

    def test_switch_must_be_a_boolean_mapping(self):
        m = self.package / "startup.yaml"
        m.write_text(m.read_text() + "projects:\n  records: yes please\n")
        with self.assertRaisesRegex(DurehError, "projects must be a mapping"):
            self.install()


if __name__ == "__main__":
    unittest.main()
