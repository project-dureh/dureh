"""`dureh slurm submit --each/--after/--resubmit-failed` (release 26): one job per subject as one Slurm array,
step dependencies, resubmission of failed items, per-item states. Fake sbatch/sacct only; never real Slurm."""
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path

from dureh_core import ops
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError
from tests.helpers import SiteCase

SUBJ = ["sub-01", "sub-02", "sub-03", "sub-04", "sub-05"]


class Each(SiteCase):
    def setUp(self):
        super().setUp()
        manifest = self.package / "startup.yaml"
        manifest.write_text(manifest.read_text()
                            .replace("managers: [pm-modules]", "managers: [pm-modules, pm-projects]")
                            .replace("workers: [py-writer,", "workers: [data-steward, pipeline-runner, py-writer,"))
        self.install()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        # sbatch: logs args; --test-only prints a start time; else prints the next job id (4300, 4301, ...)
        self.tool("sbatch", f"echo \"$@\" >> {self.fake}/sbatch.args\n"
                            "case \"$*\" in *--test-only*) echo 'sbatch: Job 9 to start at 2026-10-03T10:00:00 using 1"
                            " processors on nodes c1 in partition general' >&2; exit 0;; esac\n"
                            f"n=$(cat {self.fake}/n 2>/dev/null || echo 4300); echo $((n+1)) > {self.fake}/n\n"
                            "echo \"$n\"\n")
        # sacct: prints the file sacct-<job> (lines like `4300_0|COMPLETED|0:0`)
        self.tool("sacct", "job=''; prev=''; for a in \"$@\"; do [ \"$prev\" = -j ] && job=$a; prev=$a; done\n"
                           f"echo \"$@\" >> {self.fake}/sacct.args\ncat {self.fake}/sacct-$job 2>/dev/null; exit 0\n")
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["env"] = {"path_prepend": [str(self.fake)]}
        cfg["containers"] = {"module": ""}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        self.led = Ledger(self.s)
        self.proj = self.site_root / "projects" / "lna"
        (self.proj / "checkpoints").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.proj)], check=True)
        b = wf.wi_new(self.s, "desk", "Bundle", "g", "pm-projects", "projects/lna", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", b, "ok")
        self.led.event("wi.dispatched", "desk", b, {"role": "pm-projects", "brief": "0" * 16, "by": "desk"})
        self.w = wf.wi_new(self.s, "desk", "Run", "g", "pipeline-runner", "projects/lna", ["ok"], parent=b)["id"]
        wf.wi_approve(self.s, "desk", self.w, "ok")
        self.led.event("wi.dispatched", "desk", self.w, {"role": "pipeline-runner", "brief": "0" * 16, "by": "desk"})
        self.script = self.proj / "checkpoints" / "step1.sh"
        self.script.write_text("#!/bin/bash\n#SBATCH --time=00:05:00\necho \"ITEM=$DUREH_ITER IDX=$DUREH_ITER_INDEX\"\n")

    def tool(self, name, body):
        (self.fake / name).write_text("#!/bin/sh\n" + body)
        (self.fake / name).chmod(0o755)

    def sacct(self, job, *lines):
        (self.fake / f"sacct-{job}").write_text("".join(f"{x}\n" for x in lines))

    def calls(self):
        return [c for c in (self.fake / "sbatch.args").read_text().splitlines() if "--test-only" not in c]

    def submit(self, **kw):
        return ops.slurm_submit(self.s, "pipeline-runner", self.w, str(kw.pop("script", self.script)), **kw)

    # ------------------------------------------------------------------ items
    def test_items_parsing_and_refusals(self):
        self.assertEqual(ops.parse_items(self.s, "sub-01,sub-02 sub-03")[0], ["sub-01", "sub-02", "sub-03"])
        lst = self.proj / "checkpoints" / "subjects.txt"
        lst.write_text("# pilot\nsub-01\n\n  sub-02  \n# sub-09 excluded\nsub-JDRS003LNA\n")
        items, f = ops.parse_items(self.s, f"@{lst}")
        self.assertEqual((items, f), (["sub-01", "sub-02", "sub-JDRS003LNA"], str(lst)))
        self.assertEqual(ops.parse_items(self.s, "JDRS001LNA,JDRS002LNA")[0], ["JDRS001LNA", "JDRS002LNA"])
        self.assertEqual(ops.parse_items(self.s, "dim-20,SIGMA,ses-1_sub-01,Sub-01,a_b-c")[0],
                         ["dim-20", "SIGMA", "ses-1_sub-01", "Sub-01", "a_b-c"], "non-sub- items are plain, unparsed")
        self.assertEqual(ops.items_level(["dim-20", "SIGMA"]), "plain")
        for bad, msg in (("sub-01,../x", "invalid"), ("-x", "invalid"), ("sub 01;rm", "invalid"),
                         ("sub-01,sub-01", "duplicate"), (" , ", "at least one"), ("_a", "invalid"),
                         ("sub-01,sub-02_ses-1", "mixes session and subject"), ("sub-01,plain", "mixes plain"),
                         ("sub-01_run-1", "starts with `sub-`"), ("sub-01_ses-1_acq-x", "starts with `sub-`"),
                         ("sub-01_ses-a.b", "starts with `sub-`"), ("sub-_ses-1", "starts with `sub-`"),
                         ("sub-01-x", "starts with `sub-`"), ("sub-01,dim-20", "mixes plain and subject")):
            with self.assertRaisesRegex(DurehError, msg):
                ops.parse_items(self.s, bad)
        out = self.tmp / "outside.txt"
        out.write_text("sub-01\n")
        with self.assertRaisesRegex(DurehError, "inside the site"):
            ops.parse_items(self.s, f"@{out}")
        with self.assertRaisesRegex(DurehError, "missing"):
            ops.parse_items(self.s, f"@{self.proj}/nope.txt")

    # ----------------------------------------------------------------- submit
    def test_each_submits_one_array_without_cap_and_maps_items(self):
        run = self.submit(each=",".join(SUBJ))
        p = run["payload"]
        cmd = self.calls()[-1]
        self.assertIn("--array=0-4 ", cmd + " ")
        self.assertNotIn("%", cmd.split("--array=")[1].split()[0], "no concurrency cap by default")
        logs = self.site_root / "logs" / "slurm" / self.w
        self.assertIn(f"--output={logs}/%x-%A_%a.out", cmd)
        self.assertIn(f"--error={logs}/%x-%A_%a.err", cmd)
        self.assertEqual(p["array"]["job_id"], "4300")
        self.assertEqual((p["array"]["items"], p["array"]["count"]), (SUBJ, 5))
        items_file = Path(p["array"]["items_file"])
        self.assertEqual(items_file.parent, logs)
        self.assertEqual(items_file.read_text().split(), SUBJ)
        wrapper = Path(p["array"]["wrapper"])
        self.assertEqual(cmd.split()[-1], str(wrapper))
        self.assertIn("#SBATCH --time=00:05:00", wrapper.read_text())
        env = dict(os.environ, SLURM_ARRAY_TASK_ID="2")
        out = subprocess.run(["bash", str(wrapper)], capture_output=True, text=True, env=env)
        self.assertEqual(out.stdout.strip(), "ITEM=sub-03 IDX=2")
        env["SLURM_ARRAY_TASK_ID"] = "7"
        self.assertEqual(subprocess.run(["bash", str(wrapper)], capture_output=True, env=env).returncode, 98)
        self.assertEqual(p["script"], str(self.script), "the record keeps the original script")

    def test_each_file_max_parallel_and_array_refusal(self):
        lst = self.proj / "checkpoints" / "subjects.txt"
        lst.write_text("sub-01\nsub-02\nsub-03\n")
        run = self.submit(each=f"@{lst}", max_parallel=2)
        self.assertIn("--array=0-2%2", self.calls()[-1])
        self.assertEqual((run["payload"]["each_file"], run["payload"]["max_parallel"]), (str(lst), 2))
        with self.assertRaisesRegex(DurehError, "needs --each"):
            self.submit(max_parallel=2)
        with self.assertRaisesRegex(DurehError, "positive"):
            self.submit(each="sub-01", max_parallel=0)
        with self.assertRaisesRegex(DurehError, "remove --array"):
            self.submit(each="sub-01", extra=["--array=0-3"])
        own = self.proj / "checkpoints" / "own.sh"
        own.write_text("#!/bin/bash\n#SBATCH --array=0-9\necho\n")
        with self.assertRaisesRegex(DurehError, "remove --array"):
            self.submit(script=own, each="sub-01")
        dry = self.submit(each="sub-01,sub-02", dry_run=True)
        self.assertEqual(dry["payload"]["status"], "not-run")
        self.assertFalse(Path(dry["payload"]["array"]["items_file"]).exists(), "a dry run writes nothing")

    def test_failed_sbatch_removes_items_and_wrapper(self):
        self.tool("sbatch", "echo 'sbatch: error: bad' >&2; exit 1\n")
        with self.assertRaisesRegex(DurehError, "sbatch failed"):
            self.submit(each="sub-01")
        logs = self.site_root / "logs" / "slurm" / self.w
        self.assertEqual(list(logs.glob("each-*")), [])

    # ------------------------------------------------------------------ after
    def test_after_adds_afterok_per_run_and_refuses_unknown_or_failed(self):
        s1 = self.submit(each=",".join(SUBJ))
        single = self.submit()
        s2 = self.submit(each=",".join(SUBJ), after=[f"{s1['id']},{single['id']}"])
        self.assertIn("--dependency=afterok:4300:4301", self.calls()[-1])
        self.assertEqual(s2["payload"]["after"], [{"run": s1["id"], "job_id": "4300"},
                                                  {"run": single["id"], "job_id": "4301"}])
        group = self.submit(after=[s2["id"]])  # a group-level step: one job after the whole array
        self.assertIn("--dependency=afterok:4302", self.calls()[-1])
        self.assertNotIn("--array", self.calls()[-1])
        self.assertEqual(group["payload"]["after"][0]["run"], s2["id"])
        with self.assertRaisesRegex(DurehError, "no run record"):
            self.submit(after=["run-nope"])
        with self.assertRaisesRegex(DurehError, "invalid run id"):
            self.submit(after=["../x"])
        dry = self.submit(dry_run=True)
        with self.assertRaisesRegex(DurehError, "no submitted scheduler job"):
            self.submit(after=[dry["id"]])
        self.led.event("run.status", "desk", self.w, {"run": single["id"], "scheduler_state": "CANCELLED by 1"})
        with self.assertRaisesRegex(DurehError, "known CANCELLED"):
            self.submit(after=[single["id"]])
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|COMPLETED|0:0", "4300_2|FAILED|1:0",
                   "4300_3|COMPLETED|0:0", "4300_4|COMPLETED|0:0")
        ops.slurm_wait(self.s, "pipeline-runner", s1["id"], timeout=5, interval=1)
        with self.assertRaisesRegex(DurehError, "known FAILED.*--resubmit-failed"):
            self.submit(each="sub-01", after=[s1["id"]])
        again = ops.slurm_resubmit_failed(self.s, "pipeline-runner", s1["id"])
        with self.assertRaisesRegex(DurehError, f"use --after {again['id']}"):
            self.submit(each="sub-01", after=[s1["id"]])

    # ----------------------------------------------------------- status/wait
    def test_status_and_wait_report_per_item_and_overall(self):
        run = self.submit(each="a,b,c")
        self.sacct("4300", "4300_[0-2]|PENDING|0:0")
        ev = ops.slurm_status(self.s, "pipeline-runner", run["id"])
        self.assertEqual(ev["payload"]["scheduler_state"], "PENDING")
        self.assertEqual(ev["payload"]["items"], {"a": "PENDING", "b": "PENDING", "c": "PENDING"})
        self.assertFalse(ev["payload"]["final"])
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|RUNNING|0:0", "4300_[2]|PENDING|0:0")
        self.assertEqual(ops.slurm_status(self.s, "desk", run["id"])["payload"]["scheduler_state"], "RUNNING")
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_0.batch|COMPLETED|0:0", "4300_1|COMPLETED|0:0",
                   "4300_2|COMPLETED|0:0")
        ev = ops.slurm_wait(self.s, "pipeline-runner", run["id"], timeout=5, interval=1)
        self.assertEqual(ev["payload"]["scheduler_state"], "COMPLETED")
        self.assertTrue(ev["payload"]["final"])
        self.assertEqual(ev["payload"]["item_states"], {"a": "COMPLETED", "b": "COMPLETED", "c": "COMPLETED"})
        n = len([e for e in self.led.events(self.w) if e["type"] == "run.status"])
        again = ops.slurm_status(self.s, "pipeline-runner", run["id"])
        self.assertEqual(again["id"], ev["id"], "final per-item states are recorded once")
        self.assertEqual(len([e for e in self.led.events(self.w) if e["type"] == "run.status"]), n)
        from dureh_core.promote import _completed_run
        self.assertEqual(_completed_run(self.led, run["id"], "projects/lna")["id"], run["id"])

    def test_overall_state_rules(self):
        self.assertEqual(ops.overall_state(["COMPLETED", "COMPLETED"]), "COMPLETED")
        self.assertEqual(ops.overall_state(["COMPLETED", "TIMEOUT"]), "FAILED")
        self.assertEqual(ops.overall_state(["FAILED", "RUNNING"]), "RUNNING")
        self.assertEqual(ops.overall_state(["FAILED", "PENDING"]), "PENDING")
        self.assertEqual(ops.overall_state(["COMPLETED", "UNKNOWN"]), "PENDING")
        run = self.submit(each="a,b")
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|OUT_OF_MEMORY|0:125")
        ev = ops.slurm_wait(self.s, "pipeline-runner", run["id"], timeout=5, interval=1)
        self.assertEqual((ev["payload"]["scheduler_state"], ev["payload"]["failed_items"]), ("FAILED", ["b"]))

    def test_wait_timeout_on_array_is_not_final(self):
        run = self.submit(each="a,b")
        self.sacct("4300", "4300_0|RUNNING|0:0", "4300_1|PENDING|0:0")
        ev = ops.slurm_wait(self.s, "pipeline-runner", run["id"], timeout=0, interval=1)
        self.assertFalse(ev["payload"]["final"])
        self.assertIn("still RUNNING", ev["note"])

    # --------------------------------------------------------------- resubmit
    def test_resubmit_failed_only_the_items_not_completed(self):
        run = self.submit(each=",".join(SUBJ), extra=["--mem=1G"])
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|RUNNING|0:0", "4300_2|FAILED|1:0")
        with self.assertRaisesRegex(DurehError, "not every task has ended"):
            ops.slurm_resubmit_failed(self.s, "pipeline-runner", run["id"])
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|COMPLETED|0:0", "4300_2|FAILED|1:0",
                   "4300_3|CANCELLED by 5|0:0", "4300_4|COMPLETED|0:0")
        fixed = self.proj / "checkpoints" / "step1-fixed.sh"
        fixed.write_text("#!/bin/bash\necho fixed $DUREH_ITER\n")
        again = ops.slurm_resubmit_failed(self.s, "pipeline-runner", run["id"], script=str(fixed))
        p = again["payload"]
        self.assertEqual(p["array"]["items"], ["sub-03", "sub-04"])
        self.assertEqual(p["resubmit_of"], run["id"])
        self.assertEqual(p["resubmit_states"], {"sub-03": "FAILED", "sub-04": "CANCELLED"})
        self.assertEqual(p["script"], str(fixed))
        cmd = self.calls()[-1]
        self.assertIn("--array=0-1", cmd)
        self.assertIn("--mem=1G", cmd, "the original sbatch arguments are reused")
        self.sacct("4301", "4301_0|COMPLETED|0:0", "4301_1|COMPLETED|0:0")
        with self.assertRaisesRegex(DurehError, "nothing to resubmit"):
            ops.slurm_resubmit_failed(self.s, "pipeline-runner", again["id"])
        single = self.submit()
        with self.assertRaisesRegex(DurehError, "not an --each"):
            ops.slurm_resubmit_failed(self.s, "pipeline-runner", single["id"])

    # ------------------------------------------------------------------ purge
    def test_purge_removes_array_task_logs_and_item_list(self):
        run = self.submit(each="a,b,c")
        logs = self.site_root / "logs" / "slurm" / self.w
        mine = [logs / f"dureh-{self.w}-4300_{i}.{x}" for i in range(3) for x in ("out", "err")]
        for f in mine:
            f.write_text("x")
        keep = logs / f"dureh-{self.w}-4399_0.out"
        keep.write_text("another job")
        wf.dispatch_stop(self.s, "desk", self.w, "test")
        out = wf.purge(self.s, "desk", self.w)
        expected = mine + [Path(run["payload"]["array"]["items_file"]), Path(run["payload"]["array"]["wrapper"])]
        self.assertEqual(sorted(out["removed_logs"]), sorted(str(f) for f in expected))
        self.assertTrue(keep.is_file())

    # -------------------------------------------------------------------- cli
    def test_cli_each_after_and_resubmit(self):
        from dureh_core import cli

        base = ["--site", str(self.site_root), "slurm", "submit", "--wi", self.w, "--actor", "pipeline-runner"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main([*base, "--script", str(self.script), "--each", "sub-01", "sub-02,sub-03"]), 0)
        first = [r for r in self.led.load("run") if r["wi"] == self.w][-1]
        self.assertEqual(first["payload"]["array"]["items"], ["sub-01", "sub-02", "sub-03"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([*base, "--script", str(self.script), "--after", first["id"]]), 0)
        self.assertIn("--dependency=afterok:4300", self.calls()[-1])
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|FAILED|1:0", "4300_2|COMPLETED|0:0")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([*base, "--resubmit-failed", first["id"]]), 0)
        last = [r for r in self.led.load("run") if r["wi"] == self.w][-1]
        self.assertEqual((last["payload"]["array"]["items"], last["payload"]["script"]), (["sub-02"], str(self.script)))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(cli.main([*base, "--resubmit-failed", first["id"], "--each", "x"]), 0)
            self.assertNotEqual(cli.main([*base]), 0)

    # ------------------------------------------------------- levels and names
    def run_task(self, run, task):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DUREH_ITER", "CAMRI_"))}
        env["SLURM_ARRAY_TASK_ID"] = str(task)
        return subprocess.run(["bash", run["payload"]["array"]["wrapper"]], capture_output=True, text=True,
                              env=env).stdout.strip()

    def test_levels_and_default_names(self):
        self.script.write_text("#!/bin/bash\necho \"I=$DUREH_ITER B=$DUREH_ITER_BY X=$DUREH_ITER_INDEX "
                               "S=$DUREH_ITER_SUB E=[$DUREH_ITER_SES]\"\n")
        run = self.submit(each="sub-01,sub-02")
        self.assertEqual(run["payload"]["array"]["level"], "subject")
        self.assertEqual(run["payload"]["array"]["env"], ops.DEFAULT_ITEM_ENV)
        self.assertEqual(self.run_task(run, 1), "I=sub-02 B=subject X=1 S=02 E=[]", "an absent session exports empty")
        run = self.submit(each="sub-01_ses-1,sub-01_ses-2,sub-02_ses-1")
        self.assertEqual(run["payload"]["array"]["level"], "session")
        self.assertEqual(self.run_task(run, 1), "I=sub-01_ses-2 B=session X=1 S=01 E=[2]")
        run = self.submit(each="dim-20,JDRS002LNA")
        self.assertEqual(run["payload"]["array"]["level"], "plain")
        self.assertEqual(self.run_task(run, 0), "I=dim-20 B=plain X=0 S= E=[]")

    def test_configured_each_env_names_replace_the_defaults(self):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["slurm"]["each_env"] = {"item": "CAMRI_ITER", "by": "CAMRI_ITER_BY", "index": "CAMRI_ITER_INDEX",
                                    "sub": "CAMRI_SUBJECT", "ses": "CAMRI_SESSION"}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        self.script.write_text("#!/bin/bash\necho \"$CAMRI_ITER $CAMRI_ITER_BY $CAMRI_ITER_INDEX $CAMRI_SUBJECT [$CAMRI_SESSION] "
                               "D=${DUREH_ITER:-unset}${DUREH_ITER_SUB:-unset}\"\n")
        run = self.submit(each="sub-01_ses-1,sub-02_ses-3")
        self.assertEqual(run["payload"]["array"]["env"]["sub"], "CAMRI_SUBJECT")
        self.assertEqual(self.run_task(run, 1), "sub-02_ses-3 session 1 02 [3] D=unsetunset", "only the configured names")
        self.assertNotIn("export DUREH_", Path(run["payload"]["array"]["wrapper"]).read_text())
        cfg["slurm"]["each_env"] = {"item": "CAMRI_ITER", "index": "CAMRI_ITER_INDEX"}  # sub/ses unmapped
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        run = self.submit(each="sub-01")
        text = Path(run["payload"]["array"]["wrapper"]).read_text()
        self.assertFalse(any(w in text for w in ("_SUB", "_SES", "_BY")), "unmapped by/sub/ses are not exported")
        for bad in ({"item": "A"}, {"item": "A", "index": "A"}, {"item": "A", "index": "B", "run": "C"},
                    {"item": "A b", "index": "B"}):
            cfg["slurm"]["each_env"] = bad
            (self.site_root / "config.json").write_text(json.dumps(cfg))
            self.s = self.site()
            with self.assertRaisesRegex(DurehError, "slurm.each_env"):
                self.submit(each="sub-01")

    def test_each_from_bids_lists_subject_or_session_directories(self):
        ds = self.proj / "rawbids"
        for d in ("sub-01/ses-1", "sub-01/ses-2", "sub-02/ses-1", "sub-03"):
            (ds / d).mkdir(parents=True)
        (ds / "sub-04.txt").write_text("not a directory")
        (ds / "derivatives").mkdir()
        run = self.submit(each_from_bids=str(ds))
        self.assertEqual(run["payload"]["array"]["items"], ["sub-01", "sub-02", "sub-03"])
        self.assertEqual(run["payload"]["each_from_bids"], {"dataset": str(ds), "level": "subject"})
        run = self.submit(each_from_bids=str(ds), level="session")
        self.assertEqual(run["payload"]["array"]["items"], ["sub-01_ses-1", "sub-01_ses-2", "sub-02_ses-1"])
        self.assertEqual(run["payload"]["array"]["level"], "session")
        with self.assertRaisesRegex(DurehError, "inside the site"):
            self.submit(each_from_bids=str(self.tmp))
        with self.assertRaisesRegex(DurehError, "exclude"):
            self.submit(each="sub-01", each_from_bids=str(ds))
        with self.assertRaisesRegex(DurehError, "--level goes with"):
            self.submit(each="sub-01", level="session")
        with self.assertRaisesRegex(DurehError, "no sub-"):
            self.submit(each_from_bids=str(ds / "derivatives"))
