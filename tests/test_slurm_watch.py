"""Release 30: jobs end outside agent turns. `dureh slurm watch` reports each ended job once; lanes count only
live agents or running jobs; a dispatch whose agent never started is superseded; refused wait forms carry the
rule. Fake sbatch/sacct only (never the real session job: SLURM_JOB_ID is unset by SiteCase)."""
import io
import json
import unittest
from pathlib import Path

from helpers import SiteCase, denied, hook

from dureh_core import ops
from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.records import Ledger
from dureh_core.runtimes.claude_code import WAIT_HINT
from dureh_core.util import DurehError, read_json

FINDING = "fnd-20261004T214606784656Z-a4f231c9"  # a company site, 2026-10-04: WI-0117's 21:25 dispatch never started


class WatchCase(SiteCase):
    """desk -> pm-projects bundle -> pipeline-runner item on a git project, with a fake scheduler."""

    def setUp(self):
        super().setUp()
        manifest = self.package / "startup.yaml"
        manifest.write_text(manifest.read_text()
                            .replace("managers: [pm-modules]", "managers: [pm-modules, pm-projects]")
                            .replace("workers: [py-writer,", "workers: [data-steward, pipeline-runner, py-writer,"))
        self.install()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        self.tool("sbatch", "case \"$*\" in *--test-only*) echo 'sbatch: Job 9 to start at 2026-10-03T10:00:00 using 1"
                            " processors on nodes c1 in partition general' >&2; exit 0;; esac\n"
                            f"n=$(cat {self.fake}/n 2>/dev/null || echo 4300); echo $((n+1)) > {self.fake}/n\necho \"$n\"\n")
        # sacct: `-j` takes a comma list; each job prints the file sacct-<job>. With the batched format
        # (JobID first) a single job's line `STATE|x:y|t` is printed as `<job>|STATE|x:y|t`. One line per call
        # goes to sacct.calls.
        self.tool("sacct", "jobs=''; prev=''; batch=''; for a in \"$@\"; do [ \"$prev\" = -j ] && jobs=$a;"
                           " [ \"$a\" = --format=JobID,State,ExitCode,Elapsed ] && batch=1; prev=$a; done\n"
                           f"echo \"$jobs\" >> {self.fake}/sacct.calls\n"
                           "for job in $(echo \"$jobs\" | tr ',' ' '); do\n"
                           f"  f={self.fake}/sacct-$job; [ -f \"$f\" ] || continue\n"
                           "  if [ -n \"$batch\" ]; then while IFS= read -r l; do case \"$l\" in \"$job\"_*) echo \"$l\";;"
                           " *) echo \"$job|$l\";; esac; done < \"$f\"; else cat \"$f\"; fi\n"
                           "done; exit 0\n")
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["env"] = {"path_prepend": [str(self.fake)]}
        cfg["containers"] = {"module": ""}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        self.led = Ledger(self.s)
        self.proj = self.site_root / "projects" / "lna"
        (self.proj / "checkpoints").mkdir(parents=True)
        import subprocess
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.proj)], check=True)
        self.bundle = wf.wi_new(self.s, "desk", "Bundle", "g", "pm-projects", "projects/lna", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", self.bundle, "ok")
        self.item = wf.wi_new(self.s, "desk", "Run", "g", "pipeline-runner", "projects/lna", ["ok"], parent=self.bundle)["id"]
        wf.wi_approve(self.s, "desk", self.item, "ok")
        self.script = self.proj / "checkpoints" / "step1.sh"
        self.script.write_text("#!/bin/bash\n#SBATCH --time=00:05:00\necho hi\n")

    # ------------------------------------------------------------- helpers
    def tool(self, name, body):
        (self.fake / name).write_text("#!/bin/sh\n" + body)
        (self.fake / name).chmod(0o755)

    def sacct(self, job, *lines):
        (self.fake / f"sacct-{job}").write_text("".join(f"{x}\n" for x in lines))

    def spawn(self, caller, target, brief, agent_id=None):
        data = dict(hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
                    tool_input={"subagent_type": target, "prompt": brief, "description": "x"})
        if caller != "desk":
            data.update(agent_type=caller, agent_id=agent_id)
        return hook(self.s, **data)

    def runtime(self, name, role, agent_id):
        hook(self.s, hook_event_name=name, agent_type=role, agent_id=agent_id)

    def dispatch_bundle(self, manager_agent="a-pm"):
        """The desk dispatches the bundle and its manager starts (bound by its brief at the first tool call)."""
        self.spawn("desk", "pm-projects", wf.dispatch_brief(self.s, self.bundle, "desk")["brief"])
        self.runtime("SubagentStart", "pm-projects", manager_agent)
        hook(self.s, hook_event_name="PreToolUse", tool_name="Bash", cwd=str(self.site_root), agent_type="pm-projects",
             agent_id=manager_agent, tool_input={"command": "ls"})  # first tool call: bound to the one pending dispatch

    def dispatch_item(self, agent_id="a-pm"):
        """The manager prepares and spawns the item (wi.dispatched); whether a worker starts is up to the test."""
        brief = wf.dispatch_brief(self.s, self.item, "pm-projects")["brief"]
        self.assertIsNone(self.spawn("pm-projects", "pipeline-runner", brief, agent_id))
        return brief

    def backdate(self, event_id, seconds):
        """Age a record and everything before it by `seconds` (records are files; the order is kept)."""
        from datetime import datetime, timedelta
        d = self.s.record_dir("event")
        until = read_json(d / f"{event_id}.json")["ts"]
        for path in d.glob("ev-*.json"):
            rec = read_json(path)
            if rec["ts"] <= until:
                rec["ts"] = (datetime.fromisoformat(rec["ts"]) - timedelta(seconds=seconds)).isoformat()
                path.write_text(json.dumps(rec))

    def last_dispatch(self, wi):
        return [e for e in self.led.events(wi) if e["type"] == "wi.dispatched"][-1]

    def submit(self, **kw):
        return ops.slurm_submit(self.s, "pipeline-runner", self.item, str(self.script), **kw)

    def watch(self, reported=None):
        buf = io.StringIO()
        lines = ops.slurm_watch(self.s, "desk", once=True, out=buf)
        self.assertEqual(buf.getvalue().splitlines(), lines)
        return lines


class Watch(WatchCase):
    def setUp(self):
        super().setUp()
        self.dispatch_bundle()
        self.dispatch_item()
        self.runtime("SubagentStart", "pipeline-runner", "a-w1")
        self.runtime("SubagentStop", "pipeline-runner", "a-w1")  # the worker ended its turn while its job runs

    def test_one_line_per_ended_job_recorded_once_across_restarts(self):
        single = self.submit()
        array = self.submit(each="sub-01,sub-02,sub-03")
        self.assertEqual((single["payload"]["job_id"], array["payload"]["job_id"]), ("4300", "4301"))
        self.sacct("4300", "RUNNING|0:0|00:01:00")
        self.sacct("4301", "4301_0|COMPLETED|0:0", "4301_1|RUNNING|0:0", "4301_2|PENDING|0:0")
        self.assertEqual(self.watch(), [], "nothing has ended")
        self.sacct("4300", "COMPLETED|0:0|00:02:00")
        lines = self.watch()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        for part in (f"run={single['id']}", "job=4300", f"wi={self.item}", "role=pipeline-runner", "agent=a-w1:ended",
                     "state=COMPLETED", f"next=the manager of bundle {self.bundle} re-dispatches {self.item}"):
            self.assertIn(part, line)
        self.assertNotIn("4301", line)
        recorded = [e for e in self.led.events(self.item) if e["type"] == "run.status" and e["payload"]["run"] == single["id"]]
        self.assertEqual(len(recorded), 1)
        self.assertEqual((recorded[0]["payload"]["final"], recorded[0]["payload"]["watched"], recorded[0]["actor"]),
                         (True, True, "desk"))
        self.assertEqual(self.watch(), [], "a fresh watch (restart) reports nothing twice: the record remembers")
        self.sacct("4301", "4301_0|COMPLETED|0:0", "4301_1|FAILED|1:0", "4301_2|CANCELLED by 1|0:0")
        lines = self.watch()
        self.assertEqual(len(lines), 1)
        self.assertIn(f"run={array['id']} job=4301", lines[0])
        self.assertIn("state=FAILED items=CANCELLED:1,COMPLETED:1,FAILED:1", lines[0])
        arr = [e for e in self.led.events(self.item) if e["type"] == "run.status" and e["payload"]["run"] == array["id"]]
        self.assertEqual(len(arr), 1)
        self.assertEqual(arr[0]["payload"]["failed_items"], ["sub-02", "sub-03"])
        self.assertTrue(arr[0]["payload"]["watched"])
        self.assertEqual(self.watch(), [])
        self.assertEqual(ops.slurm_status(self.s, "pipeline-runner", array["id"])["id"], arr[0]["id"],
                         "the re-dispatched worker reads the same final record")

    def test_states_recorded_by_wait_or_status_are_not_reported(self):
        run = self.submit()
        self.sacct("4300", "COMPLETED|0:0|00:02:00")
        ops.slurm_wait(self.s, "pipeline-runner", run["id"], timeout=1, interval=1)
        self.assertEqual(self.watch(), [], "the worker saw the end itself")
        other = self.submit()
        self.sacct("4301", "TIMEOUT|0:0|00:05:00")
        ops.slurm_status(self.s, "pipeline-runner", other["id"])  # a non-waited status of a terminal state
        self.assertEqual(self.watch(), [])

    def test_unknown_states_and_dry_runs_are_skipped_and_the_loop_polls(self):
        run = self.submit()
        self.submit(dry_run=True)
        self.assertEqual(self.watch(), [], "sacct knows nothing yet")
        self.sacct("4300", "NODE_FAIL|0:0|00:00:10")
        lines = ops.slurm_watch(self.s, "desk", interval=1, rounds=2, out=io.StringIO())
        self.assertEqual(len(lines), 1)
        self.assertIn(f"run={run['id']}", lines[0])
        self.assertIn("state=NODE_FAIL", lines[0])

    def test_next_step_follows_the_agents(self):
        run = self.submit()
        self.sacct("4300", "COMPLETED|0:0|00:02:00")
        self.runtime("SubagentStop", "pm-projects", "a-pm")  # the manager ended too
        self.assertIn(f"next=dispatch brief --wi {self.bundle} (bundle", self.watch()[0])
        top = wf.wi_new(self.s, "desk", "Top", "g", "researcher", "site", ["ok"])["id"]  # a top-level item, desk-submitted job
        wf.wi_approve(self.s, "desk", top, "ok")
        self.led.event("wi.dispatched", "desk", top, {"role": "researcher", "brief": "1" * 16, "by": "desk"})
        run2 = ops.slurm_submit(self.s, "desk", top, str(self.script))
        self.sacct("4301", "COMPLETED|0:0|00:02:00")
        self.led.event("agent.started", "researcher", top, {"agent_id": "a-live"})
        lines = self.watch()
        self.assertEqual(len(lines), 1)
        self.assertIn(f"run={run2['id']} job=4301 wi={top} role=researcher agent=a-live:running state=COMPLETED"
                      " next=agent still running; nothing to do", lines[0])
        run3 = ops.slurm_submit(self.s, "desk", top, str(self.script))
        self.led.event("agent.finished", "researcher", top, {"agent_id": "a-live"})
        self.sacct("4302", "COMPLETED|0:0|00:02:00")
        self.assertIn(f"run={run3['id']} job=4302 wi={top} role=researcher agent=a-live:ended state=COMPLETED"
                      f" next=dispatch brief --wi {top}", self.watch()[0])
        run4 = ops.slurm_submit(self.s, "desk", top, str(self.script))
        self.led.event("wi.staged", "researcher", top, {"summary": "done"})
        self.sacct("4303", "COMPLETED|0:0|00:02:00")
        self.assertIn(f"run={run4['id']} job=4303 wi={top} role=researcher agent=a-live:ended state=COMPLETED"
                      f" next=none (WI staged)", self.watch()[0])

    def test_only_the_desk_watches_and_the_cli_prints(self):
        with self.assertRaisesRegex(DurehError, "only the desk watches"):
            ops.slurm_watch(self.s, "pipeline-runner", once=True, out=io.StringIO())
        run = self.submit()
        self.sacct("4300", "FAILED|1:0|00:00:03")
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--site", str(self.site_root), "slurm", "watch", "--once"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(buf.getvalue().splitlines()), 1)
        self.assertIn(f"run={run['id']} job=4300", buf.getvalue())


class Batched(WatchCase):
    """One `sacct` call per round for every unfinished tracked run (chunks of SACCT_CHUNK ids); runs with no
    final state after `watch_stale_days` are recorded once as stale and never probed again."""

    def setUp(self):
        super().setUp()
        self.dispatch_bundle()
        self.dispatch_item()
        self.runtime("SubagentStart", "pipeline-runner", "a-w1")
        self.runtime("SubagentStop", "pipeline-runner", "a-w1")
        self.runtime("SubagentStop", "pm-projects", "a-pm")  # only jobs can hold the lane now

    def calls(self):
        f = self.fake / "sacct.calls"
        out = f.read_text().splitlines() if f.exists() else []
        f.unlink(missing_ok=True)
        return out

    def age(self, run, days):
        from datetime import datetime, timedelta
        path = self.s.record_dir("run") / f"{run['id']}.json"
        rec = read_json(path)
        rec["ts"] = (datetime.fromisoformat(rec["ts"]) - timedelta(days=days)).isoformat()
        path.write_text(json.dumps(rec))

    def test_one_sacct_call_per_round_for_all_runs(self):
        singles = [self.submit() for _ in range(4)]
        arrays = [self.submit(each="sub-01,sub-02"), self.submit(each="sub-01,sub-02,sub-03")]
        jobs = [r["payload"]["job_id"] for r in singles + arrays]
        for j in jobs[:4]:
            self.sacct(j, "RUNNING|0:0|00:01:00")
        self.sacct(jobs[4], f"{jobs[4]}_0|COMPLETED|0:0", f"{jobs[4]}_1|RUNNING|0:0")
        self.sacct(jobs[5], f"{jobs[5]}_[0-2]|PENDING|0:0")
        self.calls()
        self.assertEqual(self.watch(), [])
        self.assertEqual(self.calls(), [",".join(jobs)], "one call for six unfinished runs (was six calls)")
        ops.status(self.s)
        self.assertEqual(len(self.calls()), 1, "status (lane check) asks once too")
        self.sacct(jobs[0], "COMPLETED|0:0|00:02:00")
        self.sacct(jobs[4], f"{jobs[4]}_0|COMPLETED|0:0", f"{jobs[4]}_1|FAILED|1:0")
        lines = self.watch()
        self.assertEqual(len(lines), 2)
        self.assertIn(f"job={jobs[0]} wi={self.item}", lines[0])
        self.assertIn("state=COMPLETED next=", lines[0])
        self.assertIn(f"job={jobs[4]}", lines[1])
        self.assertIn("state=FAILED items=COMPLETED:1,FAILED:1", lines[1])
        self.assertEqual(self.calls(), [",".join(jobs)])
        self.assertEqual(self.watch(), [])
        self.assertEqual(self.calls(), [",".join(jobs[1:4] + jobs[5:])], "ended runs are not asked again")
        single = [e for e in self.led.events(self.item) if e["type"] == "run.status" and e["payload"]["run"] == singles[0]["id"]]
        self.assertEqual(single[0]["payload"]["raw"], "COMPLETED|0:0|00:02:00", "raw as `slurm status` records it")

    def test_chunks_and_per_item_states_match_the_single_query(self):
        runs = [self.submit(each="a,b,c") for _ in range(2)] + [self.submit() for _ in range(3)]
        jobs = [r["payload"]["job_id"] for r in runs]
        self.sacct(jobs[0], f"{jobs[0]}_0|COMPLETED|0:0", f"{jobs[0]}_0.batch|COMPLETED|0:0", f"{jobs[0]}_[1-2]|PENDING|0:0")
        self.sacct(jobs[1], f"{jobs[1]}_0|OUT_OF_MEMORY|0:125", f"{jobs[1]}_2|CANCELLED by 1|0:0")
        self.sacct(jobs[2], "PENDING|0:0|00:00:00")
        self.calls()
        from unittest import mock
        with mock.patch.object(ops, "SACCT_CHUNK", 2):
            probed = ops._probe_runs(self.s, runs)
        self.assertEqual(self.calls(), [",".join(jobs[0:2]), ",".join(jobs[2:4]), jobs[4]])
        for r in runs[:2]:
            self.assertEqual(probed[r["id"]]["array"], ops._array_status(self.s, r), "per-item states unchanged")
        self.assertEqual(probed[runs[0]["id"]]["array"]["items"], {"a": "COMPLETED", "b": "PENDING", "c": "PENDING"})
        self.assertEqual(probed[runs[1]["id"]]["array"]["items"], {"a": "OUT_OF_MEMORY", "b": "UNKNOWN", "c": "CANCELLED"})
        self.assertEqual((probed[runs[2]["id"]]["state"], probed[runs[2]["id"]]["raw"]), ("PENDING", "PENDING|0:0|00:00:00"))
        self.assertEqual((probed[runs[3]["id"]]["state"], probed[runs[3]["id"]]["final"]), ("UNKNOWN", False))
        self.assertEqual(ops._probe_runs(self.s, []), {})

    def test_stale_runs_are_recorded_once_and_hold_no_lane(self):
        old, fresh, done = self.submit(), self.submit(), self.submit()
        self.age(old, 8)
        self.age(done, 8)
        self.sacct(fresh["payload"]["job_id"], "RUNNING|0:0|00:01:00")
        self.sacct(done["payload"]["job_id"], "COMPLETED|0:0|00:02:00")  # old but ended: reported as usual
        lanes = {l["wi"]: l["by"] for l in ops.status(self.s)["lanes"]}
        self.assertEqual(lanes, {self.bundle: [f"job {fresh['payload']['job_id']} RUNNING on {self.item} ({fresh['id']})"]},
                         "the stale run holds no lane")
        self.calls()
        lines = self.watch()
        self.assertEqual(len(lines), 2)
        stale = [l for l in lines if f"run={old['id']}" in l][0]
        self.assertIn("state=UNKNOWN next=check by hand (no scheduler state after 7 days)", stale)
        self.assertIn("state=COMPLETED", [l for l in lines if f"run={done['id']}" in l][0])
        rec = [e for e in self.led.events(self.item) if e["type"] == "run.status" and e["payload"]["run"] == old["id"]]
        self.assertEqual(len(rec), 1)
        self.assertEqual({k: rec[0]["payload"][k] for k in ("scheduler_state", "final", "stale", "watched")},
                         {"scheduler_state": "UNKNOWN", "final": True, "stale": True, "watched": True})
        self.calls()
        self.assertEqual(self.watch(), [])
        self.assertEqual(self.calls(), [fresh["payload"]["job_id"]], "the stale run is not probed again")

    def test_running_after_8_days_stays_live_and_holds_its_lane(self):
        run, arr = self.submit(), self.submit(each="a,b")
        for r in (run, arr):
            self.age(r, 8)
        job, ajob = run["payload"]["job_id"], arr["payload"]["job_id"]
        self.sacct(job, "RUNNING|0:0|8-00:00:00")
        self.sacct(ajob, f"{ajob}_0|COMPLETED|0:0", f"{ajob}_1|REQUEUED|0:0")
        self.calls()
        for _ in range(2):
            self.assertEqual(self.watch(), [], "a job sacct reports is never stale")
        self.assertEqual(self.calls(), [f"{job},{ajob}"] * 2, "and is probed every round")
        self.assertEqual([e for e in self.led.events(self.item) if e["type"] == "run.status"], [])
        lanes = {l["wi"]: l["by"] for l in ops.status(self.s)["lanes"]}
        self.assertEqual(lanes, {self.bundle: [f"job {job} RUNNING on {self.item} ({run['id']})",
                                               f"job {ajob} RUNNING on {self.item} ({arr['id']})"]})
        for state in ("PENDING", "SUSPENDED"):
            self.sacct(job, f"{state}|0:0|00:00:00")
            self.assertEqual(self.watch(), [])
            self.assertEqual(len(ops.lane_holders(self.s, Ledger(self.s))), 1)
        self.sacct(job, "COMPLETED|0:0|8-00:01:00")
        self.assertIn("state=COMPLETED", self.watch()[0])

    def test_stale_days_is_a_site_limit(self):
        run = self.submit()
        self.age(run, 8)
        cfg = json.loads(self.s.config_path.read_text())
        cfg["limits"] = {"watch_stale_days": 30}
        self.s.config_path.write_text(json.dumps(cfg))
        self.s = self.site()
        self.assertEqual(self.watch(), [], "8 days is not stale under a 30-day limit")
        self.assertEqual(len(ops.lane_holders(self.s, Ledger(self.s))), 1)
        cfg["limits"] = {"watch_stale_days": 2}
        self.s.config_path.write_text(json.dumps(cfg))
        self.s = self.site()
        self.assertIn("after 2 days", self.watch()[0])
        self.assertEqual(ops.lane_holders(self.s, Ledger(self.s)), [])


class Lanes(WatchCase):
    def top(self, title, role="researcher", target="site"):
        w = wf.wi_new(self.s, "desk", title, "g", role, target, ["ok"])["id"]
        wf.wi_approve(self.s, "desk", w, "ok")
        return w

    def lanes(self):
        st = ops.status(self.s)
        return st["active_lanes"], {l["wi"]: l["by"] for l in st["lanes"]}

    def test_only_live_agents_or_running_jobs_hold_a_lane(self):
        self.dispatch_bundle()
        self.assertEqual(self.lanes(), ("1/2", {self.bundle: ["pm-projects agent running on " + self.bundle]}))
        self.dispatch_item()
        self.runtime("SubagentStart", "pipeline-runner", "a-w1")
        self.runtime("SubagentStop", "pm-projects", "a-pm")
        self.assertEqual(self.lanes()[1], {self.bundle: [f"pipeline-runner agent starting on {self.item}"]},
                         "the bundle's lane is held by its worker (started, not yet bound), not by the ended manager")
        run = self.submit()
        self.sacct("4300", "RUNNING|0:0|00:01:00")
        self.runtime("SubagentStop", "pipeline-runner", "a-w1")  # worker ended; its job runs
        self.assertEqual(self.lanes(), ("1/2", {self.bundle: [f"job 4300 RUNNING on {self.item} ({run['id']})"]}))
        orphans = {o["wi"]: o for o in ops.status(self.s)["orphaned"]}
        self.assertIn(self.bundle, orphans, "status still shows the ended manager")
        self.sacct("4300", "COMPLETED|0:0|00:02:00")
        self.assertEqual(self.lanes(), ("0/2", {}), "an ended job frees the lane even before anyone records it")
        self.assertEqual(len(self.watch()), 1)
        self.assertEqual(self.lanes(), ("0/2", {}))
        text = ops.render_status(ops.status(self.s))
        self.assertIn("active lanes 0/2", text)
        self.assertNotIn("Lane:", text)

    def test_waiting_items_and_orphaned_bundles_hold_no_lane(self):
        a, b = self.top("A"), self.top("B")
        for w in (a, b):
            self.led.event("wi.dispatched", "desk", w, {"role": "researcher", "brief": "2" * 16, "by": "desk"})
        self.assertEqual(self.lanes(), ("0/2", {}), "dispatched without a started agent: orphaned, no lane")
        self.led.event("agent.started", "researcher", a, {"agent_id": "a-a"})
        self.led.event("agent.started", "researcher", b, {"agent_id": "a-b"})
        self.assertEqual(self.lanes()[0], "2/2")
        self.led.event("wi.staged", "researcher", a, {"summary": "done"})
        self.led.event("agent.finished", "researcher", a, {"agent_id": "a-a"})
        self.assertEqual(self.lanes(), ("1/2", {b: ["researcher agent running on " + b]}), "staged work waits for the desk")
        self.led.event("wi.verified", "desk", a, {})
        self.assertEqual(self.lanes()[0], "1/2", "verified work waits for Gate 2")
        self.led.event("rework.requested", "desk", b, {"reason": "x"})
        self.led.event("agent.finished", "researcher", b, {"agent_id": "a-b"})
        self.assertEqual(self.lanes(), ("0/2", {}), "rework without an agent holds no lane")
        c = self.top("C")
        wf.dispatch_brief(self.s, c, "desk")  # two active items, both waiting for the desk: a third dispatch is fine
        self.assertEqual(self.lanes(), ("0/2", {}))
        self.assertEqual(self.led.sql("SELECT count(*) n FROM records WHERE type='lane.refused'")[0]["n"], 0)

    def test_lane_refusal_is_recorded_and_counted(self):
        a, b, c = self.top("A"), self.top("B"), self.top("C")
        for w, agent in ((a, "a-a"), (b, "a-b")):
            self.led.event("wi.dispatched", "desk", w, {"role": "researcher", "brief": "3" * 16, "by": "desk"})
            self.led.event("agent.started", "researcher", w, {"agent_id": agent})
        with self.assertRaisesRegex(DurehError, rf"lane limit reached \(2/2\): {a} held by researcher agent running on {a}; {b} held by") as cm:
            wf.dispatch_brief(self.s, c, "desk")
        self.assertIn("slurm watch --once", str(cm.exception))
        refused = [e for e in self.led.events(c) if e["type"] == "lane.refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["actor"], "desk")
        self.assertEqual((refused[0]["payload"]["count"], refused[0]["payload"]["limit"], refused[0]["payload"]["lane"]),
                         (2, 2, "work"))
        self.assertEqual([h["wi"] for h in refused[0]["payload"]["holders"]], [a, b])
        st = ops.status(self.s)
        self.assertEqual((st["lane_refusals"]["count"], st["lane_refusals"]["last"]["wi"], st["lane_refusals"]["last"]["holders"]),
                         (1, c, [a, b]))
        text = ops.render_status(st)
        self.assertIn(f"Lane refusals (24h): 1; last {c} by desk at", text)
        self.assertIn(f"Lane: {a} researcher [dispatched] held by researcher agent running on {a}", text)
        rows = self.led.sql("SELECT wi, actor FROM records WHERE type='lane.refused'")
        self.assertEqual(rows, [{"wi": c, "actor": "desk"}])
        self.led.event("agent.finished", "researcher", a, {"agent_id": "a-a"})
        wf.dispatch_brief(self.s, c, "desk")
        self.assertEqual(len([e for e in self.led.events(c) if e["type"] == "lane.refused"]), 1)
        self.assertTrue(self.s.org.may_append("pm-projects", "lane.refused"), "a manager may record a refusal too")


class NeverStarted(WatchCase):
    """The finding's scenario: WI-0117, dispatched by pm-projects at 21:25 UTC, no agent.started, later
    `dispatch brief` refused with 'dispatched and its agent has not finished'."""

    def setUp(self):
        super().setUp()
        self.dispatch_bundle()
        self.dispatch_item()  # wi.dispatched by pm-projects; no SubagentStart follows

    def test_finding_scenario_is_superseded_after_the_grace(self):
        self.assertEqual(ops.agent_state(self.led, self.item), "not started")
        with self.assertRaisesRegex(DurehError, r"dispatched \d+ s ago and no agent has started for it yet; after 120 s"):
            wf.dispatch_brief(self.s, self.item, "pm-projects")
        old = self.last_dispatch(self.item)
        self.backdate(old["id"], 20 * 60)
        self.assertIn("re-dispatch with `dispatch brief", {o["wi"]: o for o in ops.status(self.s)["orphaned"]}[self.item]["action"])
        brief = self.dispatch_item()  # the manager recovers on its own
        prepared = [e for e in self.led.events(self.item) if e["type"] == "dispatch.prepared"][-1]
        sup = prepared["payload"]["supersedes"]
        self.assertEqual((sup["dispatched"], sup["agent"]), (old["id"], "not started"))
        self.assertGreaterEqual(sup["age_seconds"], 20 * 60)
        new = self.last_dispatch(self.item)
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(self.led.wi_status(self.item)["status"], "dispatched")
        self.runtime("SubagentStart", "pipeline-runner", "a-w2")
        bash = hook(self.s, hook_event_name="PreToolUse", tool_name="Bash", cwd=str(self.site_root), agent_type="pipeline-runner",
                    agent_id="a-w2", tool_input={"command": f"{self.site_root}/bin/dureh wi show --wi {self.item}"})
        self.assertFalse(denied(bash))
        started = [e for e in self.led.events(self.item) if e["type"] == "agent.started"]
        self.assertEqual(started[-1]["refs"], [new["id"]], "the new agent binds to the new dispatch, never the dead one")
        self.assertEqual(ops.agent_state(self.led, self.item), "running")
        self.assertIn("DUREH-BRIEF", brief)

    def test_a_starting_or_running_agent_still_blocks(self):
        self.backdate(self.last_dispatch(self.item)["id"], 20 * 60)
        self.runtime("SubagentStart", "pipeline-runner", "a-slow")  # deferred start: the runtime has no transcript yet
        self.assertEqual(ops.agent_state(self.led, self.item), "starting")
        with self.assertRaisesRegex(DurehError, "has not finished; stop it"):
            wf.dispatch_brief(self.s, self.item, "pm-projects")
        hook(self.s, hook_event_name="PreToolUse", tool_name="Bash", cwd=str(self.site_root), agent_type="pipeline-runner",
             agent_id="a-slow", tool_input={"command": "ls"})
        self.assertEqual(ops.agent_state(self.led, self.item), "running")
        with self.assertRaisesRegex(DurehError, "has not finished; stop it"):
            wf.dispatch_brief(self.s, self.item, "pm-projects")
        self.runtime("SubagentStop", "pipeline-runner", "a-slow")
        self.dispatch_item()
        sup = [e for e in self.led.events(self.item) if e["type"] == "dispatch.prepared"][-1]["payload"]["supersedes"]
        self.assertEqual(sup["agent"], "ended")

    def test_grace_is_a_site_limit(self):
        cfg = json.loads(self.s.config_path.read_text())
        cfg["limits"] = {"dispatch_start_grace_seconds": 0}
        self.s.config_path.write_text(json.dumps(cfg))
        self.s = self.site()
        self.led = Ledger(self.s)
        self.dispatch_item()  # at once
        self.assertEqual([e for e in self.led.events(self.item) if e["type"] == "dispatch.prepared"][-1]["payload"]["supersedes"]["agent"],
                         "not started")


class WaitHints(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()

    def bash(self, command, role="py-writer", **extra):
        data = dict(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": command, **extra},
                    cwd=str(self.site_root))
        if role != "desk":
            data.update(agent_type=role, agent_id="a1")
        return hook(self.s, **data)

    def reason(self, out):
        self.assertTrue(denied(out), out)
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_refused_wait_forms_carry_the_rule(self):
        for cmd, extra in (("squeue -j 4300 -h", {"run_in_background": True}),
                           ("until ! squeue -h -j 4300 | grep -q .; do sleep 30; done &", {}),
                           ("state=$(sacct -j 4300 -n -o State); echo $state", {}),
                           ("while true; do sleep 60; done", {"run_in_background": True}),
                           ("nohup sleep 600", {}),
                           ("python3 - <<'EOF'\nimport subprocess, time\nwhile True: time.sleep(5)\nEOF", {}),
                           ("bin/dureh slurm wait --run run-1 --timeout 7200 &", {})):
            self.assertIn(WAIT_HINT, self.reason(self.bash(cmd, **extra)), cmd)
        self.assertEqual(WAIT_HINT, "do not wait for jobs in the session: end your turn; the desk re-dispatches you when"
                                    " `dureh slurm watch` reports the job")

    def test_other_refusals_are_unchanged(self):
        for cmd in ("x=$(ls); echo $x", "(bin/dureh status &)", "nohup make all", "sbatch job.sh",
                    "python3 <<'EOF'\nprint(1)\nEOF"):
            self.assertNotIn(WAIT_HINT, self.reason(self.bash(cmd)), cmd)
        self.assertFalse(denied(self.bash("squeue -u me -h | head")), "a plain look at the queue is not refused")
        self.assertFalse(denied(self.bash("sleep 1")), "a short sleep was never refused; the hint changes no refusal")



class Instructions(WatchCase):
    def test_generated_instructions_say_so(self):
        claude_md = (self.site_root / "CLAUDE.md").read_text()
        self.assertIn("slurm watch` running as a Monitor", claude_md)
        self.assertIn("checks every 300 s by default with one batched `sacct` call", claude_md)
        self.assertIn("re-dispatch the owning WI", claude_md)
        self.assertIn("`lane.refused`", claude_md)
        self.assertNotIn("Wait for a scheduler job with", claude_md)
        runner = (self.site_root / ".claude" / "agents" / "pipeline-runner.md").read_text()
        self.assertIn("Never wait for a job in the session", runner)
        self.assertIn("the desk re-dispatches you when `dureh slurm watch` reports the job", runner)
        self.assertNotIn("follow it with", runner)


if __name__ == "__main__":
    unittest.main()
