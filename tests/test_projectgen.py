"""Site WI-0037/0038, release 46 (O13): generated project guidance is a generic target scaffold: the package's
project template plus a short generated block (work through the site, the gates, scheduler jobs, the project's
protected paths), and agent files for the package roles marked `project: true`. No data-flow text."""
import json
import os
import subprocess
import unittest

from helpers import ExampleSiteCase

from dureh_core import projectgen as pg
from dureh_core.sitelinks import _config_add

PROJECT_ROLES = {"runner", "publisher"}  # example-org marks them `project: true`


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ProjectGen(ExampleSiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        self.proj = self.site_root / "projects" / "p1"
        self.proj.mkdir(parents=True)
        (self.proj / "manifest.yaml").write_text("schema_version: 1\npipelines: [{family: x}]\n")
        git(self.proj, "init", "-q", "-b", "main")
        git(self.proj, "add", "-A")
        git(self.proj, "commit", "-qm", "init")
        _config_add(self.s, "projects", {"id": "p1", "path": "projects/p1"})
        self.s = self.site()

    def test_generates_agents_for_project_roles_and_commits(self):
        r = pg.generate_projects(self.s)
        self.assertTrue(r[0]["changed"])
        agents = {p.stem for p in (self.proj / ".claude" / "agents").glob("*.md")}
        self.assertEqual(agents, PROJECT_ROLES)
        self.assertEqual(set(pg.project_roles(self.s)), PROJECT_ROLES)
        self.assertTrue((self.proj / "CLAUDE.md").is_file())
        self.assertTrue((self.proj / ".claude" / ".dureh-generated.json").is_file())
        self.assertEqual(git(self.proj, "status", "--porcelain"), "")
        self.assertIn("dureh", git(self.proj, "log", "-1", "--format=%s"))

    def test_claude_md_is_the_template_plus_a_generic_block(self):
        pg.generate_projects(self.s)
        text = (self.proj / "CLAUDE.md").read_text()
        self.assertTrue(text.startswith("# Project 1.0.0\n\nThis project belongs to the example organization."), text[:80])
        for part in ("## Working in this project", str(self.site_root), "Gate 1", "Gate 2", "dureh slurm submit",
                     "## Protected paths", "`inputs/**` no-read", "`published/**` read-only"):
            self.assertIn(part, text)
        for gone in ("Data flow", "pipeline", "manifest"):
            self.assertNotIn(gone, text, "no data-flow text from the project manifest")

    def test_no_template_no_claude_md(self):
        tpl = self.site_root / "startup" / "templates" / "project" / "CLAUDE.md.template"
        tpl.chmod(0o644)
        tpl.unlink()
        pg.generate_projects(self.site())
        self.assertFalse((self.proj / "CLAUDE.md").exists())
        self.assertTrue((self.proj / ".claude" / "agents" / "runner.md").is_file())

    def test_scopes_are_project_relative_and_files_say_guidance_only(self):
        pg.generate_projects(self.s)
        text = (self.proj / ".claude" / "agents" / "runner.md").read_text()
        self.assertIn("checkpoints/**", text)
        self.assertNotIn("projects/*/", text)
        self.assertIn("guidance only", text)
        self.assertIn(str(self.site_root), text)

    def test_second_sync_is_a_no_op(self):
        pg.generate_projects(self.s)
        head = git(self.proj, "rev-parse", "HEAD")
        r = pg.generate_projects(self.s)
        self.assertFalse(r[0]["changed"])
        self.assertEqual(git(self.proj, "rev-parse", "HEAD"), head)

    def test_hand_edit_is_reported_by_the_session_check(self):
        from dureh_core.selftest import session_checks

        pg.generate_projects(self.s)
        p = self.proj / ".claude" / "agents" / "publisher.md"
        p.write_text(p.read_text() + "\nmy note\n")
        self.assertEqual(pg.project_drift(self.s), ["projects/p1: generated file was edited by hand: .claude/agents/publisher.md"])
        self.assertTrue(any("projects/p1" in line for line in session_checks(self.s)))

    def test_existing_hand_written_files_are_left_untouched(self):
        (self.proj / "CLAUDE.md").write_text("mine\n")
        git(self.proj, "add", "CLAUDE.md")
        git(self.proj, "commit", "-qm", "own CLAUDE.md")
        r = pg.generate_projects(self.s)
        self.assertEqual((self.proj / "CLAUDE.md").read_text(), "mine\n")
        self.assertIn("CLAUDE.md", r[0]["skipped"])

    def test_files_of_roles_no_longer_marked_are_removed(self):
        pg.generate_projects(self.s)
        m = self.site_root / "startup" / "startup.yaml"
        m.chmod(0o644)
        m.write_text(m.read_text().replace("    project: true\n    output: The project catalog", "    output: The project catalog"))
        r = pg.generate_projects(self.site())
        self.assertEqual(r[0]["removed"], [".claude/agents/publisher.md"])
        self.assertFalse((self.proj / ".claude" / "agents" / "publisher.md").exists())

    def test_saves_in_a_datalad_project(self):
        from helpers import TOOLS, foreign_git_above, private_tmp, run

        if not TOOLS:
            self.skipTest("datalad and git-annex not available: not run")
        above = foreign_git_above(self.tmp)
        if above is not None:  # UR-20261030-01: datalad would take it for a superdataset
            self.skipTest(f"{above} is above the test site and no clean temporary directory was found: not run")
        os.environ["PATH"] = TOOLS
        dl = self.site_root / "projects" / "dl"
        before = (os.environ.get("TMPDIR"), os.environ.get("GIT_CEILING_DIRECTORIES"))
        with private_tmp(self.tmp) as tmpdir:  # finding 68757440: never the node's shared /tmp
            self.assertTrue(tmpdir.startswith(str(self.tmp)))
            run(["datalad", "create", "-c", "text2git", str(dl)])
            _config_add(self.s, "projects", {"id": "dl", "path": "projects/dl"})
            r = pg.generate_projects(self.site(), only="dl")
        self.assertEqual((os.environ.get("TMPDIR"), os.environ.get("GIT_CEILING_DIRECTORIES")), before, "restored")
        self.assertTrue(r[0]["changed"])
        self.assertEqual(git(dl, "status", "--porcelain"), "")
        self.assertIn("dureh: regenerate", git(dl, "log", "-1", "--format=%s"))

    def test_link_project_generates(self):
        from dureh_core import sitelinks

        calls = []
        orig = pg.generate_projects
        pg.generate_projects = lambda site, only=None: calls.append(only) or []
        try:
            sitelinks._after_link(self.s, "p1")
        finally:
            pg.generate_projects = orig
        self.assertEqual(calls, ["p1"])


if __name__ == "__main__":
    unittest.main()
