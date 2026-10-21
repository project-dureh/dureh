"""Release 77: a job the desk submits itself wakes the desk when it ends (project-dureh/dureh#38: `wake` command,
`slurm wait --notify`, `day status` lists unchecked desk jobs) and the harness unit suite runs as a scheduler job
(project-dureh/dureh#41: `verify --self --submit` / `--collect`). Fake sbatch/sacct only; job ids are made up."""
import contextlib
import io
import json

from helpers import SiteCase

from dureh_core import ops, selftest, workday
from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.org import HARNESS_ROOT
from dureh_core.records import Ledger
from dureh_core.runtimes import claude_code
from dureh_core.util import DurehError


class DeskWake(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        (self.fake / "norun").write_text("")
        self.tool("sbatch", f"echo \"$@\" >> {self.fake}/sbatch.args\n"
                            "dir=.; for a in \"$@\"; do case \"$a\" in --chdir=*) dir=${a#--chdir=};; esac; done\n"
                            f"if [ ! -e {self.fake}/norun ]; then (cd \"$dir\" && bash \"${{@: -1}}\" > /dev/null 2>&1); fi\n"
                            "echo 5100\n")
        self.tool("sacct", f"echo x >> {self.fake}/sacct.calls\n"
                           f"l=$(cat {self.fake}/sacct.out 2>/dev/null || echo 'COMPLETED|0:0|00:00:02')\n"
                           "case \"$*\" in *JobID,State*) echo \"5100|$l\";; *) echo \"$l\";; esac\n")
        self.tool("scancel", f"echo \"$@\" >> {self.fake}/scancel.args\n")
        self.config(env={"path_prepend": [str(self.fake)]}, checks={"runner": "slurm", "interval": 1})
        self.set_watch("off")
        self.s = self.site()
        self.led = Ledger(self.s)

    def config(self, **kw):
        path = self.site_root / "config.json"
        cfg = json.loads(path.read_text())
        cfg.update(kw)
        path.write_text(json.dumps(cfg))

    def set_watch(self, mode):
        path = self.site_root / "config.json"
        cfg = json.loads(path.read_text())
        cfg["slurm"] = {**(cfg.get("slurm") or {}), "watch": mode}
        path.write_text(json.dumps(cfg))
        self.s = self.site()

    def tool(self, name, body):
        (self.fake / name).write_text("#!/bin/bash\n" + body)
        (self.fake / name).chmod(0o755)

    def state(self, line):
        (self.fake / "sacct.out").write_text(line + "\n")

    def cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["--site", str(self.site_root), *argv])
        return buf.getvalue()

    def staged_item(self):
        if not hasattr(self, "bundle"):
            self.bundle = wf.wi_new(self.s, "desk", "Bundle", "g", "pm-modules", "modules/demo", ["ok"])["id"]
            wf.wi_approve(self.s, "desk", self.bundle, "ok")
            self.led.event("wi.dispatched", "desk", self.bundle, {"role": "pm-modules", "brief": "0" * 16, "by": "desk"})
        wi = wf.wi_new(self.s, "desk", "Check g", "g", "py-writer", "modules/demo", ["ok"], checks=["true"],
                       parent=self.bundle)["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        wf.dispatch_brief(self.s, wi, "pm-modules")  # creates the WI worktree
        self.led.event("wi.dispatched", "desk", wi, {"role": "py-writer", "brief": "1" * 16, "by": "desk"})
        wf.wi_stage(self.s, "py-writer", wi, "done")
        return wi

    # ------------------------------------------------------------------ #38
    def test_desk_verify_submit_prints_one_wake_command(self):
        self.state("RUNNING|0:0|00:01:00")
        wi = self.staged_item()
        out = json.loads(self.cli("verify", "--wi", wi, "--submit", "--actor", "desk"))
        self.assertEqual(out["wake"], f"{self.site_root}/bin/dureh slurm wait --run {out['run']} --notify")

    def test_no_wake_for_other_roles_or_with_a_standing_watch(self):
        self.state("RUNNING|0:0|00:01:00")
        out = json.loads(self.cli("verify", "--wi", self.staged_item(), "--submit", "--actor", "checker"))
        self.assertNotIn("wake", out, "a checker ends its turn; the desk re-dispatches it")
        self.set_watch("monitor")
        out = json.loads(self.cli("verify", "--wi", self.staged_item(), "--submit", "--actor", "desk"))
        self.assertNotIn("wake", out, "the standing `slurm watch` reports it")
        self.assertIsNone(ops.desk_wake(self.s, "desk", "run-x"))

    def test_wait_notify_prints_one_line_and_records_the_final_state(self):
        self.state("RUNNING|0:0|00:01:00")
        wi = self.staged_item()
        run = wf.verify_submit(self.s, "desk", wi)["run"]
        self.assertEqual([j["run"] for j in ops.unchecked_desk_jobs(self.s)], [run])
        self.state("FAILED|1:0|00:02:00")
        text = self.cli("slurm", "wait", "--run", run, "--notify", "--interval", "1")
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 1, text)
        self.assertTrue(lines[0].startswith(f"FAILED run={run} job=5100 wi={wi}"), lines[0])
        self.assertIn("checker collects verify run", lines[0])
        final = [e for e in Ledger(self.s).events(wi) if e["type"] == "run.status" and e["payload"].get("final")]
        self.assertEqual(final[-1]["payload"]["scheduler_state"].split()[0], "FAILED")
        self.assertEqual(ops.unchecked_desk_jobs(self.s), [], "a recorded end is checked")
        # a second wait answers from the record without asking the scheduler
        calls = (self.fake / "sacct.calls").read_text().count("x")
        again = self.cli("slurm", "wait", "--run", run, "--notify")
        self.assertTrue(again.startswith(f"FAILED run={run}"), again)
        self.assertEqual((self.fake / "sacct.calls").read_text().count("x"), calls)

    def test_wait_notify_timeout_says_not_ended(self):
        self.state("RUNNING|0:0|00:01:00")
        run = wf.verify_submit(self.s, "desk", self.staged_item())["run"]
        line = ops.wait_notify(self.s, "desk", run, timeout=0, interval=1)
        self.assertTrue(line.startswith(f"NOT-ENDED run={run}"), line)
        self.assertIn("--notify", line)

    def test_day_status_lists_unchecked_desk_jobs_only(self):
        self.state("RUNNING|0:0|00:01:00")
        mine = wf.verify_submit(self.s, "desk", self.staged_item())["run"]
        wf.verify_submit(self.s, "checker", self.staged_item())  # not the desk's own
        st = workday.status(self.s)
        self.assertEqual([j["run"] for j in st["desk_jobs"]], [mine])
        text = workday.render(st)
        self.assertIn(f"Desk jobs not checked (1): {mine} job 5100 verify", text)
        self.state("COMPLETED|0:0|00:02:00")
        ops.slurm_status(self.s, "desk", mine)
        self.assertNotIn("desk_jobs", workday.status(self.s))

    def test_desk_text_names_the_wake_rule_without_a_standing_watch(self):
        claude_code.generate(self.s)
        md = (self.site_root / "CLAUDE.md").read_text()
        self.assertIn("slurm wait --run <id> --notify`: start it as a Monitor right after submitting (longest timeout; re-arm it", md)
        self.assertIn("day status` lists your submitted jobs not yet checked", md)
        self.set_watch("monitor")
        claude_code.generate(self.s)
        self.assertNotIn("--notify", (self.site_root / "CLAUDE.md").read_text())

    # ------------------------------------------------------------------ #41
    def finish_unit(self, sub, rc, report):
        prefix = Ledger(self.s).get("run", sub["run"])["payload"]["prefix"]
        with open(f"{prefix}.unit.exit", "w") as f:
            f.write(f"{rc}\n")
        with open(f"{prefix}.unit.out", "w") as f:
            f.write(report)

    def test_self_submit_runs_the_unit_suite_as_one_job_with_the_site_config(self):
        out = json.loads(self.cli("verify", "--self", "--submit", "--actor", "desk"))
        self.assertTrue(out["submitted"])
        self.assertEqual(out["wake"], f"{self.site_root}/bin/dureh slurm wait --run {out['run']} --notify")
        args = (self.fake / "sbatch.args").read_text()
        self.assertIn("--account=acct-x", args)
        self.assertIn("--partition=general", args)
        self.assertIn("-selftest", args)
        run = Ledger(self.s).get("run", out["run"])["payload"]
        self.assertEqual((run["run_type"], run["status"], run["job_id"]), ("selftest", "submitted", "5100"))
        with open(run["script"]) as f:
            script = f.read()
        self.assertIn("unittest discover -s tests -t .", script)
        self.assertIn(str(self.site_root / "logs" / "slurm" / "selftest"), run["prefix"])
        self.assertEqual([j["run"] for j in ops.unchecked_desk_jobs(self.s)], [out["run"]])

    def test_self_collect_refuses_while_running_then_records_pass_or_fail(self):
        self.state("RUNNING|0:0|00:01:00")
        sub = selftest.submit_self(self.s, "desk")
        with self.assertRaisesRegex(DurehError, "still RUNNING; nothing was recorded"):
            selftest.collect_self(self.s, "desk")
        self.finish_unit(sub, 0, "....\n----\nRan 4 tests in 1.0s\n\nOK\n")
        self.state("COMPLETED|0:0|00:05:00")
        line = ops.wait_notify(self.s, "desk", sub["run"])
        self.assertEqual(line, f"COMPLETED run={sub['run']} job=5100 wi=- next=verify --self --collect {sub['run']}")
        r = selftest.collect_self(self.s, "desk")
        self.assertEqual(r["result"], "pass", r)
        self.assertEqual(r["collected_from"], sub["run"])
        with self.assertRaisesRegex(DurehError, "already collected"):
            selftest.collect_self(self.s, "desk", sub["run"])
        sub2 = selftest.submit_self(self.s, "desk")
        report = ("=" * 70 + "\nFAIL: test_x (tests.test_a.A.test_x)\n" + "-" * 70 +
                  "\nTraceback (most recent call last):\nAssertionError: 1 != 2\n\n" + "-" * 70 +
                  "\nRan 4 tests in 1.0s\n\nFAILED (failures=1)\n")
        self.finish_unit(sub2, 1, report)
        self.state("FAILED|1:0|00:05:00")
        r = selftest.collect_self(self.s, "desk")
        self.assertEqual(r["result"], "fail")
        self.assertTrue(any("FAIL tests.test_a.A.test_x" in p for p in r["problems"]), r["problems"])

    def test_self_submit_needs_a_scheduler(self):
        self.config(checks={"runner": "local"})
        with self.assertRaisesRegex(DurehError, "needs a scheduler"):
            selftest.submit_self(self.site(), "desk")
        self.assertEqual([r for r in Ledger(self.s).load("run") if r["payload"].get("status") == "submitted"], [])

    def test_landing_text_names_skip_unit_then_submit(self):
        claude_code.generate(self.s)
        md = (self.site_root / "CLAUDE.md").read_text()
        self.assertIn("verify --self --skip-unit`, then", md)
        self.assertIn("verify --self --submit` (the unit suite as a scheduler job", md)
        text = (HARNESS_ROOT / "tools" / "release.py").read_text()
        self.assertIn("verify --self --skip-unit", text)
        self.assertIn("verify --self --submit", text)
