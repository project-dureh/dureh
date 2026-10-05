"""Release 27: `dureh verify --wi` runs the declared checks as ONE scheduler job (config checks.runner), waits for
it and records the job id and state; `--local` is the escape hatch. Evidence: the company's session job reached
MaxRSS 16.8 GB of 16 GB from ~20 module test runs inside the session (2026-10-04). Fake sbatch/sacct only."""
import json
import os
import unittest
from pathlib import Path

from helpers import SiteCase

from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.records import Ledger
from dureh_core.util import DurehError


class VerifyChecksJob(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        # sbatch: logs its arguments, runs the job script at once in --chdir (like a node would), prints a job id
        self.tool("sbatch", f"echo \"$@\" >> {self.fake}/sbatch.args\n"
                            "dir=.; for a in \"$@\"; do case \"$a\" in --chdir=*) dir=${a#--chdir=};; esac; done\n"
                            f"if [ ! -e {self.fake}/norun ]; then (cd \"$dir\" && bash \"${{@: -1}}\" > /dev/null 2>&1); fi\n"
                            "echo 5100\n")
        self.tool("sacct", f"cat {self.fake}/sacct.out 2>/dev/null || echo 'COMPLETED|0:0|00:00:02'\n")
        self.tool("scancel", f"echo \"$@\" >> {self.fake}/scancel.args\n")
        self.s = self.site()
        self.led = Ledger(self.s)
        self.bundle = wf.wi_new(self.s, "desk", "Bundle", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", self.bundle, "ok")
        self.led.event("wi.dispatched", "desk", self.bundle, {"role": "pm-modules", "brief": "0" * 16, "by": "desk"})
        self.wi = self.item(checks=["python3 -m unittest discover -s tests -q", "test -f src/demo.py && echo seen"],
                            outputs=["src/demo.py"])

    def item(self, **kw):
        wi = wf.wi_new(self.s, "desk", "Add g()", "g", "py-writer", "modules/demo", ["ok"], parent=self.bundle, **kw)["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        wf.dispatch_brief(self.s, wi, "pm-modules")  # creates the WI worktree
        self.led.event("wi.dispatched", "desk", wi, {"role": "py-writer", "brief": "1" * 16, "by": "desk"})
        wf.wi_stage(self.s, "py-writer", wi, "done")
        return wi

    def tool(self, name, body):
        (self.fake / name).write_text("#!/bin/bash\n" + body)
        (self.fake / name).chmod(0o755)

    def config(self, **patch):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg.update(patch)
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()

    def with_sbatch(self, **checks):
        self.config(env={"path_prepend": [str(self.fake)]}, checks=checks)

    def runs(self):
        return [r for r in self.led.load("run") if r["type"] == "verify"]

    # ------------------------------------------------------------ resolution
    def test_default_is_local_without_sbatch_and_slurm_with_it(self):
        # this machine may be a cluster login node: drop every PATH entry that holds an sbatch
        os.environ["PATH"] = os.pathsep.join(d for d in os.environ["PATH"].split(os.pathsep)
                                             if not os.path.exists(os.path.join(d, "sbatch")))
        self.config(checks={})  # the test helper pins local; here the default is under test
        self.assertEqual(wf.checks_runner(self.s), "local", "no sbatch on PATH")
        self.with_sbatch()
        self.assertEqual(wf.checks_runner(self.s), "slurm", "slurm settings and sbatch on the site's PATH")
        self.config(slurm={"account": None, "partition": None, "time": None}, cluster=None)
        self.assertEqual(wf.checks_runner(self.s), "local", "no scheduler configured")
        self.config(cluster={"scheduler": "slurm", "partitions": {"general": {}}})
        self.assertEqual(wf.checks_runner(self.s), "slurm", "a cluster profile counts")
        self.config(checks={"runner": "local"})
        self.assertEqual(wf.checks_runner(self.s), "local", "config wins")
        self.config(checks={"runner": "bogus"})
        with self.assertRaisesRegex(DurehError, "checks.runner"):
            wf.checks_runner(self.s)

    # ------------------------------------------------------------------ job
    def test_checks_job_logs_follow_slurm_log_dir(self):
        """Release 32: config slurm.log_dir, and a checks.sbatch_args -o never moves the job's logs."""
        self.config(slurm={"account": "acct-x", "log_dir": "scratch/joblogs"})
        self.with_sbatch(sbatch_args=["-o", "verify.out"], interval=1)
        r = wf.verify_wi(self.s, "checker", self.wi)
        self.assertEqual(r["result"], "pass", r)
        d = self.site_root / "scratch" / "joblogs" / self.wi
        a = (self.fake / "sbatch.args").read_text().split()
        self.assertTrue(a[-1].startswith(str(d / "verify-")))
        self.assertEqual(a[-3:-1], [f"--output={r['job']['log_out']}", f"--error={r['job']['log_err']}"])
        self.assertTrue(r["job"]["log_out"].startswith(str(d / "verify-")))

    def test_checks_run_as_one_job_and_the_record_keeps_it(self):
        self.with_sbatch(time="00:20:00", sbatch_args=["--mem=8G"], interval=1)
        r = wf.verify_wi(self.s, "checker", self.wi)
        self.assertEqual(r["result"], "pass", r)
        self.assertEqual(r["runner"], "slurm")
        self.assertEqual([c["exit"] for c in r["checks"]], [0, 0])
        self.assertIn("seen", r["checks"][1]["tail"])
        self.assertIsInstance(r["checks"][0]["seconds"], float)
        self.assertEqual((r["job"]["job_id"], r["job"]["state"]), ("5100", "COMPLETED"))
        self.assertIn("scheduler job 5100: COMPLETED", r["note"])
        args = (self.fake / "sbatch.args").read_text().splitlines()
        self.assertEqual(len(args), 1, "one job per verification")
        a = args[0].split()
        self.assertIn(f"--job-name=dureh-verify-{self.wi}", a)
        self.assertIn("--time=00:20:00", a)
        self.assertIn("--mem=8G", a)
        self.assertIn("--account=acct-x", a)
        self.assertIn(f"--chdir={self.site_root / 'worktrees' / self.wi}", a, "cwd = the WI worktree")
        self.assertTrue(a[-1].startswith(str(self.site_root / "logs" / "slurm" / self.wi / "verify-")))
        script = Path(a[-1]).read_text()
        self.assertIn(f"DUREH_WI={self.wi}", script)
        self.assertIn("DUREH_WORK_ROOT=", script)
        self.assertIn("python3 -m unittest discover -s tests -q", script)
        run = self.runs()[-1]["payload"]
        self.assertEqual(run["job"]["job_id"], "5100")
        self.assertEqual(run["runner"], "slurm")
        self.assertEqual(self.led.wi_status(self.wi)["status"], "verified")

    def test_time_falls_back_to_slurm_time_then_default(self):
        self.config(env={"path_prepend": [str(self.fake)]}, checks={"interval": 1},
                    slurm={"account": "acct-x", "partition": "general", "time": "06:00:00"})
        wf.verify_wi(self.s, "checker", self.wi)
        self.assertIn("--time=06:00:00", (self.fake / "sbatch.args").read_text())
        self.config(slurm={"account": "acct-x", "partition": "general"})
        wf.verify_wi(self.s, "checker", self.item(checks=["true"]))
        self.assertIn("--time=01:00:00", (self.fake / "sbatch.args").read_text().splitlines()[-1])

    def test_job_that_never_ran_the_checks_fails_with_its_state(self):
        self.with_sbatch(interval=1)
        (self.fake / "norun").write_text("")
        (self.fake / "sacct.out").write_text("OUT_OF_MEMORY|0:125|00:00:30\n")
        r = wf.verify_wi(self.s, "checker", self.wi)
        self.assertEqual(r["result"], "fail")
        self.assertEqual([c["exit"] for c in r["checks"]], ["not-run", "not-run"])
        self.assertIn("OUT_OF_MEMORY", r["checks"][0]["tail"])
        self.assertEqual(r["job"]["state"], "OUT_OF_MEMORY")
        self.assertEqual(self.led.wi_status(self.wi)["status"], "rework")

    def test_wait_timeout_cancels_the_job(self):
        self.with_sbatch(interval=1, wait=1)
        (self.fake / "norun").write_text("")
        (self.fake / "sacct.out").write_text("PENDING|0:0|00:00:00\n")
        r = wf.verify_wi(self.s, "checker", self.wi)
        self.assertEqual(r["result"], "fail")
        self.assertIn("cancelled", r["job"]["state"])
        self.assertEqual((self.fake / "scancel.args").read_text().split(), ["5100"])

    def test_failed_check_in_the_job_is_a_failed_verification(self):
        self.with_sbatch(interval=1)
        r = wf.verify_wi(self.s, "checker", self.item(checks=["false", "true"]))
        self.assertEqual(r["result"], "fail")
        self.assertEqual([c["exit"] for c in r["checks"]], [1, 0])

    def test_sbatch_failure_records_nothing(self):
        self.tool("sbatch", "echo 'sbatch: error: invalid partition' >&2; exit 1\n")
        self.with_sbatch()
        with self.assertRaisesRegex(DurehError, "sbatch failed.*--local"):
            wf.verify_wi(self.s, "checker", self.wi)
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.led.wi_status(self.wi)["status"], "staged")

    # ----------------------------------------------------------------- local
    def test_local_escape_hatch_and_cli(self):
        self.with_sbatch()
        r = wf.verify_wi(self.s, "checker", self.wi, local=True)
        self.assertEqual((r["result"], r["runner"], r.get("runner_override")), ("pass", "local", True))
        self.assertFalse((self.fake / "sbatch.args").exists(), "no job was submitted")
        self.assertNotIn("job", r)
        # the CLI flag reaches it; without --local the slurm path is taken and reported
        import contextlib
        import io
        other = self.item(checks=["true"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--site", str(self.site_root), "verify", "--wi", other, "--actor", "checker"])
        out = json.loads(buf.getvalue())
        self.assertEqual((rc, out["runner"], out["job"]["job_id"], out["job"]["state"]), (0, "slurm", "5100", "COMPLETED"))

    def test_no_declared_checks_submits_nothing(self):
        wi = self.item()
        self.with_sbatch()
        r = wf.verify_wi(self.s, "checker", wi)
        self.assertEqual((r["result"], r["runner"]), ("pass", "slurm"))
        self.assertFalse((self.fake / "sbatch.args").exists())


if __name__ == "__main__":
    unittest.main()
