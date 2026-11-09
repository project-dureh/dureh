"""Release 97 (UR-20261109-01): after `wi amend --rebase`, `dispatch brief` rebases the WI branch onto the amended
base itself (a conflict is aborted, the branch left as it was, and the brief says so), and `wi stage` refuses a head
that does not contain the amended base."""
import subprocess
from pathlib import Path

from helpers import SiteCase, hook, commit_work

from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True).stdout.strip()


class AmendRebase(SiteCase):
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

    def dispatch(self, wi):
        b = wf.dispatch_brief(self.s, wi, "desk")
        hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
             tool_input={"subagent_type": "doc-writer", "prompt": b["brief"]})
        return b

    def worked(self, rel, text, stop=True):
        """A dispatched doc-writer item whose branch committed `rel`, then stopped; returns (wi, worktree, head)."""
        wi = wf.wi_new(self.s, "desk", "t", "g", "doc-writer", "modules/demo", ["ok"], checks=["true"])["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        wt = Path(self.dispatch(wi)["worktree"])
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(text)
        git(wt, "add", rel)
        git(wt, "commit", "-qm", "work")
        if stop:
            wf.dispatch_stop(self.s, "desk", wi, "base is stale")
        return wi, wt, git(wt, "rev-parse", "HEAD")

    def main_commits(self, rel, text):
        (self.mod / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.mod / rel).write_text(text)
        git(self.mod, "add", rel)
        git(self.mod, "commit", "-qm", "main moved")
        return git(self.mod, "rev-parse", "HEAD")

    def prepared(self, wi):
        return [e for e in self.led.events(wi) if e["type"] == "dispatch.prepared"][-1]["payload"]

    def test_redispatch_rebases_the_branch_onto_the_amended_base(self):
        wi, wt, old = self.worked("docs/new.md", "x\n")
        main = self.main_commits("src/other.py", "y = 1\n")
        wf.wi_amend(self.s, "desk", wi, "Director: rebase", {}, rebase=main)
        brief = self.dispatch(wi)["brief"]
        self.assertIn(f"the harness rebased your branch onto {main[:12]} (head was {old[:12]})", brief)
        self.assertNotIn("git rebase", brief)
        head = git(wt, "rev-parse", "HEAD")
        self.assertEqual(git(wt, "rev-parse", "HEAD~1"), main, "the worker's commit now sits on the amended base")
        self.assertEqual((wt / "docs" / "new.md").read_text(), "x\n")
        self.assertEqual(self.prepared(wi)["rebase"], {"onto": main, "from": old, "status": "done", "to": head})
        self.assertEqual(self.prepared(wi)["base"], main)
        # (b) the rebased head stages once the worker committed past the base
        commit_work(self.s, wi)
        self.assertEqual(wf.wi_stage(self.s, "doc-writer", wi, "done")["type"], "wi.staged")

    def test_a_conflict_is_aborted_and_the_brief_says_so(self):
        wi, wt, old = self.worked("docs/note.md", "line one, by the worker\n")
        main = self.main_commits("docs/note.md", "line one, by main\n")
        wf.wi_amend(self.s, "desk", wi, "Director: rebase and resolve", {}, rebase=main)
        brief = self.dispatch(wi)["brief"]
        self.assertIn(f"First: the base moved. In your worktree run `git rebase {main[:12]}`", brief)
        self.assertIn(f"Rebase: the harness ran `git rebase {main[:12]}` in your worktree and it stopped in:"
                      " docs/note.md", brief)
        self.assertEqual(git(wt, "rev-parse", "HEAD"), old, "the branch is left as it was")
        self.assertEqual(git(wt, "status", "--porcelain"), "")
        self.assertFalse(wf._rebase_in_progress(wt))
        rb = self.prepared(wi)["rebase"]
        self.assertEqual((rb["status"], rb["files"], rb["from"], rb["head_after_abort"]),
                         ("conflict", ["docs/note.md"], old, old))
        # (b) a head without the amended base is refused, naming the exact command; nothing is recorded
        commit_work(self.s, wi)
        with self.assertRaises(DurehError) as cm:
            wf.wi_stage(self.s, "doc-writer", wi, "done")
        self.assertIn(f"`git -C {wt} rebase {main}`", str(cm.exception))
        self.assertFalse([e for e in self.led.events(wi) if e["type"] == "wi.staged"])
        # the worker resolves the rebase; the item stages
        subprocess.run(["git", "rebase", main], cwd=wt, capture_output=True)
        (wt / "docs" / "note.md").write_text("line one, by both\n")
        git(wt, "add", "docs/note.md")
        subprocess.run(["git", "-c", "core.editor=true", "rebase", "--continue"], cwd=wt, check=True, capture_output=True)
        self.assertEqual(wf.wi_stage(self.s, "doc-writer", wi, "done")["type"], "wi.staged")

    def test_a_dirty_worktree_is_not_rebased(self):
        wi, wt, old = self.worked("docs/new.md", "x\n")
        (wt / "docs" / "note.md").write_text("uncommitted\n")
        main = self.main_commits("src/other.py", "y = 1\n")
        wf.wi_amend(self.s, "desk", wi, "Director: rebase", {}, rebase=main)
        brief = self.dispatch(wi)["brief"]
        self.assertIn(f"run `git rebase {main[:12]}`", brief)
        self.assertEqual(git(wt, "rev-parse", "HEAD"), old)
        self.assertEqual((wt / "docs" / "note.md").read_text(), "uncommitted\n", "the worker's change is kept")
        self.assertEqual(self.prepared(wi)["rebase"]["status"], "conflict")

    def test_a_branch_that_has_the_base_is_left_alone(self):
        wi, wt, old = self.worked("docs/new.md", "x\n")
        base = git(self.mod, "rev-parse", "HEAD")
        wf.wi_amend(self.s, "desk", wi, "Director: rebase", {}, rebase=base)
        brief = self.dispatch(wi)["brief"]
        self.assertNotIn("git rebase", brief)
        self.assertNotIn("rebased your branch", brief)
        self.assertEqual(git(wt, "rev-parse", "HEAD"), old)
        self.assertNotIn("rebase", self.prepared(wi))
        self.assertEqual(wf.wi_stage(self.s, "doc-writer", wi, "done")["type"], "wi.staged")

    def test_stage_without_an_amendment_is_unchanged(self):
        wi, wt, old = self.worked("docs/new.md", "x\n", stop=False)
        self.main_commits("src/other.py", "y = 1\n")  # main moved, but no `--rebase`: no new requirement
        self.assertEqual(wf.wi_stage(self.s, "doc-writer", wi, "done")["type"], "wi.staged")
