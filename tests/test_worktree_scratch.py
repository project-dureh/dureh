"""Release 93 (UR-20261106-02): the release 92 brief told file-writing workers to write scripts to a file and run
`python3 -I <file>`, but named no place their scope lets them write (a company's py-writer drew 8 guard refusals on
`$TMPDIR` and worktree-root files in one morning). Every WI worktree now has `.dureh-scratch/`: the bound agent of a
file-writing role may write there whatever its scope, it is git-ignored, exempt from verify's ownership check and
removed at `wi stage`; the brief line names it."""
import subprocess
import unittest
from pathlib import Path

from helpers import SiteCase

from dureh_core import workflow as wf
from dureh_core.paths import WORKTREE_SCRATCH, write_decision
from dureh_core.records import Ledger


class WorktreeScratch(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)
        self.bundle = wf.wi_new(self.s, "desk", "Bundle", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", self.bundle, "ok")
        self.led.event("wi.dispatched", "desk", self.bundle, {"role": "pm-modules", "brief": "0" * 16, "by": "desk"})
        self.wi = wf.wi_new(self.s, "desk", "Add g()", "g", "py-writer", "modules/demo", ["ok"],
                            parent=self.bundle)["id"]
        wf.wi_approve(self.s, "desk", self.wi, "ok")
        self.brief = wf.dispatch_brief(self.s, self.wi, "pm-modules")["brief"]
        self.wt = self.s.worktrees / self.wi
        self.scratch = self.wt / WORKTREE_SCRATCH

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.wt, check=True, capture_output=True, text=True).stdout

    def test_brief_names_the_scratch_directory(self):
        self.assertIn(f"{self.wt}/{WORKTREE_SCRATCH}/", self.brief)
        self.assertIn("python3 -I <file>", self.brief)
        self.assertNotIn("work root (or $TMPDIR)", self.brief)
        self.assertIn("$TMPDIR", wf.script_file_line({"worktree": None}), "no worktree: scratch space, as before")

    def test_the_bound_writer_may_write_there_whatever_its_scope(self):
        for rel in (f"{WORKTREE_SCRATCH}/probe.py", f"{WORKTREE_SCRATCH}/a/b.json", f"{WORKTREE_SCRATCH}/.dureh-dir"):
            ok, why = write_decision(self.s, "py-writer", self.wt / rel, agent_wi=self.wi)
            self.assertTrue(ok, f"{rel}: {why}")
        ok, why = write_decision(self.s, "py-writer", f"{WORKTREE_SCRATCH}/x.py", cwd=self.wt, agent_wi=self.wi)
        self.assertTrue(ok, why)

    def test_everything_else_keeps_its_rule(self):
        for rel in ("probe.py", WORKTREE_SCRATCH, f"docs/{WORKTREE_SCRATCH}/x.py", ".dureh-scratchy/x.py"):
            ok, why = write_decision(self.s, "py-writer", self.wt / rel, agent_wi=self.wi)
            self.assertFalse(ok, rel)
        ok, why = write_decision(self.s, "py-writer", self.scratch / "x.py", agent_wi="WI-9999")
        self.assertFalse(ok)
        self.assertIn("dispatched for WI-9999", why)
        for role in ("researcher", "pm-modules", "desk"):  # no write scope, a manager, the desk
            ok, why = write_decision(self.s, role, self.scratch / "x.py")
            self.assertFalse(ok, role)

    def test_ignored_not_an_output_and_removed_at_stage(self):
        self.scratch.mkdir()
        (self.scratch / "probe.py").write_text("print(1)\n")
        self.assertEqual(self.git("status", "--porcelain"), "", "git-ignored through the repository's exclude file")
        self.assertNotIn(WORKTREE_SCRATCH, (self.site_root / "modules" / "demo" / ".gitignore").read_text()
                         if (self.site_root / "modules" / "demo" / ".gitignore").exists() else "")
        (self.wt / "src" / "demo.py").write_text("def f():\n    return 2\n")
        self.git("commit", "-qam", "work")
        names, _ = wf.changed_paths(self.s, self.wi)
        self.assertEqual(names, ["src/demo.py"], "verify's ownership check does not see the scratch files")
        self.led.event("wi.dispatched", "desk", self.wi, {"role": "py-writer", "brief": "1" * 16, "by": "desk"})
        wf.wi_stage(self.s, "py-writer", self.wi, "done")
        self.assertFalse(self.scratch.exists(), "removed at stage")

    def test_exclude_is_idempotent_and_reaches_older_worktrees(self):
        ex = self.git("rev-parse", "--git-path", "info/exclude").strip()
        ex = Path(ex) if Path(ex).is_absolute() else self.wt / ex
        ex.write_text("# local\n")  # a worktree made before release 93
        self.led.event("agent.finished", "py-writer", self.wi, {"role": "py-writer"})
        wf.dispatch_brief(self.s, self.wi, "pm-modules")  # reuses the worktree
        wf.ignore_worktree_scratch(self.wt)
        self.assertEqual(ex.read_text(), f"# local\n/{WORKTREE_SCRATCH}/\n")


if __name__ == "__main__":
    unittest.main()
