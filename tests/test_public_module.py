"""Release 82 (UR-20261026-01 option A): a module with config `modules[].public: true` gets commits that read as the
owner's own: `wi promote` writes `<type>: <summary>` (no WI id, no site name, no trailers) or fast-forwards a single
worker commit, worker briefs carry the commit-message rule, and verify fails a branch whose commits break it."""
import json
import subprocess
from pathlib import Path

from helpers import SiteCase, hook

from dureh_core import ops
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True).stdout.strip()


class PublicModule(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.mod = self.make_module()
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg.setdefault("modules", []).append({"id": "demo", "path": "modules/demo", "public": True})
        cfg.setdefault("startup", {})["id"] = "acme-lab"
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        self.led = Ledger(self.s)

    def item(self, label=None):
        wi = wf.wi_new(self.s, "desk", "tidy the reader", "g", "doc-writer", "modules/demo", ["ok"], checks=["true"],
                       label=label)["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        b = wf.dispatch_brief(self.s, wi, "desk")
        hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
             tool_input={"subagent_type": "doc-writer", "prompt": b["brief"]})
        return wi, Path(b["worktree"]), b["brief"]

    def commit(self, wt, rel, text, message):
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(text)
        git(wt, "add", rel)
        git(wt, "commit", "-qm", message)

    def verify(self, wi):
        self.led.event("wi.staged", "desk", wi, {"summary": "s"})
        return wf.verify_wi(self.s, "checker", wi)

    def test_brief_carries_the_commit_rule(self):
        _wi, _wt, brief = self.item()
        self.assertIn(wf.PUBLIC_COMMIT_LINE, brief)

    def test_single_clean_commit_fast_forwards(self):
        wi, wt, _ = self.item(label="docs: describe the reader")
        self.commit(wt, "docs/reader.md", "x\n", "docs: describe the reader")
        self.assertEqual(self.verify(wi)["result"], "pass")
        head = git(wt, "rev-parse", "HEAD")
        ev = wf.wi_promote(self.s, "desk", wi, "ok")
        self.assertEqual(git(self.mod, "rev-parse", "HEAD"), head, "the worker's commit as it is")
        self.assertEqual(ev["payload"]["merge"], head)
        self.assertEqual(ev["payload"]["public"], {"message": None, "fast_forward": True})
        self.assertEqual(ev["payload"]["compensation"], f"git revert {head}")
        self.assertNotIn("WI-", git(self.mod, "log", "--format=%B"))

    def test_merge_commit_reads_the_label_or_the_public_summary(self):
        wi, wt, _ = self.item(label="fix: handle empty scans")
        self.commit(wt, "docs/a.md", "a\n", "fix: handle empty scans")
        self.commit(wt, "docs/b.md", "b\n", "docs: note the empty case")
        self.assertEqual(self.verify(wi)["result"], "pass")
        with self.assertRaisesRegex(DurehError, "names a WI id"):
            wf.wi_promote(self.s, "desk", wi, "ok", "fix: tidy WI-0001")
        with self.assertRaisesRegex(DurehError, r"names the site \(.*acme-lab"):
            wf.wi_promote(self.s, "desk", wi, "ok", "fix: tidy for Acme-Lab")
        with self.assertRaisesRegex(DurehError, "is not `<type>: <short English summary>`"):
            wf.wi_promote(self.s, "desk", wi, "ok", "Promote WI: tidy")
        with self.assertRaisesRegex(DurehError, "not English"):
            wf.wi_promote(self.s, "desk", wi, "ok", "fix: 정리")
        self.assertEqual(self.led.wi_status(wi)["status"], "verified")
        ev = wf.wi_promote(self.s, "desk", wi, "ok", "fix: handle empty scans and note it")
        self.assertEqual(git(self.mod, "log", "-1", "--format=%B").strip(), "fix: handle empty scans and note it")
        self.assertEqual(len(git(self.mod, "log", "-1", "--format=%P").split()), 2, "a merge commit")
        self.assertEqual(ev["payload"]["public"], {"message": "fix: handle empty scans and note it",
                                                   "fast_forward": False})
        self.assertTrue(ev["payload"]["compensation"].startswith("git revert -m 1 "))

    def test_label_is_the_default_message_and_a_missing_one_is_refused(self):
        wi, wt, _ = self.item()
        self.commit(wt, "docs/a.md", "a\n", "docs: one")
        self.commit(wt, "docs/b.md", "b\n", "docs: two")
        self.verify(wi)
        with self.assertRaisesRegex(DurehError, "has no label.*--public-summary"):
            wf.wi_promote(self.s, "desk", wi, "ok")
        wi2, wt2, _ = self.item(label="docs: add two notes")
        self.commit(wt2, "docs/c.md", "c\n", "docs: three")
        self.commit(wt2, "docs/d.md", "d\n", "docs: four")
        self.verify(wi2)
        wf.wi_promote(self.s, "desk", wi2, "ok")
        self.assertEqual(git(self.mod, "log", "-1", "--format=%s"), "docs: add two notes")

    def test_verify_fails_commits_that_break_the_rule(self):
        wi, wt, _ = self.item(label="docs: notes")
        self.commit(wt, "docs/a.md", "a\n", "docs: notes for WI-0042\n\nCo-Authored-By: Someone <x@y>\n"
                                             "Claude-Session: https://example.org/s")
        out = self.verify(wi)
        self.assertEqual(out["result"], "fail")
        probs = " ".join(out["commit_problems"])
        self.assertIn("names a WI id", probs)
        self.assertIn("carries a Claude-Session and a Co-Authored-By trailer", probs)
        self.assertIn("commit message problem", out["note"])
        self.assertEqual(self.led.wi_status(wi)["status"], "rework")

    def test_private_module_is_unchanged(self):
        cfg = json.loads((self.site_root / "config.json").read_text())
        del cfg["modules"][0]["public"]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        wi, wt, brief = self.item()
        self.assertNotIn(wf.PUBLIC_COMMIT_LINE, brief)
        self.commit(wt, "docs/a.md", "a\n", f"docs: {wi}\n\nCo-Authored-By: x <x@y>")
        out = self.verify(wi)
        self.assertEqual(out["result"], "pass")
        self.assertNotIn("commit_problems", out)
        with self.assertRaisesRegex(DurehError, "--public-summary goes with a work item on a public module"):
            wf.wi_promote(self.s, "desk", wi, "ok", "docs: x")
        wf.wi_promote(self.s, "desk", wi, "ok")
        self.assertIn(f"Promote {wi}", git(self.mod, "log", "-1", "--format=%s"))

    def test_config_check(self):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["modules"][0]["public"] = "yes"
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.assertTrue(any("modules[0].public must be true or false" in e for e in ops.config_errors(self.site())))
