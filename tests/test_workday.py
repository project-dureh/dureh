"""Workday mode: Director switch, day plan/approval clock, decisions, end report, overtime, usage budget."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from helpers import SiteCase, denied, hook

from pathlib import Path

from dureh_core import claude_usage, sessions, workday
from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.ops import render_status, status
from dureh_core.records import Ledger
from dureh_core.util import DurehError

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=timezone(timedelta(hours=-4)))  # a Saturday
MONDAY = datetime(2026, 10, 5, 9, 0, tzinfo=timezone(timedelta(hours=-4)))
ALL_WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def transcript_line(ts: datetime, rid: str, model="claude-opus-5-5", inp=0, out=0, cr=0, cc=0) -> str:
    return json.dumps({"type": "assistant", "timestamp": ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                       "requestId": rid, "message": {"id": "m" + rid, "model": model, "usage": {
                           "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                           "cache_creation_input_tokens": cc}}}) + "\n"


class Usage(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = Path(tempfile.mkdtemp(prefix="dureh-usage-"))
        os.environ["DUREH_USAGE_CACHE"] = str(self.tmp / "cache.json")
        self.root = self.tmp / "projects" / "p"
        self.root.mkdir(parents=True)

    def tearDown(self):
        import shutil

        os.environ.pop("DUREH_USAGE_CACHE", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_duplicate_lines_count_once_and_weights_apply(self):
        f = self.root / "s.jsonl"
        now = datetime.now().astimezone()
        f.write_text(transcript_line(now, "r1", inp=1_000_000) * 3 + transcript_line(now, "r2", out=200_000)
                     + transcript_line(now, "r3", model="claude-fable-5-1", cr=1_000_000))
        s = claude_usage.summary({"weekly_units": 70}, [str(self.tmp / "projects")])
        self.assertAlmostEqual(s["today_units"], 1 + 1 + 0.1, places=3)
        self.assertAlmostEqual(s["daily_allowance"], 10, places=3)
        w = claude_usage.summary({"weekly_units": 70, "model_weights": {"fable": 3}}, [str(self.tmp / "projects")])
        self.assertAlmostEqual(w["today_units"], 2.3, places=3)

    def test_incremental_scan_and_partial_lines(self):
        f = self.root / "s.jsonl"
        now = datetime.now().astimezone()
        f.write_text(transcript_line(now, "r1", inp=1_000_000) + transcript_line(now, "r2", inp=1_000_000)[:30])
        roots = [str(self.tmp / "projects")]
        self.assertAlmostEqual(claude_usage.summary({}, roots)["today_units"], 1.0)
        with f.open("w") as fh:
            fh.write(transcript_line(now, "r1", inp=1_000_000) + transcript_line(now, "r2", inp=1_000_000))
        self.assertAlmostEqual(claude_usage.summary({}, roots)["today_units"], 2.0)

    def test_calibrate(self):
        now = datetime.now().astimezone()
        (self.root / "s.jsonl").write_text(transcript_line(now, "r1", inp=5_000_000))
        cal = claude_usage.calibrate(10, roots=[str(self.tmp / "projects")])
        self.assertAlmostEqual(cal["weekly_units"], 50.0)
        with self.assertRaises(ValueError):
            claude_usage.calibrate(0, roots=[str(self.tmp / "projects")])


class Workday(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.projects = self.tmp / "transcripts"
        (self.projects / "p").mkdir(parents=True)
        os.environ["DUREH_USAGE_CACHE"] = str(self.tmp / "usage-cache.json")
        os.environ.pop("CLAUDECODE", None)  # the Director's terminal
        # production rules (blocking clock); every weekday counts here because T0 is a Saturday and some tests
        # run at the real time. The weekday rule and the development phase have their own tests below.
        self.config(workday={"transcripts": [str(self.projects)], "phase": "production", "weekdays": ALL_WEEK})
        self.at(T0)
        self.wi = wf.wi_new(self.s, "desk", "W", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", self.wi, "ok")

    def config(self, **keys):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg.update(keys)
        (self.site_root / "config.json").write_text(json.dumps(cfg, indent=2))
        self.s = self.site()
        return cfg

    def workday_config(self, **keys):
        return self.config(workday={"transcripts": [str(self.projects)], **keys})

    def at(self, dt: datetime):
        os.environ["DUREH_FAKE_NOW"] = dt.isoformat()

    def fake_slurm(self, squeue="RUNNING|7:59:00"):
        """A site PATH with sbatch (prints a job id), squeue (one line, or nothing), scancel (records its args in
        `scancel-calls`, never cancels anything) and claude. Never the real ones."""
        fake = self.tmp / "fakebin"
        fake.mkdir(exist_ok=True)
        (fake / "sbatch").write_text(f"#!/bin/sh\necho \"$@\" >> {fake}/sbatch-calls\necho 'Submitted batch job 12345'\n")
        (fake / "squeue").write_text(f"#!/bin/sh\necho \"$@\" >> {fake}/squeue-calls\n"
                                     + (f"echo '{squeue}'\n" if squeue else ""))
        (fake / "scancel").write_text(f"#!/bin/sh\necho \"$@\" >> {fake}/scancel-calls\n")
        (fake / "claude").write_text("#!/bin/sh\nexit 0\n")
        for f in ("sbatch", "squeue", "scancel", "claude"):
            (fake / f).chmod(0o755)
        self.config(env={"path_prepend": [str(fake)]})
        return fake

    def released(self):
        return [e for e in Ledger(self.s).events() if e["type"] == "session.released"]

    def cli(self, *args):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--site", str(self.site_root), *args])
        return rc, buf.getvalue()

    def spend(self, units: float):
        now = datetime.fromisoformat(os.environ["DUREH_FAKE_NOW"])
        with (self.projects / "p" / "s.jsonl").open("a") as fh:
            fh.write(transcript_line(now, f"r{units}{os.urandom(3).hex()}", inp=int(units * 1_000_000)))

    def spawn(self, wi=None):
        brief = wf.dispatch_brief(self.s, wi or self.wi, "desk")["brief"]
        return hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
                    tool_input={"subagent_type": "pm-modules", "prompt": brief, "description": "x"})

    def start_day(self, plan="1. W", items=None):
        """`plan` is the plan text; the default names no WI id, so the day's WIs are outside the plan (after the
        end time they are refused as before). The finish-plan tests pass a plan that names them."""
        workday.switch(self.s, True, "trial")
        workday.plan(self.s, "desk", "finish W", plan, items)
        workday.approve(self.s, "desk", "승인")

    def test_off_by_default_changes_nothing(self):
        self.assertFalse(workday.status(self.s)["enabled"])
        self.assertIsNone(self.spawn())
        self.assertNotIn("Workday", render_status(status(self.s)))

    def test_needs_an_approved_plan(self):
        workday.switch(self.s, True, "trial")
        out = self.spawn()
        self.assertTrue(denied(out))
        self.assertIn("no approved day", json.dumps(out))
        workday.plan(self.s, "desk", "finish W", "1. W")
        self.assertTrue(denied(self.spawn()))
        workday.approve(self.s, "desk", "승인")
        self.assertIsNone(self.spawn())
        self.assertIn("Workday: on-duty (09:00-17:00)", render_status(status(self.s)))

    def test_eight_hours_then_end_report_and_overtime(self):
        self.start_day()
        other = wf.wi_new(self.s, "desk", "X", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", other, "ok")
        self.at(T0 + timedelta(hours=8, minutes=1))
        out = self.spawn()
        self.assertTrue(denied(out))
        self.assertIn("end report", json.dumps(out))
        with self.assertRaises(DurehError):
            workday.overtime(self.s, "desk", [self.wi], "ok")  # only after the end report
        workday.end(self.s, "desk", "W built", "W verify fails", [self.wi])
        self.assertTrue(denied(self.spawn()))
        workday.overtime(self.s, "desk", [self.wi], "1번만 초과근무")
        self.assertIsNone(self.spawn())
        out = self.spawn(other)
        self.assertTrue(denied(out))
        self.assertIn("overtime covers only", json.dumps(out))
        self.write_handoff()
        workday.close(self.s, "desk", "fixed")
        self.assertEqual(workday.status(self.s)["phase"], "off-duty")

    def test_usage_budget_per_work_unit(self):
        self.start_day()
        self.spend(7)
        workday.calibrate(self.s, 10)  # 7 units = 10% of the week -> weekly 70, daily 10
        self.assertIsNone(self.spawn())
        self.spend(3)
        other = wf.wi_new(self.s, "desk", "X", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", other, "ok")
        out = self.spawn(other)
        self.assertTrue(denied(out))
        self.assertIn("daily share", json.dumps(out))
        self.assertIn("usage today 10.0/10.0", render_status(status(self.s)))

    def test_shared_budget_file_in_exchange(self):
        ex = self.tmp / "exchange"
        ex.mkdir()
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["upstream"] = {"exchange": str(ex)}
        (self.site_root / "config.json").write_text(json.dumps(cfg, indent=2))
        self.s = self.site()
        self.spend(2)
        workday.calibrate(self.s, 20)
        data = json.loads((ex / "usage-budget.json").read_text())
        self.assertAlmostEqual(data["weekly_units"], 10.0)
        self.assertEqual(workday.budget(self.s)["source"], str(ex / "usage-budget.json"))
        self.assertEqual(data["site"], self.site_root.name)  # default label: the folder name

    def test_shared_budget_label_is_the_pinned_mailbox(self):
        ex = self.tmp / "exchange"
        ex.mkdir()
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["upstream"] = {"exchange": str(ex), "mailbox": "OLD_MAILBOX"}
        (self.site_root / "config.json").write_text(json.dumps(cfg, indent=2))
        self.s = self.site()
        self.spend(2)
        workday.calibrate(self.s, 20)
        self.assertEqual(json.loads((ex / "usage-budget.json").read_text())["site"], "OLD_MAILBOX")

    def test_decision_records_and_commits(self):
        self.start_day()
        r = workday.decide(self.s, "desk", "lock strategy", "regenerate", "pyproject changed", ["abc123"])
        self.assertTrue(r["record"])
        self.assertEqual([d["title"] for d in workday.status(self.s)["decisions"]], ["lock strategy"])

    def test_switch_is_the_directors(self):
        os.environ["DUREH_ACTOR"] = "desk"
        self.assertEqual(main(["--site", str(self.site_root), "site", "workday", "on", "--memo", "x"]), 2)
        self.assertEqual(main(["--site", str(self.site_root), "ledger", "add", "event", "--type", "workday.switched",
                               "--payload", '{"enabled": true}']), 2)
        os.environ.pop("DUREH_ACTOR")
        os.environ["CLAUDECODE"] = "1"
        self.assertEqual(main(["--site", str(self.site_root), "site", "workday", "on", "--memo", "x"]), 2)
        os.environ.pop("CLAUDECODE")
        self.assertFalse(workday.status(self.s)["enabled"])
        self.assertEqual(main(["--site", str(self.site_root), "site", "workday", "on", "--memo", "trial"]), 0)
        self.assertTrue(workday.status(self.s)["enabled"])
        self.assertEqual(main(["--site", str(self.site_root), "site", "workday", "off", "--memo", "done"]), 0)
        self.assertFalse(workday.status(self.s)["enabled"])

    def checker_spawn(self, wi):
        brief = wf.dispatch_brief(self.s, wi, "desk", "checker")["brief"]
        return hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
                    tool_input={"subagent_type": "checker", "prompt": brief, "description": "x"})

    def test_one_checker_run_after_the_end_for_items_staged_during_the_day(self):
        real = datetime.now().astimezone()  # records carry the real time
        self.at(real - timedelta(hours=2))
        self.start_day()
        self.assertIsNone(self.spawn())
        wf.wi_stage(self.s, "desk", self.wi, "done")
        self.at(real + timedelta(hours=7))
        self.assertIsNone(self.checker_spawn(self.wi))
        self.assertIsNotNone(workday.gate(self.s, self.wi, checking=True))  # a second check is overtime
        self.assertIsNotNone(workday.gate(self.s, self.wi))  # so is a re-dispatch

    def test_staged_after_the_end_gets_no_free_checker(self):
        real = datetime.now().astimezone()  # records carry the real time
        self.at(real - timedelta(hours=10))
        self.start_day()
        self.assertIsNone(self.spawn())
        self.at(real)
        wf.wi_stage(self.s, "desk", self.wi, "late")
        self.assertTrue(denied(self.checker_spawn(self.wi)))

    def test_urgent_releases_a_wi_from_plan_clock_and_budget(self):
        workday.switch(self.s, True, "trial")
        self.assertTrue(denied(self.spawn()))
        workday.urgent(self.s, "desk", [self.wi], "disk filling, go")
        self.assertIsNone(self.spawn())
        self.assertIn("urgent " + self.wi, render_status(status(self.s)))
        self.at(T0 + timedelta(days=2))
        self.assertEqual(workday.status(self.s)["urgent"], [])

    def test_usage_shows_this_sites_part(self):
        self.start_day()
        here = self.projects / claude_usage.project_key(self.site_root)
        here.mkdir()
        now = datetime.fromisoformat(os.environ["DUREH_FAKE_NOW"])
        (here / "s.jsonl").write_text(transcript_line(now, "h1", inp=2_000_000))
        self.spend(3)
        u = workday.status(self.s)["usage"]
        self.assertAlmostEqual(u["today_units"], 5.0)
        self.assertAlmostEqual(u["this_site_units"], 2.0)

    def test_the_day_runs_to_its_end_time(self):
        self.start_day()
        self.at(T0 + timedelta(hours=3))
        with self.assertRaises(DurehError) as cm:
            workday.end(self.s, "desk", "plan done", "", [])
        self.assertIn("stay on duty", str(cm.exception))
        rec = workday.end(self.s, "desk", "plan done", "", [], early="오늘은 일찍 마칩시다")
        self.assertEqual(rec["payload"]["early"], "오늘은 일찍 마칩시다")

    def test_plan_and_end_record_rollback_heads(self):
        workday.switch(self.s, True, "trial")
        rec = workday.plan(self.s, "desk", "g", "p")
        self.assertIn("modules/demo", rec["payload"]["heads"])
        self.assertRegex(rec["payload"]["heads"]["modules/demo"] or "", r"^[0-9a-f]{40}$")

    def write_handoff(self):
        h = self.site_root / "state" / "memory" / "handoff.md"
        h.parent.mkdir(parents=True, exist_ok=True)
        h.write_text("open: nothing\n")
        future = datetime.fromisoformat(os.environ["DUREH_FAKE_NOW"]).timestamp() + 60
        os.utime(h, (future, future))  # written after today's approval
        return h

    def end_day(self):
        self.start_day()
        self.at(T0 + timedelta(hours=9))
        workday.end(self.s, "desk", "done", "", [])

    def test_close_needs_a_fresh_handoff(self):
        self.end_day()
        h = self.site_root / "state" / "memory" / "handoff.md"
        if h.exists():
            h.unlink()  # the install may seed one; this test wants none
        with self.assertRaisesRegex(DurehError, "write the handoff"):
            workday.close(self.s, "desk", "x")
        h = self.write_handoff()
        old = T0.timestamp() - 3600
        os.utime(h, (old, old))
        with self.assertRaisesRegex(DurehError, "predates"):
            workday.close(self.s, "desk", "x")

    def test_close_refused_while_an_agent_runs(self):
        self.start_day()
        self.assertIsNone(self.spawn())
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        self.at(T0 + timedelta(hours=9))
        workday.end(self.s, "desk", "done", "", [])
        self.write_handoff()
        with self.assertRaisesRegex(DurehError, "still running"):
            workday.close(self.s, "desk", "x")

    # ------------------------------------------------------------ the close marker (Director, 2026-10-06, release 39)
    def _closable(self, squeue="RUNNING|7:59:00", job="777"):
        """A day at its end with a fresh handoff, fake slurm and a recorded session job (ids here are made up;
        helpers.SiteCase drops the real SLURM_JOB_ID: 2026-10-03 a close() test cancelled the real session)."""
        self.end_day()
        self.write_handoff()
        fake = self.fake_slurm(squeue=squeue)
        if job:
            Ledger(self.s).event("session.started", "desk", None, {"job_id": job, "purpose": "desk"})
        return fake

    def assert_marker(self, rec_release, fake, job="777", parent=None):
        marker = sessions.close_marker(self.s, job)
        self.assertEqual(rec_release["released"], True)
        self.assertEqual((rec_release["mode"], rec_release["job_id"], rec_release["marker"], rec_release["grace"]),
                         ("marker", job, str(marker), 90))
        self.assertTrue(marker.is_file(), "the close marker is written next to the pane log")
        body = json.loads(marker.read_text())
        self.assertEqual((body["actor"], body["record"], body["job_id"]), ("desk", parent, job))
        self.assertEqual(body["closed_at"], os.environ["DUREH_FAKE_NOW"])
        self.assertFalse((fake / "scancel-calls").exists(), "a marker close never scancels")
        self.assertEqual([e["payload"]["mode"] for e in self.released()], ["marker"])

    def test_close_from_outside_the_job_writes_the_marker(self):
        # yesterday's case: the upstream team closes from its own terminal; the recorded job gets the marker
        fake = self._closable()
        os.environ.pop("SLURM_JOB_ID", None)
        rec = workday.close(self.s, "desk", "done")
        self.assert_marker(rec["release"], fake, parent=rec["id"])
        self.assertEqual(rec["release"]["from"], "outside")
        self.assertIn("-j 777", (fake / "squeue-calls").read_text())
        self.assertEqual(self.released()[-1]["refs"], [rec["id"]])
        self.assertEqual(workday.status(self.s)["phase"], "off-duty")
        self.assertIn("session job 777 ends by itself within ~120 s (close marker written", sessions.render_release(rec["release"]))

    def test_close_inside_the_job_writes_the_marker_and_does_not_scancel(self):
        fake = self._closable()
        os.environ["SLURM_JOB_ID"] = "777"
        try:
            rec = workday.close(self.s, "desk", "done")
        finally:
            os.environ.pop("SLURM_JOB_ID")
        self.assert_marker(rec["release"], fake, parent=rec["id"])
        self.assertEqual(rec["release"]["from"], "inside")

    def test_close_never_touches_a_job_dureh_did_not_start(self):
        # Director, 2026-10-03: dureh may release only company session jobs it started. The caller's own job
        # (the DUREH team's, say) has no session.started record: no marker for it, no scancel, whatever runs.
        fake = self._closable(job=None)
        os.environ["SLURM_JOB_ID"] = "424242"
        try:
            rec = workday.close(self.s, "desk", "done")
        finally:
            os.environ.pop("SLURM_JOB_ID")
        self.assertEqual((rec["release"]["released"], rec["release"]["reason"], rec["release"]["caller_job"]),
                         (False, "no running session job", "424242"))
        self.assertFalse((fake / "scancel-calls").exists())
        self.assertEqual(list((self.site_root / "logs").glob("**/*.close")) if (self.site_root / "logs").exists() else [], [])
        self.assertEqual(workday.status(self.s)["phase"], "off-duty")
        # with a recorded running job the marker goes to that job only, never to the caller's
        Ledger(self.s).event("session.started", "desk", None, {"job_id": "777", "purpose": "desk"})
        os.environ["SLURM_JOB_ID"] = "424242"
        try:
            out = sessions.release(self.s, "desk")
        finally:
            os.environ.pop("SLURM_JOB_ID")
        self.assertEqual((out["mode"], out["job_id"], out["from"]), ("marker", "777", "outside"))
        self.assertTrue(sessions.close_marker(self.s, "777").is_file())
        self.assertFalse(sessions.close_marker(self.s, "424242").exists())
        self.assertFalse((fake / "scancel-calls").exists())
        self.assertNotIn("424242", (fake / "squeue-calls").read_text(), "only recorded jobs are even asked about")

    def test_close_with_the_job_gone_releases_nothing(self):
        fake = self._closable(squeue="")
        rec = workday.close(self.s, "desk", "done")
        self.assertEqual((rec["release"]["released"], rec["release"]["reason"]), (False, "no running session job"))
        self.assertEqual(rec["release"]["asked"][0]["job_id"], "777")
        self.assertFalse(sessions.close_marker(self.s, "777").exists())
        self.assertFalse((fake / "scancel-calls").exists())

    def test_close_cancel_now_scancels_the_recorded_job_only(self):
        fake = self._closable()
        os.environ["SLURM_JOB_ID"] = "424242"
        try:
            rc, out = self.cli("day", "close", "--cancel-now", "--note", "marker ignored")
        finally:
            os.environ.pop("SLURM_JOB_ID")
        self.assertEqual(rc, 0, out)
        self.assertEqual((fake / "scancel-calls").read_text().split(), ["777"])
        self.assertFalse(sessions.close_marker(self.s, "777").exists())
        rel = self.released()[-1]["payload"]
        self.assertEqual((rel["released"], rel["mode"], rel["job_id"], rel["exit"]), (True, "scancel", "777", 0))
        self.assertIn("session job 777 cancelled now (scancel exit 0)", out)

    def test_close_release_on_close_off(self):
        fake = self._closable()
        self.config(session={"release_on_close": False})
        rec = workday.close(self.s, "desk", "done")
        self.assertEqual((rec["release"]["released"], rec["release"]["reason"]), (False, "release_on_close is off"))
        self.assertFalse(sessions.close_marker(self.s, "777").exists())
        self.assertFalse((fake / "squeue-calls").exists())

    def test_close_picks_the_newest_recorded_job_still_in_squeue(self):
        # squeue lists job 778 (the restart) and not 777: the marker goes to 778, named like its tmux session
        fake = self._closable(squeue=None)
        (fake / "squeue").write_text(f"#!/bin/sh\necho \"$@\" >> {fake}/squeue-calls\n"
                                     "case \"$*\" in *778*) echo 'RUNNING|6:00:00';; esac\n")
        Ledger(self.s).event("session.started", "desk", None, {"job_id": "778", "purpose": "overtime"})
        rec = workday.close(self.s, "desk", "done")
        self.assertEqual((rec["release"]["job_id"], rec["release"]["purpose"]), ("778", "overtime"))
        self.assertEqual(rec["release"]["marker"], str(sessions.close_marker(self.s, "778", "overtime")))
        self.assertTrue(sessions.close_marker(self.s, "778", "overtime").is_file())
        self.assertFalse(sessions.close_marker(self.s, "777").exists())
        calls = (fake / "squeue-calls").read_text()
        self.assertIn("-j 778", calls)
        self.assertNotIn("-j 777", calls, "the newest running job is found first; older ones are not asked about")

    def test_session_release_writes_the_marker_after_a_closed_day(self):
        # the 10-05 situation: the day is closed, the job still runs; no second day close is possible
        fake = self._closable()
        rec = workday.close(self.s, "desk", "done")
        sessions.close_marker(self.s, "777").unlink()  # as if the first marker had been lost
        rc, out = self.cli("site", "session", "release")
        self.assertEqual(rc, 0, out)
        self.assertIn("session job 777 ends by itself within ~120 s (close marker written", out)
        marker = sessions.close_marker(self.s, "777")
        self.assertTrue(marker.is_file())
        self.assertIsNone(json.loads(marker.read_text())["record"])
        self.assertFalse((fake / "scancel-calls").exists())
        rel = self.released()[-1]
        self.assertEqual((rel["payload"]["mode"], rel["payload"]["job_id"]), ("marker", "777"))
        started = [e for e in Ledger(self.s).events() if e["type"] == "session.started"][-1]["id"]
        self.assertEqual(rel["refs"], [started])
        # --cancel-now: the fallback when the marker was ignored
        rc, out = self.cli("site", "session", "release", "--cancel-now")
        self.assertEqual(rc, 0, out)
        self.assertEqual((fake / "scancel-calls").read_text().split(), ["777"])
        self.assertEqual(self.released()[-1]["payload"]["mode"], "scancel")
        # nothing runs any more: refused, nothing recorded
        self.fake_slurm(squeue="")
        before = len(self.released())
        with self.assertRaisesRegex(DurehError, r"no running session job to release: job 777 .*not in squeue"):
            sessions.release(self.s, "desk", must_run=True)
        self.assertEqual(len(self.released()), before)
        self.assertEqual(self.cli("site", "session", "release")[0], 2)
        self.assertEqual(self.cli("--observe", "site", "session", "release")[0], 2, "observe mode is read-only (sets DUREH_OBSERVE: last)")

    def test_session_release_refuses_without_any_recorded_session(self):
        self.fake_slurm()
        with self.assertRaisesRegex(DurehError, "no running session job to release: no session.started record"):
            sessions.release(self.s, "desk", must_run=True)
        self.assertEqual(self.released(), [])

    # ------------------------------------------------------------ the session starts the day (Director, 2026-10-04)
    def test_session_start_submits_and_sets_the_clock(self):
        fake = self.fake_slurm()
        workday.switch(self.s, True, "trial")
        rc, out = self.cli("site", "session", "start")
        self.assertEqual(rc, 0, out)
        self.assertIn("Submitted session job 12345", out)
        from dureh_core.records import Ledger
        rec = [e for e in Ledger(self.s).events() if e["type"] == "session.started"][-1]
        self.assertIn(rec["id"], out)
        p = rec["payload"]
        self.assertEqual((p["job_id"], p["submitted_at"], p["wall_time"], p["purpose"]),
                         ("12345", T0.isoformat(), "24:00:00", "desk"))
        self.assertTrue((self.site_root / "logs" / "session").is_dir())
        self.assertIn(p["script"], (fake / "sbatch-calls").read_text())
        self.assertEqual(Path(p["script"]).read_text(), self.cli("site", "session", "plan")[1], "plan == what was submitted")
        # no plan yet: off-duty, but the clock is visible
        st = workday.status(self.s)
        self.assertEqual((st["phase"], st["start"], st["end"]), ("off-duty", T0.isoformat(), (T0 + timedelta(hours=8)).isoformat()))
        self.at(T0 + timedelta(hours=2))
        workday.plan(self.s, "desk", "finish W", "1. W")
        self.assertTrue(denied(self.spawn()))  # the plan's content still needs the approval
        workday.approve(self.s, "desk", "승인")
        st = workday.status(self.s)
        self.assertEqual((st["start"], st["end"], st["job_id"]), (T0.isoformat(), (T0 + timedelta(hours=8)).isoformat(), "12345"))
        self.assertIn("Workday: on-duty (09:00-17:00, session job 12345)", render_status(status(self.s)))
        self.assertIsNone(self.spawn())
        self.at(T0 + timedelta(hours=8, minutes=1))  # 8 h after the session start, not after the approval
        self.assertIn("end report", workday.gate(self.s, self.wi) or "")
        self.assertEqual(workday.status(self.s)["phase"], "past-end")

    def test_session_start_refused_when_the_runtime_cannot_start(self):
        fake = self.fake_slurm()
        (fake / "claude").unlink()
        os.environ["PATH"] = str(fake)  # nothing but the fakes
        with self.assertRaisesRegex(DurehError, "cannot start here.*not on PATH"):
            sessions.start(self.s, "desk")
        self.assertFalse((fake / "sbatch-calls").exists())
        self.assertEqual([e for e in Ledger(self.s).events() if e["type"] == "session.started"], [])

    def test_session_status_lines_and_observe(self):
        self.fake_slurm()
        rc, out = self.cli("site", "session", "status")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0], "no session job started by dureh")
        self.assertIn("day: workday mode off (off-duty)", out)
        workday.switch(self.s, True, "trial")
        sessions.start(self.s, "desk")
        rc, out = self.cli("--observe", "site", "session", "status")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^session: ev-\S+ job 12345 submitted 2026-10-03T09:00:00-04:00 \(wall time 24:00:00, purpose desk\)$")
        self.assertEqual(lines[1], "job 12345: RUNNING, time left 7:59:00")
        self.assertEqual(lines[2], "day: off-duty, 2026-10-03T09:00:00-04:00 to 2026-10-03T17:00:00-04:00; no day plan")
        self.assertEqual(self.cli("--observe", "site", "session", "start")[0], 2, "observe mode is read-only")
        self.fake_slurm(squeue="")
        self.assertIn("job 12345: not in squeue", self.cli("site", "session", "status")[1])
        self.assertEqual(self.cli("ledger", "add", "event", "--type", "session.started", "--payload", '{"job_id": "1"}')[0], 2)

    def _sessions(self):
        return [e for e in Ledger(self.s).events() if e["type"] == "session.started"]

    def test_session_start_records_the_first_prompt(self):
        fake = self.fake_slurm(squeue="RUNNING|6:00:00")
        workday.switch(self.s, True, "trial")
        rc, out = self.cli("site", "session", "start")
        self.assertEqual(rc, 0, out)
        self.assertIn("purpose desk, no first prompt)", out)
        self.assertIn("The working day runs from now.", out)
        first = self._sessions()[-1]
        self.assertIsNone(first["payload"]["prompt"])
        self.assertNotIn("출근", Path(first["payload"]["script"]).read_text())
        # an overtime session later the same day: a second session, not a restart, with its prompt recorded
        self.at(T0 + timedelta(hours=9))
        rc, out = self.cli("site", "session", "start", "--purpose", "overtime")
        self.assertEqual(rc, 0, out)
        self.assertIn("purpose overtime, first prompt 'Tester, 추가근무 출근')", out)
        self.assertIn(f"Second session today after the desk session (job 12345, {first['id']}); the working day still "
                      f"runs from {T0.isoformat()}", out)
        p = self._sessions()[-1]["payload"]
        self.assertEqual((p["purpose"], p["prompt"], p["previous"]["record"], p["day_start"]),
                         ("overtime", "Tester, 추가근무 출근", first["id"], T0.isoformat()))
        self.assertNotIn("restart", p)
        self.assertNotIn("force", p)
        self.assertIn("'Tester, 추가근무 출근'", Path(p["script"]).read_text())
        self.assertNotIn("-j 12345", (fake / "squeue-calls").read_text() if (fake / "squeue-calls").exists() else "",
                         "a different purpose needs no squeue check")
        # a second overtime session while the first still runs is refused like the day's; --prompt is recorded
        self.at(T0 + timedelta(hours=10))
        with self.assertRaisesRegex(DurehError, "today's overtime session job 12345 .* still in squeue"):
            sessions.start(self.s, "desk", "overtime")
        rc, out = self.cli("site", "session", "start", "--purpose", "overtime", "--force", "--memo", "node lost",
                           "--prompt", "지아, 추가근무: 보고서만")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._sessions()[-1]["payload"]["prompt"], "지아, 추가근무: 보고서만")
        rc, out = self.cli("site", "session", "start", "--purpose", "overtime", "--force", "--memo", "x", "--no-prompt")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self._sessions()[-1]["payload"]["prompt"])

    def test_second_session_start_today_refused_while_the_job_is_queued(self):
        fake = self.fake_slurm(squeue="RUNNING|6:00:00")
        workday.switch(self.s, True, "trial")
        first = sessions.start(self.s, "desk")
        self.at(T0 + timedelta(hours=2))
        with self.assertRaisesRegex(DurehError, rf"job 12345 \({first['id']}, submitted {T0.isoformat()}\) is still in squeue: "
                                                r"RUNNING, time left 6:00:00.*one desk session a day.*--purpose desk --force --memo"):
            sessions.start(self.s, "desk")
        self.assertIn("-j 12345", (fake / "squeue-calls").read_text())
        self.assertEqual(len((fake / "sbatch-calls").read_text().splitlines()), 1, "nothing submitted")
        self.assertEqual(len(self._sessions()), 1)
        rc, out = self.cli("site", "session", "start")
        self.assertEqual(rc, 2)
        # --force needs a memo; with one, the second start is recorded with why, and the day's clock stays
        with self.assertRaisesRegex(DurehError, "--force needs --memo"):
            sessions.start(self.s, "desk", force=True)
        self.assertEqual(len(self._sessions()), 1)
        rc, out = self.cli("site", "session", "start", "--force", "--memo", "remote control lost; Director asked")
        self.assertEqual(rc, 0, out)
        self.assertIn("Forced second start (remote control lost; Director asked): today's desk session job 12345", out)
        self.assertIn(f"the working day still runs from {T0.isoformat()}", out)
        rec = self._sessions()[-1]
        p = rec["payload"]
        self.assertEqual(p["force"]["memo"], "remote control lost; Director asked")
        self.assertIn("still in squeue: RUNNING", p["force"]["why"])
        self.assertEqual(p["previous"], {"record": first["id"], "job_id": "12345", "submitted_at": T0.isoformat(), "purpose": "desk"})
        self.assertEqual((p["day_start"], p["submitted_at"]), (T0.isoformat(), (T0 + timedelta(hours=2)).isoformat()))
        self.assertNotIn("restart", p)
        st = workday.status(self.s)
        self.assertEqual((st["session"], st["start"], st["end"]), (rec["id"], T0.isoformat(), (T0 + timedelta(hours=8)).isoformat()))

    def test_second_session_start_today_is_a_restart_when_the_job_is_gone(self):
        self.fake_slurm(squeue="")
        workday.switch(self.s, True, "trial")
        first = sessions.start(self.s, "desk")
        self.at(T0 + timedelta(hours=3))
        rc, out = self.cli("site", "session", "start")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"Restart: today's job 12345 ({first['id']}) is gone from squeue; the working day still runs from "
                      f"{T0.isoformat()}", out)
        rec = self._sessions()[-1]
        p = rec["payload"]
        self.assertIs(p["restart"], True)
        self.assertEqual(p["previous"]["record"], first["id"])
        self.assertEqual((p["day_start"], p["submitted_at"]), (T0.isoformat(), (T0 + timedelta(hours=3)).isoformat()))
        self.assertNotIn("force", p)
        st = workday.status(self.s)
        self.assertEqual((st["session"], st["start"], st["end"]), (rec["id"], T0.isoformat(), (T0 + timedelta(hours=8)).isoformat()))
        # a third start the same day keeps the first start too
        self.at(T0 + timedelta(hours=5))
        p = sessions.start(self.s, "desk")["payload"]
        self.assertEqual((p["previous"]["record"], p["day_start"]), (rec["id"], T0.isoformat()))
        self.assertEqual(workday.status(self.s)["end"], (T0 + timedelta(hours=8)).isoformat())

    def test_session_start_on_another_day_is_a_plain_start(self):
        self.fake_slurm(squeue="RUNNING|20:00:00")  # yesterday's job still holding its node
        workday.switch(self.s, True, "trial")
        sessions.start(self.s, "desk")
        self.at(T0 + timedelta(days=1))
        p = sessions.start(self.s, "desk")["payload"]
        self.assertNotIn("restart", p)
        self.assertNotIn("previous", p)
        self.assertNotIn("day_start", p)
        st = workday.status(self.s)
        self.assertEqual((st["start"], st["end"]), ((T0 + timedelta(days=1)).isoformat(), (T0 + timedelta(days=1, hours=8)).isoformat()))

    def test_second_session_start_refused_when_squeue_is_unavailable(self):
        fake = self.fake_slurm()
        (fake / "squeue").write_text("#!/bin/sh\necho 'slurm_load_jobs error: Unable to contact slurm controller' >&2\nexit 1\n")
        workday.switch(self.s, True, "trial")
        sessions.start(self.s, "desk")
        self.at(T0 + timedelta(hours=1))
        with self.assertRaisesRegex(DurehError, "job 12345 .* may still be running: squeue unavailable .*Unable to contact"):
            sessions.start(self.s, "desk")
        self.assertEqual(len(self._sessions()), 1)
        p = sessions.start(self.s, "desk", force=True, memo="controller down, Director asked")["payload"]
        self.assertIn("squeue unavailable", p["force"]["why"])

    def test_approve_without_a_session_uses_the_config_window(self):
        self.workday_config(phase="production", weekdays=ALL_WEEK, start="10:00", end="18:30")
        workday.switch(self.s, True, "trial")
        workday.plan(self.s, "desk", "g", "p")
        rec = workday.approve(self.s, "desk", "ok")
        self.assertEqual((rec["payload"]["start"][11:16], rec["payload"]["end"][11:16]), ("10:00", "18:30"))

    # ------------------------------------------------------------ weekday rule (production) and development phase
    def test_weekday_rule_makes_a_saturday_overtime(self):
        self.workday_config(phase="production")  # default Mon-Fri; T0 is a Saturday
        self.start_day()
        st = workday.status(self.s)
        self.assertEqual(st["phase"], "overtime")
        self.assertFalse(st["working_day"])
        out = self.spawn()
        self.assertTrue(denied(out))
        self.assertIn("Sat is not a working day", json.dumps(out))
        self.assertIn("day overtime", json.dumps(out))
        other = wf.wi_new(self.s, "desk", "X", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", other, "ok")
        workday.overtime(self.s, "desk", [self.wi], "토요일 W만")  # before the end report: the whole day is overtime
        self.assertIsNone(self.spawn())
        self.assertTrue(denied(self.spawn(other)))
        with self.assertRaisesRegex(DurehError, "stay on duty"):
            workday.end(self.s, "desk", "done", "", [])
        workday.end(self.s, "desk", "done", "", [], early="그만")
        self.assertEqual(workday.status(self.s)["phase"], "overtime")
        self.assertIn("not a working day", render_status(status(self.s)))

    def test_weekday_rule_config_names_and_monday(self):
        self.workday_config(phase="production", weekdays=["mon", "Tuesday", 2])
        self.assertEqual(workday.settings(self.s)["weekdays"], {0, 1, 2})
        self.at(MONDAY)
        self.start_day()
        self.assertEqual(workday.status(self.s)["phase"], "on-duty")
        self.assertIsNone(self.spawn())
        self.workday_config(phase="production", weekdays=["Funday"])
        with self.assertRaisesRegex(DurehError, "unknown weekday"):
            workday.settings(self.s)
        self.workday_config(phase="staging")
        with self.assertRaisesRegex(DurehError, "unknown phase"):
            workday.settings(self.s)

    def test_development_phase_warns_after_eight_hours_and_has_no_weekday_rule(self):
        self.workday_config()  # phase defaults to development
        self.assertEqual(workday.settings(self.s)["phase"], "development")
        self.start_day()  # a Saturday: an ordinary day
        st = workday.status(self.s)
        self.assertEqual(st["phase"], "on-duty")
        self.assertIn("Workday: on-duty [development phase]", render_status(status(self.s)))
        self.at(T0 + timedelta(hours=8, minutes=1))
        self.assertIsNone(self.spawn(), "the hours device only warns")
        st = workday.status(self.s)
        self.assertEqual(st["phase"], "on-duty")
        self.assertTrue(st["hours_exceeded"])
        text = render_status(status(self.s))
        self.assertIn("⚠ the 8-hour day ended at", text)
        self.assertIn("not blocking", text)
        workday.status(self.s)
        self.assertIsNone(workday.gate(self.s, self.wi))
        noted = [e for e in Ledger(self.s).events() if e["type"] == "hours.exceeded"]
        self.assertEqual(len(noted), 1, "recorded once per day")
        self.assertEqual(noted[0]["payload"]["phase"], "development")
        workday.end(self.s, "desk", "done", "", [])  # after the hours: no --early needed
        self.assertEqual(self.cli("ledger", "add", "event", "--type", "hours.exceeded", "--payload", "{}")[0], 2)

    def test_development_phase_keeps_the_usage_gate(self):
        self.workday_config()
        self.start_day()
        self.spend(7)
        workday.calibrate(self.s, 10)
        self.spend(3)
        self.at(T0 + timedelta(hours=9))
        out = self.spawn()
        self.assertTrue(denied(out))
        self.assertIn("daily share", json.dumps(out))

    # ------------------------------------------------------------ attendance devices: Director switches (2026-10-04)
    def test_devices_are_the_directors_switches(self):
        os.environ["CLAUDECODE"] = "1"
        self.assertEqual(self.cli("site", "workday", "set", "hours", "off", "--memo", "x")[0], 2)
        with self.assertRaisesRegex(DurehError, "^노동법 위반은 대표만 할 수 있습니다$"):
            workday.black_company(self.s, True, "x")
        os.environ.pop("CLAUDECODE")
        self.assertEqual(workday.status(self.s)["devices"], {"hours": True, "weekday": True, "usage": True, "session": True})
        self.assertEqual(self.cli("site", "workday", "set", "hours", "off")[0], 2, "memo required")
        self.assertEqual(self.cli("site", "workday", "set", "coffee", "off", "--memo", "x")[0], 2)
        self.assertEqual(self.cli("site", "workday", "set", "hours", "off", "--memo", "개발 중")[0], 0)
        rec = [e for e in Ledger(self.s).events() if e["type"] == "workday.device"][-1]
        self.assertEqual((rec["payload"]["device"], rec["payload"]["enabled"], rec["payload"]["by"]), ("hours", False, "Director"))
        self.assertEqual(workday.status(self.s)["devices_off"], ["hours"])
        self.assertEqual(self.cli("ledger", "add", "event", "--type", "workday.device", "--payload", "{}")[0], 2)

    def test_hours_off_removes_the_clock(self):
        self.start_day()
        workday.set_device(self.s, "hours", False, "밤샘")
        self.at(T0 + timedelta(hours=9))
        st = workday.status(self.s)
        self.assertEqual(st["phase"], "on-duty")
        self.assertIsNone(workday.gate(self.s, self.wi))
        self.assertIn("devices off: hours", render_status(status(self.s)))
        workday.end(self.s, "desk", "done", "", [])  # no clock, no --early

    def test_weekday_off_makes_a_saturday_ordinary(self):
        self.workday_config(phase="production")
        workday.switch(self.s, True, "trial")
        workday.set_device(self.s, "weekday", False, "주말 근무")
        workday.plan(self.s, "desk", "g", "p")
        workday.approve(self.s, "desk", "ok")
        self.assertEqual(workday.status(self.s)["phase"], "on-duty")
        self.assertIsNone(self.spawn())

    def test_usage_off_counts_but_does_not_gate(self):
        self.start_day()
        self.spend(7)
        workday.calibrate(self.s, 10)
        self.spend(3)
        self.assertIsNotNone(workday.gate(self.s, self.wi))
        workday.set_device(self.s, "usage", False, "오늘만")
        self.assertIsNone(workday.gate(self.s, self.wi))
        self.assertIn("usage today 10.0/10.0", render_status(status(self.s)))

    def test_session_off_skips_start_and_close_checks(self):
        self.start_day()
        self.assertIsNone(self.spawn())
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        self.at(T0 + timedelta(hours=9))
        workday.end(self.s, "desk", "done", "", [])
        h = self.site_root / "state" / "memory" / "handoff.md"
        if h.exists():
            h.unlink()
        with self.assertRaisesRegex(DurehError, "still running"):
            workday.close(self.s, "desk", "x")
        workday.set_device(self.s, "session", False, "테스트")
        rec = workday.close(self.s, "desk", "x")  # agent running, no handoff: not checked
        self.assertFalse(rec["payload"]["checked"])
        fake = self.fake_slurm()
        (fake / "claude").unlink()
        os.environ["PATH"] = str(fake)
        self.assertEqual(sessions.start(self.s, "desk")["payload"]["job_id"], "12345")  # the runtime check only notes

    def test_black_company_mode(self):
        self.start_day()
        workday.set_device(self.s, "hours", False, "먼저")
        os.environ["CLAUDECODE"] = "1"
        self.assertEqual(self.cli("site", "workday", "black-company", "on", "--memo", "x")[0], 2)
        os.environ.pop("CLAUDECODE")
        self.assertEqual(self.cli("site", "workday", "black-company", "off", "--memo", "x")[0], 2, "not on")
        self.assertEqual(self.cli("site", "workday", "black-company", "on", "--memo", "마감")[0], 0)
        rec = [e for e in Ledger(self.s).events() if e["type"] == "workday.black-company"][-1]
        self.assertEqual(rec["payload"]["previous"], {"hours": False, "weekday": True, "usage": True, "session": True})
        self.assertEqual(rec["payload"]["by"], "Director")
        st = workday.status(self.s)
        self.assertEqual(st["devices_off"], ["hours", "weekday", "usage", "session"])
        self.assertEqual(st["black_company"]["record"], rec["id"])
        self.assertEqual(self.cli("site", "workday", "black-company", "on", "--memo", "x")[0], 2, "already on")
        with self.assertRaisesRegex(DurehError, "블랙기업모드 is on"):
            workday.set_device(self.s, "usage", True, "x")
        first = f"⚠ 블랙기업모드 (since {rec['ts']}, by Director)"
        self.assertEqual(render_status(status(self.s)).splitlines()[0], first)
        self.assertEqual(self.cli("day", "status")[1].splitlines()[0], first)
        self.assertNotIn("24시간", self.cli("day", "status")[1])
        self.spend(7)
        workday.calibrate(self.s, 10)
        self.spend(3)
        self.assertIsNone(workday.gate(self.s, self.wi), "usage counted, not gated")
        self.assertIn("usage today 10.0/10.0", render_status(status(self.s)))
        self.at(datetime.fromisoformat(rec["ts"]) + timedelta(hours=25))
        self.assertIn("24시간이 지났습니다", render_status(status(self.s)).splitlines()[0])
        self.assertEqual(self.cli("site", "workday", "black-company", "off", "--memo", "복귀")[0], 0)
        st = workday.status(self.s)
        self.assertIsNone(st["black_company"])
        self.assertEqual(st["devices_off"], ["hours"], "the state before black-company mode is restored")
        self.assertNotIn("블랙기업모드", render_status(status(self.s)))
        workday.switch(self.s, False, "off")
        workday.black_company(self.s, True, "x")
        self.assertTrue(self.cli("day", "status")[1].startswith("⚠ 블랙기업모드"), "shown even with workday mode off")

    def test_desk_instructions_mention_workday(self):
        text = (self.site_root / "CLAUDE.md").read_text()
        self.assertIn("day plan", text)
        self.assertIn("day overtime", text)
        self.assertIn("stay on duty", text)
        self.assertIn("day close", text)
        self.assertIn("adapter sync --harness <release checkout>", text)
        # finish the plan (Director, 2026-10-06)
        self.assertIn("finished the same day", text)
        self.assertIn("never move a plan item to tomorrow", text)
        self.assertIn("records the overtime itself", text)
        self.assertIn("wi new --fix-for", text)
        self.assertIn("Write the end report when the plan is done", text)

    # ------------------------------------------------------------ finish the plan (Director, 2026-10-06)
    def overtime_events(self):
        return [e for e in Ledger(self.s).events() if e["type"] == "day.overtime"]

    def verify(self, wi):
        """Stage and verify a WI without a checker: finished for the day (promotion is a desk command)."""
        wf.wi_stage(self.s, "desk", wi, "done")
        Ledger(self.s).event("wi.verified", "desk", wi, {"run": None, "head": None})

    def other_wi(self, title="X"):
        other = wf.wi_new(self.s, "desk", title, "g", "pm-modules", "modules/demo", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", other, "ok")
        return other

    def test_finish_plan_continues_plan_items_after_the_end_time(self):
        # production, blocking clock; the plan names W (by id, as real plans do); X is outside the plan
        self.start_day(f"| 1 | {self.wi} build W | pm-modules | 2 | 3 |")
        other = self.other_wi()
        self.assertEqual(workday.status(self.s)["plan_items"], [self.wi])
        self.at(T0 + timedelta(hours=8, minutes=1))
        st = workday.status(self.s)
        self.assertEqual((st["phase"], st["finishing"], st["open"]), ("past-end", True, [self.wi]))
        self.assertEqual(st["reason"], f"end time passed 17:00; finishing the plan (1 item open: {self.wi})")
        self.assertIn(f"Workday: past-end (09:00-17:00); end time passed 17:00; finishing the plan (1 item open: {self.wi})",
                      render_status(status(self.s)))
        self.assertEqual(self.overtime_events(), [], "nothing recorded before the first work unit")
        self.assertIsNone(self.spawn(), "a plan item goes on after the end time")
        evs = self.overtime_events()
        self.assertEqual(len(evs), 1)
        p = evs[0]["payload"]
        self.assertEqual((p["items"], p["memo"], p["by"], p["rule"], p["plan"], p["first_work_unit"]),
                         ([self.wi], "Director rule 2026-10-06: 초과근무로 문제를 모두 해결하고 당일 업무를 마무리",
                          "Director (standing rule)", "finish-plan", st["plan"], True))
        self.assertEqual(evs[0]["refs"], [st["plan"]])
        self.assertIsNone(workday.gate(self.s, self.wi))  # (a second dispatch_brief waits for the start grace)
        self.assertIsNone(workday.gate(self.s, self.wi, checking=True))
        self.assertEqual(len(self.overtime_events()), 1, "recorded once")
        # new work outside the plan is still refused; the Director's explicit overtime admits it
        out = self.spawn(other)
        self.assertTrue(denied(out))
        self.assertIn("only the plan's open items", json.dumps(out, ensure_ascii=False))
        self.assertIn("day overtime", json.dumps(out))
        with self.assertRaisesRegex(DurehError, "not finished"):
            workday.end(self.s, "desk", "W still failing", "", [])  # the plan is not done: no end report yet
        workday.decide(self.s, "desk", "retry", "rerun", "flaky", [])  # decisions still recorded while finishing
        workday.overtime(self.s, "desk", [other], "X도 오늘")
        self.assertIsNone(self.spawn(other))
        self.assertEqual(len(self.overtime_events()), 2)
        self.assertNotIn("rule", self.overtime_events()[-1]["payload"])
        # the plan done: status says so, the end report is written, with the past-end section, then the close
        self.verify(self.wi)
        st = workday.status(self.s)
        self.assertEqual(st["phase"], "past-end")
        self.assertNotIn("finishing", st)
        self.assertIn("the plan is done; write the end report", st["reason"])
        rec = workday.end(self.s, "desk", "W verified past the end time", "", [])
        past = rec["payload"]["past_end"]
        self.assertEqual((past["rule"], past["by"], past["from"], past["stop"], past["open"]),
                         ("finish-plan", "Director (standing rule)", (T0 + timedelta(hours=8)).isoformat(), None, []))
        self.assertEqual(past["items"], [{"wi": self.wi, "title": "W", "outcome": "verified", "finished": True}])
        self.assertEqual(workday.status(self.s)["phase"], "overtime", "the Director's own item still stands")
        self.assertEqual(workday.status(self.s)["overtime"], [other])
        self.write_handoff()
        workday.close(self.s, "desk", "done")
        self.assertEqual(workday.status(self.s)["phase"], "off-duty")

    def test_finish_plan_admits_a_fix_for_a_plan_item(self):
        self.start_day(f"1. {self.wi}")
        self.at(T0 + timedelta(hours=8, minutes=1))
        self.assertIsNone(self.spawn())
        # a fix created after the plan: --fix-for a plan WI (or --parent, the bundle's child)
        fix = wf.wi_new(self.s, "desk", "fix W", "g", "pm-modules", "modules/demo", ["ok"], fix_for=self.wi)["id"]
        wf.wi_approve(self.s, "desk", fix, "plan approval covers the fix")
        child = wf.wi_new(self.s, "desk", "W child", "g", "py-writer", "modules/demo", ["ok"], parent=self.wi)["id"]
        wf.wi_approve(self.s, "desk", child, "ok")
        unrelated = self.other_wi("Y")
        st = workday.status(self.s)
        self.assertEqual((st["fixes"], st["open"]), ([fix, child], [self.wi, fix, child]))
        self.assertIsNone(self.spawn(fix))
        evs = self.overtime_events()
        self.assertEqual(len(evs), 2, "the fix is appended by a later event")
        self.assertEqual((evs[1]["payload"]["items"], evs[1]["payload"]["appended"], evs[1]["payload"]["rule"]),
                         ([fix, child], True, "finish-plan"))
        self.assertTrue(denied(self.spawn(unrelated)))
        self.assertEqual(len(self.overtime_events()), 2)
        # a fix of a fix follows the chain; the record carries fix_for
        fix2 = wf.wi_new(self.s, "desk", "fix the fix", "g", "pm-modules", "modules/demo", ["ok"], fix_for=fix)["id"]
        self.assertEqual(Ledger(self.s).get("work-item", fix2)["payload"]["fix_for"], fix)
        self.assertIn(fix2, workday.status(self.s)["open"])
        with self.assertRaises(DurehError):
            wf.wi_new(self.s, "desk", "bad", "g", "pm-modules", "modules/demo", ["ok"], fix_for="WI-9999")
        rc, out = self.cli("wi", "new", "--title", "cli fix", "--goal", "g", "--role", "pm-modules", "--target",
                           "modules/demo", "--acceptance", "ok", "--fix-for", self.wi)
        self.assertEqual(rc, 0, out)

    def test_finish_plan_off_restores_the_refusal(self):
        self.config(workday={"transcripts": [str(self.projects)], "phase": "production", "weekdays": ALL_WEEK,
                             "finish_plan": False})
        self.assertFalse(workday.settings(self.s)["finish_plan"])
        self.start_day(f"1. {self.wi}")
        self.at(T0 + timedelta(hours=8, minutes=1))
        out = self.spawn()
        self.assertTrue(denied(out))
        self.assertIn("end report", json.dumps(out))
        st = workday.status(self.s)
        self.assertEqual(st["phase"], "past-end")
        self.assertNotIn("finishing", st)
        self.assertEqual(self.overtime_events(), [])
        with self.assertRaisesRegex(DurehError, "end report"):
            workday.overtime(self.s, "desk", [self.wi], "x")
        workday.end(self.s, "desk", "done", "", [])  # as before: the end report at the end time

    def test_finish_plan_usage_share_still_stops(self):
        self.start_day(f"1. {self.wi}")
        self.spend(7)
        workday.calibrate(self.s, 10)  # weekly 70, daily 10
        self.at(T0 + timedelta(hours=8, minutes=1))
        self.assertIsNone(self.spawn())
        self.spend(3)
        self.assertIn("daily share", workday.gate(self.s, self.wi) or "")
        fix = wf.wi_new(self.s, "desk", "fix W", "g", "pm-modules", "modules/demo", ["ok"], fix_for=self.wi)["id"]
        wf.wi_approve(self.s, "desk", fix, "ok")
        out = self.spawn(fix)
        self.assertTrue(denied(out))
        self.assertIn("daily share", json.dumps(out))
        st = workday.status(self.s)
        self.assertEqual(st["hard_stop"]["stop"], "usage")
        self.assertIn("reached the daily share", st["warning"])
        self.assertIn("write the end report", render_status(status(self.s)))
        rec = workday.end(self.s, "desk", "W unfinished: usage", "", [self.wi])  # a stop: the report may be written
        past = rec["payload"]["past_end"]
        self.assertEqual((past["stop"]["stop"], past["open"]), ("usage", [self.wi, fix]))
        self.assertEqual([i["finished"] for i in past["items"]], [False, False])

    def test_finish_plan_wall_time_warns_and_stops(self):
        fake = self.fake_slurm(squeue="RUNNING|0:40:00")
        Ledger(self.s).event("session.started", "desk", None, {"job_id": "777", "purpose": "desk",
                                                               "submitted_at": T0.isoformat()})
        self.start_day(f"1. {self.wi}")
        self.at(T0 + timedelta(hours=8, minutes=1))
        st = workday.status(self.s)
        self.assertEqual((st["job_id"], st["hard_stop"]["stop"]), ("777", "wall-time"))
        self.assertIn("0:40:00 of wall time left", st["warning"])
        self.assertIn("-j 777", (fake / "squeue-calls").read_text())
        self.assertIsNone(self.spawn(), "no duration notion for a spawn: the gate only warns through status")
        workday.end(self.s, "desk", "W unfinished: job ends", "", [self.wi])
        # with time to spare there is no stop and the end report waits for the plan
        self.fake_slurm(squeue="RUNNING|5:00:00")
        self.s = self.site()
        workday.switch(self.s, True, "again")
        self.at(T0 + timedelta(days=1))
        Ledger(self.s).event("session.started", "desk", None, {"job_id": "778", "purpose": "desk",
                                                               "submitted_at": (T0 + timedelta(days=1)).isoformat()})
        self.start_day(f"1. {self.wi}")
        self.at(T0 + timedelta(days=1, hours=8, minutes=1))
        st = workday.status(self.s)
        self.assertNotIn("hard_stop", st)
        with self.assertRaisesRegex(DurehError, "not finished"):
            workday.end(self.s, "desk", "x", "", [])

    def test_finish_plan_urgent_is_a_stop(self):
        self.start_day(f"1. {self.wi}")
        self.at(T0 + timedelta(hours=8, minutes=1))
        other = self.other_wi()
        workday.urgent(self.s, "desk", [other], "disk filling")
        self.assertEqual(workday.status(self.s)["hard_stop"]["stop"], "urgent")
        workday.end(self.s, "desk", "stopped for the disk", "", [])

    def test_finish_plan_in_the_development_phase(self):
        self.workday_config()  # development: nothing blocks, the overtime is still recorded, the report waits
        self.start_day(f"1. {self.wi}")
        other = self.other_wi()
        self.at(T0 + timedelta(hours=8, minutes=1))
        st = workday.status(self.s)
        self.assertEqual((st["phase"], st["finishing"], st["hours_exceeded"]), ("on-duty", True, True))
        self.assertEqual(st["reason"], f"end time passed 17:00; finishing the plan (1 item open: {self.wi})")
        self.assertIn("not blocking); end time passed 17:00; finishing the plan", st["warning"])
        self.assertIsNone(self.spawn(other), "outside the plan: still only a warning in development")
        self.assertEqual(self.overtime_events(), [])
        self.assertIsNone(self.spawn())
        self.assertEqual([e["payload"]["items"] for e in self.overtime_events()], [[self.wi]])
        with self.assertRaisesRegex(DurehError, "not finished"):
            workday.end(self.s, "desk", "x", "", [])
        wf.dispatch_stop(self.s, "desk", self.wi, "blocked on upstream")  # a stopped item is finished for the day
        self.assertNotIn("finishing", workday.status(self.s))
        rec = workday.end(self.s, "desk", "W stopped", "", [])
        self.assertEqual(rec["payload"]["past_end"]["items"][0]["outcome"], "stopped")

    def test_finish_plan_does_not_replace_the_weekend_memo(self):
        self.workday_config(phase="production")  # Mon-Fri; T0 is a Saturday
        self.start_day(f"1. {self.wi}")
        self.assertTrue(denied(self.spawn()))
        self.at(T0 + timedelta(hours=8, minutes=1))
        out = self.spawn()
        self.assertTrue(denied(out))
        self.assertIn("end report", json.dumps(out))
        self.assertEqual(self.overtime_events(), [])
        self.assertNotIn("finishing", workday.status(self.s))

    def test_day_plan_wi_option_and_text_extraction(self):
        workday.switch(self.s, True, "trial")
        other = self.other_wi()
        with self.assertRaises(DurehError):
            workday.plan(self.s, "desk", "g", "p", ["WI-9999"])
        rec = workday.plan(self.s, "desk", "g", f"text names {other} only", [self.wi])
        self.assertEqual(rec["payload"]["items"], [self.wi])
        self.assertEqual(workday.plan_items(rec), [self.wi], "--wi wins over the text")
        self.assertEqual(workday.plan_items({"payload": {"plan": f"a {other}, b {self.wi}, {other} again"}}),
                         sorted([other, self.wi]))
        workday.approve(self.s, "desk", "ok")
        self.at(T0 + timedelta(hours=9))
        workday.end(self.s, "desk", "x", "", [], early="그만")
        self.at(T0 + timedelta(days=1))
        rc, out = self.cli("day", "plan", "--goal", "g", "--file", str(self.write_plan(f"1. {other}")), "--wi", other)
        self.assertEqual(rc, 0, out)

    def write_plan(self, text):
        p = self.tmp / "plan.md"
        p.write_text(text)
        return p


if __name__ == "__main__":
    unittest.main()
