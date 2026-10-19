"""`purge --wi WI --path P`: untracked or ignored paths of a dispatched or staged WI's worktree (release 72,
project-dureh/dureh#23, option A)."""
import contextlib
import io
import json
import subprocess
import unittest

from helpers import SiteCase

from dureh_core import cli
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError


class PurgePaths(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        mod = self.make_module()
        (mod / ".gitignore").write_text("runs/\n*.log\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=mod, check=True)
        subprocess.run(["git", "commit", "-qm", "ignore runs"], cwd=mod, check=True)
        b = wf.wi_new(self.s, "desk", "b", "g", "pm-modules", "modules/demo", ["a"])["id"]
        self.w = wf.wi_new(self.s, "desk", "t", "g", "py-writer", "modules/demo", ["a"], parent=b)["id"]
        wf.wi_approve(self.s, "desk", self.w, "ok")
        wf._create_worktree(self.s, self.w, "modules/demo")
        self.led = Ledger(self.s)
        self.led.event("wi.dispatched", "desk", self.w, {"role": "py-writer", "brief": "1" * 16, "by": "desk"})
        self.wt = self.site_root / "worktrees" / self.w

    def stage(self):
        self.led.event("wi.staged", "desk", self.w, {"summary": "s"})

    def purged(self):
        return [e["payload"] for e in Ledger(self.s).events(self.w) if e["type"] == "purged"]

    def test_purge_path_removes_an_untracked_file(self):
        (self.wt / "src" / "stray.txt").write_text("x")
        out = wf.purge_paths(self.s, "desk", self.w, ["src/stray.txt"])
        self.assertFalse((self.wt / "src" / "stray.txt").exists())
        self.assertTrue((self.wt / "src" / "demo.py").is_file(), "the tracked sibling stays")
        self.assertEqual(out["removed"], [str(self.wt.resolve() / "src" / "stray.txt")])

    def test_purge_path_removes_ignored_files_and_a_directory_of_them(self):
        self.stage()
        (self.wt / "runs" / "a").mkdir(parents=True)
        (self.wt / "runs" / "a" / "t1.log").write_text("")
        (self.wt / "runs" / "t2.log").write_text("")
        (self.wt / "top.log").write_text("")
        wf.purge_paths(self.s, "desk", self.w, ["runs", "top.log", "runs/t2.log"])
        self.assertFalse((self.wt / "runs").exists() or (self.wt / "top.log").exists())
        self.assertEqual(self.purged()[-1]["paths"], ["runs", "top.log"], "a path inside a planned directory goes with it")
        st = subprocess.run(["git", "status", "--porcelain"], cwd=self.wt, capture_output=True, text=True).stdout
        self.assertEqual(st.strip(), "")

    def test_purge_path_refuses_tracked_files(self):
        (self.wt / "src" / "stray.txt").write_text("x")
        for p in ("src/demo.py", "src", ".", "./src/"):
            with self.assertRaisesRegex(DurehError, "tracked|worktree itself"):
                wf.purge_paths(self.s, "desk", self.w, [p])
        with self.assertRaisesRegex(DurehError, "tracked files"):  # validated as a set: nothing removed
            wf.purge_paths(self.s, "desk", self.w, ["src/stray.txt", "src/demo.py"])
        self.assertTrue((self.wt / "src" / "stray.txt").is_file() and (self.wt / "src" / "demo.py").is_file())
        self.assertEqual(self.purged(), [])

    def test_purge_path_refuses_paths_outside_the_worktree(self):
        outside = self.tmp / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        (self.wt / "out-file").symlink_to(outside / "keep.txt")
        (self.wt / "out-dir").symlink_to(outside)
        for p, msg in ((str(outside / "keep.txt"), "relative"), ("../precious/keep.txt", r"\.\."),
                       ("src/../../x", r"\.\."), ("out-file", "outside"), ("out-dir", "outside"),
                       ("out-dir/keep.txt", "outside"), (".git", "git metadata"), ("nope.txt", "no such path")):
            with self.assertRaisesRegex(DurehError, msg, msg=p):
                wf.purge_paths(self.s, "desk", self.w, [p])
        self.assertTrue((outside / "keep.txt").is_file())
        self.assertTrue((self.wt / "out-file").is_symlink() and (self.wt / "out-dir").is_symlink())

    def test_purge_path_removes_an_in_tree_symlink_as_a_link(self):
        (self.wt / "link.log").symlink_to(self.wt / "src" / "demo.py")
        wf.purge_paths(self.s, "desk", self.w, ["link.log"])
        self.assertFalse((self.wt / "link.log").is_symlink())
        self.assertTrue((self.wt / "src" / "demo.py").is_file(), "the link's target is never followed")

    def test_purge_path_dry_run_changes_nothing(self):
        (self.wt / "runs").mkdir()
        (self.wt / "runs" / "t.log").write_text("x")
        out = wf.purge_paths(self.s, "desk", self.w, ["runs"], dry_run=True)
        self.assertEqual(out["planned"], [str(self.wt.resolve() / "runs")])
        self.assertEqual(out["removed"], [])
        self.assertTrue((self.wt / "runs" / "t.log").is_file())
        self.assertEqual(self.purged(), [])

    def test_purge_path_records_a_purged_event(self):
        (self.wt / "runs").mkdir()
        (self.wt / "runs" / "t.log").write_text("x")
        wf.purge_paths(self.s, "desk", self.w, ["runs/"])
        ev = self.purged()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["paths"], ["runs"])
        self.assertEqual(ev[0]["removed"], [str(self.wt.resolve() / "runs")])
        self.assertEqual(self.led.wi_status(self.w)["status"], "dispatched", "the WI's status is unchanged")

    def test_purge_path_only_desk_and_only_active_work(self):
        (self.wt / "stray.txt").write_text("x")
        with self.assertRaisesRegex(DurehError, "only the desk"):
            wf.purge_paths(self.s, "py-writer", self.w, ["stray.txt"])
        wf.dispatch_stop(self.s, "desk", self.w, "test")
        with self.assertRaisesRegex(DurehError, "dispatched or staged"):
            wf.purge_paths(self.s, "desk", self.w, ["stray.txt"])
        self.assertTrue((self.wt / "stray.txt").is_file())

    def test_whole_wi_purge_unchanged(self):
        (self.wt / "stray.txt").write_text("x")
        with self.assertRaisesRegex(DurehError, "purge only promoted, stopped, or reverted work"):
            wf.purge(self.s, "desk", self.w)
        wf.dispatch_stop(self.s, "desk", self.w, "test")
        out = wf.purge(self.s, "desk", self.w)
        self.assertIn(str(self.wt), out["removed"])
        self.assertFalse(self.wt.exists())
        ev = self.purged()
        self.assertEqual(len(ev), 1)
        self.assertNotIn("paths", ev[0])

    def test_purge_path_through_the_cli(self):
        (self.wt / "runs").mkdir()
        (self.wt / "runs" / "t.log").write_text("x")
        (self.wt / "a.log").write_text("x")

        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["--site", str(self.site_root), "purge", "--wi", self.w, *argv])
            return code, out.getvalue(), err.getvalue()

        code, out, _ = run("--path", "runs", "--path", "a.log", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["dry_run"] and (self.wt / "runs").is_dir())
        code, _, err = run("--path", "src/demo.py")
        self.assertNotEqual(code, 0)
        self.assertIn("tracked", err)
        code, out, _ = run("--path", "runs", "--path", "a.log")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["removed"]), 2)
        self.assertFalse((self.wt / "runs").exists() or (self.wt / "a.log").exists())


if __name__ == "__main__":
    unittest.main()
