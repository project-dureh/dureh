"""Release 81 (UR-20261023-02, option 1): an item owned by a role with the verify capability and without a worktree
(a read-only sweep with no worker) is staged straight from approved or rework, so the checker can be dispatched
for it without a no-op member run to reach dispatched. Other items still need a dispatch first."""
from helpers import SiteCase

from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError


class CheckerItemStage(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)

    def sweep(self, checks=("true",)):
        wi = wf.wi_new(self.s, "desk", "Readiness sweep", "g", "checker", "modules/demo", ["ok"],
                       checks=list(checks))["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        return wi

    def test_approved_sweep_is_staged_and_verified_without_a_dispatch(self):
        wi = self.sweep()
        ev = wf.wi_stage(self.s, "desk", wi, "sweep ready")
        self.assertEqual((self.led.wi_status(wi)["status"], ev["payload"]["head"]), ("staged", None))
        self.assertFalse([e for e in self.led.events(wi) if e["type"] == "wi.dispatched"], "no member run needed")
        b = wf.dispatch_brief(self.s, wi, "desk", as_role="checker")
        self.assertIn("sweep ready", b["brief"])
        self.assertEqual(wf.verify_wi(self.s, "checker", wi)["result"], "pass")
        self.assertEqual(self.led.wi_status(wi)["status"], "verified")

    def test_a_failed_sweep_goes_from_rework_back_to_staged(self):
        wi = self.sweep(checks=("false",))
        wf.wi_stage(self.s, "desk", wi, "first sweep")
        self.assertEqual(wf.verify_wi(self.s, "checker", wi)["result"], "fail")
        self.assertEqual(self.led.wi_status(wi)["status"], "rework")
        wf.wi_amend(self.s, "desk", wi, "check amended", {"checks": ["true"]})
        wf.wi_stage(self.s, "checker", wi, "amended check")  # the owner role may stage it too
        self.assertEqual(self.led.wi_status(wi)["status"], "staged")
        with self.assertRaisesRegex(DurehError, "already staged at no worktree"):
            wf.wi_stage(self.s, "desk", wi, "again")
        self.assertEqual(wf.verify_wi(self.s, "checker", wi)["result"], "pass")

    def test_other_items_still_need_a_dispatch(self):
        wi = wf.wi_new(self.s, "desk", "doc", "g", "doc-writer", "modules/demo", ["ok"], checks=["true"])["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        with self.assertRaisesRegex(DurehError, "only an item owned by a role with the verify capability"):
            wf.wi_stage(self.s, "desk", wi, "early")
        with self.assertRaisesRegex(DurehError, r"approved -> staged is only for an item owned by a role"):
            self.led.event("wi.staged", "desk", wi, {"summary": "raw"})  # the ledger refuses it as well
        self.assertEqual(self.led.wi_status(wi)["status"], "approved")
        proposed = wf.wi_new(self.s, "desk", "Sweep 2", "g", "checker", "modules/demo", ["ok"], checks=["true"])["id"]
        with self.assertRaisesRegex(DurehError, "invalid transition .* proposed -> staged"):
            wf.wi_stage(self.s, "desk", proposed, "not approved")

    def test_a_sweep_with_a_worktree_is_not_a_direct_stage(self):
        wi = self.sweep()
        (self.s.worktrees / wi).mkdir(parents=True)
        self.assertFalse(self.led.checker_item(wi))
        with self.assertRaisesRegex(DurehError, r"approved -> staged is only for"):
            self.led.event("wi.staged", "desk", wi, {"summary": "raw"})
