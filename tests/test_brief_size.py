"""UR-20260930-02: `wi new` rejects items whose brief could never be dispatched."""
import unittest

from helpers import SiteCase

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
