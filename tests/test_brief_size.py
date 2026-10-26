"""UR-20260930-02: `wi new` rejects items whose brief could never be dispatched."""
import unittest

from helpers import SiteCase, commit_work

from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError


class BriefSizeAtCreation(SiteCase):
    def setUp(self):
        super().setUp()
        self.install(limits={"manager_brief_bytes": 4096})
        self.make_module()
        self.s = self.site()
        self.bundle = wf.wi_new(self.s, "desk", "Bundle", "Improve demo", "pm-modules", "modules/demo", ["ok"])["id"]

    def count(self):
        return len(Ledger(self.s).work_items())

    def test_oversized_worker_brief_is_rejected_and_not_recorded(self):
        before = self.count()
        with self.assertRaisesRegex(DurehError, r"brief would be \d+ bytes, over the 2048-byte limit"):
            wf.wi_new(self.s, "desk", "Big", "x" * 2100, "py-writer", "modules/demo", ["done"], parent=self.bundle)
        self.assertEqual(self.count(), before)

    def test_estimate_matches_the_dispatched_brief(self):
        wi = wf.wi_new(self.s, "desk", "Notes", "write notes", "doc-writer", "modules/demo", ["notes exist"])["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        wf.dispatch_brief(self.s, wi, "desk")
        led = Ledger(self.s)
        sent = [e for e in led.events(wi) if e["type"] == "dispatch.prepared"][-1]["payload"]["bytes"]
        self.assertEqual(wf.brief_bytes(self.s, led.get("work-item", wi)), sent)

    def test_checker_estimate_matches_the_dispatched_checker_brief(self):
        """Release 70 (UR-20261016-01): the checker brief measured at `wi new` is the one `dispatch brief --role
        checker` prepares once the item is staged (local checks runner: one variant of the run instructions)."""
        led = Ledger(self.s)
        wf.wi_approve(self.s, "desk", self.bundle, "ok")
        led.event("wi.dispatched", "desk", self.bundle, {"role": "pm-modules", "brief": "0" * 16, "by": "desk"})
        wi = wf.wi_new(self.s, "desk", "Add g()", "g", "py-writer", "modules/demo", ["ok"], parent=self.bundle,
                       checks=["true"])["id"]
        estimate = wf.checker_brief_bytes(self.s, led.get("work-item", wi), "checker")
        wf.wi_approve(self.s, "desk", wi, "ok")
        wf.dispatch_brief(self.s, wi, "pm-modules")
        led.event("wi.dispatched", "desk", wi, {"role": "py-writer", "brief": "1" * 16, "by": "desk"})
        commit_work(self.s, wi)
        wf.wi_stage(self.s, "py-writer", wi, "added g() " * 50)
        r = wf.dispatch_brief(self.s, wi, "pm-modules", as_role="checker")
        prep = [e for e in led.events(wi) if e["type"] == "dispatch.prepared"][-1]["payload"]
        self.assertEqual(prep["bytes"], estimate)
        self.assertIn("added g() added g()", r["brief"])
        self.assertGreater(prep["staging_bytes"], 500)
        self.assertEqual([c.name for c in wf.checker_roles(self.s)], ["checker"], "the desk is not a checker brief")

    def test_child_that_overflows_the_parent_bundle_brief_is_rejected(self):
        title = "t" * 300
        with self.assertRaisesRegex(DurehError, rf"bundle {self.bundle} brief would be \d+ bytes, over the 4096-byte"):
            for _ in range(20):
                wf.wi_new(self.s, "desk", title, "g", "py-writer", "modules/demo", ["ok"], parent=self.bundle)

    def test_role_the_desk_cannot_dispatch_needs_a_bundle(self):
        with self.assertRaisesRegex(DurehError, r"desk cannot dispatch py-writer; create it with --parent .*pm-modules"):
            wf.wi_new(self.s, "desk", "Loose", "g", "py-writer", "modules/demo", ["ok"])

    def test_parent_must_be_able_to_dispatch_the_role(self):
        doc = wf.wi_new(self.s, "desk", "Doc", "x", "doc-writer", "modules/demo", ["ok"])["id"]
        with self.assertRaisesRegex(DurehError, r"parent .* is owned by doc-writer, which cannot dispatch py-writer"):
            wf.wi_new(self.s, "desk", "Wrong", "g", "py-writer", "modules/demo", ["ok"], parent=doc)


if __name__ == "__main__":
    unittest.main()
