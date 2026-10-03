"""`dureh wi amend` (UNC_Lee_CAMRI-ORG finding fnd-20261003T054002713911Z-18b8280b): change checks, acceptance,
constraints or outputs, or move the base, without a replacement WI and a new Gate 1."""
import subprocess
import unittest

from helpers import SiteCase, hook

from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError


class WiAmend(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.mod = self.make_module()
        self.s = self.site()
        self.bundle = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        self.item = wf.wi_new(self.s, "desk", "item", "g", "py-writer", "modules/demo", ["old criterion"],
                              checks=["false"], parent=self.bundle)["id"]
        for wi in (self.bundle, self.item):
            wf.wi_approve(self.s, "desk", wi, "ok")
        brief = wf.dispatch_brief(self.s, self.bundle, "desk")["brief"]
        hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
             tool_input={"subagent_type": "pm-modules", "prompt": brief, "description": "x"})
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")

    def payload(self, wi):
        return Ledger(self.s).get("work-item", wi)["payload"]

    def test_amend_checks_and_acceptance_apply_on_read(self):
        ev = wf.wi_amend(self.s, "desk", self.item, "plan approval covers it",
                         {"checks": ["true"], "acceptance": ["new criterion"]})
        p = self.payload(self.item)
        self.assertEqual(p["checks"], ["true"])
        self.assertEqual(p["acceptance"], ["new criterion"])
        self.assertEqual(ev["payload"]["before"]["checks"], ["false"])
        brief = wf.dispatch_brief(self.s, self.item, "pm-modules")["brief"]
        self.assertIn("- new criterion", brief)
        self.assertNotIn("old criterion", brief)

    def test_goal_role_target_are_not_amendable(self):
        for f in ({"goal": "other"}, {"role": "doc-writer"}, {"target": "modules/x"}):
            with self.assertRaises(DurehError):
                wf.wi_amend(self.s, "desk", self.item, "x", f)

    def test_needs_desk_and_memo_and_something_to_change(self):
        with self.assertRaises(DurehError):
            wf.wi_amend(self.s, "pm-modules", self.item, "x", {"checks": ["true"]})
        with self.assertRaises(DurehError):
            wf.wi_amend(self.s, "desk", self.item, " ", {"checks": ["true"]})
        with self.assertRaises(DurehError):
            wf.wi_amend(self.s, "desk", self.item, "x", {})

    def test_refused_while_its_agent_runs(self):
        with self.assertRaises(DurehError) as cm:
            wf.wi_amend(self.s, "desk", self.bundle, "x", {"acceptance": ["y"]})
        self.assertIn("running", str(cm.exception))

    def test_rebase_moves_the_base_and_tells_the_worker(self):
        first = wf.dispatch_brief(self.s, self.item, "pm-modules")
        old_base = Ledger(self.s).events(self.item)[-1]["payload"]["base"]
        wf.dispatch_stop(self.s, "desk", self.item, "base is stale")
        (self.mod / "src" / "other.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.mod, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "main moved"], cwd=self.mod, check=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.mod, capture_output=True, text=True).stdout.strip()
        wf.wi_amend(self.s, "desk", self.item, "plan approval covers it", {}, rebase="")
        self.assertEqual(Ledger(self.s).wi_status(self.item)["status"], "approved")  # re-opened
        again = wf.dispatch_brief(self.s, self.item, "pm-modules")["brief"]
        self.assertIn(f"git rebase {head[:12]}", again)
        self.assertNotEqual(old_base, head)
        self.assertEqual(Ledger(self.s).events(self.item)[-1]["payload"]["base"], head)
        self.assertNotIn("git rebase", first["brief"])


if __name__ == "__main__":
    unittest.main()
