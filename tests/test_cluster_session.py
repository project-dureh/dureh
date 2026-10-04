"""Cluster profile, session runtime seam, day job plan, and the submit check against the working day."""
import contextlib
import io
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

from helpers import SiteCase

from dureh_core import cluster, ops, sessions, workday
from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.records import Ledger
from dureh_core.util import DurehError

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=timezone(timedelta(hours=-4)))

SINFO = """general*|(null)|120|11-00:00:00
gpu|gpu:a100:4(S:0-1)|3|2-00:00:00
gpu|gpu:l40:8(S:0-1)|2|2-00:00:00
volta-gpu|gpu:tesla_v100-sxm2-16gb:4|5|6-00:00:00
"""
ASSOC = "rc_lab_pi|normal,gpu_access\nrc_lab_pi|normal\nother_acct|interact\n"


class ClusterSession(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        self.config(env={"path_prepend": [str(self.fake)]})

    def config(self, **keys):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg.update(keys)
        (self.site_root / "config.json").write_text(json.dumps(cfg, indent=2))
        self.s = self.site()
        return cfg

    def tool(self, name, body):
        (self.fake / name).write_text("#!/bin/sh\n" + body)
        (self.fake / name).chmod(0o755)

    def cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--site", str(self.site_root), *args])
        return rc, buf.getvalue()

    # ------------------------------------------------------------ cluster
    def test_detect_parses_sinfo_and_sacctmgr(self):
        self.tool("sinfo", f"cat <<'X'\n{SINFO}X\n")
        self.tool("sacctmgr", f"echo \"$@\" > {self.tmp}/acct-args\ncat <<'X'\n{ASSOC}X\n")
        os.environ["USER"] = "tester"
        p = cluster.detect(self.s)
        self.assertEqual(p["scheduler"], "slurm")
        self.assertEqual(p["partitions"]["general"], {"nodes": 120, "time_limit": "11-00:00:00", "default": True,
                                                      "gpus": []})
        gpu = p["partitions"]["gpu"]
        self.assertEqual(gpu["nodes"], 5)
        self.assertEqual(gpu["gpus"], [{"type": "a100", "per_node": 4, "nodes": 3},
                                       {"type": "l40", "per_node": 8, "nodes": 2}])
        self.assertEqual(p["partitions"]["volta-gpu"]["gpus"][0]["type"], "tesla_v100-sxm2-16gb")
        self.assertEqual(p["account"], "rc_lab_pi")
        self.assertEqual(p["accounts"], ["rc_lab_pi", "other_acct"])
        self.assertEqual(p["qos"], ["normal", "gpu_access", "interact"])
        self.assertIn("user=tester", (self.tmp / "acct-args").read_text())

    def test_detect_write_keeps_key_order_and_extra_keys(self):
        self.tool("sinfo", f"cat <<'X'\n{SINFO}X\n")
        self.tool("sacctmgr", "exit 1\n")
        self.config(cluster={"container_runtime": "apptainer"})
        before = list(json.loads((self.site_root / "config.json").read_text()))
        self.assertEqual(cluster.profile(self.s)["partitions"], {})
        rc, out = self.cli("site", "cluster", "detect", "--write")
        self.assertEqual(rc, 0)
        cfg = json.loads((self.site_root / "config.json").read_text())
        self.assertEqual(list(cfg), before)
        self.assertEqual(cfg["cluster"]["container_runtime"], "apptainer")
        self.assertIsNone(cfg["cluster"]["account"])
        self.assertIn("gpu", cfg["cluster"]["partitions"])
        rc, out = self.cli("site", "cluster", "show")
        self.assertEqual(json.loads(out)["partitions"]["general"]["nodes"], 120)

    def test_detect_without_sinfo_fails_and_defaults(self):
        self.config(env={"path_prepend": [str(self.fake)]})
        os.environ["PATH"] = str(self.fake)
        with self.assertRaisesRegex(DurehError, "cannot detect the cluster"):
            cluster.detect(self.s)
        self.assertEqual(cluster.profile(self.s), {"scheduler": "slurm", "partitions": {}})

    def test_parse_time(self):
        self.assertEqual(cluster.parse_time("90"), timedelta(minutes=90))
        self.assertEqual(cluster.parse_time("04:00:00"), timedelta(hours=4))
        self.assertEqual(cluster.parse_time("1-12"), timedelta(days=1, hours=12))
        self.assertEqual(cluster.parse_time("2-00:30"), timedelta(days=2, minutes=30))
        self.assertIsNone(cluster.parse_time("infinite"))
        self.assertEqual(cluster.hms(7.5), "07:30:00")

    # ------------------------------------------------------------ session runtime
    def test_runtime_default_argv_and_template(self):
        rt = sessions.get(self.s)
        self.assertEqual(rt.name, "claude-code")
        self.assertTrue(rt.talks_to_director)
        self.assertEqual(rt.launch_command(self.s, "desk"), ["claude", "remote-control", "--permission-mode", "auto"])
        self.config(session={"command": ["/opt/bin/claude", "--verbose"],
                             "options": {"model": "opus"},
                             "flags": {"model": "--model"}})
        rt = sessions.get(self.s)
        self.assertEqual(rt.launch_command(self.s, "desk"),
                         ["/opt/bin/claude", "--verbose", "--permission-mode", "auto", "--model", "opus"])
        self.assertTrue(any("not on PATH" in p for p in rt.check(self.s)))
        self.config(session={"options": {"effort": "high"}})
        with self.assertRaisesRegex(DurehError, "effort have no flag"):
            sessions.get(self.s).launch_command(self.s, "desk")

    def test_runtime_check_finds_command_on_path(self):
        self.tool("claude", "exit 0\n")
        self.assertEqual(sessions.get(self.s).check(self.s), [])

    def test_unknown_runtime_names_available(self):
        self.config(session={"runtime": "other-cli"})
        with self.assertRaisesRegex(DurehError, "unknown session runtime 'other-cli'.*available: claude-code"):
            sessions.get(self.s)

    # ------------------------------------------------------------ session plan
    def test_session_plan_prints_script(self):
        self.config(session={"slurm": {"cpus": 4, "mem": "16g"}},
                    cluster={"partitions": {"general": {"nodes": 1, "time_limit": "11-00:00:00", "default": True}}})
        rc, out = self.cli("site", "session", "plan")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("#!/bin/bash\n"))
        self.assertIn("#SBATCH --time=24:00:00\n", out)  # 8 h day + 16 h overtime allowance, capped at 24 h
        self.assertIn("#SBATCH --account=acct-x\n", out)
        self.assertIn("#SBATCH --partition=general\n", out)
        self.assertIn("#SBATCH --cpus-per-task=4\n", out)
        self.assertIn("#SBATCH --mem=16g\n", out)
        self.assertNotIn("--gres", out)
        self.assertIn(f"#SBATCH --chdir={self.site_root}\n", out)
        self.assertIn(f"#SBATCH --output={self.site_root}/logs/sessions/%x-%j.out", out)
        self.assertIn(f"export DUREH_SITE={self.site_root}", out)
        self.assertTrue(out.rstrip().endswith("exec claude remote-control --permission-mode auto"))
        self.assertFalse((self.site_root / "logs" / "sessions").exists(), "a plan creates nothing")

    def test_session_plan_wall_time_from_overtime_allowance(self):
        self.config(workday={"overtime_hours": 2})
        self.assertIn("#SBATCH --time=10:00:00\n", sessions.plan(self.s))
        self.config(workday={"hours": 6, "overtime_hours": 0})
        self.assertIn("#SBATCH --time=06:00:00\n", sessions.plan(self.s))
        self.config(workday={"hours": 10, "overtime_hours": 20})
        self.assertIn("#SBATCH --time=24:00:00\n", sessions.plan(self.s))
        self.config(workday={"overtime_hours": 2},
                    cluster={"partitions": {"general": {"time_limit": "04:00:00"}}})
        self.assertIn("time limit 04:00:00 is shorter", sessions.plan(self.s))

    # ------------------------------------------------------------ submit check against the day
    def _on_duty(self):
        os.environ.pop("CLAUDECODE", None)
        os.environ["DUREH_USAGE_CACHE"] = str(self.tmp / "usage-cache.json")
        os.environ["DUREH_FAKE_NOW"] = T0.isoformat()
        (self.tmp / "transcripts").mkdir()
        self.config(workday={"transcripts": [str(self.tmp / "transcripts")]})
        workday.switch(self.s, True, "test")
        workday.plan(self.s, "desk", "goal", "plan")
        workday.approve(self.s, "desk", "ok")  # 09:00-17:00 -04:00
        os.environ["DUREH_FAKE_NOW"] = (T0 + timedelta(hours=1)).isoformat()

    def _job(self):
        w = wf.wi_new(self.s, "desk", "run", "g", "researcher", "site", ["a"])["id"]
        wf.wi_approve(self.s, "desk", w, "ok")
        Ledger(self.s).event("wi.dispatched", "desk", w, {"role": "researcher"})
        script = self.tmp / "job.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        return w, str(script)

    def _sbatch(self, start):
        self.tool("sbatch", f"echo \"$@\" >> {self.tmp}/sbatch-calls\n"
                            "case \"$*\" in *--test-only*) "
                            f"echo 'sbatch: Job 99 to start at {start} using 1 processors on nodes c1 in partition general' >&2;"
                            " exit 0;; esac\necho 4242\n")

    def test_submit_refused_when_job_ends_after_the_day(self):
        self._on_duty()
        w, script = self._job()
        self._sbatch("2026-10-03T14:00:00")
        n = len(Ledger(self.s).load("run"))
        with self.assertRaisesRegex(DurehError, "after today's end 2026-10-03T17:00:00-04:00"):
            ops.slurm_submit(self.s, "desk", w, script, extra=["--time=04:00:00"])
        self.assertEqual(len(Ledger(self.s).load("run")), n, "no run recorded")
        calls = (self.tmp / "sbatch-calls").read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn("--test-only", calls[0])
        self.assertFalse((self.site_root / "logs" / "slurm" / w).exists())

    def test_submit_allowed_when_job_fits_the_day(self):
        self._on_duty()
        w, script = self._job()
        self._sbatch("2026-10-03T14:00:00")
        self.config(slurm={"account": "acct-x", "partition": "general", "time": "02:30:00"})
        run = ops.slurm_submit(self.s, "desk", w, script)
        self.assertEqual(run["payload"]["job_id"], "4242")
        self.assertEqual(run["payload"]["start_check"]["expected_end"], "2026-10-03T16:30:00-04:00")
        self.assertEqual((run["payload"]["start_check"]["time"], run["payload"]["start_check"]["time_source"]),
                         ("02:30:00", "config"))
        run = ops.slurm_submit(self.s, "desk", w, script, extra=["-t", "1:00:00"])
        self.assertEqual(run["payload"]["start_check"]["expected_end"], "2026-10-03T15:00:00-04:00")
        self.assertEqual(run["payload"]["start_check"]["time_source"], "sbatch-arg")

    def test_submit_check_uses_the_scripts_time_over_config(self):
        # UR-20261003-09: the check estimated the end from config slurm.time and ignored the script's directive
        self._on_duty()
        w, script = self._job()
        self._sbatch("2026-10-03T14:00:00")
        self.config(slurm={"account": "acct-x", "partition": "general", "time": "01:00:00"})
        Path(script).write_text("#!/bin/sh\n#SBATCH --time=04:00:00\necho hi\n")
        with self.assertRaisesRegex(DurehError, r"--time 04:00:00 \(from the script\).*after today's end"):
            ops.slurm_submit(self.s, "desk", w, script)
        self.assertNotIn("--time=01:00:00", (self.tmp / "sbatch-calls").read_text(), "config time is only a fallback")
        Path(script).write_text("#!/bin/sh\n#SBATCH -t 02:00:00\necho hi\n")
        run = ops.slurm_submit(self.s, "desk", w, script)
        self.assertEqual(run["payload"]["start_check"]["expected_end"], "2026-10-03T16:00:00-04:00")
        self.assertEqual((run["payload"]["start_check"]["time"], run["payload"]["start_check"]["time_source"]),
                         ("02:00:00", "script"))
        self.assertNotIn("--time=", " ".join(run["payload"]["command"]))
        run = ops.slurm_submit(self.s, "desk", w, script, extra=["--time=00:30:00"])
        self.assertEqual(run["payload"]["start_check"]["time_source"], "sbatch-arg")
        self.assertEqual(run["payload"]["start_check"]["expected_end"], "2026-10-03T14:30:00-04:00")

    def test_submit_check_unavailable_or_skipped(self):
        self._on_duty()
        w, script = self._job()
        self.tool("sbatch", "case \"$*\" in *--test-only*) echo 'error: no' >&2; exit 1;; esac\necho 4242\n")
        run = ops.slurm_submit(self.s, "desk", w, script, extra=["--time=04:00:00"])
        self.assertEqual(run["payload"]["start_check"], "unavailable")
        self.assertEqual(run["payload"]["job_id"], "4242")
        run = ops.slurm_submit(self.s, "desk", w, script)  # no --time anywhere
        self.assertIn("skipped", run["payload"]["start_check"])

    def test_submit_not_checked_when_workday_off(self):
        w, script = self._job()
        self._sbatch("2026-10-03T23:00:00")
        run = ops.slurm_submit(self.s, "desk", w, script, extra=["--time=10:00:00"])
        self.assertNotIn("start_check", run["payload"])
        self.assertNotIn("--test-only", (self.tmp / "sbatch-calls").read_text())

    # ------------------------------------------------------------ job directory (UR-20261003-08)
    def test_submit_runs_in_the_wi_target_root(self):
        mod = self.make_module()
        w = wf.wi_new(self.s, "desk", "run", "g", "researcher", "modules/demo", ["a"])["id"]
        wf.wi_approve(self.s, "desk", w, "ok")
        Ledger(self.s).event("wi.dispatched", "desk", w, {"role": "researcher"})
        self.tool("sbatch", f"echo \"$@\" >> {self.tmp}/sbatch-calls\necho 4243\n")
        script = self.tmp / "job.sh"
        script.write_text("#!/bin/sh\n#SBATCH -o checkpoints/phase-d/%x-%j.out\necho hi\n")
        run = ops.slurm_submit(self.s, "desk", w, str(script))
        p = run["payload"]
        self.assertEqual(p["chdir"], str(mod))
        self.assertIn(f"--chdir={mod}", (self.tmp / "sbatch-calls").read_text())
        self.assertEqual(p["log_out"], f"{mod}/checkpoints/phase-d/%x-%j.out")
        self.assertEqual(p["log_err"], p["log_out"])  # stderr follows -o when no -e is given
        own = self.tmp / "own.sh"
        own.write_text(f"#!/bin/sh\n#SBATCH --chdir={self.tmp}\n#SBATCH --output=o.out\n#SBATCH -e e.err\necho hi\n")
        p = ops.slurm_submit(self.s, "desk", w, str(own))["payload"]
        self.assertEqual(p["chdir"], str(self.tmp))
        self.assertNotIn("--chdir", (self.tmp / "sbatch-calls").read_text().splitlines()[-1].replace(str(own), ""))
        self.assertEqual((p["log_out"], p["log_err"]), (f"{self.tmp}/o.out", f"{self.tmp}/e.err"))

    def test_submit_site_target_runs_in_site_root_with_our_logs(self):
        w, script = self._job()
        self.tool("sbatch", f"echo \"$@\" >> {self.tmp}/sbatch-calls\necho 4244\n")
        p = ops.slurm_submit(self.s, "desk", w, script)["payload"]
        self.assertEqual(p["chdir"], str(self.site_root))
        self.assertEqual(p["log_out"], f"{self.site_root}/logs/slurm/{w}/%x-%j.out")
        p = ops.slurm_submit(self.s, "desk", w, script, extra=["-D", str(self.tmp)], dry_run=True)["payload"]
        self.assertEqual(p["chdir"], str(self.tmp))
        self.assertEqual(p["command"].count(f"--chdir={self.site_root}"), 0)
