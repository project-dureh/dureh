"""DataLad commands that write are held to the agent's own WI worktree; compute, deletion and push are refused."""
import json
import os
import subprocess
import unittest
from pathlib import Path

from helpers import SiteCase, denied, hook

from dureh_core import workflow as wf
from dureh_core.records import Ledger


class DataladGuard(SiteCase):
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

    def test_reads_anywhere(self):
        for c in ("datalad status", f"datalad -C {self.wb} diff", f"datalad subdatasets -d {self.wb}", "datalad wtf",
                  f"datalad siblings -d {self.wb}", "datalad --version"):
            self.assertIsNone(self.bash(c), c)

    def test_writes_in_the_own_worktree_or_scratch(self):
        scratch = os.environ["TMPDIR"]
        for c in (f"datalad save -d {self.wa} -m x", f"cd {self.wa} && datalad save -m x", f"datalad -C {self.wa} save",
                  f"datalad create -d {self.wa} {self.wa}/derivatives/x", f"datalad save --dataset={self.wa} -m x",
                  f"datalad create {scratch}/ds", f"datalad get -d {self.wa} src"):
            self.assertIsNone(self.bash(c), c)

    def test_writes_elsewhere_are_denied(self):
        for c in (f"datalad save -d {self.wb} -m x", f"cd {self.wb} && datalad save", f"datalad -C {self.wb} save",
                  f"datalad save --dataset={self.wb}", f"datalad save -d {self.mod}", f"datalad save -d {self.site_root}",
                  f"datalad create {self.wb}/derivatives/y", f"datalad -C {self.wb} frobnicate"):
            self.assertTrue(denied(self.bash(c)), c)

    def test_compute_deletion_and_push_are_refused_even_in_the_own_worktree(self):
        for c, why in ((f"cd {self.wa} && datalad run -m x make", "scheduler"), (f"datalad -C {self.wa} rerun", "scheduler"),
                       (f"datalad -C {self.wa} containers-run -n c x", "scheduler"), (f"datalad drop -d {self.wa} src", "purge"),
                       (f"datalad remove -d {self.wa} src", "purge"), (f"datalad -C {self.wa} push --to origin", "dureh push")):
            out = self.bash(c)
            self.assertTrue(denied(out), c)
            self.assertIn(why, out["hookSpecificOutput"]["permissionDecisionReason"], c)

    def test_unbound_agent_cannot_write(self):
        self.assertTrue(denied(self.bash(f"datalad save -d {self.wa}", agent_id="a-9")))

    def test_desk_is_not_restricted(self):
        out = hook(self.s, hook_event_name="PreToolUse", tool_name="Bash", cwd=str(self.site_root),
                   tool_input={"command": f"datalad status -d {self.wb}"})
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
