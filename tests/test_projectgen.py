"""Site WI-0037/0038: generated project-local agent files and project CLAUDE.md."""
import json
import os
import shutil
import subprocess
import unittest

from helpers import SiteCase

from dureh_core import projectgen as pg
from dureh_core.records import Ledger
from dureh_core.sitelinks import _config_add

PROJECT_ROLES = {"data-steward", "pipeline-runner", "interpreter", "doc-writer", "checker"}
MANIFEST = """schema_version: 1
template_version: 0.2.0
project:
  id: lna
  title: Rat LNA
software:
  camri: 2.0.0a1
  container_digest: sha256:abc123
datasets:
  rawbids: rawbids
  derivatives: [fmriprep_camri_rat_bold]
containers:
  - name: camri
    digest: sha256:abc123
external_links:
  - path: sourcedata/atlases
    target: /proj/atlases
"""


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ProjectGen(SiteCase):
    def setUp(self):
        super().setUp()
        manifest = self.package / "startup.yaml"
        manifest.write_text(manifest.read_text()
                            .replace("managers: [pm-modules]", "managers: [pm-modules, pm-projects]")
                            .replace("workers: [py-writer,",
                                     "workers: [data-steward, pipeline-runner, interpreter, py-writer,"))
        tpl = self.package / "templates" / "project"
        tpl.mkdir(parents=True)
        (tpl / "CLAUDE.md.template").write_text("# Project agent instructions (template {{template_version}})\n\nFixed text.\n")
        self.install()
        self.s = self.site()
        self.proj = self.site_root / "projects" / "lna"
        self.proj.mkdir(parents=True)
        (self.proj / "camri-manifest.yaml").write_text(MANIFEST)
        git(self.proj, "init", "-q", "-b", "main")
        git(self.proj, "add", "-A")
        git(self.proj, "commit", "-qm", "init")
        _config_add(self.s, "projects", {"id": "lna", "path": "projects/lna"})
        self.s = self.site()

    def test_generates_agents_for_project_roles_and_commits(self):
        r = pg.generate_projects(self.s)
        self.assertTrue(r[0]["changed"])
        agents = {p.stem for p in (self.proj / ".claude" / "agents").glob("*.md")}
        self.assertEqual(agents, PROJECT_ROLES)
        self.assertTrue((self.proj / "CLAUDE.md").is_file())
        self.assertTrue((self.proj / ".claude" / ".dureh-generated.json").is_file())
        self.assertEqual(git(self.proj, "status", "--porcelain"), "")
        self.assertIn("dureh", git(self.proj, "log", "-1", "--format=%s"))

    def test_scopes_are_project_relative_and_files_say_guidance_only(self):
        pg.generate_projects(self.s)
        text = (self.proj / ".claude" / "agents" / "data-steward.md").read_text()
        self.assertIn("derivatives/**", text)
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
        p = self.proj / ".claude" / "agents" / "interpreter.md"
        p.write_text(p.read_text() + "\nmy note\n")
        self.assertEqual(pg.project_drift(self.s), ["projects/lna: generated file was edited by hand: .claude/agents/interpreter.md"])
        self.assertTrue(any("projects/lna" in line for line in session_checks(self.s)))

    def test_existing_hand_written_files_are_left_untouched(self):
        (self.proj / "CLAUDE.md").write_text("mine\n")
        git(self.proj, "add", "CLAUDE.md")
        git(self.proj, "commit", "-qm", "own CLAUDE.md")
        r = pg.generate_projects(self.s)
        self.assertEqual((self.proj / "CLAUDE.md").read_text(), "mine\n")
        self.assertIn("CLAUDE.md", r[0]["skipped"])

    def test_claude_md_is_filled_from_manifest_and_records(self):
        led = Ledger(self.s)
        from dureh_core import workflow as wf

        b = wf.wi_new(self.s, "desk", "B", "g", "pm-projects", "projects/lna", ["ok"])["id"]
        w = wf.wi_new(self.s, "desk", "Promote", "g", "data-steward", "projects/lna", ["ok"], parent=b)["id"]
        led.append("run", "promotion", "data-steward", w, {
            "run_type": "promotion", "status": "completed", "source": "checkpoints/fmriprep/x",
            "dataset": "derivatives/fmriprep_camri_rat_bold", "dest": ["derivatives/fmriprep_camri_rat_bold/x"],
            "from_run": "run-1", "job_id": "4242", "commit": "c0ffee"})
        pg.generate_projects(self.s)
        text = (self.proj / "CLAUDE.md").read_text()
        self.assertTrue(text.startswith("# Project agent instructions (template 0.2.0)"))
        for part in ("rawbids -> checkpoints -> derivatives -> reports", "camri 2.0.0a1", "sha256:abc123",
                     "sourcedata/atlases -> /proj/atlases", "derivatives/fmriprep_camri_rat_bold",
                     f"{w} (data-steward), run run-1, job 4242, commit c0ffee", "Gate 1", "dureh promote"):
            self.assertIn(part, text)

    def test_saves_in_a_datalad_project(self):
        from test_promote import TOOLS, run

        if not TOOLS:
            self.skipTest("datalad and git-annex not available: not run")
        os.environ["PATH"] = TOOLS
        dl = self.site_root / "projects" / "dl"
        run(["datalad", "create", "-c", "text2git", str(dl)])
        _config_add(self.s, "projects", {"id": "dl", "path": "projects/dl"})
        r = pg.generate_projects(self.site(), only="dl")
        self.assertTrue(r[0]["changed"])
        self.assertEqual(git(dl, "status", "--porcelain"), "")
        self.assertIn("dureh: regenerate", git(dl, "log", "-1", "--format=%s"))

    def test_link_project_generates(self):
        from dureh_core import sitelinks

        calls = []
        orig = pg.generate_projects
        pg.generate_projects = lambda site, only=None: calls.append(only) or []
        try:
            sitelinks._after_link(self.s, "lna")
        finally:
            pg.generate_projects = orig
        self.assertEqual(calls, ["lna"])


if __name__ == "__main__":
    unittest.main()
