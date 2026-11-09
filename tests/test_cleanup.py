"""Release 98 (UR-20261109-02, two sites): `worktree retire`, `purge --scratch-wi`, `cleanup report` and the cleanup
line of `day close`."""
import contextlib
import io
import json
import os
import subprocess

from helpers import SiteCase

from dureh_core import cleanup
from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.records import Ledger
from dureh_core.util import DurehError


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class CleanupCase(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        self.mod = self.make_module()
        self.bundle = wf.wi_new(self.s, "desk", "b", "g", "pm-modules", "modules/demo", ["a"])["id"]

    def item(self, state, merge=True, commit=True):
        """A WI with a worktree in `state` (promoted: its branch merged into main unless merge=False)."""
        w = wf.wi_new(self.s, "desk", "t", "g", "py-writer", "modules/demo", ["a"], parent=self.bundle)["id"]
        wf.wi_approve(self.s, "desk", w, "ok")
        made = wf._create_worktree(self.s, w, "modules/demo")
        led = Ledger(self.s)
        led.event("dispatch.prepared", "desk", w, {"base": made["base"]})
        led.event("wi.dispatched", "desk", w, {"role": "py-writer", "brief": "1" * 16, "by": "desk"})
        wt = self.s.worktrees / w
        if commit:
            (wt / f"{w}.txt").write_text(w)
            _git(wt, "add", "-A")
            _git(wt, "commit", "-qm", f"work {w}")
        if state == "dispatched":
            return w
        if state == "stopped":
            led.event("wi.stopped", "desk", w, {"reason": "r"})
            return w
        led.event("wi.staged", "desk", w, {"summary": "s"})
        led.event("wi.verified", "desk", w, {})
        if merge:
            _git(self.mod, "merge", "-q", "--no-ff", "-m", f"merge {w}", made["branch"])
        led.event("wi.promoted", "desk", w, {})
        return w

    def branch(self, w):
        return f"wi/{self.s.site_id}/{w}"

    def has_branch(self, w):
        return bool(_git(self.mod, "branch", "--list", self.branch(w)))

    def events(self, type_):
        return [e for e in Ledger(self.s).events() if e["type"] == type_]

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--site", str(self.site_root), *argv])
        return code, out.getvalue(), err.getvalue()


class Retire(CleanupCase):
    def test_promoted_merged_retires_worktree_venv_and_branch(self):
        w = self.item("promoted")
        wt = self.s.worktrees / w
        (wt / ".venv" / "lib").mkdir(parents=True)
        (wt / ".venv" / "lib" / "x.py").write_text("x" * 100)
        dry = cleanup.retire(self.s, "desk", [w], dry_run=True)
        row = dry["rows"][0]
        self.assertEqual((row["verdict"], row["branch"], row["merged"]), ("retire", self.branch(w), True))
        self.assertGreaterEqual(row["size"], 100)
        self.assertTrue(wt.is_dir() and self.has_branch(w), "dry run changes nothing")
        self.assertEqual(self.events(cleanup.RETIRED), [])
        out = cleanup.retire(self.s, "desk", [w])
        self.assertEqual(out["rows"][0]["verdict"], "retired")
        self.assertFalse(os.path.lexists(wt))
        self.assertFalse(self.has_branch(w))
        self.assertNotIn(str(wt), _git(self.mod, "worktree", "list"))
        ev = self.events(cleanup.RETIRED)
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["wi"], ev[0]["payload"]["branch"], ev[0]["payload"]["merged"]), (w, self.branch(w), True))
        self.assertIn(f"{w}.txt", os.listdir(self.mod), "the merged work stays in main")

    def test_refuses_active_dirty_and_unmerged(self):
        active = self.item("dispatched")
        dirty = self.item("promoted")
        (self.s.worktrees / dirty / "src" / "demo.py").write_text("changed\n")
        unmerged = self.item("promoted", merge=False)
        for w, msg in ((active, "is dispatched"), (dirty, "uncommitted changes"), (unmerged, "not merged")):
            with self.assertRaisesRegex(DurehError, msg, msg=w):
                cleanup.retire(self.s, "desk", [w])
            self.assertTrue((self.s.worktrees / w).is_dir() and self.has_branch(w))
        ok = self.item("promoted")
        with self.assertRaisesRegex(DurehError, "nothing retired"):  # --wi: all or nothing
            cleanup.retire(self.s, "desk", [ok, dirty])
        self.assertTrue((self.s.worktrees / ok).is_dir())
        self.assertEqual(self.events(cleanup.RETIRED), [])

    def test_untracked_venv_and_caches_are_not_dirty(self):
        w = self.item("promoted")
        wt = self.s.worktrees / w
        (wt / ".venv").mkdir()
        (wt / ".venv" / "pyvenv.cfg").write_text("")
        (wt / "src" / "__pycache__").mkdir()
        (wt / "src" / "__pycache__" / "demo.pyc").write_text("")
        self.assertEqual(cleanup.retire(self.s, "desk", [w], dry_run=True)["rows"][0]["verdict"], "retire")
        (wt / "notes.txt").write_text("untracked work")
        row = cleanup.retire(self.s, "desk", [w], dry_run=True)["rows"][0]
        self.assertEqual(row["verdict"], "refused")
        self.assertIn("notes.txt", row["reason"])

    def test_all_promoted_batch_retires_passing_items_and_lists_the_rest(self):
        a, b = self.item("promoted"), self.item("promoted")
        bad = self.item("promoted", merge=False)
        stopped = self.item("stopped")
        out = cleanup.retire(self.s, "desk", states=["promoted"])
        verdicts = {r["wi"]: r["verdict"] for r in out["rows"]}
        self.assertEqual((verdicts[a], verdicts[b], verdicts[bad]), ("retired", "retired", "refused"))
        self.assertNotIn(stopped, verdicts)
        self.assertTrue((self.s.worktrees / bad).is_dir() and (self.s.worktrees / stopped).is_dir())
        self.assertEqual(len(self.events(cleanup.RETIRED)), 2)
        # nothing left to do: a second run retires nothing and does not fail
        again = cleanup.retire(self.s, "desk", states=["promoted"])
        self.assertEqual({r["wi"]: r["verdict"] for r in again["rows"]}[a], "nothing")

    def test_branch_without_worktree_is_deleted_when_merged(self):
        w = self.item("promoted")
        _git(self.mod, "worktree", "remove", "--force", str(self.s.worktrees / w))
        out = cleanup.retire(self.s, "desk", [w])
        self.assertEqual((out["rows"][0]["worktree"], out["rows"][0]["verdict"]), (None, "retired"))
        self.assertFalse(self.has_branch(w))

    def test_stopped_empty_branch_retires_without_approval(self):
        w = self.item("stopped", commit=False)
        row = cleanup.retire(self.s, "desk", [w], dry_run=True)["rows"][0]
        self.assertTrue(row.get("empty") or row["merged"])
        self.assertEqual(row["verdict"], "retire")
        cleanup.retire(self.s, "desk", [w])
        self.assertFalse(self.has_branch(w))

    def test_stopped_unmerged_needs_a_bound_approval_used_once(self):
        w = self.item("stopped")
        row = cleanup.retire(self.s, "desk", [w], dry_run=True)["rows"][0]
        self.assertEqual((row["verdict"], row["commits"]), ("needs-approval", "1"))
        with self.assertRaisesRegex(DurehError, "Director's approval"):
            cleanup.retire(self.s, "desk", [w])
        with self.assertRaisesRegex(DurehError, "needs --memo"):
            cleanup.retire_approve(self.s, "desk", [w], "", "Director")
        dec = cleanup.retire_approve(self.s, "desk", [w], "board item 9", "Director")["id"]
        # the branch moves after the approval: refused
        wt = self.s.worktrees / w
        (wt / "more.txt").write_text("m")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", "more")
        with self.assertRaisesRegex(DurehError, "does not approve exactly"):
            cleanup.retire(self.s, "desk", [w], approval=dec)
        dec = cleanup.retire_approve(self.s, "desk", [w], "board item 9", "Director")["id"]
        push = Ledger(self.s).append("decision", "push-approval", "desk", None,
                                     {"subject": "p", "disposition": "approve", "by": "Director"})["id"]
        with self.assertRaisesRegex(DurehError, "not a retire approval"):
            cleanup.retire(self.s, "desk", [w], approval=push)
        cleanup.retire(self.s, "desk", [w], approval=dec)
        self.assertFalse(self.has_branch(w) or (self.s.worktrees / w).exists())
        self.assertEqual(self.events(cleanup.RETIRED)[-1]["refs"], [dec])
        with self.assertRaisesRegex(DurehError, "already used|does not approve"):
            cleanup.retire(self.s, "desk", [w], approval=dec)

    def test_only_the_desk_retires(self):
        w = self.item("promoted")
        with self.assertRaisesRegex(DurehError, "only the desk"):
            cleanup.retire(self.s, "py-writer", [w])

    def test_cli_syntax_and_refusals(self):
        w = self.item("promoted")
        code, out, _ = self.cli("worktree", "retire", "--all-promoted", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"{w} promoted retire: worktree", out)
        self.assertIn("summary: would retire 1 worktree(s) and 1 branch(es)", out)
        code, _, err = self.cli("worktree", "retire", "--wi", w, "--state", "promoted")
        self.assertEqual(code, 2)
        self.assertIn("not both", err)
        code, _, err = self.cli("worktree", "retire", "--wi", w, "--memo", "x")
        self.assertEqual(code, 2)
        code, out, _ = self.cli("worktree", "retire", "--wi", w, "--json")
        self.assertEqual((code, json.loads(out)["rows"][0]["verdict"]), (0, "retired"))
        code, _, err = self.cli("--observe", "worktree", "retire", "--all-promoted", "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("observe mode", err)


class ScratchPurge(CleanupCase):
    def setUp(self):
        super().setUp()
        self.w = self.item("promoted")
        self.dir = self.site_root / "scratch" / self.w
        (self.dir / "cache").mkdir(parents=True)
        (self.dir / "cache" / "a.bin").write_bytes(b"a" * 1000)
        (self.dir / "out").mkdir()
        (self.dir / "out" / "keep.npy").write_bytes(b"k" * 10)

    def test_dry_run_lists_manifest_and_sizes_and_deletes_nothing(self):
        out = cleanup.purge_scratch(self.s, "desk", self.w, ["cache"], None, dry_run=True)
        self.assertEqual((out["files"], out["bytes"]), (1, 1000))
        self.assertEqual(len(out["paths"][0]["manifest"]), 64)
        self.assertIn("mtime", out["binding"])
        self.assertTrue((self.dir / "cache" / "a.bin").is_file())
        whole = cleanup.purge_scratch(self.s, "desk", self.w, None, None, dry_run=True)
        self.assertEqual(whole["bytes"], 1010)
        self.assertEqual([t["name"] for t in whole["paths"][0]["top"]], ["cache/", "out/"])

    def test_approved_path_is_deleted_and_recorded_others_stay(self):
        dec = cleanup.scratch_approve(self.s, "desk", self.w, ["cache"], "board item 3", "Director")["id"]
        out = cleanup.purge_scratch(self.s, "desk", self.w, ["cache"], dec)
        self.assertEqual(out["removed"], ["cache"])
        self.assertFalse((self.dir / "cache").exists())
        self.assertTrue((self.dir / "out" / "keep.npy").is_file())
        ev = self.events(cleanup.SCRATCH_PURGED)[-1]
        self.assertEqual((ev["wi"], ev["refs"], ev["payload"]["bytes"]), (self.w, [dec], 1000))
        with self.assertRaisesRegex(DurehError, "no such path"):
            cleanup.purge_scratch(self.s, "desk", self.w, ["cache"], dec)

    def test_whole_directory(self):
        dec = cleanup.scratch_approve(self.s, "desk", self.w, None, "m", "Director")["id"]
        cleanup.purge_scratch(self.s, "desk", self.w, None, dec)
        self.assertFalse(self.dir.exists())
        self.assertTrue((self.site_root / "scratch").is_dir())

    def test_no_default_approval_binding_and_single_use(self):
        with self.assertRaisesRegex(DurehError, "--approval <decision id> is required"):
            cleanup.purge_scratch(self.s, "desk", self.w, ["cache"], None)
        dec = cleanup.scratch_approve(self.s, "desk", self.w, ["cache"], "m", "Director")["id"]
        with self.assertRaisesRegex(DurehError, "does not approve deleting exactly"):
            cleanup.purge_scratch(self.s, "desk", self.w, None, dec)  # another path set
        (self.dir / "cache" / "new.bin").write_bytes(b"n")
        with self.assertRaisesRegex(DurehError, "does not approve deleting exactly"):
            cleanup.purge_scratch(self.s, "desk", self.w, ["cache"], dec)  # the tree changed
        (self.dir / "cache" / "new.bin").unlink()
        cleanup.purge_scratch(self.s, "desk", self.w, ["cache"], dec)
        (self.dir / "cache").mkdir()
        (self.dir / "cache" / "a.bin").write_bytes(b"a" * 1000)
        os.utime(self.dir / "cache" / "a.bin", ns=(0, 0))
        with self.assertRaisesRegex(DurehError, "already used|does not approve"):
            cleanup.purge_scratch(self.s, "desk", self.w, ["cache"], dec)
        self.assertTrue((self.dir / "cache" / "a.bin").is_file())
        # a project-cache approval never deletes scratch files
        proj = self.site_root / "projects" / "p1" / "cache" / "c"
        proj.mkdir(parents=True)
        (proj / "x.img").write_bytes(b"x")
        other = wf.purge_cache_approve(self.s, "desk", "p1", "c", ["x.img"], "m")["id"]
        with self.assertRaisesRegex(DurehError, "does not approve"):
            cleanup.purge_scratch(self.s, "desk", self.w, ["cache"], other)

    def test_refusals_active_item_paths_and_symlinks(self):
        active = self.item("dispatched")
        (self.site_root / "scratch" / active).mkdir()
        with self.assertRaisesRegex(DurehError, "is dispatched"):
            cleanup.purge_scratch(self.s, "desk", active, None, None, dry_run=True)
        outside = self.tmp / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        (self.dir / "link-dir").symlink_to(outside)
        for p, msg in (("/abs", "relative"), ("../x", r"\.\."), ("nope", "no such path"),
                       ("link-dir/keep.txt", "outside")):
            with self.assertRaisesRegex(DurehError, msg, msg=p):
                cleanup.purge_scratch(self.s, "desk", self.w, [p], None, dry_run=True)
        with self.assertRaisesRegex(DurehError, "overlapping"):
            cleanup.purge_scratch(self.s, "desk", self.w, ["out", "out/keep.npy"], None, dry_run=True)
        with self.assertRaisesRegex(DurehError, "WI id"):
            cleanup.purge_scratch(self.s, "desk", "../x", None, None, dry_run=True)
        # a symlink inside goes as a link; its target is never followed
        dec = cleanup.scratch_approve(self.s, "desk", self.w, None, "m", "Director")["id"]
        cleanup.purge_scratch(self.s, "desk", self.w, None, dec)
        self.assertTrue((outside / "keep.txt").is_file())
        # scratch/<WI> itself a symlink: refused
        w2 = self.item("promoted")
        (self.site_root / "scratch" / w2).symlink_to(outside)
        with self.assertRaisesRegex(DurehError, "not a plain directory"):
            cleanup.purge_scratch(self.s, "desk", w2, None, None, dry_run=True)

    def test_cli(self):
        code, out, _ = self.cli("purge", "--scratch-wi", self.w, "--path", "cache", "--dry-run")
        self.assertEqual((code, json.loads(out)["bytes"]), (0, 1000))
        code, _, err = self.cli("purge", "--scratch-wi", self.w, "--path", "cache")
        self.assertEqual(code, 2)
        self.assertIn("--approval <decision id> is required", err)
        code, _, err = self.cli("purge", "--scratch-wi", self.w, "--path", "cache", "--approve", "--memo", "m")
        self.assertEqual(code, 2)
        self.assertIn("--by is required", err)
        code, out, _ = self.cli("purge", "--scratch-wi", self.w, "--path", "cache", "--approve", "--memo", "m",
                                "--by", "Director")
        dec = json.loads(out)["id"]
        code, _, err = self.cli("purge", "--scratch-wi", self.w, "--wi", self.w)
        self.assertEqual(code, 2)
        self.assertIn("one of", err)
        code, out, _ = self.cli("purge", "--scratch-wi", self.w, "--path", "cache", "--approval", dec)
        self.assertEqual((code, json.loads(out)["removed"]), (0, ["cache"]))


class Report(CleanupCase):
    def test_report_counts_candidates_review_and_scratch_and_writes_nothing(self):
        a = self.item("promoted")
        bad = self.item("promoted", merge=False)
        st = self.item("stopped")
        active = self.item("dispatched")
        for w in (a, active):
            (self.site_root / "scratch" / w).mkdir(parents=True, exist_ok=True)
            (self.site_root / "scratch" / w / "f").write_bytes(b"f" * 50)
        before = sorted(os.listdir(self.s.record_dir("event")))
        r = cleanup.report(self.s)
        self.assertEqual((r["retire"]["worktrees"], r["retire"]["branches"], r["retire"]["wis"]), (1, 1, [a]))
        self.assertEqual(sorted(x["wi"] for x in r["review"]), sorted([bad, st]))
        self.assertEqual([d["wi"] for d in r["scratch"]["dirs"]], [a])
        self.assertEqual(r["scratch"]["bytes"], 50)
        self.assertFalse(r["partial"])
        self.assertTrue(r["summary"].startswith("cleanup: 1 worktree(s) and 1 branch(es)"))
        self.assertEqual(sorted(os.listdir(self.s.record_dir("event"))), before)
        capped = cleanup.report(self.s, time_cap=0)
        self.assertTrue(capped["partial"])
        self.assertIn("partial", capped["summary"])

    def test_report_runs_with_observe(self):
        self.item("promoted")
        code, out, err = self.cli("--observe", "cleanup", "report", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["retire"]["worktrees"], 1)
        code, out, _ = self.cli("--observe", "cleanup", "report")
        self.assertTrue(out.startswith("cleanup: 1 worktree(s)"))


class CloseStep(CleanupCase):
    def config(self, **kw):
        cfg = json.loads(self.s.config_path.read_text())
        cfg.update(kw)
        self.s.config_path.write_text(json.dumps(cfg))
        self.s = self.site()

    def test_off_by_default_reports_only(self):
        w = self.item("promoted")
        out = cleanup.close_step(self.s, "desk")
        self.assertNotIn("retired", out)
        self.assertTrue(out["summary"].startswith("cleanup: 1 worktree(s)"))
        self.assertTrue((self.s.worktrees / w).is_dir())

    def test_retire_on_close_retires_promoted_only(self):
        w = self.item("promoted")
        st = self.item("stopped")
        (self.site_root / "scratch" / w).mkdir(parents=True)
        self.config(cleanup={"retire_on_close": True})
        out = cleanup.close_step(self.s, "desk")
        self.assertEqual(out["retired"], [w])
        self.assertTrue(out["summary"].startswith("retired 1 promoted item(s) at close; cleanup: 0 worktree(s)"))
        self.assertFalse((self.s.worktrees / w).exists())
        self.assertTrue((self.s.worktrees / st).is_dir())
        self.assertTrue((self.site_root / "scratch" / w).is_dir(), "scratch is never deleted automatically")

    def test_config_must_be_a_boolean(self):
        from dureh_core.ops import config_errors

        self.config(cleanup={"retire_on_close": "yes"})
        self.assertTrue(any("cleanup.retire_on_close" in e for e in config_errors(self.s)))


class Citations(CleanupCase):
    def setUp(self):
        super().setUp()
        self.w = self.item("promoted")
        self.dir = self.site_root / "scratch" / self.w
        (self.dir / "cache").mkdir(parents=True)
        (self.dir / "cache" / "c.bin").write_bytes(b"c")
        (self.dir / "mask.nii.gz").write_bytes(b"m" * 10)
        rn = self.site_root / "projects" / "p1" / "research-notes"
        rn.mkdir(parents=True)
        (rn / "positions.md").write_text(
            "# positions\n\n- " + "7e" * 32 + f"  scratch/{self.w}/mask.nii.gz\n")
        (self.site_root / "notes" / "day").mkdir(parents=True, exist_ok=True)
        (self.site_root / "notes" / "day" / "end.md").write_text(
            f"cache at {self.site_root}/scratch/{self.w}/cache/c.bin\nnot this: other/scratch/{self.w}/cache\n"
            f"nor scratch/{self.w}0/x\n")

    def test_dry_run_lists_citations_and_pins(self):
        out = cleanup.purge_scratch(self.s, "desk", self.w, ["cache", "mask.nii.gz"], None, dry_run=True)
        rows = {r["path"]: r for r in out["paths"]}
        self.assertEqual(rows["cache"]["cited"], "cited by notes/day/end.md:1")
        self.assertNotIn("pinned", rows["cache"])
        self.assertEqual(rows["mask.nii.gz"]["pinned"], ["projects/p1/research-notes/positions.md:3"])
        whole = cleanup.purge_scratch(self.s, "desk", self.w, None, None, dry_run=True)["paths"][0]
        self.assertEqual(len(whole["cited"].split("; ")), 2)

    def test_pinned_path_needs_an_approval_that_names_it(self):
        old = cleanup.scratch_approve(self.s, "desk", self.w, ["mask.nii.gz"], "m", "Director")
        self.assertEqual(old["payload"]["pinned"], ["projects/p1/research-notes/positions.md:3"])
        # a decision recorded before the pin existed does not name it: refused
        rn = self.site_root / "projects" / "p1" / "research-notes" / "positions.md"
        text = rn.read_text()
        rn.write_text("# positions\n")
        unpinned = cleanup.scratch_approve(self.s, "desk", self.w, ["mask.nii.gz"], "m", "Director")["id"]
        rn.write_text(text)
        with self.assertRaisesRegex(DurehError, "pinned by sha256 in projects/p1/research-notes/positions.md:3"):
            cleanup.purge_scratch(self.s, "desk", self.w, ["mask.nii.gz"], unpinned)
        self.assertTrue((self.dir / "mask.nii.gz").is_file())
        cleanup.purge_scratch(self.s, "desk", self.w, ["mask.nii.gz"], old["id"])
        self.assertFalse((self.dir / "mask.nii.gz").exists())

    def test_worktree_pinned_by_a_note_is_not_retired_without_approval(self):
        (self.site_root / "notes" / "pin.md").write_text("ab" * 32 + f" worktrees/{self.w}/out.json\n")
        row = cleanup.retire(self.s, "desk", [self.w], dry_run=True)["rows"][0]
        self.assertEqual(row["verdict"], "needs-approval")
        self.assertEqual(row["pinned"], ["notes/pin.md:1"])
        out = cleanup.retire(self.s, "desk", states=["promoted"])
        self.assertEqual(out["rows"][0]["verdict"], "needs-approval")
        self.assertTrue((self.s.worktrees / self.w).is_dir())
        dec = cleanup.retire_approve(self.s, "desk", [self.w], "m", "Director")
        self.assertEqual(dec["payload"]["binding"]["retire"][0]["pinned"], ["notes/pin.md:1"])
        cleanup.retire(self.s, "desk", [self.w], approval=dec["id"])
        self.assertFalse((self.s.worktrees / self.w).exists())

    def test_report_shows_citations(self):
        r = cleanup.report(self.s)
        self.assertIn("pinned by projects/p1/research-notes/positions.md:3", r["scratch"]["dirs"][0]["cited"])
