"""`dureh slurm submit --via '<module command>'` (proposals/module-submit-contract.md, Director 2026-10-06): the harness
runs a module's own submit command in the WI context and ingests the submission record it writes; status, wait, watch,
resubmit-failed, cancel and purge work on the recorded jobs. A fake module command (writes a `camri.submission.v1`-shaped
record with one array job and one single job), fake sacct/squeue/scancel only; never real Slurm."""
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from dureh_core import ops
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError
from tests.helpers import SiteCase, denied, hook

SUBMIT = {"record_arg": "--submission-record", "log_arg": "--log-dir", "after_arg": "--after", "items_arg": "--items"}

WRITEREC = r'''
import json, os, sys
rec, n, logdir, items_spec, cwd = sys.argv[1:6]
items = ["sub-01", "sub-02", "sub-03"]
if items_spec.startswith("@"):
    items = [l.strip() for l in open(items_spec[1:]) if l.strip() and not l.startswith("#")]
logdir = logdir or os.path.join(cwd, "checkpoints", "logs")
os.makedirs(logdir, exist_ok=True)
script = os.path.join(logdir, f"camri-step-{n}.sbatch")
open(script, "w").write("#!/bin/bash\n#SBATCH --time=00:30:00\n#SBATCH --job-name=camri-step\necho hi\n")
gscript = os.path.join(logdir, f"camri-group-{int(n) + 1}.sbatch")
open(gscript, "w").write("#!/bin/bash\n#SBATCH --time=00:10:00\n#SBATCH --job-name=camri-group\necho group\n")
data = {"schema_version": "camri.submission.v1", "tool": "camri", "tool_version": "2.0.0a1", "root": cwd,
        "manifest": {"path": "camri-manifest.yaml", "sha256": "abc"}, "workflow": "fmriprep.default",
        "pipeline": "fmriprep", "steps": ["step1", "group"], "backend": "slurm",
        "submitted_at": "2026-10-06T12:00:00Z", "cwd": cwd,
        "jobs": [{"job_id": n, "kind": "array", "count": len(items), "max_parallel": None, "script": script,
                  "name": "camri-step", "dependency": [],
                  "logs": {"stdout": f"{logdir}/camri-%x-%A_%a.out", "stderr": f"{logdir}/camri-%x-%A_%a.err"}},
                 {"job_id": str(int(n) + 1), "kind": "single", "script": gscript, "name": "camri-group",
                  "dependency": [n],
                  "logs": {"stdout": f"{logdir}/camri-%x-%j.out", "stderr": f"{logdir}/camri-%x-%j.err"}}],
        "items": [{"index": i, "item": it, "level": "subject", "sub": it[4:], "ses": None, "path": f"rawbids/{it}",
                   "files": f"items/{it}.files"} for i, it in enumerate(items)],
        "selection": {"level": "subject", "root": "rawbids", "stages": [{"name": "level", "in": 3, "out": 3}],
                      "skipped": [], "strict": False, "limit": None, "filter": "", "list_file": None},
        "records": {"execution_json": "checkpoints/x/execution.json", "tasks_dir": "checkpoints/x/tasks",
                    "items_tsv": "checkpoints/x/items.tsv"}}
json.dump(data, open(rec, "w"))
'''


