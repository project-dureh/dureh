"""Release 37 (UR-20261005-02, a company site's WI-0122): a checker lost two background `verify --wi` runs to the
end-of-agent cleanup, rework briefs carried no failure context, and `wi amend --constraint` replaced the whole list.
`verify --wi --submit/--collect`, `slurm watch` lines for verify jobs, lanes held by a pending verify job, the
"Last verification" block of rework briefs, and `wi amend --add-constraint`. Fake sbatch/sacct only."""
import contextlib
import io
import json
import subprocess
import unittest

from helpers import SiteCase

from dureh_core import ops
from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.records import Ledger
from dureh_core.util import DurehError


class AsyncVerify(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        # sbatch: logs its arguments, runs the job script at once unless `norun` exists, prints a job id
        self.tool("sbatch", f"echo \"$@\" >> {self.fake}/sbatch.args\n"
                            "dir=.; for a in \"$@\"; do case \"$a\" in --chdir=*) dir=${a#--chdir=};; esac; done\n"
                            f"if [ ! -e {self.fake}/norun ]; then (cd \"$dir\" && bash \"${{@: -1}}\" > /dev/null 2>&1); fi\n"
                            "echo 5100\n")
        # sacct: the state in sacct.out (default COMPLETED); the batched format (JobID first) gets the job id prefix
        self.tool("sacct", f"l=$(cat {self.fake}/sacct.out 2>/dev/null || echo 'COMPLETED|0:0|00:00:02')\n"
                           "case \"$*\" in *JobID,State*) echo \"5100|$l\";; *) echo \"$l\";; esac\n")
        self.tool("scancel", f"echo \"$@\" >> {self.fake}/scancel.args\n")
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg.update(env={"path_prepend": [str(self.fake)]}, checks={"runner": "slurm", "interval": 1})
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        self.led = Ledger(self.s)
        self.bundle = wf.wi_new(self.s, "desk", "Bundle", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", self.bundle, "ok")
        self.led.event("wi.dispatched", "desk", self.bundle, {"role": "pm-modules", "brief": "0" * 16, "by": "desk"})

    def tool(self, name, body):
        (self.fake / name).write_text("#!/bin/bash\n" + body)
        (self.fake / name).chmod(0o755)

    def item(self, checks, **kw):
        wi = wf.wi_new(self.s, "desk", "Add g()", "g", "py-writer", "modules/demo", ["ok"], parent=self.bundle,
                       checks=checks, **kw)["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        wf.dispatch_brief(self.s, wi, "pm-modules")  # creates the WI worktree
        self.led.event("wi.dispatched", "desk", wi, {"role": "py-writer", "brief": "1" * 16, "by": "desk"})
        wf.wi_stage(self.s, "py-writer", wi, "done")
        return wi

    def running(self):
        (self.fake / "norun").write_text("")
        (self.fake / "sacct.out").write_text("RUNNING|0:0|00:01:00\n")

    def finish(self, sub):
        """The node runs the job script; accounting says COMPLETED."""
        (self.fake / "norun").unlink()
        subprocess.run(["bash", sub["script"]], capture_output=True, check=False)
        (self.fake / "sacct.out").write_text("COMPLETED|0:0|00:02:00\n")

    def submit(self, wi):
        r = wf.verify_submit(self.s, "checker", wi)
        self.assertTrue(r["submitted"], r)
        r["script"] = self.led.get("run", r["run"])["payload"]["script"]
        return r

    # ------------------------------------------------------------ submit/collect
    def test_submit_returns_the_run_id_and_records_a_pending_run(self):
        wi = self.item(["true"])
        self.running()
        r = self.submit(wi)
        run = self.led.get("run", r["run"])["payload"]
        self.assertEqual((run["run_type"], run["status"], run["runner"], run["job_id"]), ("verify", "submitted", "slurm", "5100"))
        for k in ("script", "log_out", "log_err", "head", "checks", "prefix"):
            self.assertTrue(run.get(k), k)
        self.assertEqual(self.led.wi_status(wi)["status"], "staged", "nothing is verified yet")
        self.assertEqual(wf.pending_verify(self.led, wi)["id"], r["run"])
        with self.assertRaisesRegex(DurehError, "waiting to be collected"):
            wf.verify_submit(self.s, "checker", wi)
        buf = io.StringIO()
        other = self.item(["true"])
        with contextlib.redirect_stdout(buf):
            main(["--site", str(self.site_root), "verify", "--wi", other, "--submit", "--actor", "checker"])
        out = json.loads(buf.getvalue())
        self.assertTrue(out["submitted"])
        self.assertEqual(out["job_id"], "5100")
        self.assertTrue(out["run"])

    def test_collect_while_running_is_refused_and_records_nothing(self):
        wi = self.item(["true"])
        self.running()
        r = self.submit(wi)
        n = len(self.led.load("run"))
        with self.assertRaisesRegex(DurehError, "still RUNNING; nothing was recorded"):
            wf.verify_collect(self.s, "checker", wi)
        (self.fake / "sacct.out").write_text("PENDING|0:0|00:00:00\n")
        with self.assertRaisesRegex(DurehError, "still PENDING"):
            wf.verify_collect(self.s, "checker", wi, r["run"])
        self.assertEqual(len(self.led.load("run")), n)
        self.assertEqual(self.led.wi_status(wi)["status"], "staged")

    def test_collect_after_completion_records_like_the_synchronous_path(self):
        for checks in (["true", "test -f src/demo.py && echo seen"], ["false", "echo fine"]):
            sync_wi, async_wi = self.item(checks), self.item(checks)
            sync = wf.verify_wi(self.s, "checker", sync_wi)
            self.running()
            sub = self.submit(async_wi)
            self.finish(sub)
            got = wf.verify_collect(self.s, "checker", async_wi)
            for k in ("result", "violations", "missing_outputs", "runner", "changed"):
                self.assertEqual(got[k], sync[k], k)
            self.assertEqual([(c["command"], c["exit"]) for c in got["checks"]],
                             [(c["command"], c["exit"]) for c in sync["checks"]])
            self.assertEqual([c["tail"] for c in got["checks"]], [c["tail"] for c in sync["checks"]])
            self.assertEqual(got["note"], sync["note"])
            self.assertEqual(got["collected_from"], sub["run"])
            self.assertEqual(self.led.wi_status(async_wi)["status"], self.led.wi_status(sync_wi)["status"])
            types = [e["type"] for e in self.led.events(async_wi)][-3:]
            self.assertIn("verification.recorded", types)
            self.assertIsNone(wf.pending_verify(self.led, async_wi))
        self.assertEqual(self.led.wi_status(async_wi)["status"], "rework")

    def test_collect_refuses_a_moved_head(self):
        wi = self.item(["true"])
        self.running()
        sub = self.submit(wi)
        self.finish(sub)
        wt = self.site_root / "worktrees" / wi
        (wt / "x.txt").write_text("x\n")
        subprocess.run(["git", "add", "x.txt"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=wt, check=True)
        with self.assertRaisesRegex(DurehError, "Submit again"):
            wf.verify_collect(self.s, "checker", wi)

    def test_nothing_to_submit_verifies_synchronously(self):
        wi = self.item([])
        r = wf.verify_submit(self.s, "checker", wi)
        self.assertEqual((r["submitted"], r["result"]), (False, "pass"))
        self.assertFalse((self.fake / "sbatch.args").exists())

    # ---------------------------------------------------------- watch, lanes
    def test_slurm_watch_reports_the_verify_job_with_the_collect_step(self):
        wi = self.item(["true"])
        self.running()
        sub = self.submit(wi)
        reported = set()
        self.assertEqual(ops.watch_round(self.s, "desk", reported), [], "still running")
        self.finish(sub)
        lines = ops.watch_round(self.s, "desk", reported)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(f"run={sub['run']} job=5100 wi={wi}", lines[0])
        self.assertIn(f"state=COMPLETED next=dispatch brief --wi {self.bundle} (bundle; its manager dispatches the checker"
                      f" for {wi}: checker collects verify run {sub['run']})", lines[0])
        self.assertEqual(ops.watch_round(self.s, "desk", set()), [], "recorded once")
        r = wf.verify_collect(self.s, "checker", wi)  # uses the state the watch recorded
        self.assertEqual(r["result"], "pass")
        self.assertEqual(r["job"]["state"], "COMPLETED")

    def test_top_level_line_names_the_checker_dispatch(self):
        wi = self.item(["true"])
        self.running()
        sub = self.submit(wi)
        run = self.led.get("run", sub["run"])
        # the same run seen as a top-level item's: drop the parent from the payload the line reads
        rec = self.led.get("work-item", wi)
        rec["payload"]["parent"] = None
        orig = Ledger.get
        try:
            Ledger.get = lambda led, kind, rid: rec if (kind, rid) == ("work-item", wi) else orig(led, kind, rid)
            line = ops._watch_line(self.led, self.led.events(), run, {"state": "COMPLETED"})
        finally:
            Ledger.get = orig
        self.assertIn(f"next=dispatch brief --wi {wi} --role checker (checker collects verify run {sub['run']})", line)

    def test_a_pending_verify_job_holds_the_lane(self):
        wi = self.item(["true"])
        self.assertEqual(ops.lane_holders(self.s, self.led), [])
        self.running()
        sub = self.submit(wi)
        holders = ops.lane_holders(self.s, Ledger(self.s))
        self.assertEqual([h["wi"] for h in holders], [self.bundle])
        self.assertIn(f"job 5100 RUNNING on {wi} ({sub['run']})", holders[0]["by"])
        self.finish(sub)
        ops.watch_round(self.s, "desk", set())
        self.assertEqual(ops.lane_holders(self.s, Ledger(self.s)), [], "released once its end is recorded")

    # ------------------------------------------------------------ briefs
    def test_checker_brief_says_submit_then_collect(self):
        wi = self.item(["true"])
        b = wf.dispatch_brief(self.s, wi, "pm-modules", as_role="checker")["brief"]
        self.assertIn(f"verify --wi {wi} --submit`, report the run id", b)
        self.assertIn("Never run verify in the background", b)
        self.running()
        sub = self.submit(wi)
        b = wf.dispatch_brief(self.s, wi, "pm-modules", as_role="checker")["brief"]
        self.assertIn(f"Verify run {sub['run']}", b)
        self.assertIn(f"verify --wi {wi} --collect`", b)

    def test_rework_brief_carries_the_last_failed_verification(self):
        long = "for i in $(seq 1 30); do echo line-$i; done; exit 3"
        wi = self.item([long, "true"])
        self.running()
        sub = self.submit(wi)
        self.finish(sub)
        wf.verify_collect(self.s, "checker", wi)
        self.assertEqual(self.led.wi_status(wi)["status"], "rework")
        r = wf.dispatch_brief(self.s, wi, "pm-modules")
        brief = r["brief"]
        self.assertIn("Last verification (run ", brief)
        self.assertIn("- check 1 `", brief)
        self.assertIn("exit 3", brief)
        self.assertNotIn("- check 2", brief, "passed checks are not listed")
        self.assertIn("line-30", brief)
        self.assertIn("line-11", brief)
        self.assertNotIn("line-10\n", brief, "at most 20 last lines per check")
        run = self.led.get("run", sub["run"])["payload"]
        self.assertIn(f"{run['prefix']}.check1.out", brief)
        self.assertIn(run["log_out"], brief)
        prep = [e for e in self.led.events(wi) if e["type"] == "dispatch.prepared"][-1]["payload"]
        block = brief.split("Last verification", 1)[1]
        self.assertGreater(prep["verification_bytes"], 0)
        self.assertEqual(prep["bytes"], len(brief.encode()) - len(r["header"].encode()) - 2 - prep["verification_bytes"],
                         "the recorded budget bytes cover the desk-written part only")
        self.assertLessEqual(prep["verification_bytes"], wf.VERIFICATION_BLOCK_BYTES)
        self.assertTrue(block)
        # a first dispatch carries no block
        fresh = wf.wi_new(self.s, "desk", "f", "g", "py-writer", "modules/demo", ["ok"], parent=self.bundle)["id"]
        wf.wi_approve(self.s, "desk", fresh, "ok")
        self.assertNotIn("Last verification", wf.dispatch_brief(self.s, fresh, "pm-modules")["brief"])

    def test_block_is_outside_the_budget_and_capped(self):
        noisy = "python3 -c \"print(('x' * 190 + chr(10)) * 40)\"; exit 1"
        wi = self.item([noisy, noisy + " ", noisy + "  "])
        self.running()
        sub = self.submit(wi)
        self.finish(sub)
        wf.verify_collect(self.s, "checker", wi)
        block = wf.last_verification_block(self.led, wi)
        self.assertLessEqual(len(block.encode()), wf.VERIFICATION_BLOCK_BYTES)
        self.assertIn("- check 3", block, "every failed check is named even when the lines are cut")
        limit = int(self.s.limit("worker_brief_bytes", 2048))
        r = wf.dispatch_brief(self.s, wi, "pm-modules")
        self.assertGreater(len(r["brief"].encode()), limit, "the block does not count against the 2048-byte budget")

    # ------------------------------------------------------------ amend
    def test_add_constraint_appends_and_constraint_replaces(self):
        wi = wf.wi_new(self.s, "desk", "c", "g", "py-writer", "modules/demo", ["ok"], parent=self.bundle,
                       constraints=["Skip docs/.", "No push."])["id"]
        ev = wf.wi_amend(self.s, "desk", wi, "plan ok", {}, add_constraints=["Fix test_x first."])
        self.assertEqual(self.led.get("work-item", wi)["payload"]["constraints"],
                         ["Skip docs/.", "No push.", "Fix test_x first."])
        self.assertEqual(ev["payload"]["added_constraints"], ["Fix test_x first."])
        wf.wi_amend(self.s, "desk", wi, "plan ok", {"constraints": ["Only this."]})
        self.assertEqual(self.led.get("work-item", wi)["payload"]["constraints"], ["Only this."])
        wf.wi_amend(self.s, "desk", wi, "plan ok", {"constraints": ["A."]}, add_constraints=["B.", "C."])
        self.assertEqual(self.led.get("work-item", wi)["payload"]["constraints"], ["A.", "B.", "C."])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["--site", str(self.site_root), "wi", "amend", "--wi", wi, "--memo", "ok", "--add-constraint", "D.",
                  "--add-constraint", "E.", "--actor", "desk"])
        self.assertEqual(self.led.get("work-item", wi)["payload"]["constraints"], ["A.", "B.", "C.", "D.", "E."])

    def test_staged_items_take_add_constraint_only(self):
        wi = self.item(["true"], constraints=["No push."])
        ev = wf.wi_amend(self.s, "desk", wi, "plan ok", {}, add_constraints=["Log is at logs/x."])
        self.assertEqual(ev["payload"]["status"], "staged")
        self.assertEqual(self.led.wi_status(wi)["status"], "staged", "appending does not move the item")
        self.assertEqual(self.led.get("work-item", wi)["payload"]["constraints"], ["No push.", "Log is at logs/x."])
        for fields, rebase in (({"constraints": ["x"]}, None), ({"checks": ["true"]}, None), ({}, "")):
            with self.assertRaisesRegex(DurehError, f"dispatch stop --wi {wi}.*wi amend --wi {wi}"):
                wf.wi_amend(self.s, "desk", wi, "ok", fields, rebase=rebase, add_constraints=["y"] if fields else None)
        with self.assertRaisesRegex(DurehError, "needs text"):
            wf.wi_amend(self.s, "desk", wi, "ok", {}, add_constraints=[" "])


if __name__ == "__main__":
    unittest.main()
