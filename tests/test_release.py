"""D1: tools/release.py cuts tagged, pushed, read-only release checkouts and prunes unused ones."""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parent.parent
SCRIPT = HARNESS / "tools" / "release.py"
GIT_ENV = {"GIT_AUTHOR_NAME": "DUREH Test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "DUREH Test", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


class Release(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dureh-release-")).resolve()
        self.origin = self.tmp / "origin.git"
        git(["init", "--bare", "-q", str(self.origin)], self.tmp)
        self.repo = self.tmp / "repo"
        git(["clone", "-q", str(self.origin), str(self.repo)], self.tmp)
        git(["checkout", "-q", "-b", "main"], self.repo)
        (self.repo / "dureh_core").mkdir()
        (self.repo / "dureh_core" / "__init__.py").write_text("")
        (self.repo / "dureh").write_text("#!/bin/sh\necho harness\n")
        (self.repo / "dureh").chmod(0o755)
        self.commit("first")
        git(["push", "-q", "-u", "origin", "main"], self.repo)
        self.releases = self.tmp / "releases"
        self.sites = self.tmp / "sites"
        self.sites.mkdir()

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            if not p.is_symlink():
                p.chmod(p.stat().st_mode | 0o200)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit(self, msg):
        with (self.repo / "CHANGES").open("a") as f:
            f.write(msg + "\n")
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", msg], self.repo)

    def run_script(self, *args, ok=True):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo), "--releases", str(self.releases),
                               "--sites", str(self.sites), *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout + proc.stderr

    def site_runs(self, name, release):
        (self.sites / name).mkdir(exist_ok=True)
        (self.sites / name / "config.json").write_text(json.dumps({"harness": {"root": str(release)}}))

    def test_cut_tags_pushes_and_checks_out_read_only(self):
        today = dt.datetime.now(dt.timezone.utc).date()
        tag = f"r{today:%Y.%m.%d}-1"
        out = self.run_script("cut")
        self.assertIn(tag, out)
        self.assertIn(tag, git(["tag"], self.origin), "tag reaches the remote")
        rel = self.releases / f"dureh-{tag}"
        self.assertEqual(git(["rev-parse", "HEAD"], rel), git(["rev-parse", "HEAD"], self.repo))
        self.assertEqual(git(["describe", "--tags", "--exact-match"], rel), tag)
        self.assertEqual(git(["remote", "get-url", "origin"], rel), str(self.origin), "pinned url is the remote, not the clone")
        self.assertFalse(os.access(rel / "dureh", os.W_OK))
        self.assertFalse(os.access(rel / "dureh_core", os.W_OK))
        self.assertTrue(os.access(rel / ".git", os.W_OK), "worktrees and contrib branches still need .git")
        self.assertIn("adapter sync --harness", out)
        # a second release the same day gets -2; the same revision is not re-tagged
        self.assertIn("nothing to do", self.run_script("cut"))  # re-cutting a released revision is a no-op
        self.commit("second")
        git(["push", "-q"], self.repo)
        out = self.run_script("cut")
        self.assertIn(f"r{today:%Y.%m.%d}-2", out)

    def test_cut_refuses_dirty_or_unpushed(self):
        (self.repo / "dirty").write_text("x")
        self.assertIn("uncommitted", self.run_script("cut", ok=False))
        (self.repo / "dirty").unlink()
        self.commit("unpushed")
        self.assertIn("no remote branch", self.run_script("cut", ok=False))

    def test_prune_keeps_in_use_and_previous_holds_attached(self):
        tags = []
        for n in range(3):
            if n:
                self.commit(f"c{n}")
                git(["push", "-q"], self.repo)
            out = self.run_script("cut")
            tags.append(out.split()[1])
        rels = [self.releases / f"dureh-{t}" for t in tags]
        self.assertIn("no site", self.run_script("prune", ok=False), "nothing is pruned when no site runs a release")
        self.site_runs("alpha", rels[2])
        git(["branch", "contrib/c-deadbeef"], rels[0])
        out = self.run_script("prune", "--dry-run")
        self.assertIn("hold   " + rels[0].name, out)
        self.assertIn("keep   " + rels[1].name, out)
        self.assertIn("keep   " + rels[2].name, out)
        git(["branch", "-D", "contrib/c-deadbeef"], rels[0])
        out = self.run_script("prune")
        self.assertIn("deleted " + rels[0].name, out)
        self.assertFalse(rels[0].exists())
        self.assertTrue(rels[1].exists() and rels[2].exists())
        out = self.run_script("list")
        self.assertIn("sites: alpha", out)


if __name__ == "__main__":
    unittest.main()
