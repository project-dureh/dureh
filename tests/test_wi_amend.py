"""`dureh wi amend` (a company site's finding fnd-20261003T054002713911Z-18b8280b): change checks, acceptance,
constraints or outputs, or move the base, without a replacement WI and a new Gate 1."""
import os
import subprocess
import unittest

from helpers import SiteCase, hook

from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError


class AmendCase(SiteCase):
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


class WiAmend(AmendCase):
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


class Reopen(AmendCase):
    """Release 45 (6f): a verified, unpromoted item is re-opened with `wi amend --reopen`, without `dispatch stop`."""

    def verified_item(self, checks=("true",)):
        wi = wf.wi_new(self.s, "desk", "doc", "g", "doc-writer", "modules/demo", ["ok"], checks=list(checks))["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        wf.dispatch_brief(self.s, wi, "desk")
        led = Ledger(self.s)
        led.event("wi.dispatched", "desk", wi, {"role": "doc-writer"})
        wf.wi_stage(self.s, "desk", wi, "done")
        self.assertEqual(wf.verify_wi(self.s, "checker", wi)["result"], "pass")
        self.assertEqual(led.wi_status(wi)["status"], "verified")
        return wi

    def test_reopen_voids_the_verification_and_goes_back_to_approved(self):
        wi = self.verified_item()
        ev = wf.wi_amend(self.s, "desk", wi, "Director: also cover the edge case", {}, reopen=True,
                         add_constraints=["cover the edge case"])
        led = Ledger(self.s)
        self.assertEqual(ev["payload"]["reopened_from"], "verified")
        self.assertEqual(led.wi_status(wi)["status"], "approved")
        last = led.events(wi)[-1]
        self.assertEqual(last["type"], "wi.reopened")
        self.assertEqual(last["refs"], [ev["id"]])
        self.assertTrue(last["payload"]["voids"].startswith("run-"))
        self.assertFalse([e for e in led.events(wi) if e["type"] == "wi.stopped"], "no dispatch stop")
        self.assertIn("- cover the edge case", wf.dispatch_brief(self.s, wi, "desk")["brief"])
        with self.assertRaises(DurehError):
            wf.wi_promote(self.s, "desk", wi, "ok")  # the verification no longer stands

    def test_verified_without_reopen_is_refused_with_the_way(self):
        wi = self.verified_item()
        with self.assertRaises(DurehError) as cm:
            wf.wi_amend(self.s, "desk", wi, "x", {"checks": ["true"]})
        self.assertIn("--reopen", str(cm.exception))
        self.assertEqual(Ledger(self.s).wi_status(wi)["status"], "verified")

    def test_reopen_only_for_verified_and_never_promoted(self):
        with self.assertRaises(DurehError):
            wf.wi_amend(self.s, "desk", self.item, "x", {}, reopen=True)  # approved
        wi = self.verified_item()
        wf.wi_promote(self.s, "desk", wi, "ok")
        with self.assertRaises(DurehError):
            wf.wi_amend(self.s, "desk", wi, "x", {}, reopen=True)
        self.assertEqual(Ledger(self.s).wi_status(wi)["status"], "promoted")

    def test_approve_cannot_reopen_a_verified_item(self):
        wi = self.verified_item()
        with self.assertRaises(DurehError) as cm:
            wf.wi_approve(self.s, "desk", wi, "ok")
        self.assertIn("verified -> approved", str(cm.exception))

    def test_desk_text_names_it(self):
        from dureh_core.runtimes import claude_code

        claude_code.generate(self.s)
        self.assertIn("wi amend --wi <WI> --reopen --memo", (self.site_root / "CLAUDE.md").read_text())

    def test_cli(self):
        from dureh_core.cli import main

        wi = self.verified_item()
        os.environ["DUREH_SITE"] = str(self.site_root)
        self.assertEqual(main(["wi", "amend", "--wi", wi, "--memo", "Director's words", "--reopen"]), 0)
        self.assertEqual(Ledger(self.s).wi_status(wi)["status"], "approved")


if __name__ == "__main__":
    unittest.main()
