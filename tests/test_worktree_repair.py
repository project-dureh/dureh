"""UR-20261001-05: broken WI worktrees are refused at dispatch and repaired by a desk command."""
import shutil
import subprocess
import unittest
from pathlib import Path

from helpers import SiteCase, denied, hook

from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.site import Site
from dureh_core.util import DurehError


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


class WorktreeRepair(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.bundle = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        self.wi = wf.wi_new(self.s, "desk", "item", "g", "py-writer", "modules/demo", ["ok"], parent=self.bundle)["id"]
        for w in (self.bundle, self.wi):
            wf.wi_approve(self.s, "desk", w, "ok")
        self.spawn("desk", "pm-modules", wf.dispatch_brief(self.s, self.bundle, "desk")["brief"])
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        wf.dispatch_brief(self.s, self.wi, "pm-modules")  # creates the worktree
        wf.dispatch_stop(self.s, "desk", self.wi, "before the move")
        wf.wi_approve(self.s, "desk", self.wi, "again")

    def spawn(self, caller, target, brief, agent_id=None, site=None):
        data = dict(hook_event_name="PreToolUse", tool_name="Agent", cwd=str((site or self.s).root),
                    tool_input={"subagent_type": target, "prompt": brief, "description": "x"})
        if caller != "desk":
            data.update(agent_type=caller, agent_id=agent_id)
        return hook(site or self.s, **data)

    def move_site(self):
        new = self.tmp / "moved" / "site"
        new.parent.mkdir()
        shutil.move(str(self.site_root), str(new))
        self.site_root = new
        self.s = Site(new)
        return self.s.worktrees / self.wi

    def test_dispatch_refuses_a_broken_worktree(self):
        wt = self.move_site()
        self.assertNotEqual(git(wt, "rev-parse", "--git-dir", check=False).returncode, 0)
        with self.assertRaisesRegex(DurehError, rf"worktree {wt} is broken.*dureh worktree repair --wi {self.wi}"):
            wf.dispatch_brief(self.s, self.wi, "pm-modules")

    def test_repair_relinks_and_keeps_branch_and_commits(self):
        wt = self.s.worktrees / self.wi
        (wt / "src" / "demo.py").write_text("def f():\n    return 2\n")
        git(wt, "commit", "-qam", "work in progress")
        head = git(wt, "rev-parse", "HEAD").stdout.strip()
        wt = self.move_site()
        r = wf.worktree_repair(self.s, "desk", self.wi)
        self.assertEqual(r["state"], "repaired")
        self.assertEqual(git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), f"wi/{self.wi}")
        self.assertEqual(git(wt, "rev-parse", "HEAD").stdout.strip(), head)
        self.assertNotIn("prunable", git(self.site_root / "modules" / "demo", "worktree", "list").stdout)
        self.assertTrue(any(e["type"] == "worktree.repaired" for e in Ledger(self.s).events(self.wi)))
        self.assertIn("brief", wf.dispatch_brief(self.s, self.wi, "pm-modules"))

    def test_missing_worktree_dir_is_pruned_and_recreated_from_the_branch(self):
        wt = self.s.worktrees / self.wi
        shutil.rmtree(wt)
        r = wf.worktree_repair(self.s, "desk", self.wi)
        self.assertEqual(r["state"], "pruned")
        self.assertNotIn(str(wt), git(self.site_root / "modules" / "demo", "worktree", "list").stdout)
        b = wf.dispatch_brief(self.s, self.wi, "pm-modules")
        self.assertTrue(Path(b["worktree"]).is_dir())

    def test_spawn_refuses_when_the_prepared_worktree_broke(self):
        brief = wf.dispatch_brief(self.s, self.wi, "pm-modules")["brief"]
        (self.s.worktrees / self.wi / ".git").write_text("gitdir: /nonexistent/old/path\n")
        out = self.spawn("pm-modules", "py-writer", brief, "a-pm")
        self.assertTrue(denied(out))
        self.assertIn("broken", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_only_the_desk_repairs(self):
        with self.assertRaisesRegex(DurehError, "only the desk"):
            wf.worktree_repair(self.s, "py-writer", self.wi)


if __name__ == "__main__":
    unittest.main()
