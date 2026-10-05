"""UR-20261001-02: git commands that write are held to the agent's own WI worktree."""
import json
import os
import subprocess
import unittest
from pathlib import Path

from helpers import SiteCase, denied, hook

from dureh_core import workflow as wf
from dureh_core.records import Ledger


class GitGuard(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.mod = self.make_module()
        self.s = self.site()
        self.session = self.tmp / "sess.jsonl"
        self.session.write_text("")
        bundle = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        self.a, self.b = [wf.wi_new(self.s, "desk", f"item {n}", "g", "py-writer", "modules/demo", ["ok"],
                                    parent=bundle)["id"] for n in (1, 2)]
        for wi in (bundle, self.a, self.b):
            wf.wi_approve(self.s, "desk", wi, "ok")
        self.spawn("desk", "pm-modules", wf.dispatch_brief(self.s, bundle, "desk")["brief"])
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        briefs = {}
        for wi in (self.a, self.b):
            briefs[wi] = wf.dispatch_brief(self.s, wi, "pm-modules")
            self.spawn("pm-modules", "py-writer", briefs[wi]["brief"], "a-pm")
        t = self.session.with_suffix("") / "subagents" / "agent-a-1.jsonl"
        t.parent.mkdir(parents=True)
        t.write_text(json.dumps({"message": {"content": briefs[self.a]["brief"]}}) + "\n")
        hook(self.s, hook_event_name="SubagentStart", agent_type="py-writer", agent_id="a-1",
             transcript_path=str(self.session))
        self.wa, self.wb = Path(briefs[self.a]["worktree"]), Path(briefs[self.b]["worktree"])

    def spawn(self, caller, target, brief, agent_id=None):
        data = dict(hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
                    tool_input={"subagent_type": target, "prompt": brief, "description": "x"})
        if caller != "desk":
            data.update(agent_type=caller, agent_id=agent_id)
        self.assertIsNone(hook(self.s, **data))

    def bash(self, command, agent_id="a-1", cwd=None):
        return hook(self.s, hook_event_name="PreToolUse", tool_name="Bash", agent_type="py-writer", agent_id=agent_id,
                    transcript_path=str(self.session), cwd=str(cwd or self.site_root), tool_input={"command": command})

    def test_writes_in_the_own_worktree_are_allowed(self):
        for c in (f"git -C {self.wa} add src/demo.py", f"git -C {self.wa} commit -m 'add g -> h'",
                  f"cd {self.wa} && git cherry-pick abc123", f"git -C {self.wa}/src restore demo.py"):
            self.assertFalse(denied(self.bash(c)), c)

    def test_writes_in_another_worktree_are_denied_and_recorded(self):
        for c in (f"git -C {self.wb} cherry-pick 9dc16ec", f"cd {self.wb} && git commit -qm x",
                  f"git -C {self.wb} add .", f"git -C {self.wb} reset --hard", f"git -C {self.wb} stash",
                  f"git -C {self.wb} checkout -- src/demo.py", f"git -C {self.wb} branch -D wi/{self.a}",
                  f"git -C {self.wb} frobnicate"):
            self.assertTrue(denied(self.bash(c)), c)
        ev = [e for e in Ledger(self.s).load("event") if e["type"] == "guard.denied"][-1]["payload"]
        self.assertEqual(ev["bound_wi"], self.a)
        self.assertIn(f"dispatched for {self.a}", ev["reason"])

    def test_reads_anywhere_are_allowed(self):
        for c in (f"git -C {self.wb} status", f"git -C {self.wb} log --oneline -3", f"git -C {self.wb} diff HEAD~1",
                  f"git -C {self.wb} branch", f"git -C {self.wb} branch --show-current", f"git -C {self.wb} stash list",
                  f"git -C {self.wb} config --get user.name", f"git -C {self.wb} rev-parse HEAD", "git --no-pager log -1"):
            self.assertFalse(denied(self.bash(c)), c)

    def test_main_trees_and_redirected_git_dirs_are_denied(self):
        for c in (f"git -C {self.mod} commit -qm x", f"git -C {self.site_root} add -A",
                  f"git --git-dir={self.wb}/.git --work-tree={self.wb} commit -m x",
                  f"GIT_DIR={self.wb}/.git git commit -m x", f"git -C {self.wa} --work-tree={self.wb} add ."):
            self.assertTrue(denied(self.bash(c)), c)

    def test_scratch_repositories_are_allowed(self):
        scratch = Path(os.environ["TMPDIR"]) / "play"
        self.assertFalse(denied(self.bash(f"git init -q {scratch}")))
        self.assertFalse(denied(self.bash(f"git -C {scratch} commit --allow-empty -m x")))

    def test_unbound_agent_cannot_write_with_git(self):
        self.assertTrue(denied(self.bash(f"git -C {self.wa} commit -m x", agent_id="a-9")))


if __name__ == "__main__":
    unittest.main()
