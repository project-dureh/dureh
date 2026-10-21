"""Release 77 (project-dureh/dureh#40): `purge --project P --cache C --path F... --approval D` deletes unpinned files
from `projects/<P>/cache/<C>/` with a Director approval bound to those files and their sha256. Data deletion is
fail-closed: no default approval, no fallback."""
import contextlib
import hashlib
import io
import json
import os

from helpers import SiteCase

from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.records import Ledger
from dureh_core.util import DurehError


class PurgeCache(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        self.proj = self.site_root / "projects" / "p1"
        self.cache = self.proj / "cache" / "containers"
        self.cache.mkdir(parents=True)
        for name in ("old-a.img", "old-b.img", "current.img"):
            (self.cache / name).write_bytes(name.encode() * 100)
        (self.proj / "manifest.yaml").write_text("images:\n  - current.img\n")

    def approve(self, paths, memo="board item 7, Option 1"):
        return wf.purge_cache_approve(self.s, "desk", "p1", "containers", paths, memo)["id"]

    def events(self):
        return [e for e in Ledger(self.s).events() if e["type"] == wf.CACHE_PURGED]

    def test_dry_run_lists_sizes_and_sha256_and_deletes_nothing(self):
        out = wf.purge_cache(self.s, "desk", "p1", "containers", ["old-a.img", "old-b.img"], None, dry_run=True)
        digest = hashlib.sha256(b"old-a.img" * 100).hexdigest()
        self.assertEqual(out["files"][0], {"path": "old-a.img", "size": 900, "sha256": digest})
        self.assertEqual(out["bytes"], 1800)
        self.assertTrue((self.cache / "old-a.img").is_file())
        self.assertEqual(self.events(), [])

    def test_approved_purge_removes_exactly_the_bound_files_and_records_them(self):
        dec = self.approve(["old-a.img", "old-b.img"])
        out = wf.purge_cache(self.s, "desk", "p1", "containers", ["old-b.img", "old-a.img"], dec)
        self.assertEqual(sorted(out["removed"]), ["old-a.img", "old-b.img"])
        self.assertFalse((self.cache / "old-a.img").exists())
        self.assertTrue((self.cache / "current.img").is_file())
        ev = self.events()[-1]
        self.assertEqual(ev["refs"], [dec])
        self.assertEqual({f["path"]: f["size"] for f in ev["payload"]["files"]}, {"old-a.img": 900, "old-b.img": 900})
        self.assertTrue(all(len(f["sha256"]) == 64 for f in ev["payload"]["files"]))

    def test_no_approval_no_default(self):
        with self.assertRaisesRegex(DurehError, "--approval <decision id> is required"):
            wf.purge_cache(self.s, "desk", "p1", "containers", ["old-a.img"], None)
        self.assertTrue((self.cache / "old-a.img").is_file())

    def test_approval_must_bind_these_files_and_their_content(self):
        dec = self.approve(["old-a.img"])
        with self.assertRaisesRegex(DurehError, "does not approve deleting exactly these files"):
            wf.purge_cache(self.s, "desk", "p1", "containers", ["old-a.img", "old-b.img"], dec)
        (self.cache / "old-a.img").write_bytes(b"changed since approval")
        with self.assertRaisesRegex(DurehError, "does not approve deleting exactly these files"):
            wf.purge_cache(self.s, "desk", "p1", "containers", ["old-a.img"], dec)
        push = Ledger(self.s).append("decision", "push-approval", "desk", None,
                                     {"subject": "push", "disposition": "approve", "by": "Director"})
        with self.assertRaisesRegex(DurehError, "is not a purge approval"):
            wf.purge_cache(self.s, "desk", "p1", "containers", ["old-a.img"], push["id"])
        self.assertTrue((self.cache / "old-a.img").is_file())

    def test_an_approval_is_used_once(self):
        dec = self.approve(["old-a.img"])
        wf.purge_cache(self.s, "desk", "p1", "containers", ["old-a.img"], dec)
        (self.cache / "old-a.img").write_bytes(b"old-a.img" * 100)  # the same file again
        with self.assertRaisesRegex(DurehError, "already used"):
            wf.purge_cache(self.s, "desk", "p1", "containers", ["old-a.img"], dec)

    def test_pinned_files_are_refused(self):
        with self.assertRaisesRegex(DurehError, r"pinned \(its name appears in projects/p1/manifest.yaml\)"):
            self.approve(["current.img"])
        digest = hashlib.sha256(b"old-b.img" * 100).hexdigest()
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["env"] = {"vars": {"IMAGE_DIGEST": digest}}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        with self.assertRaisesRegex(DurehError, r"pinned \(its sha256 appears in config.json\)"):
            wf.purge_cache(self.site(), "desk", "p1", "containers", ["old-b.img"], None, dry_run=True)

    def test_paths_outside_the_project_cache_are_refused(self):
        (self.proj / "data.txt").write_text("x")
        (self.site_root / "outside.img").write_text("x")
        os.symlink(self.site_root / "outside.img", self.cache / "link.img")
        (self.cache / "sub").mkdir()
        for path, why in (("../../data.txt", "with `..`"), (str(self.site_root / "outside.img"), "outside projects/p1/cache"),
                          ("link.img", "symlink"), ("sub", "not a regular file"), ("missing.img", "no such file")):
            with self.assertRaisesRegex(DurehError, why):
                wf.purge_cache(self.s, "desk", "p1", "containers", [path], None, dry_run=True)
        with self.assertRaisesRegex(DurehError, "plain name"):
            wf.purge_cache(self.s, "desk", "../p1", "containers", ["old-a.img"], None, dry_run=True)

    def test_desk_only(self):
        with self.assertRaisesRegex(DurehError, "only the desk purges"):
            wf.purge_cache(self.s, "py-writer", "p1", "containers", ["old-a.img"], None, dry_run=True)
        with self.assertRaisesRegex(DurehError, "only the desk purges"):
            wf.purge_cache_approve(self.s, "py-writer", "p1", "containers", ["old-a.img"], "m")

    def test_cli(self):
        def cli(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                main(["--site", str(self.site_root), "purge", *argv])
            return json.loads(buf.getvalue())

        base = ["--project", "p1", "--cache", "containers", "--path", "old-a.img"]
        self.assertTrue(cli(*base, "--dry-run")["dry_run"])
        dec = cli(*base, "--approve", "--by", "Director", "--memo", "board item 7")
        self.assertEqual((dec["type"], dec["payload"]["by"]), (wf.CACHE_APPROVAL, "Director"))
        self.assertEqual(cli(*base, "--approval", dec["id"])["removed"], ["old-a.img"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["--site", str(self.site_root), "purge", "--wi", "WI-0001", "--project", "p1"]), 2)
        self.assertIn("not both", err.getvalue())
