"""Release 60 (UR-20261013-01): `wi promote` on a merge conflict refuses before the merge, records
`wi.promote-failed` and leaves the target's main tree untouched; a merge that still fails is aborted."""
import subprocess
from pathlib import Path

from helpers import SiteCase, hook

from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True).stdout.strip()


class PromoteCase(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.mod = self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)
        (self.mod / "docs").mkdir()
        (self.mod / "docs" / "note.md").write_text("line one\n")
        git(self.mod, "add", "docs/note.md")
        git(self.mod, "commit", "-qm", "note")

    def verified(self, rel="docs/note.md", text="line one, by the worker\n"):
        """A verified doc-writer item on modules/demo whose branch changed `rel`; returns (wi, worktree)."""
        wi = wf.wi_new(self.s, "desk", "t", "g", "doc-writer", "modules/demo", ["ok"], checks=["true"])["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        b = wf.dispatch_brief(self.s, wi, "desk")
        hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
             tool_input={"subagent_type": "doc-writer", "prompt": b["brief"]})
        wt = Path(b["worktree"])
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(text)
        git(wt, "add", rel)
        git(wt, "commit", "-qm", "work")
        self.led.event("wi.staged", "desk", wi, {"summary": "s"})
        self.assertEqual(wf.verify_wi(self.s, "checker", wi)["result"], "pass")
        return wi, wt

    def main_commits(self, rel, text):
        (self.mod / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.mod / rel).write_text(text)
        git(self.mod, "add", rel)
        git(self.mod, "commit", "-qm", "main moved")
        return git(self.mod, "rev-parse", "HEAD")

    def assert_main_untouched(self, head):
        self.assertFalse((self.mod / ".git" / "MERGE_HEAD").exists(), "no merge in progress")
        self.assertEqual(git(self.mod, "rev-parse", "HEAD"), head, "HEAD unchanged")
        self.assertEqual(git(self.mod, "status", "--porcelain"), "", "status clean")

    def failed_event(self, wi):
        evs = [e for e in self.led.events(wi) if e["type"] == "wi.promote-failed"]
        self.assertEqual(len(evs), 1)
        return evs[0]


class PreCheck(PromoteCase):
    def test_conflict_is_refused_before_the_merge_and_recorded(self):
        wi, wt = self.verified()
        wi_head = git(wt, "rev-parse", "HEAD")
        base = git(self.mod, "rev-parse", "HEAD")
        main_head = self.main_commits("docs/note.md", "line one, by main\n")  # same line, other change
        with self.assertRaises(DurehError) as cm:
            wf.wi_promote(self.s, "desk", wi, "ok")
        msg = str(cm.exception)
        self.assertIn("docs/note.md", msg)
        self.assertIn(f"wi amend --wi {wi} --reopen --rebase {main_head[:12]}", msg)
        self.assert_main_untouched(main_head)
        self.assertEqual(self.led.wi_status(wi)["status"], "verified")
        ev = self.failed_event(wi)
        self.assertEqual(ev["payload"], {"target": "modules/demo", "base": base, "head": wi_head,
                                         "target_head": main_head, "conflicts": ["docs/note.md"], "stage": "pre-check"})
        self.assertFalse([e for e in self.led.events(wi) if e["type"] == "wi.promoted"])
        self.assertFalse([d for d in self.led.load("decision") if d["wi"] == wi and d["type"] == "gate2"], "no Gate 2 decision")
        # the recovery path: re-open onto the current main, then dispatch again
        amend = wf.wi_amend(self.s, "desk", wi, "Director: rebase and resolve", {}, rebase=main_head, reopen=True)
        self.assertEqual(amend["payload"]["rebase_onto"], main_head)
        self.assertEqual(self.led.wi_status(wi)["status"], "approved")
        self.assertIn(f"git rebase {main_head[:12]}", wf.dispatch_brief(self.s, wi, "desk")["brief"])

    def test_a_clean_promote_merges_as_before(self):
        wi, wt = self.verified("docs/new.md", "x\n")
        self.main_commits("src/other.py", "y = 1\n")  # main moved, no overlap
        ev = wf.wi_promote(self.s, "desk", wi, "ok")
        self.assertTrue(ev["payload"]["merge"])
        self.assertEqual(git(self.mod, "rev-parse", "HEAD"), ev["payload"]["merge"])
        self.assertIn(f"Promote {wi}", git(self.mod, "log", "-1", "--format=%s"))
        self.assertTrue((self.mod / "docs" / "new.md").is_file())
        self.assertEqual(self.led.wi_status(wi)["status"], "promoted")
        self.assertFalse([e for e in self.led.events(wi) if e["type"] == "wi.promote-failed"])
        self.assertEqual(git(self.mod, "status", "--porcelain"), "")

    def test_merge_conflicts_helper(self):
        wi, wt = self.verified()
        branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD")
        head = git(self.mod, "rev-parse", "HEAD")
        self.assertEqual(wf._merge_conflicts(self.mod, head, branch), [])
        head = self.main_commits("docs/note.md", "line one, by main\n")
        self.assertEqual(wf._merge_conflicts(self.mod, head, branch), ["docs/note.md"])
        self.assertIsNone(wf._merge_conflicts(self.mod, head, "no-such-branch"), "git error: no verdict")
        self.assert_main_untouched(head)
