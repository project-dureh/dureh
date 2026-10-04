"""Workday mode: Director switch, day plan/approval clock, decisions, end report, overtime, usage budget."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from helpers import SiteCase, denied, hook

from dureh_core import claude_usage, workday
from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.ops import render_status, status
from dureh_core.util import DurehError

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=timezone(timedelta(hours=-4)))


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
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["workday"] = {"transcripts": [str(self.projects)]}
        (self.site_root / "config.json").write_text(json.dumps(cfg, indent=2))
        self.s = self.site()
        self.at(T0)
        self.wi = wf.wi_new(self.s, "desk", "W", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", self.wi, "ok")

    def at(self, dt: datetime):
        os.environ["DUREH_FAKE_NOW"] = dt.isoformat()

    def spend(self, units: float):
        now = datetime.fromisoformat(os.environ["DUREH_FAKE_NOW"])
        with (self.projects / "p" / "s.jsonl").open("a") as fh:
            fh.write(transcript_line(now, f"r{units}{os.urandom(3).hex()}", inp=int(units * 1_000_000)))

    def spawn(self, wi=None):
        brief = wf.dispatch_brief(self.s, wi or self.wi, "desk")["brief"]
        return hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
                    tool_input={"subagent_type": "pm-modules", "prompt": brief, "description": "x"})

    def start_day(self):
        workday.switch(self.s, True, "trial")
        workday.plan(self.s, "desk", "finish W", "1. W")
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

    def test_close_releases_the_session_job(self):
        self.end_day()
        self.write_handoff()
        fake = self.tmp / "fakebin"
        fake.mkdir()
        (fake / "scancel").write_text(f"#!/bin/sh\necho \"$@\" > {fake}/args\n")
        (fake / "scancel").chmod(0o755)
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["env"] = {"path_prepend": [str(fake)]}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        from dureh_core.records import Ledger
        Ledger(self.s).event("session.started", "desk", None, {"job_id": "777"})
        os.environ["SLURM_JOB_ID"] = "777"
        try:
            rec = workday.close(self.s, "desk", "done")
        finally:
            os.environ.pop("SLURM_JOB_ID")
        self.assertTrue(rec["release"]["released"])
        self.assertEqual((fake / "args").read_text().strip(), "777")
        self.assertEqual(workday.status(self.s)["phase"], "off-duty")

    def test_close_never_cancels_a_job_dureh_did_not_start(self):
        # Director, 2026-10-03: dureh may release only company session jobs it started; tests never
        # name the real session (helpers.SiteCase drops SLURM_JOB_ID; ids here are made up).
        self.end_day()
        self.write_handoff()
        fake = self.tmp / "fakebin"
        fake.mkdir()
        (fake / "scancel").write_text(f"#!/bin/sh\necho \"$@\" > {fake}/args\n")
        (fake / "scancel").chmod(0o755)
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["env"] = {"path_prepend": [str(fake)]}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        os.environ["SLURM_JOB_ID"] = "424242"
        try:
            rec = workday.close(self.s, "desk", "done")
        finally:
            os.environ.pop("SLURM_JOB_ID")
        self.assertFalse(rec["release"]["released"])
        self.assertIn("session.started", rec["release"]["reason"])
        self.assertFalse((fake / "args").exists())
        self.assertEqual(workday.status(self.s)["phase"], "off-duty")

    def test_close_outside_a_job_releases_nothing(self):
        self.end_day()
        self.write_handoff()
        os.environ.pop("SLURM_JOB_ID", None)
        rec = workday.close(self.s, "desk", "done")
        self.assertFalse(rec["release"]["released"])

    def test_desk_instructions_mention_workday(self):
        text = (self.site_root / "CLAUDE.md").read_text()
        self.assertIn("day plan", text)
        self.assertIn("day overtime", text)
        self.assertIn("stay on duty", text)
        self.assertIn("day close", text)
        self.assertIn("adapter sync --harness <release checkout>", text)


if __name__ == "__main__":
    unittest.main()
