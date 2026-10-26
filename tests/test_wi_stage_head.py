"""Release 81 (UR-20261023-01): `wi stage` refuses a WI branch whose head is still its dispatch base, and a staged
item whose head moved since its last `wi.staged` is staged again at the new head."""
import subprocess
from pathlib import Path

from helpers import SiteCase, commit_work

from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class StageHead(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)
        self.wi = wf.wi_new(self.s, "desk", "t", "g", "doc-writer", "modules/demo", ["ok"], checks=["true"])["id"]
        wf.wi_approve(self.s, "desk", self.wi, "ok")
        b = wf.dispatch_brief(self.s, self.wi, "desk")
        self.led.event("wi.dispatched", "desk", self.wi, {"role": "doc-writer"})
        self.wt = Path(b["worktree"])
        self.base = [e for e in self.led.events(self.wi) if e["type"] == "dispatch.prepared"][-1]["payload"]["base"]

    def staged(self):
        return [e for e in self.led.events(self.wi) if e["type"] == "wi.staged"]

    def test_head_equal_to_base_is_refused_and_records_nothing(self):
        self.assertEqual(git(self.wt, "rev-parse", "HEAD"), self.base)
        with self.assertRaisesRegex(DurehError, r"is its dispatch base \(nothing committed past it\); commit the work"):
            wf.wi_stage(self.s, "desk", self.wi, "done")
        self.assertEqual(self.staged(), [])
        self.assertEqual(self.led.wi_status(self.wi)["status"], "dispatched")
        head = commit_work(self.s, self.wi)  # the worker commits: staging now works, at that head
        ev = wf.wi_stage(self.s, "desk", self.wi, "done")
        self.assertEqual(ev["payload"]["head"], head)
        self.assertNotIn("restaged_from", ev["payload"])
        self.assertEqual(self.led.wi_status(self.wi)["status"], "staged")

    def test_restage_at_a_new_head_and_the_same_head_is_refused(self):
        commit_work(self.s, self.wi)
        first = wf.wi_stage(self.s, "desk", self.wi, "first")
        with self.assertRaisesRegex(DurehError, "already staged at head"):
            wf.wi_stage(self.s, "desk", self.wi, "again")
        head = commit_work(self.s, self.wi, "more")
        second = wf.wi_stage(self.s, "desk", self.wi, "second")
        self.assertEqual((second["payload"]["head"], second["payload"]["restaged_from"]), (head, first["id"]))
        self.assertEqual(self.led.wi_status(self.wi)["status"], "staged")
        self.assertEqual(len(self.staged()), 2)
        r = wf.verify_wi(self.s, "checker", self.wi)
        self.assertEqual(r["result"], "pass")
        self.assertEqual(self.led.get("run", r["run"])["payload"]["head"], head)

    def test_an_item_staged_at_its_base_before_release_81_is_staged_again_once_committed(self):
        """The site's sequence (findings 1368f671, cc00d3b9): staged at the base, then a commit, then a re-stage."""
        self.led.event("wi.staged", "desk", self.wi, {"summary": "early", "head": self.base})  # as release 80 did
        head = commit_work(self.s, self.wi)
        ev = wf.wi_stage(self.s, "desk", self.wi, "committed now")
        self.assertEqual(ev["payload"]["head"], head)
        self.assertEqual(wf.staging_summary_block(self.led, self.wi).count(head[:12]), 1)
        self.assertEqual(wf.verify_wi(self.s, "checker", self.wi)["result"], "pass")

    def test_dirty_worktree_is_still_refused_first(self):
        (self.wt / "new.md").write_text("x\n")
        with self.assertRaisesRegex(DurehError, "uncommitted changes"):
            wf.wi_stage(self.s, "desk", self.wi, "done")
