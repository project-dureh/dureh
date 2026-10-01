"""Promotion of verified checkpoints into a derivatives subdataset (site WI-0036 spec)."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from helpers import SiteCase

from dureh_core import promote as pr
from dureh_core.records import Ledger
from dureh_core.util import DurehError

ANNEX_BIN = Path.home() / "micromamba" / "envs" / "annex" / "bin"


def _tools_path():
    path = os.environ.get("PATH", "")
    if ANNEX_BIN.is_dir():
        path = f"{ANNEX_BIN}{os.pathsep}{path}"
    return path if shutil.which("datalad", path=path) and shutil.which("git-annex", path=path) else None


TOOLS = _tools_path()


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


@unittest.skipUnless(TOOLS, "datalad and git-annex not available: not run")
class Promote(SiteCase):
    @classmethod
    def setUpClass(cls):
        import tempfile

        from helpers import GIT_ENV

        cls._saved = dict(os.environ)
        os.environ.update(GIT_ENV, PATH=TOOLS)
        cls._tmp = Path(tempfile.mkdtemp(prefix="dureh-promote-")).resolve()
        cls.template = cls._tmp / "lna"
        run(["datalad", "create", "-c", "text2git", str(cls.template)])
        (cls.template / ".gitignore").write_text("checkpoints/\n")
        run(["datalad", "save", "-d", str(cls.template), "-m", "ignore checkpoints"])
        run(["datalad", "create", "-d", str(cls.template), str(cls.template / "derivatives" / "fmriprep")])
        os.environ.clear()
        os.environ.update(cls._saved)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["chmod", "-R", "u+w", str(cls._tmp)], check=False)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def tearDown(self):
        subprocess.run(["chmod", "-R", "u+w", str(self.tmp)], check=False)
        super().tearDown()

    def setUp(self):
        super().setUp()
        os.environ["PATH"] = TOOLS
        manifest = self.package / "startup.yaml"
        manifest.write_text(manifest.read_text()
                            .replace("managers: [pm-modules]", "managers: [pm-modules, pm-projects]")
                            .replace("workers: [py-writer,", "workers: [data-steward, pipeline-runner, py-writer,"))
        self.install()
        self.s = self.site()
        self.led = Ledger(self.s)
        self.proj = self.site_root / "projects" / "lna"
        shutil.copytree(self.template, self.proj, symlinks=True)
        self.ds = self.proj / "derivatives" / "fmriprep"
        self.ck = self.proj / "checkpoints" / "fmriprep" / "sub-01" / "func"
        self.ck.mkdir(parents=True)
        (self.ck / "bold.nii.gz").write_bytes(b"\x1f\x8b" + os.urandom(4096))
        bundle = self.wi("Bundle", "pm-projects")
        self.steward = self.wi("Promote", "data-steward", parent=bundle)
        self.runner = self.wi("Run", "pipeline-runner", parent=bundle)
        self.run_id = self.led.append("run", "slurm", "pipeline-runner", self.runner,
                                      {"run_type": "slurm", "status": "submitted", "job_id": "4242"})["id"]

    def wi(self, title, role, parent=None):
        from dureh_core import workflow as wf

        w = wf.wi_new(self.s, "desk", title, "g", role, "projects/lna", ["ok"], parent=parent)["id"]
        wf.wi_approve(self.s, "desk", w, "ok")
        self.led.event("wi.dispatched", "desk", w, {"role": role, "brief": "0" * 16, "by": "desk"})
        return w

    def completed(self, state="COMPLETED"):
        self.led.event("run.status", "pipeline-runner", self.runner,
                       {"run": self.run_id, "job_id": "4242", "scheduler_state": state}, [self.run_id])

    def promote(self, **kw):
        args = dict(actor="data-steward", wi=self.steward, run_id=self.run_id,
                    source="checkpoints/fmriprep/sub-01/func/bold.nii.gz", dataset="fmriprep")
        args.update(kw)
        return pr.promote(self.s, **args)

    def test_promotes_into_the_subdataset_and_records_it(self):
        self.completed()
        r = self.promote()
        dest = self.ds / "sub-01" / "func" / "bold.nii.gz"
        self.assertEqual(dest.read_bytes(), (self.ck / "bold.nii.gz").read_bytes())
        self.assertEqual(run(["git", "status", "--porcelain"], self.ds), "")
        self.assertEqual(run(["git", "status", "--porcelain"], self.proj), "")
        msg = run(["git", "log", "-1", "--format=%B"], self.ds)
        for part in (self.steward, self.run_id, "4242", "checkpoints/fmriprep/sub-01/func/bold.nii.gz"):
            self.assertIn(part, msg)
        rec = [x for x in self.led.load("run") if x["payload"].get("run_type") == "promotion"]
        self.assertEqual(rec[-1]["id"], r["id"])
        self.assertEqual(rec[-1]["payload"]["dest"], ["derivatives/fmriprep/sub-01/func/bold.nii.gz"])
        self.assertFalse(any(p.name.startswith(f"{self.steward}-promote") for p in self.s.worktrees.iterdir()))
        self.assertNotIn("dureh-promote", run(["git", "remote"], self.ds))

    def test_refuses_without_a_completed_run(self):
        with self.assertRaisesRegex(DurehError, "completed"):
            self.promote()
        self.completed("RUNNING")
        with self.assertRaisesRegex(DurehError, "completed"):
            self.promote()

    def test_refuses_paths_outside_checkpoints_and_the_named_dataset(self):
        self.completed()
        (self.proj / "rawbids").mkdir()
        (self.proj / "rawbids" / "x.nii.gz").write_bytes(b"raw")
        bad = [dict(source="rawbids/x.nii.gz"), dict(source="checkpoints/../rawbids/x.nii.gz"),
               dict(dest="../escape.nii.gz"), dict(dest=".datalad/config"), dict(dataset="../sourcedata"),
               dict(dataset="fmriprep/sub")]
        for kw in bad:
            with self.assertRaises(DurehError, msg=str(kw)):
                self.promote(**kw)
        self.assertEqual(run(["git", "status", "--porcelain"], self.ds), "")

    def test_refuses_a_symlink_that_leaves_checkpoints(self):
        self.completed()
        (self.proj / "rawbids").mkdir()
        (self.proj / "rawbids" / "x.nii.gz").write_bytes(b"raw")
        (self.ck / "link.nii.gz").symlink_to(self.proj / "rawbids" / "x.nii.gz")
        with self.assertRaisesRegex(DurehError, "outside checkpoints"):
            self.promote(source="checkpoints/fmriprep/sub-01/func")

    def test_cli_runs_as_the_bound_agent_only(self):
        import contextlib
        import io

        from dureh_core import cli

        self.completed()
        self.led.event("agent.started", "data-steward", self.steward, {"agent_id": "a-ds", "brief": "0" * 16,
                                                                       "bound_by": "brief"})
        argv = ["--site", str(self.site_root), "promote", "--wi", self.steward, "--run", self.run_id,
                "--source", "checkpoints/fmriprep/sub-01/func/bold.nii.gz", "--dataset", "fmriprep"]
        os.environ.update(DUREH_ACTOR="data-steward", DUREH_AGENT_ID="a-other")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(argv), 2)
        self.assertIn("bound to no work item", err.getvalue())
        os.environ["DUREH_AGENT_ID"] = "a-ds"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(argv), 0)
        self.assertTrue((self.ds / "sub-01" / "func" / "bold.nii.gz").is_file())

    def test_refuses_other_roles_overwrite_and_dirty_dataset(self):
        self.completed()
        with self.assertRaisesRegex(DurehError, "data-steward"):
            self.promote(actor="py-writer")
        self.promote()
        with self.assertRaisesRegex(DurehError, "already exists"):
            self.promote()
        (self.ds / "stray.txt").write_text("x")
        with self.assertRaisesRegex(DurehError, "not clean"):
            self.promote(dest="sub-01/func/other.nii.gz")


if __name__ == "__main__":
    unittest.main()
