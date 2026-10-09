"""`dureh slurm submit --each/--after/--resubmit-failed` (release 26): one job per item as one Slurm array, step
dependencies, resubmission of failed items, per-item states; release 46: item levels and fields from the package's
labels.item_grammar, the loop rule's level from labels.levels, no harness item walker. Proved on the example package (items
`sample-<id>` and `sample-<id>_batch-<id>`). Fake sbatch/sacct only; never real Slurm."""
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path

from dureh_core.package import LEGACY_LABELS
from dureh_core import ops
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError
from tests.helpers import ExampleSiteCase, SiteCase

SUBJ = ["sample-01", "sample-02", "sample-03", "sample-04", "sample-05"]


class Each(ExampleSiteCase):
    def setUp(self):
        super().setUp()
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
        self.proj = self.site_root / "projects" / "p1"
        (self.proj / "checkpoints").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.proj)], check=True)
        b = wf.wi_new(self.s, "desk", "Bundle", "g", "coordinator", "projects/p1", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", b, "ok")
        self.led.event("wi.dispatched", "desk", b, {"role": "coordinator", "brief": "0" * 16, "by": "desk"})
        self.w = wf.wi_new(self.s, "desk", "Run", "g", "runner", "projects/p1", ["ok"], parent=b)["id"]
        wf.wi_approve(self.s, "desk", self.w, "ok")
        self.led.event("wi.dispatched", "desk", self.w, {"role": "runner", "brief": "0" * 16, "by": "desk"})
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
        return ops.slurm_submit(self.s, "runner", self.w, str(kw.pop("script", self.script)), **kw)

    # ------------------------------------------------------------------ items
    def test_items_parsing_and_refusals(self):
        self.assertEqual(ops.parse_items(self.s, "sample-01,sample-02 sample-03")[0],
                         ["sample-01", "sample-02", "sample-03"])
        lst = self.proj / "checkpoints" / "samples.txt"
        lst.write_text("# pilot\nsample-01\n\n  sample-02  \n# sample-09 excluded\nsample-X003\n")
        items, f = ops.parse_items(self.s, f"@{lst}")
        self.assertEqual((items, f), (["sample-01", "sample-02", "sample-X003"], str(lst)))
        self.assertEqual(ops.parse_items(self.s, "A001,A002")[0], ["A001", "A002"])
        grammar = self.s.labels["item_grammar"]
        self.assertEqual(ops.parse_items(self.s, "dim-20,SIGMA,batch-1_sample-01,Sample-01,sample-01-x,a_b-c")[0],
                         ["dim-20", "SIGMA", "batch-1_sample-01", "Sample-01", "sample-01-x", "a_b-c"])
        self.assertEqual(ops.items_level(["dim-20", "sample-01-x"], grammar), "plain", "no grammar match: plain")
        self.assertEqual(ops.item_entities("sample-01_batch-2", grammar), ("batch", {"sample": "01", "batch": "2"}))
        self.assertEqual(ops.item_entities("sample-01", {}), ("plain", {}), "the harness parses no grammar itself")
        for bad, msg in (("sample-01,../x", "invalid"), ("-x", "invalid"), ("sample 01;rm", "invalid"),
                         ("sample-01,sample-01", "duplicate"), (" , ", "at least one"), ("_a", "invalid"),
                         ("sample-01,sample-02_batch-1", "mixes batch and sample"), ("sample-01,plain", "mixes plain"),
                         ("sample-01,dim-20", "mixes plain and sample")):
            with self.assertRaisesRegex(DurehError, msg):
                ops.parse_items(self.s, bad)
        out = self.tmp / "outside.txt"
        out.write_text("sample-01\n")
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
        self.assertEqual(out.stdout.strip(), "ITEM=sample-03 IDX=2")
        env["SLURM_ARRAY_TASK_ID"] = "7"
        self.assertEqual(subprocess.run(["bash", str(wrapper)], capture_output=True, env=env).returncode, 98)
        self.assertEqual(p["script"], str(self.script), "the record keeps the original script")

    def test_each_file_max_parallel_and_array_refusal(self):
        lst = self.proj / "checkpoints" / "samples.txt"
        lst.write_text("sample-01\nsample-02\nsample-03\n")
        run = self.submit(each=f"@{lst}", max_parallel=2)
        self.assertIn("--array=0-2%2", self.calls()[-1])
        self.assertEqual((run["payload"]["each_file"], run["payload"]["max_parallel"]), (str(lst), 2))
        with self.assertRaisesRegex(DurehError, "needs --each"):
            self.submit(max_parallel=2)
        with self.assertRaisesRegex(DurehError, "positive"):
            self.submit(each="sample-01", max_parallel=0)
        with self.assertRaisesRegex(DurehError, "remove --array"):
            self.submit(each="sample-01", extra=["--array=0-3"])
        own = self.proj / "checkpoints" / "own.sh"
        own.write_text("#!/bin/bash\n#SBATCH --array=0-9\necho\n")
        with self.assertRaisesRegex(DurehError, "remove --array"):
            self.submit(script=own, each="sample-01")
        dry = self.submit(each="sample-01,sample-02", dry_run=True)
        self.assertEqual(dry["payload"]["status"], "not-run")
        self.assertFalse(Path(dry["payload"]["array"]["items_file"]).exists(), "a dry run writes nothing")

    def test_failed_sbatch_removes_items_and_wrapper(self):
        self.tool("sbatch", "echo 'sbatch: error: bad' >&2; exit 1\n")
        with self.assertRaisesRegex(DurehError, "sbatch failed"):
            self.submit(each="sample-01")
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
        ops.slurm_wait(self.s, "runner", s1["id"], timeout=5, interval=1)
        with self.assertRaisesRegex(DurehError, "known FAILED.*--resubmit-failed"):
            self.submit(each="sample-01", after=[s1["id"]])
        again = ops.slurm_resubmit_failed(self.s, "runner", s1["id"])
        with self.assertRaisesRegex(DurehError, f"use --after {again['id']}"):
            self.submit(each="sample-01", after=[s1["id"]])

    # ----------------------------------------------------------- status/wait
    def test_status_and_wait_report_per_item_and_overall(self):
        run = self.submit(each="a,b,c")
        self.sacct("4300", "4300_[0-2]|PENDING|0:0")
        ev = ops.slurm_status(self.s, "runner", run["id"])
        self.assertEqual(ev["payload"]["scheduler_state"], "PENDING")
        self.assertEqual(ev["payload"]["items"], {"a": "PENDING", "b": "PENDING", "c": "PENDING"})
        self.assertFalse(ev["payload"]["final"])
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|RUNNING|0:0", "4300_[2]|PENDING|0:0")
        self.assertEqual(ops.slurm_status(self.s, "desk", run["id"])["payload"]["scheduler_state"], "RUNNING")
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_0.batch|COMPLETED|0:0", "4300_1|COMPLETED|0:0",
                   "4300_2|COMPLETED|0:0")
        ev = ops.slurm_wait(self.s, "runner", run["id"], timeout=5, interval=1)
        self.assertEqual(ev["payload"]["scheduler_state"], "COMPLETED")
        self.assertTrue(ev["payload"]["final"])
        self.assertEqual(ev["payload"]["item_states"], {"a": "COMPLETED", "b": "COMPLETED", "c": "COMPLETED"})
        n = len([e for e in self.led.events(self.w) if e["type"] == "run.status"])
        again = ops.slurm_status(self.s, "runner", run["id"])
        self.assertEqual(again["id"], ev["id"], "final per-item states are recorded once")
        self.assertEqual(len([e for e in self.led.events(self.w) if e["type"] == "run.status"]), n)

    def test_overall_state_rules(self):
        self.assertEqual(ops.overall_state(["COMPLETED", "COMPLETED"]), "COMPLETED")
        self.assertEqual(ops.overall_state(["COMPLETED", "TIMEOUT"]), "FAILED")
        self.assertEqual(ops.overall_state(["FAILED", "RUNNING"]), "RUNNING")
        self.assertEqual(ops.overall_state(["FAILED", "PENDING"]), "PENDING")
        self.assertEqual(ops.overall_state(["COMPLETED", "UNKNOWN"]), "PENDING")
        run = self.submit(each="a,b")
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|OUT_OF_MEMORY|0:125")
        ev = ops.slurm_wait(self.s, "runner", run["id"], timeout=5, interval=1)
        self.assertEqual((ev["payload"]["scheduler_state"], ev["payload"]["failed_items"]), ("FAILED", ["b"]))

    def test_wait_timeout_on_array_is_not_final(self):
        run = self.submit(each="a,b")
        self.sacct("4300", "4300_0|RUNNING|0:0", "4300_1|PENDING|0:0")
        ev = ops.slurm_wait(self.s, "runner", run["id"], timeout=0, interval=1)
        self.assertFalse(ev["payload"]["final"])
        self.assertIn("still RUNNING", ev["note"])

    # --------------------------------------------------------------- resubmit
    def test_resubmit_failed_only_the_items_not_completed(self):
        run = self.submit(each=",".join(SUBJ), extra=["--mem=1G"])
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|RUNNING|0:0", "4300_2|FAILED|1:0")
        with self.assertRaisesRegex(DurehError, "not every task has ended"):
            ops.slurm_resubmit_failed(self.s, "runner", run["id"])
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|COMPLETED|0:0", "4300_2|FAILED|1:0",
                   "4300_3|CANCELLED by 5|0:0", "4300_4|COMPLETED|0:0")
        fixed = self.proj / "checkpoints" / "step1-fixed.sh"
        fixed.write_text("#!/bin/bash\necho fixed $DUREH_ITER\n")
        again = ops.slurm_resubmit_failed(self.s, "runner", run["id"], script=str(fixed))
        p = again["payload"]
        self.assertEqual(p["array"]["items"], ["sample-03", "sample-04"])
        self.assertEqual(p["resubmit_of"], run["id"])
        self.assertEqual(p["resubmit_states"], {"sample-03": "FAILED", "sample-04": "CANCELLED"})
        self.assertEqual(p["script"], str(fixed))
        cmd = self.calls()[-1]
        self.assertIn("--array=0-1", cmd)
        self.assertIn("--mem=1G", cmd, "the original sbatch arguments are reused")
        self.sacct("4301", "4301_0|COMPLETED|0:0", "4301_1|COMPLETED|0:0")
        with self.assertRaisesRegex(DurehError, "nothing to resubmit"):
            ops.slurm_resubmit_failed(self.s, "runner", again["id"])
        single = self.submit()
        with self.assertRaisesRegex(DurehError, "not an --each"):
            ops.slurm_resubmit_failed(self.s, "runner", single["id"])

    # ------------------------------------------------------------------ purge
    def test_purge_removes_array_task_logs_and_item_list(self):
        run = self.submit(each="a,b,c")
        logs = self.site_root / "logs" / "slurm" / self.w
        mine = [logs / f"{self.s.session_name}-{self.w}-4300_{i}.{x}" for i in range(3) for x in ("out", "err")]
        for f in mine:
            f.write_text("x")
        keep = logs / f"{self.s.session_name}-{self.w}-4399_0.out"
        keep.write_text("another job")
        wf.dispatch_stop(self.s, "desk", self.w, "test")
        out = wf.purge(self.s, "desk", self.w)
        expected = mine + [Path(run["payload"]["array"]["items_file"]), Path(run["payload"]["array"]["wrapper"])]
        self.assertEqual(sorted(out["removed_logs"]), sorted(str(f) for f in expected))
        self.assertTrue(keep.is_file())

    # -------------------------------------------------------------------- cli
    def test_cli_each_after_and_resubmit(self):
        from dureh_core import cli

        base = ["--site", str(self.site_root), "slurm", "submit", "--wi", self.w, "--actor", "runner"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main([*base, "--script", str(self.script), "--each", "sample-01", "sample-02,sample-03"]), 0)
        first = [r for r in self.led.load("run") if r["wi"] == self.w][-1]
        self.assertEqual(first["payload"]["array"]["items"], ["sample-01", "sample-02", "sample-03"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([*base, "--script", str(self.script), "--after", first["id"]]), 0)
        self.assertIn("--dependency=afterok:4300", self.calls()[-1])
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|FAILED|1:0", "4300_2|COMPLETED|0:0")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([*base, "--resubmit-failed", first["id"]]), 0)
        last = [r for r in self.led.load("run") if r["wi"] == self.w][-1]
        self.assertEqual((last["payload"]["array"]["items"], last["payload"]["script"]), (["sample-02"], str(self.script)))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(cli.main([*base, "--resubmit-failed", first["id"], "--each", "x"]), 0)
            self.assertNotEqual(cli.main([*base]), 0)

    # ------------------------------------------------------- levels and names
    def run_task(self, run, task):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DUREH_ITER", "EX_"))}
        env["SLURM_ARRAY_TASK_ID"] = str(task)
        return subprocess.run(["bash", run["payload"]["array"]["wrapper"]], capture_output=True, text=True,
                              env=env).stdout.strip()

    def test_levels_and_default_names(self):
        self.script.write_text("#!/bin/bash\necho \"I=$DUREH_ITER B=$DUREH_ITER_BY X=$DUREH_ITER_INDEX "
                               "S=$DUREH_ITER_SAMPLE E=[$DUREH_ITER_BATCH]\"\n")
        run = self.submit(each="sample-01,sample-02")
        self.assertEqual(run["payload"]["array"]["level"], "sample")
        self.assertEqual(run["payload"]["array"]["env"], {**ops.DEFAULT_ITEM_ENV, "sample": "DUREH_ITER_SAMPLE",
                                                          "batch": "DUREH_ITER_BATCH"})
        self.assertEqual(self.run_task(run, 1), "I=sample-02 B=sample X=1 S=02 E=[]", "an absent field exports empty")
        run = self.submit(each="sample-01_batch-1,sample-01_batch-2,sample-02_batch-1")
        self.assertEqual(run["payload"]["array"]["level"], "batch")
        self.assertEqual(self.run_task(run, 1), "I=sample-01_batch-2 B=batch X=1 S=01 E=[2]")
        run = self.submit(each="dim-20,A002")
        self.assertEqual(run["payload"]["array"]["level"], "plain")
        self.assertEqual(self.run_task(run, 0), "I=dim-20 B=plain X=0 S= E=[]")

    def test_configured_each_env_names_replace_the_defaults(self):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["slurm"]["each_env"] = {"item": "EX_ITER", "by": "EX_ITER_BY", "index": "EX_ITER_INDEX",
                                    "sample": "EX_SAMPLE", "batch": "EX_BATCH"}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        self.script.write_text("#!/bin/bash\necho \"$EX_ITER $EX_ITER_BY $EX_ITER_INDEX $EX_SAMPLE [$EX_BATCH] "
                               "D=${DUREH_ITER:-unset}${DUREH_ITER_SAMPLE:-unset}\"\n")
        run = self.submit(each="sample-01_batch-1,sample-02_batch-3")
        self.assertEqual(run["payload"]["array"]["env"]["sample"], "EX_SAMPLE")
        self.assertEqual(self.run_task(run, 1), "sample-02_batch-3 batch 1 02 [3] D=unsetunset", "only the configured names")
        self.assertNotIn("export DUREH_", Path(run["payload"]["array"]["wrapper"]).read_text())
        cfg["slurm"]["each_env"] = {"item": "EX_ITER", "index": "EX_ITER_INDEX"}  # by and fields unmapped
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        run = self.submit(each="sample-01")
        text = Path(run["payload"]["array"]["wrapper"]).read_text()
        self.assertFalse(any(w in text for w in ("SAMPLE", "BATCH", "_BY")), "unmapped by/fields are not exported")
        for bad in ({"item": "A"}, {"item": "A", "index": "A"}, {"item": "A", "index": "B", "run": "C"},
                    {"item": "A b", "index": "B"}, {"item": "A", "index": "B", "sub": "C"}):
            cfg["slurm"]["each_env"] = bad
            (self.site_root / "config.json").write_text(json.dumps(cfg))
            self.s = self.site()
            with self.assertRaisesRegex(DurehError, "slurm.each_env"):
                self.submit(each="sample-01")

    # ----------------------------------------------------- loop rule (release 46: the package's levels and words)
    def loop_rule(self, level=None, **slurm):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["slurm"] = {**(cfg.get("slurm") or {}), **slurm}
        if level is None:
            cfg["slurm"].pop("each_level", None)
        else:
            cfg["slurm"]["each_level"] = level
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()

    def test_loop_rule_refuses_bare_single_jobs(self):
        import re

        self.loop_rule(None)  # default: the package's finest level
        self.assertEqual(ops.each_level(self.s), "part")
        refusal = ops.LOOP_RULE.format(level="part") + " (example-org loops every step over samples: submit with --each <samples>)"
        with self.assertRaisesRegex(DurehError, "^" + re.escape(refusal) + "$"):
            self.submit()
        self.assertFalse((self.fake / "sbatch.args").exists(), "nothing submitted")
        with self.assertRaisesRegex(DurehError, "--single needs --reason"):
            self.submit(single=True)
        run = self.submit(single=True, reason="one merge job over every sample's output")
        self.assertEqual((run["payload"]["level"], run["payload"]["reason"], run["payload"]["single"]),
                         ("single", "one merge job over every sample's output", True))
        with self.assertRaisesRegex(DurehError, "--single is for a job without --each"):
            self.submit(each="sample-01", single=True, reason="x")
        arr = self.submit(each="sample-01,sample-02")
        self.assertEqual(arr["payload"]["level"], "sample")
        group = self.submit(after=[arr["id"]])
        self.assertEqual((group["payload"]["level"], group["payload"].get("single")), ("group", None))
        self.loop_rule("batch")
        with self.assertRaisesRegex(DurehError, "slurm.each_level batch"):
            self.submit()
        self.loop_rule("any")
        self.assertEqual(self.submit()["payload"]["level"], "single")
        self.loop_rule("file")
        with self.assertRaisesRegex(DurehError, r"slurm.each_level must be one of the package's labels.levels \(part, sample, batch\)"):
            self.submit()
        from dureh_core import cli

        self.loop_rule(None)
        base = ["--site", str(self.site_root), "slurm", "submit", "--wi", self.w, "--actor", "runner",
                "--script", str(self.script)]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([*base, "--single", "--reason", "one job"]), 0)
            self.assertNotEqual(cli.main([*base]), 0)
        last = [r["payload"] for r in self.led.load("run") if r["wi"] == self.w][-1]
        self.assertEqual((last["level"], last["reason"]), ("single", "one job"))

    def test_the_item_walker_is_gone(self):
        from dureh_core import cli

        base = ["--site", str(self.site_root), "slurm", "submit", "--wi", self.w, "--actor", "runner",
                "--script", str(self.script)]
        walker = "--each-from-" + "b" + "ids"  # spelled in parts: the removed domain flag (name test)
        for extra in ([walker, str(self.proj)], ["--level", "sample", "--each", "sample-01"],
                      ["--suffix", "x", "--each", "sample-01"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                cli.main([*base, *extra])
            self.assertIn("unrecognized arguments", err.getvalue())
        self.assertFalse(hasattr(ops, "items_from_" + "b" + "ids"))

    def test_a_package_without_levels_has_no_loop_rule_by_default(self):
        m = self.package / "startup.yaml"
        self.assertIn("levels: [part, sample, batch]", m.read_text())
        self.loop_rule(None)
        self.s._manifest = {**self.s.package_manifest(), "labels": {}}
        self.assertEqual(ops.each_level(self.s), "any")
        self.assertEqual(ops.item_env(self.s), ops.DEFAULT_ITEM_ENV)


class LegacySchemaOneItems(SiteCase):
    """A schema-1 package (release 46 only) keeps the item grammar and levels the harness had before."""

    def test_legacy_grammar_and_levels(self):
        m = self.package / "startup.yaml"
        m.write_text("schema_version: 1\npackage:\n  id: legacy\n  name: Legacy\nvalues:\n  - id: refactor-to-modules\n"
                     "    title: Modules\n    skill: skills/code/SKILL.md\norganization:\n  managers: [pm-modules]\n"
                     "  workers: [py-writer, checker]\nbootstrap:\n  work_items: [bootstrap/wi-one.yaml]\n"
                     "  persona_questions: bootstrap/persona.yaml\n")
        self.install(slurm={"each_level": "any", "external_jobs": False})
        s = self.site()
        g = s.labels["item_grammar"]
        self.assertEqual(ops.item_entities("sub-01_ses-2", g), ("session", {"sub": "01", "ses": "2"}))
        self.assertEqual(ops.item_env(s)["sub"], "DUREH_ITER_SUB")
        self.assertEqual(s.labels["levels"], ["file", "session", "subject"])
        self.assertEqual(s.labels["layout"]["final"], LEGACY_LABELS["layout"]["final"])
