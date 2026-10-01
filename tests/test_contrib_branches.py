"""D2: harness/package WIs promote to contrib/<opaque id>; D4: WI branches are wi/<site-id>/WI-NNNN."""
import json
import os
import subprocess
import unittest
from pathlib import Path

from helpers import SiteCase, hook

from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.site import Site


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Branches(SiteCase):
    def setUp(self):
        super().setUp()
        git(self.package, "init", "-q", "-b", "main")
        git(self.package, "add", "-A")
        git(self.package, "commit", "-qm", "package")
        self.install()
        self.mod = self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)

    def dispatch(self, role, target, title="t"):
        """Create, approve and dispatch one worker item (desk-dispatchable roles only); return (wi, worktree)."""
        wi = wf.wi_new(self.s, "desk", title, "g", role, target, ["ok"], checks=["true"])["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        b = wf.dispatch_brief(self.s, wi, "desk")
        hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
             tool_input={"subagent_type": role, "prompt": b["brief"]})
        return wi, Path(b["worktree"])

    def commit_and_verify(self, wi, wt, rel):
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text("x\n")
        git(wt, "add", rel)
        git(wt, "commit", "-qm", "work")
        self.led.event("wi.staged", "desk", wi, {"summary": "s"})
        self.assertEqual(wf.verify_wi(self.s, "checker", wi)["result"], "pass")

    def test_package_wi_promotes_to_an_opaque_contrib_branch_without_merging(self):
        wi, wt = self.dispatch("doc-writer", "package")
        main_before = git(self.package, "rev-parse", "main")
        self.commit_and_verify(wi, wt, "docs/note.md")
        head = git(wt, "rev-parse", "HEAD")
        ev = wf.wi_promote(self.s, "desk", wi, "Director approved")
        branch = ev["payload"]["contribution"]
        self.assertRegex(branch, r"^contrib/c-[0-9a-f]{8}$")
        self.assertNotIn(self.s.site_id, branch)
        self.assertNotIn(wi, branch)
        self.assertEqual(git(self.package, "rev-parse", branch), head)
        self.assertEqual(git(self.package, "rev-parse", "main"), main_before, "nothing is merged")
        self.assertIsNone(ev["payload"]["merge"])
        self.assertEqual(self.led.wi_status(wi)["status"], "promoted")

    def test_contribution_notifies_upstream_through_the_mailbox(self):
        x = self.tmp / "exchange"
        x.mkdir()
        cfg = json.loads(self.s.config_path.read_text())
        cfg["upstream"] = {"exchange": str(x)}
        self.s.config_path.write_text(json.dumps(cfg))
        self.s = Site(self.site_root)
        wi, wt = self.dispatch("doc-writer", "package", title="Explain the thing")
        self.commit_and_verify(wi, wt, "docs/note.md")
        branch = wf.wi_promote(self.s, "desk", wi, "ok")["payload"]["contribution"]
        sent = list((x / self.site_root.name / "to-upstream").glob("*-notice-*.md"))
        self.assertEqual(len(sent), 1)
        text = sent[0].read_text()
        self.assertIn(branch, text)
        self.assertIn("Explain the thing", text)

    def test_new_worktrees_use_the_site_namespace(self):
        wi, wt = self.dispatch("doc-writer", "modules/demo")
        self.assertEqual(git(wt, "rev-parse", "--abbrev-ref", "HEAD"), f"wi/{self.s.site_id}/{wi}")

    def test_a_legacy_branch_keeps_working_and_merges(self):
        wi = wf.wi_new(self.s, "desk", "t", "g", "doc-writer", "modules/demo", ["ok"], checks=["true"])["id"]
        legacy_wt = self.s.worktrees / wi
        self.s.worktrees.mkdir(exist_ok=True)
        git(self.mod, "worktree", "add", "-q", "-b", f"wi/{wi}", str(legacy_wt))  # made by an older harness
        wf.wi_approve(self.s, "desk", wi, "ok")
        b = wf.dispatch_brief(self.s, wi, "desk")
        self.assertIn(f"branch wi/{wi}", b["brief"])
        hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
             tool_input={"subagent_type": "doc-writer", "prompt": b["brief"]})
        self.commit_and_verify(wi, legacy_wt, "docs/n.md")
        ev = wf.wi_promote(self.s, "desk", wi, "ok")
        self.assertTrue(ev["payload"]["merge"])
        self.assertIn(f"Promote {wi}", git(self.mod, "log", "-1", "--format=%s"))

    def test_purge_deletes_only_this_sites_branches(self):
        wi, wt = self.dispatch("doc-writer", "modules/demo")
        wf.dispatch_stop(self.s, "desk", wi, "done")
        git(self.mod, "branch", f"wi/{wi}")  # another site's legacy branch with the same WI number
        wf.purge(self.s, "desk", wi)
        branches = git(self.mod, "branch", "--list", "wi/*")
        self.assertNotIn(f"wi/{self.s.site_id}/{wi}", branches)
        self.assertIn(f"wi/{wi}", branches, "a legacy branch whose tip this site never recorded is kept")


if __name__ == "__main__":
    unittest.main()
