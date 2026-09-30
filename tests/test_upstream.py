"""`dureh upstream`: draft, list, status transitions, and export bundles."""
import json
import unittest
from pathlib import Path

from helpers import SiteCase

from dureh_core import upstream as up
from dureh_core.records import Ledger
from dureh_core.util import DurehError


class Upstream(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        self.led = Ledger(self.s)
        self.finding = self.led.append(
            "finding", "finding", "desk", None,
            {"summary": "the harness cannot do X", "evidence_level": "measured", "tags": ["upstream"]},
        )["id"]

    def test_draft_writes_file_from_finding(self):
        r = up.draft(self.s, "desk", self.finding, "bug", "blocking")
        path = Path(r["path"])
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("- Type: bug", text)
        self.assertIn("- Severity: blocking", text)
        self.assertIn("- Status: open", text)
        self.assertIn(self.finding, text)
        self.assertIn("the harness cannot do X", text)
        self.assertTrue(r["ur"].startswith("UR-"))

    def test_draft_is_desk_only(self):
        with self.assertRaisesRegex(DurehError, "only the desk"):
            up.draft(self.s, "py-writer", self.finding, "bug", "blocking")

    def test_draft_rejects_bad_type_and_unknown_finding(self):
        with self.assertRaisesRegex(DurehError, "--type"):
            up.draft(self.s, "desk", self.finding, "nope", "blocking")
        with self.assertRaises(DurehError):
            up.draft(self.s, "desk", "fnd-does-not-exist", "bug", "blocking")

    def test_second_draft_same_day_increments_sequence(self):
        r1 = up.draft(self.s, "desk", self.finding, "bug", "blocking")
        r2 = up.draft(self.s, "desk", self.finding, "friction", "minor")
        self.assertNotEqual(r1["ur"], r2["ur"])
        self.assertEqual(r1["ur"][:11], r2["ur"][:11])  # same UR-YYYYMMDD- prefix

    def test_list_shows_id_type_severity_status(self):
        r = up.draft(self.s, "desk", self.finding, "change", "degrading")
        rows = up.list_requests(self.s)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], r["ur"])
        self.assertEqual(row["type"], "change")
        self.assertEqual(row["severity"], "degrading")
        self.assertEqual(row["status"], "open")

    def test_status_transitions_and_refuses_invalid(self):
        r = up.draft(self.s, "desk", self.finding, "bug", "blocking")
        ur = r["ur"]
        s1 = up.status(self.s, "desk", ur, "sent")
        self.assertEqual(s1["to"], "sent")
        with self.assertRaisesRegex(DurehError, "invalid transition"):
            up.status(self.s, "desk", ur, "landed")
        up.status(self.s, "desk", ur, "accepted")
        with self.assertRaisesRegex(DurehError, "--rev"):
            up.status(self.s, "desk", ur, "landed")
        up.status(self.s, "desk", ur, "landed", rev="abc1234")
        rows = up.list_requests(self.s)
        self.assertIn("landed", rows[0]["status"])
        self.assertIn("abc1234", rows[0]["status"])
        # a landed request is terminal
        with self.assertRaisesRegex(DurehError, "invalid transition"):
            up.status(self.s, "desk", ur, "sent")
        events = [e for e in self.led.events() if e["type"] == "upstream.status"]
        self.assertEqual(len(events), 3)

    def test_status_is_desk_only(self):
        r = up.draft(self.s, "desk", self.finding, "bug", "blocking")
        with self.assertRaisesRegex(DurehError, "only the desk"):
            up.status(self.s, "py-writer", r["ur"], "sent")

    def test_status_unknown_ur(self):
        with self.assertRaises(DurehError):
            up.status(self.s, "desk", "UR-20200101-01", "sent")

    def test_export_bundles_finding_and_wi_no_network(self):
        wi = self.led.new_work_item("desk", {"title": "t", "goal": "g", "role": "py-writer", "target": "harness",
                                              "acceptance": ["a"], "lane": "work", "bundle": None, "value": None,
                                              "checks": [], "constraints": [], "parent": None, "source": None,
                                              "scope_frozen": False, "academic": False})["id"]
        finding2 = self.led.append("finding", "finding", "desk", wi,
                                    {"summary": "s2", "evidence_level": "reviewed"})["id"]
        r = up.draft(self.s, "desk", self.finding, "bug", "blocking")
        ur_path = Path(r["path"])
        ur_path.write_text(ur_path.read_text(encoding="utf-8") + f"\nAlso see {finding2} and {wi}.\n",
                            encoding="utf-8")
        out = up.export(self.s, "desk", r["ur"])
        bundle = Path(out["bundle"])
        self.assertTrue((bundle / f"{r['ur']}.md").is_file())
        self.assertTrue((bundle / "findings" / f"{self.finding}.json").is_file())
        self.assertTrue((bundle / "findings" / f"{finding2}.json").is_file())
        self.assertTrue((bundle / "work-items" / f"{wi}.json").is_file())
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn(finding2, manifest["findings"])
        self.assertIn(wi, manifest["work_items"])
        # no project data or secrets: only the UR file, finding/WI metadata, and the manifest
        all_files = sorted(p.name for p in bundle.rglob("*") if p.is_file())
        self.assertEqual(all_files, sorted([f"{r['ur']}.md", "manifest.json", f"{self.finding}.json",
                                            f"{finding2}.json", f"{wi}.json"]))

    def test_export_is_desk_only(self):
        r = up.draft(self.s, "desk", self.finding, "bug", "blocking")
        with self.assertRaisesRegex(DurehError, "only the desk"):
            up.export(self.s, "py-writer", r["ur"])

    def test_export_refuses_to_overwrite_existing_bundle(self):
        r = up.draft(self.s, "desk", self.finding, "bug", "blocking")
        up.export(self.s, "desk", r["ur"])
        with self.assertRaises(DurehError):
            up.export(self.s, "desk", r["ur"])


if __name__ == "__main__":
    unittest.main()
