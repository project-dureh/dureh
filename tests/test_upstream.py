"""`dureh upstream`: draft, list, status transitions, and export bundles."""
import json
import unittest
from pathlib import Path

from helpers import SiteCase

from dureh_core import upstream as up
from dureh_core.records import Ledger
from dureh_core.util import DurehError

FACTS_FOR = {
    "blocking": {"blocks_job": True, "loses_data": False, "workaround": True, "affects_all": False},
    "degrading": {"blocks_job": False, "loses_data": False, "workaround": False, "affects_all": False},
    "minor": {"blocks_job": False, "loses_data": False, "workaround": True, "affects_all": False},
}


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

    def draft(self, type_, severity, actor="desk"):
        """Draft with the four facts that derive `severity` (release 53)."""
        return up.draft(self.s, actor, self.finding, type_, facts=FACTS_FOR[severity])

    def test_draft_writes_file_from_finding(self):
        r = self.draft("bug", "blocking")
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
            self.draft("bug", "blocking", actor="py-writer")

    def test_draft_rejects_bad_type_and_unknown_finding(self):
        with self.assertRaisesRegex(DurehError, "--type"):
            self.draft("nope", "blocking")
        with self.assertRaises(DurehError):
            up.draft(self.s, "desk", "fnd-does-not-exist", "bug", facts=FACTS_FOR["blocking"])

    def test_second_draft_same_day_increments_sequence(self):
        r1 = self.draft("bug", "blocking")
        r2 = self.draft("friction", "minor")
        self.assertNotEqual(r1["ur"], r2["ur"])
        self.assertEqual(r1["ur"][:11], r2["ur"][:11])  # same UR-YYYYMMDD- prefix

    def test_list_shows_id_type_severity_status(self):
        r = self.draft("change", "degrading")
        rows = up.list_requests(self.s)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], r["ur"])
        self.assertEqual(row["type"], "change")
        self.assertEqual(row["severity"], "degrading")
        self.assertEqual(row["status"], "open")

    def test_status_transitions_and_refuses_invalid(self):
        r = self.draft("bug", "blocking")
        ur = r["ur"]
        s1 = up.status(self.s, "desk", ur, "sent")
        self.assertEqual(s1["to"], "sent")
        with self.assertRaisesRegex(DurehError, "invalid transition"):
            up.status(self.s, "desk", ur, "deferred")  # only an accepted request can be deferred
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

    def test_sent_may_land_directly(self):
        ur = self.draft("bug", "blocking")["ur"]
        up.status(self.s, "desk", ur, "sent")
        with self.assertRaisesRegex(DurehError, "--rev"):
            up.status(self.s, "desk", ur, "landed")
        r = up.status(self.s, "desk", ur, "landed", rev="def5678")
        self.assertEqual((r["from"], r["to"]), ("sent", "landed"))
        self.assertIn("landed def5678", up.list_requests(self.s)[0]["status"])

    def test_deferred_goes_back_to_accepted_or_declined(self):
        ur = self.draft("change", "minor")["ur"]
        up.status(self.s, "desk", ur, "sent")
        up.status(self.s, "desk", ur, "accepted")
        up.status(self.s, "desk", ur, "deferred")
        self.assertTrue(up.list_requests(self.s)[0]["status"].startswith("deferred"))
        with self.assertRaisesRegex(DurehError, "invalid transition"):
            up.status(self.s, "desk", ur, "landed", rev="abc1234")
        up.status(self.s, "desk", ur, "accepted")
        up.status(self.s, "desk", ur, "deferred")
        up.status(self.s, "desk", ur, "declined")
        self.assertTrue(up.list_requests(self.s)[0]["status"].startswith("declined"))

    def test_cli_accepts_to_deferred(self):
        import contextlib
        import io

        from dureh_core import cli

        ur = self.draft("change", "minor")["ur"]
        up.status(self.s, "desk", ur, "sent")
        up.status(self.s, "desk", ur, "accepted")
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["--site", str(self.site_root), "upstream", "status", "--ur", ur,
                             "--to", "deferred"])
        self.assertEqual(code, 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--site", str(self.site_root), "upstream", "list"])
        self.assertIn(f"{ur}\tchange\tminor\tdeferred", out.getvalue())

    def test_status_is_desk_only(self):
        r = self.draft("bug", "blocking")
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
        r = self.draft("bug", "blocking")
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
        r = self.draft("bug", "blocking")
        with self.assertRaisesRegex(DurehError, "only the desk"):
            up.export(self.s, "py-writer", r["ur"])

    def test_export_refuses_to_overwrite_existing_bundle(self):
        r = self.draft("bug", "blocking")
        up.export(self.s, "desk", r["ur"])
        with self.assertRaises(DurehError):
            up.export(self.s, "desk", r["ur"])


    def test_status_names_landed_requests_still_open(self):
        """Release 85 (dureh-team#91): a request a `landed` message names but the site still lists as open shows a
        close hint in `status` with the message's harness revision; open -> landed is allowed."""
        import json as _json
        from dureh_core import ops
        from dureh_core.site import Site

        ur = self.draft("change", "minor")["ur"]
        ex = self.tmp / "exchange"
        cfg = _json.loads(self.s.config_path.read_text())
        cfg["upstream"] = {"exchange": str(ex)}
        self.s.config_path.write_text(_json.dumps(cfg))
        s = Site(self.site_root)
        box = ex / s.mailbox_name / "from-upstream"
        box.mkdir(parents=True)
        (box / "20261029T125624Z-landed-8e1bf1.md").write_text(
            f"---\nid: 8e1bf1\nkind: landed\nrefs: [{ur}]\n---\n{ur} landed: release 84 (harness 7dcb8dd, r1).\n")
        c = up.collaboration(s)
        self.assertEqual(c["landed_open"], [{"ur": ur, "rev": "7dcb8dd", "message": "8e1bf1"}])
        text = ops.render_status(ops.status(s))
        self.assertIn(f"`dureh upstream status --ur {ur} --to landed --rev 7dcb8dd`", text)
        up.status(s, "desk", ur, "landed", rev="7dcb8dd")  # open -> landed
        self.assertEqual(up.collaboration(s)["landed_open"], [])


if __name__ == "__main__":
    unittest.main()
