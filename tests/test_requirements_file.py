"""Release 45 (6g): `wi new|amend --requirements <file>` pins a requirements file by sha256. The brief names it in
one line (only that line counts toward the 2048-byte limit); dispatch and verify refuse, recording nothing, when the
file changed or vanished since it was pinned, until it is restored or re-pinned with `wi amend --requirements`."""
import os

from helpers import SiteCase, commit_work

from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.records import Ledger
from dureh_core.util import DurehError, sha256_file


class RequirementsFile(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)
        self.req = self.site_root / "notes" / "req-design.md"
        self.req.parent.mkdir(exist_ok=True)
        self.req.write_text("R1 ...\n" * 600)  # about 4 KB: over the brief limit as text

    def new(self, **kw):
        wi = wf.wi_new(self.s, "desk", "Design doc", "write it", "doc-writer", "modules/demo", ["R1-R23 met"],
                       requirements="notes/req-design.md", **kw)["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        return wi

    def test_pinned_and_named_in_the_brief(self):
        wi = self.new()
        p = self.led.get("work-item", wi)["payload"]["requirements"]
        self.assertEqual(p, {"path": "notes/req-design.md", "sha256": sha256_file(self.req),
                             "bytes": self.req.stat().st_size})
        brief = wf.dispatch_brief(self.s, wi, "desk")["brief"]
        self.assertIn(f"Requirements (read first; part of the acceptance): {self.req} (sha256 {p['sha256'][:12]}", brief)
        self.assertLess(len(brief.encode()), 2048, "the file's text is not in the brief")

    def test_changed_file_refuses_dispatch_and_verify_until_re_pinned(self):
        wi = self.new()
        wf.dispatch_brief(self.s, wi, "desk")
        self.led.event("wi.dispatched", "desk", wi, {"role": "doc-writer"})
        commit_work(self.s, wi)
        wf.wi_stage(self.s, "desk", wi, "done")
        self.req.write_text("R1 changed\n")
        n = len(self.led.load("run"))
        with self.assertRaises(DurehError) as cm:
            wf.verify_wi(self.s, "checker", wi)
        self.assertIn("changed", str(cm.exception))
        self.assertIn(f"wi amend --wi {wi}", str(cm.exception))
        self.assertEqual(len(self.led.load("run")), n, "nothing recorded")
        with self.assertRaises(DurehError):
            wf.dispatch_brief(self.s, wi, "desk", as_role="checker")
        wf.wi_amend(self.s, "desk", wi, "Director: the new R1 stands", {"requirements": "notes/req-design.md"})
        self.assertEqual(wf.verify_wi(self.s, "checker", wi)["result"], "pass")

    def test_missing_file_refuses(self):
        wi = self.new()
        self.req.unlink()
        with self.assertRaises(DurehError) as cm:
            wf.dispatch_brief(self.s, wi, "desk")
        self.assertIn("is missing", str(cm.exception))

    def test_refusals_at_creation(self):
        outside = self.tmp / "outside.md"
        outside.write_text("x\n")
        for raw in (str(outside), "notes/none.md", "../outside.md"):
            with self.assertRaises(DurehError, msg=raw):
                wf.wi_new(self.s, "desk", "t", "g", "doc-writer", "modules/demo", ["ok"], requirements=raw)
        self.req.write_text("x" * 70000)
        with self.assertRaisesRegex(DurehError, "requirements_bytes"):
            self.new()

    def test_cli(self):
        os.environ["DUREH_SITE"] = str(self.site_root)
        self.assertEqual(main(["wi", "new", "--title", "t", "--goal", "g", "--role", "doc-writer", "--target",
                               "modules/demo", "--acceptance", "ok", "--requirements", str(self.req)]), 0)
        wi = self.led.work_items()[-1]["id"]
        self.assertEqual(self.led.get("work-item", wi)["payload"]["requirements"]["path"], "notes/req-design.md")