class ViaCase(SiteCase):
    """desk -> pm-projects bundle -> pipeline-runner item on a git project; module `camri` = a fake submit command."""

    def setUp(self):
        super().setUp()
        manifest = self.package / "startup.yaml"
        manifest.write_text(manifest.read_text()
                            .replace("managers: [pm-modules]", "managers: [pm-modules, pm-projects]")
                            .replace("workers: [py-writer,", "workers: [data-steward, pipeline-runner, py-writer,"))
        self.install()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        (self.fake / "writerec.py").write_text(WRITEREC)
        # the fake module command: logs its arguments and cwd, writes the record, prints `submission-record: <path>`
        # (mode file: ok | quiet (no stdout line) | silent (exit 0, nothing written) | fail (exit 3))
        self.tool("camri", f"echo \"$@\" >> {self.fake}/camri.args\necho \"$(pwd)\" >> {self.fake}/camri.cwd\n"
                           f"mode=$(cat {self.fake}/mode 2>/dev/null || echo ok)\n"
                           "case \"$mode\" in fail) echo 'camri: boom: no manifest' >&2; exit 3;; silent) exit 0;; esac\n"
                           "rec=''; prev=''; logdir=''; items=''\n"
                           "for a in \"$@\"; do [ \"$prev\" = --submission-record ] && rec=$a; [ \"$prev\" = --log-dir ]"
                           " && logdir=$a; [ \"$prev\" = --items ] && items=$a; prev=$a; done\n"
                           "[ -n \"$rec\" ] || rec=\"$(pwd)/submissions/rec.json\"\nmkdir -p \"$(dirname \"$rec\")\"\n"
                           f"n=$(cat {self.fake}/n 2>/dev/null || echo 4300); echo $((n+2)) > {self.fake}/n\n"
                           f"python3 {self.fake}/writerec.py \"$rec\" \"$n\" \"$logdir\" \"$items\" \"$(pwd)\"\n"
                           "echo 'camri: submitted'\n[ \"$mode\" = quiet ] || echo \"submission-record: $rec\"\n")
        self.tool("sacct", "jobs=''; prev=''; batch=''; for a in \"$@\"; do [ \"$prev\" = -j ] && jobs=$a;"
                           " [ \"$a\" = --format=JobID,State,ExitCode,Elapsed ] && batch=1; prev=$a; done\n"
                           f"echo \"$jobs\" >> {self.fake}/sacct.calls\n"
                           "for job in $(echo \"$jobs\" | tr ',' ' '); do\n"
                           f"  f={self.fake}/sacct-$job; [ -f \"$f\" ] || continue\n"
                           "  if [ -n \"$batch\" ]; then while IFS= read -r l; do case \"$l\" in \"$job\"_*) echo \"$l\";;"
                           " *) echo \"$job|$l\";; esac; done < \"$f\"; else cat \"$f\"; fi\n"
                           "done; exit 0\n")
        self.tool("scancel", f"echo \"$@\" > {self.fake}/scancel.args\n")
        self.tool("squeue", f"cat {self.fake}/squeue.out 2>/dev/null\n")
        self.tool("sbatch", "echo 'sbatch: should not run' >&2; exit 7\n")
        self.config(SUBMIT)
        self.proj = self.site_root / "projects" / "lna"
        (self.proj / "checkpoints").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.proj)], check=True)
        self.bundle = wf.wi_new(self.s, "desk", "Bundle", "g", "pm-projects", "projects/lna", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", self.bundle, "ok")
        self.led.event("wi.dispatched", "desk", self.bundle, {"role": "pm-projects", "brief": "0" * 16, "by": "desk"})
        self.w = wf.wi_new(self.s, "desk", "Run", "g", "pipeline-runner", "projects/lna", ["ok"], parent=self.bundle)["id"]
        wf.wi_approve(self.s, "desk", self.w, "ok")
        self.led.event("wi.dispatched", "desk", self.w, {"role": "pipeline-runner", "brief": "0" * 16, "by": "desk"})
        self.logs = self.site_root / "logs" / "slurm" / self.w
        self.via = "camri run fmriprep step1 --workflow fmriprep.default --level subject --submit --detach"

    def tool(self, name, body):
        (self.fake / name).write_text("#!/bin/sh\n" + body)
        (self.fake / name).chmod(0o755)

    def config(self, submit, **slurm):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["env"] = {"path_prepend": [str(self.fake)]}
        cfg["containers"] = {"module": ""}
        cfg["slurm"] = {**cfg.get("slurm", {}), **slurm}
        # submit=None: no modules[] entry at all (the command's first word matches nothing)
        cfg["modules"] = [{"id": "camri", "path": "modules/camri", "submit": submit}] if submit is not None else []
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        self.led = Ledger(self.s)

    def sacct(self, job, *lines):
        (self.fake / f"sacct-{job}").write_text("".join(f"{x}\n" for x in lines))

    def args(self):
        return (self.fake / "camri.args").read_text().splitlines() if (self.fake / "camri.args").exists() else []

    def submit(self, via=None, **kw):
        return ops.slurm_submit_via(self.s, "pipeline-runner", self.w, via or self.via, **kw)

    def runs(self):
        return [r for r in self.led.load("run") if r["wi"] == self.w]

    def all_done(self, n="4300"):
        self.sacct(n, f"{n}_0|COMPLETED|0:0", f"{n}_1|COMPLETED|0:0", f"{n}_2|COMPLETED|0:0")
        self.sacct(str(int(n) + 1), "COMPLETED|0:0|00:01:00")

    # ----------------------------------------------------------------- submit
    def test_dry_run_runs_nothing_and_records_not_run(self):
        run = self.submit(dry_run=True)
        p = run["payload"]
        self.assertEqual((p["run_type"], p["status"]), ("slurm", "not-run"))
        self.assertEqual(self.args(), [], "the module command did not run")
        self.assertEqual(p["via"]["command"][:3], ["camri", "run", "fmriprep"])
        self.assertEqual(p["via"]["module"], "camri")
        self.assertEqual(p["command"][-4], "--submission-record", "record_arg and log_arg would be added")
        self.assertTrue(p["command"][-3].startswith(str(self.logs / "via-")))
        self.assertEqual(p["command"][-2:], ["--log-dir", str(self.logs)])
        self.assertNotIn("job_id", p)

    def test_success_records_the_jobs_the_array_and_the_record(self):
        run = self.submit()
        p = run["payload"]
        args = self.args()[-1].split()
        self.assertEqual(args[:2], ["run", "fmriprep"])
        self.assertEqual(args[args.index("--log-dir") + 1], str(self.logs))
        record = args[args.index("--submission-record") + 1]
        self.assertEqual(Path(record).parent, self.logs)
        self.assertEqual(os.path.realpath((self.fake / "camri.cwd").read_text().strip()), os.path.realpath(self.proj),
                         "the command runs in the WI's job root")
        self.assertEqual((p["run_type"], p["status"], p["job_id"]), ("slurm", "submitted", "4300"))
        self.assertEqual([(j["job_id"], j["kind"]) for j in p["jobs"]], [("4300", "array"), ("4301", "single")])
        self.assertEqual(p["jobs"][1]["dependency"], ["4300"])
        self.assertEqual(p["array"], {"job_id": "4300", "items": ["sub-01", "sub-02", "sub-03"], "count": 3,
                                      "level": "subject", "items_file": None})
        self.assertEqual(p["log_out"], f"{self.logs}/camri-%x-%A_%a.out")
        self.assertEqual(p["script"], f"{self.logs}/camri-step-4300.sbatch")
        self.assertIn("script_sha256", p)
        self.assertEqual(p["chdir"], str(self.proj))
        v = p["via"]
        self.assertEqual((v["record"], v["schema_version"], v["tool"], v["tool_version"]),
                         (record, "camri.submission.v1", "camri", "2.0.0a1"))
        self.assertEqual(v["selection"]["level"], "subject")
        self.assertEqual(v["manifest"]["sha256"], "abc")
        self.assertEqual(v["extra"]["workflow"], "fmriprep.default", "unknown keys are kept under via.extra")
        self.assertEqual(v["added"], ["--submission-record", record, "--log-dir", str(self.logs)])
        self.assertIn(run["id"], [r["id"] for r in ops._tracked_runs(self.led)], "watch tracks it")
        self.assertNotIn("start_check", p, "no workday mode: no start check")
        hint = ops._log_hint(self.s, run)
        self.assertEqual(hint["log_out"], f"{self.logs}/camri-camri-step-4300_%a.out")

    def test_failure_records_only_an_event(self):
        (self.fake / "mode").write_text("fail")
        with self.assertRaisesRegex(DurehError, r"exit 3.*boom: no manifest"):
            self.submit()
        self.assertEqual(self.runs(), [], "no run record")
        evs = [e for e in self.led.events(self.w) if e["type"] == "slurm.via-failed"]
        self.assertEqual(len(evs), 1)
        self.assertEqual((evs[0]["payload"]["exit"], evs[0]["payload"]["module"]), (3, "camri"))
        self.assertIn("boom", evs[0]["payload"]["stderr_tail"])
        self.assertEqual(evs[0]["actor"], "pipeline-runner", "the role may record its own failure")
        (self.fake / "mode").write_text("ok")
        self.tool("nosuch-tool", "exit 0\n")
        with self.assertRaisesRegex(DurehError, "command not found: ghost-tool"):
            self.submit("ghost-tool run x")
        self.assertEqual(len([e for e in self.led.events(self.w) if e["type"] == "slurm.via-failed"]), 2)

    def test_record_path_from_flag_config_or_stdout_line(self):
        # no matching module entry: the harness adds nothing and reads the `submission-record:` stdout line
        self.config(None)
        run = self.submit()
        self.assertNotIn("--submission-record", self.args()[-1])
        self.assertIsNone(run["payload"]["via"]["module"])
        self.assertEqual(run["payload"]["via"]["record"], f"{self.proj}/submissions/rec.json")
        self.assertEqual(run["payload"]["job_id"], "4300")
        # --record names the file (relative to the job root) when the command prints no line
        (self.fake / "mode").write_text("quiet")
        run = self.submit(record="submissions/rec.json")
        self.assertEqual(run["payload"]["via"]["record"], f"{self.proj}/submissions/rec.json")
        self.assertEqual(run["payload"]["job_id"], "4302")
        with self.assertRaisesRegex(DurehError, "named no submission record"):
            self.submit()
        (self.fake / "mode").write_text("silent")
        with self.assertRaisesRegex(DurehError, "does not exist after the command exited 0"):
            self.submit(record="submissions/missing.json")
        # record_arg configured: --record is passed to the module and read back
        (self.fake / "mode").write_text("ok")
        self.config(SUBMIT)
        run = self.submit(record="checkpoints/sub.json")
        self.assertIn(f"--submission-record {self.proj}/checkpoints/sub.json", self.args()[-1])
        self.assertEqual(run["payload"]["via"]["record"], f"{self.proj}/checkpoints/sub.json")
        self.assertTrue((self.proj / "checkpoints" / "sub.json").is_file())

    def test_via_module_names_the_entry_for_a_wrapped_command(self):
        self.tool("uv", f"shift 2; exec {self.fake}/camri \"$@\"\n")  # `uv run camri ...`
        run = self.submit("uv run camri run fmriprep step1 --submit --detach")
        self.assertIsNone(run["payload"]["via"]["module"], "first word `uv` matches no module: nothing added")
        self.assertNotIn("--submission-record", self.args()[-1])
        run = self.submit("uv run camri run fmriprep step1 --submit --detach", via_module="camri")
        self.assertEqual(run["payload"]["via"]["module"], "camri")
        self.assertIn("--submission-record", self.args()[-1])
        with self.assertRaisesRegex(DurehError, "no entry with that id"):
            self.submit(via_module="nope")
        self.config({"record_arg": "bad flag"})
        with self.assertRaisesRegex(DurehError, r"modules\[camri\].submit must map"):
            self.submit()

    def test_guards_and_refused_combinations(self):
        with self.assertRaisesRegex(DurehError, "only pipeline-runner"):
            ops.slurm_submit_via(self.s, "py-writer", self.w, self.via)
        with self.assertRaisesRegex(DurehError, "not a pipeline-runner work item"):
            ops.slurm_submit_via(self.s, "pipeline-runner", self.bundle, self.via)
        idle = wf.wi_new(self.s, "desk", "Idle", "g", "pipeline-runner", "projects/lna", ["ok"], parent=self.bundle)["id"]
        wf.wi_approve(self.s, "desk", idle, "ok")
        with self.assertRaisesRegex(DurehError, "not dispatched"):
            ops.slurm_submit_via(self.s, "pipeline-runner", idle, self.via)
        with self.assertRaisesRegex(DurehError, "needs a command"):
            self.submit("  ")
        from dureh_core import cli

        base = ["--site", str(self.site_root), "slurm", "submit", "--wi", self.w, "--actor", "pipeline-runner"]
        for extra, msg in ((["--module", "camri"], "phase 2"), (["--script", "x.sh"], "--script does not go with --via"),
                           (["--each", "sub-01"], "--each does not go"), (["--sbatch-arg=--mem=1G"], "--sbatch-arg")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertNotEqual(cli.main([*base, "--via", self.via, *extra]), 0)
            self.assertIn(msg, err.getvalue(), extra)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertNotEqual(cli.main([*base, "--script", "x.sh", "--record", "r.json"]), 0)
        self.assertIn("go with --via", err.getvalue())
        self.assertEqual(self.args(), [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([*base, "--via", self.via, "--via-module", "camri"]), 0)
        self.assertEqual(self.runs()[-1]["payload"]["via"]["module"], "camri")

    def test_after_passes_through_after_arg_or_is_refused(self):
        first = self.submit()
        self.config(None)
        with self.assertRaisesRegex(DurehError, "after_arg.*no modules\\[\\] entry matches"):
            self.submit(after=[first["id"]])
        self.config({})
        with self.assertRaisesRegex(DurehError, r"modules\[camri\].submit has no after_arg"):
            self.submit(after=[first["id"]])
        self.config(SUBMIT)
        second = self.submit(after=[first["id"]])
        self.assertIn("--after 4300 --after 4301", self.args()[-1], "one --after per job of the dependency")
        self.assertEqual(second["payload"]["after"], [{"run": first["id"], "job_id": "4300", "job_ids": ["4300", "4301"]}])
        self.assertEqual(ops._dep_job_ids(second["payload"]["after"]), ["4300", "4301"])

    # ----------------------------------------------------------- status/wait
    def test_status_and_wait_over_two_jobs(self):
        run = self.submit()
        self.sacct("4300", "4300_[0-2]|PENDING|0:0")
        ev = ops.slurm_status(self.s, "pipeline-runner", run["id"])
        self.assertEqual(ev["payload"]["scheduler_state"], "PENDING")
        self.assertEqual(ev["payload"]["jobs"], {"4300": "PENDING", "4301": "UNKNOWN"})
        self.assertEqual(ev["payload"]["items"], {"sub-01": "PENDING", "sub-02": "PENDING", "sub-03": "PENDING"})
        self.assertFalse(ev["payload"]["final"])
        self.assertIn("4300,4301", (self.fake / "sacct.calls").read_text(), "one batched sacct call")
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|COMPLETED|0:0", "4300_2|COMPLETED|0:0")
        self.sacct("4301", "RUNNING|0:0|00:01:00")
        self.assertEqual(ops.slurm_status(self.s, "desk", run["id"])["payload"]["scheduler_state"], "RUNNING",
                         "the array completed but the group job runs: not final")
        self.sacct("4301", "FAILED|1:0|00:02:00")
        ev = ops.slurm_wait(self.s, "pipeline-runner", run["id"], timeout=5, interval=1)
        p = ev["payload"]
        self.assertEqual((p["scheduler_state"], p["final"], p["waited"]), ("FAILED", True, True))
        self.assertEqual(p["jobs"], {"4300": "COMPLETED", "4301": "FAILED"})
        self.assertEqual(p["failed_jobs"], ["4301"])
        self.assertEqual(p["item_states"], {"sub-01": "COMPLETED", "sub-02": "COMPLETED", "sub-03": "COMPLETED"})
        self.assertNotIn("failed_items", p)
        n = len([e for e in self.led.events(self.w) if e["type"] == "run.status"])
        self.assertEqual(ops.slurm_status(self.s, "pipeline-runner", run["id"])["id"], ev["id"], "final state recorded once")
        self.assertEqual(len([e for e in self.led.events(self.w) if e["type"] == "run.status"]), n)
        from dureh_core.promote import _completed_run
        with self.assertRaisesRegex(DurehError, "no completed scheduler state"):
            _completed_run(self.led, run["id"], "projects/lna")
        ok = self.submit()
        self.all_done("4302")
        ev = ops.slurm_wait(self.s, "pipeline-runner", ok["id"], timeout=5, interval=1)
        self.assertEqual((ev["payload"]["scheduler_state"], ev["payload"]["failed_jobs"] if "failed_jobs" in ev["payload"] else None),
                         ("COMPLETED", None))
        self.assertEqual(_completed_run(self.led, ok["id"], "projects/lna")["id"], ok["id"], "promote --run accepts it")
        timeout = self.submit()
        self.sacct("4304", "4304_0|RUNNING|0:0", "4304_[1-2]|PENDING|0:0")
        ev = ops.slurm_wait(self.s, "pipeline-runner", timeout["id"], timeout=0, interval=1)
        self.assertFalse(ev["payload"]["final"])
        self.assertIn("still RUNNING", ev["note"])

    def test_watch_reports_a_via_run_once_when_every_job_ended(self):
        run = self.submit()
        reported = set()
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|FAILED|1:0", "4300_2|COMPLETED|0:0")
        self.assertEqual(ops.watch_round(self.s, "desk", reported), [], "the group job has no state yet")
        self.sacct("4301", "CANCELLED by 1|0:0|00:00:10")
        lines = ops.watch_round(self.s, "desk", reported)
        self.assertEqual(len(lines), 1)
        self.assertIn(f"run={run['id']} job=4300", lines[0])
        self.assertIn("state=FAILED items=COMPLETED:2,FAILED:1", lines[0])
        ev = [e for e in self.led.events(self.w) if e["type"] == "run.status"][-1]
        self.assertEqual((ev["payload"]["watched"], ev["payload"]["jobs"]), (True, {"4300": "FAILED", "4301": "CANCELLED"}))
        self.assertEqual(ev["payload"]["failed_items"], ["sub-02"])
        self.assertEqual(ops.watch_round(self.s, "desk", set()), [], "recorded final states are not reported again")

    def test_cancel_cancels_every_job(self):
        run = self.submit()
        ev = ops.slurm_cancel(self.s, "pipeline-runner", run["id"])
        self.assertEqual((self.fake / "scancel.args").read_text().split(), ["4300", "4301"])
        self.assertEqual(ev["payload"]["jobs"], ["4300", "4301"])

    # --------------------------------------------------------------- resubmit
    def test_resubmit_failed_reruns_the_command_with_items_arg(self):
        run = self.submit()
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|RUNNING|0:0", "4300_2|FAILED|1:0")
        with self.assertRaisesRegex(DurehError, "not every job has ended"):
            ops.slurm_resubmit_failed(self.s, "pipeline-runner", run["id"])
        self.sacct("4300", "4300_0|COMPLETED|0:0", "4300_1|TIMEOUT|0:0", "4300_2|FAILED|1:0")
        self.sacct("4301", "CANCELLED|0:0|00:00:01")
        with self.assertRaisesRegex(DurehError, "does not apply"):
            ops.slurm_resubmit_failed(self.s, "pipeline-runner", run["id"], script="x.sh")
        again = ops.slurm_resubmit_failed(self.s, "pipeline-runner", run["id"])
        p = again["payload"]
        self.assertEqual(p["resubmit_of"], run["id"])
        self.assertEqual(p["resubmit_states"], {"sub-02": "TIMEOUT", "sub-03": "FAILED"})
        items_file = Path(p["resubmit_items_file"])
        self.assertEqual((items_file.parent, items_file.read_text().split()), (self.logs, ["sub-02", "sub-03"]))
        args = self.args()[-1]
        self.assertIn(f"--items @{items_file}", args)
        self.assertIn("--submission-record", args)
        self.assertEqual(p["array"]["items"], ["sub-02", "sub-03"], "the module made the new array over the failed items")
        self.assertEqual(p["job_id"], "4302")
        self.all_done("4302")
        with self.assertRaisesRegex(DurehError, "every item completed"):
            ops.slurm_resubmit_failed(self.s, "pipeline-runner", again["id"])

    def test_resubmit_failed_without_items_arg_names_items_and_command(self):
        run = self.submit()
        self.sacct("4300", "4300_0|FAILED|1:0", "4300_1|COMPLETED|0:0", "4300_2|COMPLETED|0:0")
        self.sacct("4301", "COMPLETED|0:0|00:00:01")
        self.config({"record_arg": "--submission-record", "log_arg": "--log-dir"})
        n = len(self.args())
        with self.assertRaisesRegex(DurehError, r"did not complete: sub-01 .*no items_arg.*rerun by hand: slurm submit "
                                                f"--wi {self.w} --via 'camri run fmriprep .* @{self.logs}/via-failed-"):
            ops.slurm_resubmit_failed(self.s, "pipeline-runner", run["id"])
        self.assertEqual(len(self.args()), n, "nothing ran")
        self.assertEqual(len(self.runs()), 1, "nothing recorded")
        self.assertTrue(list(self.logs.glob("via-failed-*.items")), "the failed items are listed in a file")

    # ------------------------------------------------------------------ purge
    def test_purge_removes_the_recorded_logs_and_scripts_of_every_job(self):
        run = self.submit()
        mine = [self.logs / f"camri-camri-step-4300_{i}.{x}" for i in range(3) for x in ("out", "err")]
        mine += [self.logs / f"camri-camri-group-4301.{x}" for x in ("out", "err")]
        for f in mine:
            f.write_text("x")
        keep = self.logs / "camri-camri-step-4399_0.out"
        keep.write_text("another job")
        scripts = [Path(j["script"]) for j in run["payload"]["jobs"]]
        self.assertTrue(all(s.is_file() for s in scripts))
        wf.dispatch_stop(self.s, "desk", self.w, "test")
        out = wf.purge(self.s, "desk", self.w)
        self.assertEqual(sorted(out["removed_logs"]), sorted(str(f) for f in mine + scripts))
        self.assertTrue(keep.is_file())

    # ------------------------------------------------------------ start check
    def test_workday_start_check_is_warn_only(self):
        (self.fake / "squeue.out").write_text("2026-10-06T17:45:00\n")
        day = {"phase": "on-duty", "end": "2026-10-06T18:00:00-04:00"}
        err = io.StringIO()
        with mock.patch("dureh_core.workday.enabled", return_value=True), \
                mock.patch("dureh_core.workday.day", return_value=day), contextlib.redirect_stderr(err):
            run = self.submit()
        c = run["payload"]["start_check"]
        self.assertEqual((c["late"], c["past_end"], c["time"], c["time_source"]), (True, True, "00:30:00", "script"))
        self.assertEqual(c["expected_end"], "2026-10-06T18:15:00-04:00")
        self.assertEqual(run["payload"]["status"], "submitted", "warn-only: the run is recorded as submitted")
        self.assertIn("warning: workday mode: job 4300", err.getvalue())
        (self.fake / "squeue.out").write_text("N/A\n")
        with mock.patch("dureh_core.workday.enabled", return_value=True), mock.patch("dureh_core.workday.day", return_value=day):
            self.assertEqual(self.submit()["payload"]["start_check"], "unavailable")

    # ------------------------------------------------------------------ hook
    def test_bash_guard_lets_the_via_command_through(self):
        def bash(command, role="pipeline-runner"):
            data = dict(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": command},
                        cwd=str(self.site_root), agent_type=role, agent_id="a1")
            return hook(self.s, **data)

        out = bash(f"{self.site_root}/bin/dureh slurm submit --wi {self.w} --via '{self.via}'")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertTrue(denied(bash("sbatch job.sh")))

    # ---------------------------------------------------------------- ingest
    def test_ingest_checks_the_minimal_schema(self):
        rec = self.proj / "r.json"

        def load(data):
            rec.write_text(json.dumps(data))
            return ops.ingest_submission(rec, self.proj, self.s)

        base = {"schema_version": "x.v1", "jobs": [{"job_id": 7, "kind": "single", "script": "code/job.sh",
                                                     "logs": {"stdout": "logs/%j.out"}}]}
        out = load(base)
        self.assertEqual(out["jobs"][0], {"job_id": "7", "kind": "single", "script": f"{self.proj}/code/job.sh",
                                          "log_out": f"{self.proj}/logs/%j.out", "log_err": None})
        self.assertEqual(load({**base, "schema": "s"})["schema_version"], "x.v1")
        self.assertEqual(load({"schema": "s", "jobs": base["jobs"]})["schema_version"], "s")
        arr = {"schema_version": "x", "jobs": [{"job_id": "9", "kind": "array", "items": ["a", "b"], "log_out": "o-%A_%a"}]}
        self.assertEqual(load(arr)["jobs"][0]["items"], ["a", "b"])
        self.assertEqual(load(arr)["jobs"][0]["level"], "plain")
        stems = {**arr, "jobs": [{"job_id": "9", "kind": "array", "items": ["sub-01_ses-1_task-rest_bold"]}], "level": "file"}
        self.assertEqual(load(stems)["jobs"][0]["level"], "file", "a module's file-level items are not parsed")
        for bad, msg in (({"jobs": base["jobs"]}, "no schema_version"), ({"schema_version": "x"}, "no jobs"),
                         ({"schema_version": "x", "jobs": [{"kind": "single"}]}, "no usable job_id"),
                         ({"schema_version": "x", "jobs": [{"job_id": "1", "kind": "task"}]}, "kind must be array or single"),
                         ({"schema_version": "x", "jobs": [{"job_id": "1", "kind": "array"}]}, "lists items"),
                         ({"schema_version": "x", "jobs": [{"job_id": "1", "kind": "array", "count": 3, "items": ["a"]}]}, "differs"),
                         ({"schema_version": "x", "jobs": [{"job_id": "1", "kind": "array", "items": ["a", "a"]}]}, "duplicate"),
                         ({"schema_version": "x", "jobs": [{"job_id": "1", "kind": "array", "items": ["../x"]}]}, "do not match"),
                         ({"schema_version": "x", "jobs": [{"job_id": "1 2", "kind": "single"}]}, "no usable job_id")):
            with self.assertRaisesRegex(DurehError, msg):
                load(bad)
        rec.write_text("not json")
        with self.assertRaisesRegex(DurehError, "not readable JSON"):
            ops.ingest_submission(rec, self.proj, self.s)
