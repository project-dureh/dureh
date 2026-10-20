"""Workday mode: Director switch, day plan/approval clock, decisions, end report, overtime, usage budget."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from helpers import SiteCase, denied, hook

from pathlib import Path

from dureh_core import claude_usage, sessions, slack, workday
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

    def test_week_window_rolling_without_weekly_reset(self):
        """Release 72 (#24): no weekly_reset keeps the rolling seven local days ending today."""
        at = datetime(2026, 10, 19, 14, 30, tzinfo=timezone(timedelta(hours=-4)))
        self.assertEqual(claude_usage.week_window({}, at), {"kind": "rolling", "start": "2026-10-13T00:00:00-04:00",
                                                            "end": "2026-10-19T14:30:00-04:00"})

    def test_week_window_from_a_past_or_future_reset(self):
        """Release 72 (#24): the provider resets every 7 days from weekly_reset; the window starts at the latest
        reset at or before now, whether the instant given is in the past or in the future."""
        at = datetime(2026, 10, 19, 14, 30, tzinfo=timezone(timedelta(hours=-4)))
        for reset, start in (("2026-10-02T13:00:00-04:00", "2026-10-16T13:00:00-04:00"),  # two weeks back
                             ("2026-10-23T13:00:00-04:00", "2026-10-16T13:00:00-04:00"),  # in the future
                             ("2026-10-16T17:00:00+00:00", "2026-10-16T17:00:00+00:00"),  # another offset
                             ("2026-10-19T14:30:00-04:00", "2026-10-19T14:30:00-04:00")):  # exactly now
            w = claude_usage.week_window({"weekly_reset": reset}, at)
            self.assertEqual((w["kind"], w["start"], w["end"]), ("reset", start, "2026-10-19T14:30:00-04:00"), reset)

    def test_week_units_count_from_the_latest_reset(self):
        """Release 72 (#24): with weekly_reset the week sums usage since the latest reset, the reset day cut at
        the instant; summary names the window. Without it, the rolling seven days as before."""
        now = datetime.now().astimezone().replace(microsecond=0)
        (self.root / "s.jsonl").write_text(
            transcript_line(now - timedelta(days=6), "a", inp=1_000_000)
            + transcript_line(now - timedelta(days=2, hours=2), "b", inp=4_000_000)  # before the reset
            + transcript_line(now - timedelta(days=2), "c", inp=2_000_000)
            + transcript_line(now - timedelta(hours=1), "d", inp=1_000_000))
        roots = [str(self.tmp / "projects")]
        rolling = claude_usage.summary({"weekly_units": 80}, roots, at=now)
        self.assertAlmostEqual(rolling["week_units"], 8.0)
        self.assertEqual(rolling["window_kind"], "rolling")
        start = now - timedelta(days=2, hours=1)
        for reset in (start, start + timedelta(days=7), start - timedelta(days=14)):  # now, future, past
            s = claude_usage.summary({"weekly_units": 80, "weekly_reset": reset.isoformat()}, roots, at=now)
            self.assertAlmostEqual(s["week_units"], 3.0, msg=reset.isoformat())
            self.assertAlmostEqual(s["week_percent"], 3.8)
            self.assertEqual((s["window_kind"], s["window_start"], s["window_end"]),
                             ("reset", start.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")))
        cal = claude_usage.calibrate(10, roots=roots, weekly_reset=start.isoformat(), at=now)
        self.assertAlmostEqual(cal["weekly_units"], 30.0)
        self.assertEqual(cal["window"]["kind"], "reset")


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

    def test_end_report_spawn_table_counts_build_and_doc_with_their_window(self):
        """Release 59 (D4 follow-up): a worker-role spawn is `build`, a doc-writer spawn is `doc`, in `usage` and in
        the end report; both tables name their window; the events carry `kind_reason`."""
        from dureh_core import ops

        self.start_day()
        worker = wf.wi_new(self.s, "desk", "R", "g", "researcher", "site", ["ok"])["id"]
        doc = wf.wi_new(self.s, "desk", "D", "g", "doc-writer", "modules/demo", ["ok"])["id"]
        for w in (worker, doc):
            wf.wi_approve(self.s, "desk", w, "ok")
        for w, role in ((worker, "researcher"), (doc, "doc-writer")):
            brief = wf.dispatch_brief(self.s, w, "desk")["brief"]
            self.assertIsNone(hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
                                   tool_input={"subagent_type": role, "prompt": brief, "description": "x"}))
        model = self.s.model_for_tier(self.s.org.role("researcher").tier)["model"]
        events = [e for e in Ledger(self.s).events() if e["type"] == "wi.dispatched"]
        self.assertEqual([(e["payload"]["kind"], e["payload"]["kind_reason"]) for e in events],
                         [("build", "default"), ("doc", "role in routing_doc_roles")])
        today = datetime.now(timezone.utc).date().isoformat()
        u = ops.usage(self.s)["spawns_today"]
        self.assertEqual(u["kinds"]["build"][model], 1)
        self.assertEqual(sum(u["kinds"]["doc"].values()), 1)
        self.assertEqual(u["window"], {"since": None, "day": today})
        self.at(T0 + timedelta(hours=8, minutes=1))
        rec = workday.end(self.s, "desk", "R and D", "", [])
        table = rec["payload"]["spawns"]
        plan = [e for e in Ledger(self.s).events() if e["type"] == "day.planned"][-1]
        self.assertEqual(table["kinds"]["build"][model], 1)
        self.assertEqual((sum(table["kinds"]["doc"].values()), table["total"]), (1, 2))
        self.assertEqual(table["window"], {"since": plan["id"], "day": plan["ts"][:10]})

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

    def test_day_status_week_from_the_shared_weekly_reset(self):
        """Release 72 (#24): the week usage follows `weekly_reset` in <exchange>/usage-budget.json, `day status
        --json` names the window, and calibrate keeps weekly_reset when it rewrites the file."""
        ex = self.tmp / "exchange"
        ex.mkdir()
        self.config(upstream={"exchange": str(ex)})
        workday.switch(self.s, True, "trial")
        reset = T0 - timedelta(hours=1)
        f = ex / "usage-budget.json"
        f.write_text(json.dumps({"weekly_units": 100, "weekly_reset": (reset - timedelta(days=21)).isoformat()}))
        self.at(T0 - timedelta(hours=2))
        self.spend(4)  # before the reset
        self.at(T0)
        self.spend(1)
        rc, out = self.cli("day", "status", "--json")
        u = json.loads(out)["usage"]
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(u["week_units"], 1.0)
        self.assertEqual((u["window_kind"], u["window_start"], u["window_end"]),
                         ("reset", reset.isoformat(), T0.isoformat()))
        workday.calibrate(self.s, 10)
        data = json.loads(f.read_text())
        self.assertAlmostEqual(data["weekly_units"], 10.0)
        self.assertEqual(data["weekly_reset"], (reset - timedelta(days=21)).isoformat())
        f.write_text(json.dumps({"weekly_units": 100}))
        u = workday.status(self.s)["usage"]
        self.assertAlmostEqual(u["week_units"], 5.0)
        self.assertEqual(u["window_kind"], "rolling")

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
        with self.assertRaisesRegex(DurehError, "--approval <id> is required"):
            workday.end(self.s, "desk", "plan done", "", [], early="오늘은 일찍 마칩시다")
        dec = self.director_ok()
        rec = workday.end(self.s, "desk", "plan done", "", [], early="오늘은 일찍 마칩시다", approval=dec)
        self.assertEqual(rec["payload"]["early"], "오늘은 일찍 마칩시다")

    # ------------------------------------------------------------ early end and close (release 74, #33)
    def director_ok(self, by="Director", disposition="approve", type_="record"):
        """A recorded approval for an early end (`ledger add decision --disposition approve --by Director`)."""
        return Ledger(self.s).append("decision", type_, "desk", None, {"subject": "early close", "disposition": disposition,
                                                                       "by": by, "memo": "오늘은 일찍"})["id"]

    def test_status_shows_the_end_in_local_time_and_utc(self):
        self.start_day()
        self.at(T0 + timedelta(hours=3, minutes=56))
        st = workday.status(self.s)
        self.assertEqual((st["end_utc"], st["start_utc"]), ("2026-10-03T21:00:00Z", "2026-10-03T13:00:00Z"))
        self.assertEqual(st["end_local"], "17:00 UTC-04:00 (21:00 UTC)")
        self.assertEqual(st["end_in"], "4 h 04 min")
        text = render_status(status(self.s))
        self.assertIn("ends 17:00 UTC-04:00 (21:00 UTC), 4 h 04 min left", text)
        rc, out = self.cli("day", "status", "--json")
        self.assertEqual((rc, json.loads(out)["end_utc"]), (0, "2026-10-03T21:00:00Z"))
        rc, out = self.cli("day", "status")
        self.assertIn("(21:00 UTC)", out)
        self.at(T0 + timedelta(hours=9))
        self.assertIn("end passed", render_status(status(self.s)))
        self.assertNotIn("end_in", workday.status(self.s))

    def test_end_before_the_end_says_local_utc_and_time_left(self):
        self.start_day()
        self.at(T0 + timedelta(hours=4, minutes=30))
        with self.assertRaises(DurehError) as cm:
            workday.end(self.s, "desk", "done", "", [])
        msg = str(cm.exception)
        self.assertIn("17:00 UTC-04:00 (21:00 UTC)", msg)
        self.assertIn("3 h 30 min from now", msg)
        self.assertIn("--early --approval <id> --reason", msg)
        with self.assertRaisesRegex(DurehError, "no recorded approval dec-nope"):
            workday.end(self.s, "desk", "done", "", [], early="일찍", approval="dec-nope")
        for bad in (self.director_ok(by="desk"), self.director_ok(disposition="defer"),
                    self.director_ok(type_="push-approval")):
            with self.assertRaisesRegex(DurehError, "is not an approval"):
                workday.end(self.s, "desk", "done", "", [], early="일찍", approval=bad)
        self.assertEqual([e for e in Ledger(self.s).events() if e["type"] == "day.ended"], [])
        dec = self.director_ok(by="DUREH")  # approval layer 2
        rec = workday.end(self.s, "desk", "done", "", [], early="대표님 승인: 일찍", approval=dec)
        ec = rec["payload"]["early_close"]
        self.assertEqual((ec["planned_end_utc"], ec["left"], ec["approval"]["id"], ec["approval"]["by"], ec["reason"]),
                         ("2026-10-03T21:00:00Z", "3 h 30 min", dec, "DUREH", "대표님 승인: 일찍"))
        self.assertIn(dec, rec["refs"])

    def test_desk_rule_names_the_early_guard(self):
        from dureh_core.runtimes.claude_code import _claude_md

        text = " ".join(_claude_md(self.s).split())
        for words in ("Never end or close the day before its end without the Director's recorded approval",
                      "day status` first; it shows the end in local time with UTC beside it",
                      "every time the harness prints is local unless marked UTC",
                      "day end|close --early --approval <id> --reason",
                      "`day end` and `day close` still keep to today's session clock"):
            self.assertIn(words, text)
        doc = (Path(__file__).resolve().parents[1] / "docs" / "workday.md").read_text()
        self.assertIn("never end or close the day before its end without the\nDirector's recorded approval; check "
                      "`day status` first; times are local.", doc)

    def test_early_approval_from_the_board_decision(self):
        self.start_day()
        self.at(T0 + timedelta(hours=2))
        url = "https://github.com/project-dureh/office/issues/40"
        led = Ledger(self.s)
        later = led.event("github.decision.changed", "desk", None, {"item_id": "I1", "url": url, "from": None,
                                                                    "to": "Later", "layer": 3, "first_poll": False})
        with self.assertRaisesRegex(DurehError, "is Later"):
            workday.end(self.s, "desk", "done", "", [], early="x", approval="project-dureh/office#40")
        ok = led.event("github.decision.changed", "desk", None, {"item_id": "I1", "url": url, "from": "Later",
                                                                 "to": "Approved", "layer": 3, "first_poll": False})
        with self.assertRaisesRegex(DurehError, "is Later"):
            workday.end(self.s, "desk", "done", "", [], early="x", approval=later["id"])
        rec = workday.end(self.s, "desk", "done", "", [], early="board", approval="project-dureh/office#40")
        self.assertEqual((rec["payload"]["early_close"]["approval"]["id"],
                          rec["payload"]["early_close"]["approval"]["kind"]), (ok["id"], "github"))

    def test_early_approval_older_than_a_day_is_refused(self):
        now = datetime.now().astimezone() + timedelta(days=2)
        dec = self.director_ok()  # recorded now (real time): two days before the fake now
        self.at(now)
        self.start_day()
        self.at(now + timedelta(hours=1))
        with self.assertRaisesRegex(DurehError, "more than 24 h ago"):
            workday.end(self.s, "desk", "done", "", [], early="x", approval=dec)

    def test_close_before_the_end_needs_the_approval(self):
        self.start_day()
        self.write_handoff()
        self.fake_slurm()
        self.at(T0 + timedelta(hours=2))
        dec = self.director_ok()
        rep = workday.end(self.s, "desk", "done", "", [], early="일찍", approval=dec)
        rec = workday.close(self.s, "desk", "x")  # the end report's approval covers the close
        self.assertEqual((rec["payload"]["early_close"]["covered_by"], rec["payload"]["early_close"]["approval"]["id"]),
                         (rep["id"], dec))
        self.assertIn(dec, rec["refs"])

    def test_close_before_the_end_without_an_early_report_is_refused(self):
        self.start_day()
        self.write_handoff()
        self.fake_slurm()
        self.at(T0 + timedelta(hours=8))
        workday.set_device(self.s, "hours", False, "x")  # the report goes in with the clock off ...
        workday.end(self.s, "desk", "done", "", [])
        workday.set_device(self.s, "hours", True, "x")  # ... and the clock is back for the close
        self.at(T0 + timedelta(hours=7))
        with self.assertRaisesRegex(DurehError, r"`day close` refused: the day ends at 17:00 UTC-04:00 \(21:00 UTC\), "
                                                r"1 h 00 min from now"):
            workday.close(self.s, "desk", "x")
        self.assertEqual([e for e in Ledger(self.s).events() if e["type"] == "day.closed"], [])
        rec = workday.close(self.s, "desk", "x", early="대표님 OK", approval=self.director_ok())
        self.assertEqual(rec["payload"]["early_close"]["reason"], "대표님 OK")

    def test_cli_early_flags(self):
        self.start_day()
        self.at(T0 + timedelta(hours=1))
        rc, out = self.cli("day", "end", "--done", "x", "--reason", "r")
        self.assertEqual(rc, 2)
        rc, out = self.cli("day", "end", "--done", "x", "--early")
        self.assertEqual(rc, 2)
        rc, out = self.cli("day", "end", "--done", "x")
        self.assertEqual(rc, 2)
        dec = self.director_ok()
        rc, out = self.cli("day", "end", "--done", "x", "--early", "--approval", dec, "--reason", "대표님: 오늘은 여기까지")
        self.assertEqual(rc, 0, out)
        rep = [e for e in Ledger(self.s).events() if e["type"] == "day.ended"][-1]
        self.assertEqual((rep["payload"]["early"], rep["payload"]["early_close"]["approval"]["id"]),
                         ("대표님: 오늘은 여기까지", dec))

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
        workday.end(self.s, "desk", "done", "", [], early="그만", approval=self.director_ok())
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

    # ------------------------------------------------------------ workday mode off (release 67, after 10-15)
    def _closable_off(self, job="777", ago=timedelta(hours=9)):
        """Workday mode off (never switched on): no plan, no end report; a fresh handoff, fake slurm and a recorded
        session job submitted `ago` before the fake now (release 74: by default the day's 8 hours have passed)."""
        self.write_handoff()
        fake = self.fake_slurm()
        if job:
            at = datetime.fromisoformat(os.environ["DUREH_FAKE_NOW"]) - ago
            Ledger(self.s).event("session.started", "desk", None,
                                 {"job_id": job, "purpose": "desk", "submitted_at": at.isoformat()})
        return fake

    def test_workday_off_close_before_the_session_clock_end_is_refused(self):
        """Release 74 (#33, mokoji 2026-10-19, workday mode off): closed at 11:35 local, four hours before the end."""
        self.assertFalse(workday.enabled(self.s))
        self._closable_off(ago=timedelta(hours=4, minutes=4))  # session 04:56 local: the day ends 12:56
        st = workday.status(self.s)
        self.assertEqual((st["end_utc"], st["end_in"]), ("2026-10-03T16:56:00Z", "3 h 56 min"))
        self.assertIn("Day clock (workday mode off): session start 04:56 UTC-04:00 (08:56 UTC), ends 12:56 UTC-04:00 "
                      "(16:56 UTC), 3 h 56 min left", render_status(status(self.s)))
        with self.assertRaisesRegex(DurehError, r"`day close` refused: the day ends at 12:56 UTC-04:00 \(16:56 UTC\), "
                                                r"3 h 56 min from now"):
            workday.close(self.s, "desk", "x")
        rc, out = self.cli("day", "close", "--early", "--reason", "끝")
        self.assertEqual(rc, 2)
        self.assertEqual([e for e in Ledger(self.s).events() if e["type"] in ("day.closed", "session.released")], [])
        dec = self.director_ok()
        rc, out = self.cli("day", "close", "--early", "--approval", dec, "--reason", "대표님: 일찍 퇴근")
        self.assertEqual(rc, 0, out)
        rec = [e for e in Ledger(self.s).events() if e["type"] == "day.closed"][-1]
        self.assertEqual((rec["payload"]["workday"], rec["payload"]["early_close"]["approval"]["id"]), (False, dec))
        self.assertEqual(rec["refs"], [dec])

    def test_workday_off_end_before_the_session_clock_end_is_refused(self):
        plan, _ = self._plan_off()
        self._closable_off(ago=timedelta(hours=1))
        with self.assertRaisesRegex(DurehError, "`day end` refused: the day ends at"):
            workday.end(self.s, "desk", "W done", "", [])
        rep = workday.end(self.s, "desk", "W done", "", [], early="일찍", approval=self.director_ok())
        self.assertIs(rep["payload"]["workday"], False)
        rec = workday.close(self.s, "desk", "done")  # covered by the report's approval
        self.assertEqual(rec["payload"]["early_close"]["covered_by"], rep["id"])
        workday.set_device(self.s, "hours", False, "밤샘")  # the Director's switch: no clock, no guard
        self.assertIsNone(workday.planned_end(self.s, workday.day(self.s)))

    def test_close_with_workday_off_records_and_releases(self):
        self.assertFalse(workday.enabled(self.s))
        fake = self._closable_off()
        os.environ.pop("SLURM_JOB_ID", None)
        rec = workday.close(self.s, "desk", "done")
        self.assertEqual(rec["type"], "day.closed")
        self.assertIs(rec["payload"]["workday"], False)
        self.assertEqual(rec["refs"], [], "no plan to reference")
        self.assertTrue(rec["payload"]["checked"])
        self.assert_marker(rec["release"], fake, parent=rec["id"])
        self.assertEqual(self.released()[-1]["refs"], [rec["id"]])

    def test_close_with_workday_off_and_no_session_needs_only_the_handoff(self):
        self._closable_off(job=None)
        h = self.site_root / "state" / "memory" / "handoff.md"
        old = T0.timestamp() - 86400
        os.utime(h, (old, old))  # no session start to compare with: existence is enough
        rec = workday.close(self.s, "desk", "done")
        self.assertIs(rec["payload"]["workday"], False)
        self.assertEqual((rec["release"]["released"], rec["release"]["reason"]), (False, "no running session job"))

    def test_close_with_workday_off_still_checks_handoff_and_agents(self):
        self._closable_off()
        h = self.site_root / "state" / "memory" / "handoff.md"
        old = T0.timestamp() - 10 * 3600  # before the session start (9 h ago)
        os.utime(h, (old, old))
        with self.assertRaisesRegex(DurehError, "predates the running session's start .*job 777"):
            workday.close(self.s, "desk", "x")
        h.unlink()
        with self.assertRaisesRegex(DurehError, "write the handoff"):
            workday.close(self.s, "desk", "x")
        self.write_handoff()
        self.assertIsNone(self.spawn())  # workday mode off: no gate
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        with self.assertRaisesRegex(DurehError, "still running"):
            workday.close(self.s, "desk", "x")
        self.assertEqual([e for e in Ledger(self.s).events() if e["type"] in ("day.closed", "session.released")], [])

    def test_close_with_workday_on_still_needs_the_end_report(self):
        self.start_day()
        self.write_handoff()
        with self.assertRaisesRegex(DurehError, "after its end report"):
            workday.close(self.s, "desk", "x")
        self.at(T0 + timedelta(hours=9))
        workday.end(self.s, "desk", "done", "", [])
        rec = workday.close(self.s, "desk", "x")
        self.assertNotIn("workday", rec["payload"])
        self.assertEqual(len(rec["refs"]), 1, "the day plan is referenced")

    # ------------------------------------------------------------ day plan with workday mode off (release 70)
    def _plan_off(self, approve=True):
        rec = workday.plan(self.s, "desk", "finish W", "1. W", [self.wi], ["link the dataset"])
        return rec, (workday.approve(self.s, "desk", "승인") if approve else None)

    def test_decide_with_workday_off_records_against_the_plan(self):
        """#27 (2026-10-19, workday mode off): `day decide` was refused, so decisions lived only in the end report."""
        self.assertFalse(workday.enabled(self.s))
        with self.assertRaisesRegex(DurehError, "decisions are recorded during the working day"):
            workday.decide(self.s, "desk", "t", "c", "w", [])
        plan, _ = self._plan_off()
        out = workday.decide(self.s, "desk", "dataset", "Option 2", "fallback after 30 min", [])
        rec = [e for e in Ledger(self.s).events() if e["type"] == "day.decision"][-1]
        self.assertEqual((rec["id"], rec["refs"]), (out["record"], [plan["id"]]))

    def test_plan_with_workday_off_records_items_actions_and_approval(self):
        self.assertFalse(workday.enabled(self.s))
        plan, appr = self._plan_off()
        self.assertEqual(plan["type"], "day.planned")
        self.assertIs(plan["payload"]["workday"], False)
        self.assertEqual((plan["payload"]["items"], plan["payload"]["director_actions"]), ([self.wi], ["link the dataset"]))
        self.assertIn("heads", plan["payload"])
        self.assertEqual(appr["type"], "day.approved")
        self.assertEqual(appr["refs"], [plan["id"]])
        self.assertIs(appr["payload"]["workday"], False)
        self.assertEqual(appr["payload"]["by"], "Director")
        self.assertNotIn("start", appr["payload"], "no hours: no clock starts")
        self.assertNotIn("end", appr["payload"])
        d = workday.day(self.s)
        self.assertEqual((d["phase"], d["plan"], d["approval"], d["plan_items"], d["workday"]),
                         ("on-duty", plan["id"], appr["id"], [self.wi], False))
        self.assertIsNone(self.spawn(), "workday mode off: no gate")
        st = workday.status(self.s)
        self.assertFalse(st["enabled"])
        self.assertEqual((st["day"]["plan"], st["day"]["approval"]), (plan["id"], appr["id"]))
        text = render_status(status(self.s))
        self.assertIn(f"Day (workday mode off): on-duty, plan {plan['id']}, approved {appr['id']}", text)
        self.assertIn("link the dataset", text)
        with self.assertRaisesRegex(DurehError, "no day plan waits for approval"):
            workday.approve(self.s, "desk", "again")
        with self.assertRaisesRegex(DurehError, "workday mode is off"):
            workday.overtime(self.s, "desk", [self.wi], "ok")

    def test_end_and_close_with_workday_off_reference_the_plan(self):
        plan, _ = self._plan_off()
        with self.assertRaisesRegex(DurehError, "no overtime to propose"):
            workday.end(self.s, "desk", "done", "", [self.wi])
        rep = workday.end(self.s, "desk", "W done", "none", [])  # no clock: no --early needed
        self.assertEqual(rep["type"], "day.ended")
        self.assertIs(rep["payload"]["workday"], False)
        self.assertIsNone(rep["payload"]["start"])
        self.assertNotIn("past_end", rep["payload"])
        self.assertEqual(rep["refs"], [plan["id"]])
        self.assertEqual(workday.day(self.s)["phase"], "off-duty")
        with self.assertRaisesRegex(DurehError, "no open day to end"):
            workday.end(self.s, "desk", "again", "", [])
        self._closable_off()
        rec = workday.close(self.s, "desk", "done")
        self.assertIs(rec["payload"]["workday"], False)
        self.assertEqual((rec["refs"], rec["payload"]["plan"]), ([plan["id"]], plan["id"]))

    def test_close_with_workday_off_references_an_unended_plan_of_today_only(self):
        self._plan_off()
        self.at(T0 + timedelta(days=1))
        self._closable_off()
        rec = workday.close(self.s, "desk", "done")
        self.assertEqual(rec["refs"], [], "yesterday's plan is not today's")
        self.assertNotIn("plan", rec["payload"])
        self.at(T0 + timedelta(days=2))
        plan, _ = self._plan_off()
        self._closable_off()
        rec = workday.close(self.s, "desk", "done")  # day end is not required with workday mode off
        self.assertEqual(rec["refs"], [plan["id"]])

    def test_plan_with_workday_off_replaces_an_unapproved_or_old_plan(self):
        first, _ = self._plan_off(approve=False)
        second, _ = self._plan_off(approve=False)
        self.assertEqual((second["payload"]["supersedes"], second["refs"]), (first["id"], [first["id"]]))
        workday.approve(self.s, "desk", "승인")
        with self.assertRaisesRegex(DurehError, "approved and open; write its end report"):
            self._plan_off(approve=False)
        self.at(T0 + timedelta(days=1))
        third, _ = self._plan_off(approve=False)
        self.assertEqual(third["payload"]["supersedes"], second["id"])

    def test_plan_approved_with_workday_off_needs_a_clocked_approval_once_switched_on(self):
        plan, _ = self._plan_off()
        workday.switch(self.s, True, "trial")
        d = workday.day(self.s)
        self.assertEqual((d["phase"], d["plan"]), ("planned", plan["id"]))
        self.assertTrue(denied(self.spawn()), "the gate holds until the clock starts")
        appr = workday.approve(self.s, "desk", "승인")
        self.assertEqual((appr["payload"]["start"], appr["payload"]["end"]),
                         ("2026-10-03T09:00:00-04:00", "2026-10-03T17:00:00-04:00"))
        self.assertNotIn("workday", appr["payload"])
        self.assertEqual(workday.day(self.s)["phase"], "on-duty")
        self.assertIsNone(self.spawn())

    def test_plan_with_workday_on_is_unchanged(self):
        self.start_day()
        plan = [e for e in Ledger(self.s).events() if e["type"] == "day.planned"][-1]
        appr = [e for e in Ledger(self.s).events() if e["type"] == "day.approved"][-1]
        self.assertNotIn("workday", plan["payload"])
        self.assertNotIn("workday", appr["payload"])
        self.assertIn("start", appr["payload"])
        with self.assertRaisesRegex(DurehError, "still open; write its end report first"):
            workday.plan(self.s, "desk", "again", "1. W")

    # ------------------------------------------------------------ day approve --by (release 71)
    def test_approve_by_dureh_with_workday_on(self):
        workday.switch(self.s, True, "trial")
        workday.plan(self.s, "desk", "finish W", "1. W")
        with self.assertRaisesRegex(DurehError, "--by must be one of Director, DUREH"):
            workday.approve(self.s, "desk", "ok", by="desk")
        with self.assertRaisesRegex(DurehError, "the Director's own"):
            workday.approve(self.s, "desk", "ok", via="slack", by="DUREH")
        self.assertEqual(workday.day(self.s)["phase"], "planned", "a refused approval records nothing")
        rc, out = self.cli("day", "approve", "--by", "DUREH", "--memo", "layer 2: plan within the daily share")
        self.assertEqual(rc, 0)
        appr = [e for e in Ledger(self.s).events() if e["type"] == "day.approved"][-1]
        self.assertEqual(appr["payload"]["by"], "DUREH")
        self.assertEqual(sorted(appr["payload"]), ["by", "end", "memo", "session", "start"])
        d = workday.day(self.s)
        self.assertEqual((d["phase"], d["approved_by"]), ("on-duty", "DUREH"))
        self.assertIsNone(self.spawn(), "a layer-2 approval opens the day like the Director's")
        rc, out = self.cli("day", "status")
        self.assertIn(f"approved {appr['id']} by DUREH", out)

    def test_approve_by_dureh_with_workday_off(self):
        workday.plan(self.s, "desk", "finish W", "1. W", [self.wi])
        appr = workday.approve(self.s, "desk", "layer 2", by="DUREH")
        self.assertEqual((appr["payload"]["by"], appr["payload"]["workday"]), ("DUREH", False))
        d = workday.day(self.s)
        self.assertEqual((d["phase"], d["approved_by"]), ("on-duty", "DUREH"))
        self.assertEqual(workday.status(self.s)["day"]["approved_by"], "DUREH")
        text = render_status(status(self.s))
        self.assertIn(f"approved {appr['id']} by DUREH", text)

    def test_approve_default_is_the_director(self):
        self.start_day()
        appr = [e for e in Ledger(self.s).events() if e["type"] == "day.approved"][-1]
        self.assertEqual(appr["payload"]["by"], "Director")
        rc, out = self.cli("day", "status")
        self.assertIn(f"approved {appr['id']} by Director", out)
        with self.assertRaises(SystemExit):
            import contextlib, io
            with contextlib.redirect_stderr(io.StringIO()):
                self.cli("day", "approve")  # --memo is still required

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
        self.assertIn("day plan --addendum --file", text)  # release 73
        self.assertIn("day approve --addendum --by Director|DUREH", text)
        self.assertIn("stay on duty", text)
        self.assertIn("day close", text)
        self.assertIn("with workday mode off it has no end report to wait for", text)  # release 67
        self.assertIn("adapter sync --harness <release checkout>", text)
        # finish the plan (Director, 2026-10-06)
        self.assertIn("finished the same day", text)
        self.assertIn("never move a plan item to tomorrow", text)
        self.assertIn("records the overtime itself", text)
        self.assertIn("wi new --fix-for", text)
        self.assertIn("Write the end report when the plan is done", text)
        # overtime pre-approval (UR-20261006-01) and the overtime plan (Director, 2026-10-06)
        self.assertIn("may be recorded while on-duty as soon as the Director approves", text)
        self.assertIn("day overtime-plan --file <plan>", text)
        # the job report at day end; release 46: the generic loop text (a package adds its own rule's words)
        self.assertIn("jobs today: N runs, M tasks, K failed", text)
        self.assertIn("Loop with one array per step", text)
        self.assertNotIn("file level", text)

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

    def plan_overtime(self, text="overtime: finish the open items in order, 1 spawn each"):
        return workday.overtime_plan(self.s, "desk", text)

    # ------------------------------------------------------------ overtime pre-approval (UR-20261006-01) and plan
    def test_overtime_pre_approved_while_on_duty(self):
        self.start_day(f"1. {self.wi}")
        other, third = self.other_wi(), self.other_wi("Y")
        self.at(T0 + timedelta(hours=6))
        self.assertIsNone(self.spawn())  # the plan item, during the day
        rec = workday.overtime(self.s, "desk", [other], "X도 오늘 끝내세요")
        p = rec["payload"]
        self.assertEqual((p["items"], p["memo"], p["by"], p["pre_approved"], p["phase"], p["end"], p["at"]),
                         ([other], "X도 오늘 끝내세요", "Director", True, "on-duty", (T0 + timedelta(hours=8)).isoformat(),
                          (T0 + timedelta(hours=6)).isoformat()))
        self.assertNotIn("rule", p)
        st = workday.status(self.s)
        self.assertEqual((st["phase"], st["overtime"], st["overtime_started"], st["overtime_plan"]),
                         ("on-duty", [other], True, None))
        self.assertNotIn("overtime_plan_missing", st, "no nagging before the end time")
        self.assertIn(f"; overtime: 1 item ({other} (X)), plan missing", render_status(status(self.s)))
        # the shared predicate's reasons
        fix = wf.wi_new(self.s, "desk", "fix X", "g", "pm-modules", "modules/demo", ["ok"], fix_for=other)["id"]
        wf.wi_approve(self.s, "desk", fix, "ok")
        pfix = wf.wi_new(self.s, "desk", "fix W", "g", "pm-modules", "modules/demo", ["ok"], fix_for=self.wi)["id"]
        wf.wi_approve(self.s, "desk", pfix, "ok")
        self.assertEqual(workday.allowed_past_end(self.s, other), "Director overtime (X도 오늘 끝내세요)")
        self.assertEqual(workday.allowed_past_end(self.s, fix), f"Director overtime (X도 오늘 끝내세요, as a fix for {other})")
        self.assertEqual(workday.allowed_past_end(self.s, self.wi), "plan item")
        self.assertEqual(workday.allowed_past_end(self.s, pfix), f"fix for {self.wi}")
        self.assertIsNone(workday.allowed_past_end(self.s, third))
        self.assertIsNone(workday.allowed_past_end(self.s, None))
        self.assertEqual(workday.past_end_rule(self.s, other)[0], "director-overtime")
        self.assertEqual(workday.past_end_rule(self.s, pfix)[0], "finish-plan")
        # past the end time: the pre-approved item and its fix pass the gate; no automatic record is added for them
        self.at(T0 + timedelta(hours=8, minutes=1))
        self.assertEqual(workday.status(self.s)["phase"], "past-end")
        self.assertIsNone(workday.gate(self.s, other))
        self.assertIsNone(workday.gate(self.s, fix))
        self.assertIsNone(self.spawn(other))
        self.assertEqual(len(self.overtime_events()), 1, "the Director's record stands alone")
        # the first overtime unit is in flight; the next waits for the overtime plan
        self.assertEqual(workday.gate(self.s, fix), "workday mode: " + workday.PLAN_MISSING)
        self.assertTrue(workday.status(self.s)["overtime_plan_missing"])
        workday.overtime_plan(self.s, "desk", f"1. {other} then {fix}")
        self.assertIsNone(workday.gate(self.s, fix))
        # a WI neither in the plan nor approved: the refusal names the way out with the exact command
        why = workday.gate(self.s, third)
        self.assertIn("only the plan's open items", why)
        self.assertIn(f"{third} is not in today's plan and has no overtime record: either record the Director's approval "
                      f"with `dureh day overtime --items {third} --memo \"<Director's words>\"` (allowed while on-duty) or "
                      "add it to the plan's bundle (`dureh wi new --fix-for <plan WI>` or `--parent <plan WI>`).", why)
        self.assertNotIn("shorten --time", why)
        self.assertTrue(denied(self.spawn(third)))
        # a second `day overtime` appends; accepted while finishing too
        rec2 = workday.overtime(self.s, "desk", [third], "Y도")
        self.assertEqual((rec2["payload"]["pre_approved"], rec2["payload"]["phase"]), (True, "past-end"))
        self.assertEqual(workday.status(self.s)["overtime"], sorted([other, third]))
        self.assertIsNone(workday.gate(self.s, third))
        self.assertIn(f"; overtime: 2 items ({other} (X), {third} (Y)), plan recorded 17:01",
                      render_status(status(self.s)))
        # the end report lists the Director's items with their outcomes and the overtime plan
        self.verify(self.wi)
        rec = workday.end(self.s, "desk", "done", "", [], early="fix W는 내일")  # pfix stays open: the Director ends it
        past = rec["payload"]["past_end"]
        self.assertEqual(([x["wi"] for x in past["items"]], past["early"]), ([pfix], "fix W는 내일"))
        self.assertEqual([(x["wi"], x["outcome"], x["finished"]) for x in past["director"]],
                         [(other, "dispatched", False), (third, "approved", False)])
        self.assertEqual(past["director_memos"], {other: "X도 오늘 끝내세요", third: "Y도"})
        self.assertEqual(past["overtime_plan"]["plan"], f"1. {other} then {fix}")
        self.assertEqual(workday.status(self.s)["phase"], "overtime")

    def test_overtime_plan_with_the_record_and_by_its_own_command(self):
        self.start_day(f"1. {self.wi}")
        other = self.other_wi()
        with self.assertRaisesRegex(DurehError, "no overtime started"):
            workday.overtime_plan(self.s, "desk", "nothing yet")
        self.at(T0 + timedelta(hours=7))
        f1 = self.tmp / "ot1.md"
        f1.write_text(f"| {other} | 1 spawn | ~1 unit |\n")
        rc, out = self.cli("day", "overtime", "--items", other, "--memo", "X도 오늘", "--plan", str(f1), "--goal", "X done")
        self.assertEqual(rc, 0, out)
        st = workday.status(self.s)
        self.assertEqual((st["overtime_plan"]["plan"], st["overtime_plan"]["goal"], st["overtime_plan"]["at"][11:16]),
                         (f1.read_text(), "X done", "16:00"))
        self.assertIn(f"; overtime: 1 item ({other} (X)), plan recorded 16:00", render_status(status(self.s)))
        self.at(T0 + timedelta(hours=8, minutes=1))
        self.assertIsNone(self.spawn(other))
        self.assertIsNone(workday.gate(self.s, other), "the plan came with the approval: nothing waits")
        f2 = self.tmp / "ot2.md"
        f2.write_text(f"| {other} | 2 spawns | ~2 units |\n")
        rc, out = self.cli("day", "overtime-plan", "--file", str(f2), "--usage", "2 units")
        self.assertEqual(rc, 0, out)
        st = workday.status(self.s)
        self.assertEqual((st["overtime_plan"]["plan"], st["overtime_plan"]["items"]), (f2.read_text(), [other]))
        plans = [e for e in Ledger(self.s).events() if e["type"] == "day.overtime-plan"]
        self.assertEqual((len(plans), plans[0]["payload"]["expected_usage"], plans[0]["payload"]["updates"]),
                         (1, "2 units", self.overtime_events()[0]["id"]))
        rc, out = self.cli("day", "status")
        self.assertIn("plan recorded 17:01", out)

    def test_finish_plan_continues_plan_items_after_the_end_time(self):
        # production, blocking clock; the plan names W (by id, as real plans do); X is outside the plan
        self.start_day(f"| 1 | {self.wi} build W | pm-modules | 2 | 3 |")
        other = self.other_wi()
        self.assertEqual(workday.status(self.s)["plan_items"], [self.wi])
        self.at(T0 + timedelta(hours=8, minutes=1))
        st = workday.status(self.s)
        self.assertEqual((st["phase"], st["finishing"], st["open"]), ("past-end", True, [self.wi]))
        self.assertEqual(st["reason"], f"end time passed 17:00; finishing the plan (1 item open: {self.wi})")
        self.assertIn(f"Workday: past-end (09:00-17:00); end time passed 17:00; finishing the plan (1 item open: {self.wi} (W))",
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
        # overtime plan (Director, 2026-10-06): the first overtime work unit is in flight; the next waits for the plan
        self.assertEqual(workday.gate(self.s, self.wi), "workday mode: " + workday.PLAN_MISSING)
        st = workday.status(self.s)
        self.assertTrue(st["overtime_plan_missing"])
        self.assertIn("overtime plan missing", st["warning"])
        self.assertIn(f"; overtime: 1 item ({self.wi} (W)), plan missing", render_status(status(self.s)))
        with self.assertRaisesRegex(DurehError, "required"):
            workday.overtime_plan(self.s, "desk", " ")
        self.at(T0 + timedelta(hours=8, minutes=5))
        plan_rec = workday.overtime_plan(self.s, "desk", f"1. {self.wi} rerun verify (1 spawn, ~1 unit)", goal="finish W",
                                         usage="1 unit")
        pp = plan_rec["payload"]
        self.assertEqual((pp["items"], pp["goal"], pp["expected_usage"], pp["updates"], pp["at"][11:16]),
                         ([self.wi], "finish W", "1 unit", None, "17:05"))
        self.assertEqual(plan_rec["refs"], [st["plan"]])
        self.assertIsNone(workday.gate(self.s, self.wi))  # (a second dispatch_brief waits for the start grace)
        self.assertIsNone(workday.gate(self.s, self.wi, checking=True))
        self.assertEqual(len(self.overtime_events()), 1, "recorded once")
        st = workday.status(self.s)
        self.assertNotIn("overtime_plan_missing", st)
        self.assertEqual(st["overtime_plan"]["record"], plan_rec["id"])
        self.assertIn(f"; overtime: 1 item ({self.wi} (W)), plan recorded 17:05", render_status(status(self.s)))
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
        self.assertEqual(past["director"], [{"wi": other, "title": "X", "outcome": "dispatched", "finished": False}])
        self.assertEqual((past["director_memos"], past["overtime_plan"]["record"], past["overtime_plan"]["goal"]),
                         ({other: "X도 오늘"}, plan_rec["id"], "finish W"))
        self.assertEqual(workday.status(self.s)["phase"], "overtime", "the Director's own item still stands")
        self.assertEqual(workday.status(self.s)["overtime"], [other])
        self.write_handoff()
        workday.close(self.s, "desk", "done")
        self.assertEqual(workday.status(self.s)["phase"], "off-duty")

    def test_finish_plan_admits_a_fix_for_a_plan_item(self):
        self.start_day(f"1. {self.wi}")
        self.at(T0 + timedelta(hours=8, minutes=1))
        self.assertIsNone(self.spawn())
        self.plan_overtime()
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
        self.plan_overtime()
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

    def test_overtime_plan_in_the_development_phase_once_the_end_time_passed(self):
        """Release 59: the clock never blocks in development, so nothing records the overtime by itself; the plan
        is allowed once the end time passed and the day is still open, and it opens the overtime portion."""
        self.workday_config()  # development
        self.start_day(f"1. {self.wi}")
        with self.assertRaisesRegex(DurehError, "no overtime started"):
            workday.overtime_plan(self.s, "desk", "too early")  # before the end time, as in production
        self.at(T0 + timedelta(hours=8, minutes=5))
        st = workday.status(self.s)
        self.assertEqual((st["phase"], st["hours_exceeded"], st["overtime_started"]), ("on-duty", True, False))
        self.assertNotIn("overtime:", render_status(status(self.s)))
        f = self.tmp / "ot-dev.md"
        f.write_text(f"1. {self.wi} 1 spawn\n")
        rc, out = self.cli("day", "overtime-plan", "--file", str(f), "--goal", "finish W")
        self.assertEqual(rc, 0, out)
        rec = [e for e in Ledger(self.s).events() if e["type"] == "day.overtime-plan"][-1]["payload"]
        self.assertEqual((rec["plan"], rec["goal"], rec["items"], rec["opened_by"]),
                         (f.read_text(), "finish W", [self.wi], "hours.exceeded"))
        st = workday.status(self.s)
        self.assertEqual((st["phase"], st["overtime_started"], st["overtime_plan"]["goal"]), ("on-duty", True, "finish W"))
        self.assertNotIn("overtime_plan_missing", st)
        rc, out = self.cli("day", "status")
        self.assertIn("overtime: 0 items, plan recorded 17:05", out)
        self.assertIsNone(self.spawn(), "still only a warning in development")
        self.assertIn(f"overtime: 1 item ({self.wi} (W)), plan recorded 17:05", render_status(status(self.s)))
        wf.dispatch_stop(self.s, "desk", self.wi, "enough")
        rec = workday.end(self.s, "desk", "W stopped", "", [])
        self.assertEqual(rec["payload"]["past_end"]["overtime_plan"]["plan"], f.read_text())

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

    # ------------------------------------------------------------ job report at day end (Director, 2026-10-06)
    def test_end_report_lists_every_job_of_the_day(self):
        led = Ledger(self.s)
        before = led.append("run", "slurm", "desk", self.wi, {"run_type": "slurm", "status": "submitted", "job_id": "4000",
                                                               "command": ["sbatch", "old.sh"], "script": "/x/old.sh"})
        self.start_day()
        self.at(T0 + timedelta(hours=2))
        arr = led.append("run", "slurm", "desk", self.wi, {
            "run_type": "slurm", "status": "submitted", "job_id": "4300", "level": "file", "logs": "/x/logs/WI",
            "command": ["sbatch", "--parsable", f"--job-name=dureh-{self.wi}", "--array=0-2", "/x/logs/WI/each-1.sbatch"],
            "script": "/x/step1.sh", "array": {"job_id": "4300", "items": ["a_bold", "b_bold", "c d_bold"], "count": 3,
                                                "level": "file"}})
        led.event("run.status", "desk", self.wi, {"run": arr["id"], "job_id": "4300", "scheduler_state": "FAILED",
                                                   "counts": {"COMPLETED": 2, "FAILED": 1}, "final": True}, [arr["id"]])
        single = led.append("run", "slurm", "desk", self.wi, {
            "run_type": "slurm", "status": "submitted", "job_id": "4301", "level": "single", "reason": "merge",
            "command": ["sbatch", "--parsable", "/x/merge.sh"], "script": "/x/merge.sh", "logs": "/x/logs/WI"})
        led.event("run.status", "desk", self.wi, {"run": single["id"], "job_id": "4301", "scheduler_state": "COMPLETED",
                                                   "raw": "COMPLETED|0:0|00:01:00"}, [single["id"]])
        led.event("run.status", "desk", self.wi, {"run": single["id"], "job_id": "4301", "scheduler_state": "CANCELLED by 1",
                                                   "raw": "CANCELLED by 1|0:0|00:01:00"}, [single["id"]])
        via = led.append("run", "slurm", "desk", self.wi, {
            "run_type": "slurm", "status": "submitted", "job_id": "5000", "level": "file", "logs": "/x/logs/WI",
            "command": ["exmod", "run", "pipe", "step", "--level", "file", "--submit", "--detach", "--record", "/x/r.json"],
            "via": {"text": "exmod run pipe step --level file --submit --detach", "command": ["exmod", "run", "pipe", "step"]},
            "jobs": [{"job_id": "5000", "kind": "array", "count": 2, "items": ["s1", "s2"], "level": "file"},
                     {"job_id": "5001", "kind": "single"}],
            "array": {"job_id": "5000", "items": ["s1", "s2"], "count": 2, "level": "file"}})
        verify = led.append("run", "verify", "desk", self.wi, {
            "run_type": "verify", "status": "submitted", "runner": "slurm", "job_id": "4302", "script": "/x/checks.sh",
            "command": "sbatch --parsable --time=00:30:00 /x/checks.sh"})
        led.append("run", "slurm", "desk", self.wi, {"run_type": "slurm", "status": "not-run", "command": ["sbatch", "dry.sh"]})
        led.append("run", "agent", "desk", self.wi, {"run_type": "agent", "status": "completed"})
        self.at(T0 + timedelta(hours=8, minutes=1))
        rec = workday.end(self.s, "desk", "ran the pipeline", "", [])
        jobs = rec["payload"]["jobs"]
        self.assertEqual((jobs["count"], jobs["tasks"], jobs["jobs"], jobs["failed"], jobs["line"]),
                         (4, 8, 5, 1, "jobs today: 4 runs, 8 tasks, 1 failed"))
        self.assertEqual([j["run"] for j in jobs["runs"]], [arr["id"], single["id"], via["id"], verify["id"]],
                         "runs before the plan, dry runs and agent runs are not jobs of the day")
        a, s, v, c = jobs["runs"]
        self.assertEqual((a["wi"], a["kind"], a["step"], a["level"], a["tasks"], a["jobs"], a["logs"]),
                         (self.wi, "slurm", "step1.sh", "file", 3, 1, "/x/logs/WI"))
        self.assertEqual(a["job_ids"], ["4300_[0-2]", "4300_0", "4300_1", "4300_2"])
        self.assertEqual(a["command"], f"sbatch --parsable --job-name=dureh-{self.wi} --array=0-2 /x/logs/WI/each-1.sbatch")
        self.assertEqual((a["state"], a["states"]), ("FAILED", {"completed": 2, "failed": 1, "cancelled": 0, "other": 0}))
        self.assertEqual((s["level"], s["reason"], s["job_ids"], s["state"], s["states"]),
                         ("single", "merge", ["4301"], "CANCELLED by 1", {"completed": 0, "failed": 0, "cancelled": 1, "other": 0}))
        self.assertEqual((v["kind"], v["step"], v["tasks"], v["jobs"], v["job_ids"], v["state"], v["states"]["other"]),
                         ("via", "exmod run pipe step --level file --submit --detach", 3, 2,
                          ["5000_[0-1]", "5000_0", "5000_1", "5001"], "no state recorded", 3))
        self.assertEqual(v["command"], "exmod run pipe step --level file --submit --detach --record /x/r.json")
        self.assertEqual((c["kind"], c["step"], c["command"], c["level"]),
                         ("verify", "checks.sh", "sbatch --parsable --time=00:30:00 /x/checks.sh", "single"))
        # the Slack end-report summary (first paragraph) carries the jobs line, once
        self.assertEqual(slack.jobs_line(self.s), "jobs today: 4 runs, 8 tasks, 1 failed")
        self.assertEqual(slack.end_report_summary(self.s, "오늘 요약 한 문단\n\n자세한 내용"),
                         "오늘 요약 한 문단\njobs today: 4 runs, 8 tasks, 1 failed")
        self.assertEqual(slack.end_report_summary(self.s, "요약 (jobs today: 4 runs, 8 tasks, 1 failed)\n\n본문"),
                         "요약 (jobs today: 4 runs, 8 tasks, 1 failed)")
        self.assertEqual(before["payload"]["job_id"], "4000")

    # ------------------------------------------------------------ day plan --addendum (release 73, #26)
    def addendum(self, text, *extra):
        f = self.tmp / "addendum.md"
        f.write_text(text)
        return self.cli("day", "plan", "--addendum", "--file", str(f), *extra)

    def test_addendum_appends_without_restarting_the_day(self):
        self.start_day(f"1. {self.wi}")
        before = workday.day(self.s)
        other = self.other_wi()
        self.at(T0 + timedelta(hours=1))
        rc, out = self.addendum(f"1. {other} (~6 units)", "--goal", "more")
        self.assertEqual(rc, 0, out)
        rec = [e for e in Ledger(self.s).events() if e["type"] == "day.planned"][-1]
        p = rec["payload"]
        self.assertEqual((p["addendum"], p["addendum_to"], p["goal"], rec["refs"]), (True, before["plan"], "more",
                                                                                      [before["plan"]]))
        self.assertEqual(workday.plan_items(rec), [other])
        d = workday.day(self.s)
        self.assertEqual((d["plan"], d["approval"], d["start"], d["end"], d["phase"]),
                         (before["plan"], before["approval"], before["start"], before["end"], "on-duty"))
        self.assertEqual([(a["record"], a["items"], a["approval"]) for a in d["addenda"]], [(rec["id"], [other], None)])
        self.assertEqual(d["plan_items"], [self.wi], "an unapproved addendum does not count yet")
        rc, out = self.cli("day", "status")
        self.assertIn(f"Addendum {rec['id']} (10:00): more: {other}", out)
        self.assertIn("waits for approval", out)
        rc, out = self.cli("day", "approve", "--addendum", "--by", "DUREH", "--memo", "layer 2: addendum ok")
        self.assertEqual(rc, 0, out)
        appr = [e for e in Ledger(self.s).events() if e["type"] == "day.approved"][-1]
        self.assertEqual((appr["payload"]["addendum"], appr["payload"]["by"], appr["refs"]),
                         (rec["id"], "DUREH", [rec["id"], before["plan"]]))
        d = workday.day(self.s)
        self.assertEqual((d["approval"], d["approved_by"], d["start"]), (before["approval"], "Director", before["start"]))
        self.assertEqual(d["plan_items"], sorted([self.wi, other]))
        self.assertEqual(d["addenda"][0]["approved_by"], "DUREH")
        rc, out = self.cli("day", "status")
        self.assertIn(f"approved {appr['id']} by DUREH", out)
        self.assertIn(f"approved {before['approval']} by Director", out)
        with self.assertRaisesRegex(DurehError, "already approved"):
            workday.approve(self.s, "desk", "again", addendum=True)
        with self.assertRaisesRegex(DurehError, "still open; write its end report first"):
            workday.plan(self.s, "desk", "again", "1. W")  # a second plan is refused as before

    def test_addendum_needs_an_approved_open_plan(self):
        with self.assertRaisesRegex(DurehError, "approved and open"):
            workday.plan(self.s, "desk", "", "1. X", addendum=True)
        workday.switch(self.s, True, "trial")
        workday.plan(self.s, "desk", "finish W", "1. W")
        with self.assertRaisesRegex(DurehError, "approved and open"):
            workday.plan(self.s, "desk", "", "1. X", addendum=True)
        with self.assertRaisesRegex(DurehError, "no addendum to approve"):
            workday.approve(self.s, "desk", "ok", addendum=True)
        workday.approve(self.s, "desk", "승인")
        with self.assertRaisesRegex(DurehError, "no addendum to approve"):
            workday.approve(self.s, "desk", "ok", addendum=True)
        with self.assertRaisesRegex(DurehError, "plan text"):
            workday.plan(self.s, "desk", "", "  ", addendum=True)
        with self.assertRaises(DurehError):
            workday.plan(self.s, "desk", "", "1. X", ["WI-9999"], addendum=True)
        with self.assertRaisesRegex(DurehError, "not via slack"):
            workday.approve(self.s, "desk", "ok", via="slack", addendum=True)
        self.at(T0 + timedelta(hours=8, minutes=1))
        workday.end(self.s, "desk", "x", "", [])
        with self.assertRaisesRegex(DurehError, "approved and open"):
            workday.plan(self.s, "desk", "", "1. X", addendum=True)
        rc, out = self.cli("day", "plan", "--file", str(self.write_plan("1. W")))
        self.assertNotEqual(rc, 0, "a day plan without --addendum still needs --goal")

    def test_addendum_items_follow_the_finish_plan_rule(self):
        self.start_day(f"1. {self.wi}")
        other, third = self.other_wi(), self.other_wi("Y")
        workday.plan(self.s, "desk", "", f"1. {other}", addendum=True)
        workday.approve(self.s, "desk", "추가 승인", addendum=True)
        workday.plan(self.s, "desk", "", f"1. {third}", addendum=True)  # never approved
        self.at(T0 + timedelta(hours=8, minutes=1))
        self.assertEqual(workday.allowed_past_end(self.s, other), "plan item")
        self.assertIsNone(workday.allowed_past_end(self.s, third))
        st = workday.status(self.s)
        self.assertTrue(st["finishing"])
        self.assertEqual(sorted(st["open"]), sorted([self.wi, other]))
        self.assertEqual(len(st["addenda"]), 2)

    def test_end_report_lists_addenda(self):
        self.start_day(f"1. {self.wi}")
        other = self.other_wi()
        add = workday.plan(self.s, "desk", "more", f"1. {other}", addendum=True)
        appr = workday.approve(self.s, "desk", "ok", by="DUREH", addendum=True)
        self.at(T0 + timedelta(hours=8, minutes=1))
        rec = workday.end(self.s, "desk", "x", "", [], early="그만")
        a = rec["payload"]["addenda"]
        self.assertEqual([(x["record"], x["goal"], x["items"], x["approval"], x["approved_by"]) for x in a],
                         [(add["id"], "more", [other], appr["id"], "DUREH")])
        self.assertIn(other, rec["payload"]["titles"])

    def test_addendum_with_workday_off(self):
        workday.plan(self.s, "desk", "finish W", "1. W", [self.wi])
        workday.approve(self.s, "desk", "layer 2", by="DUREH")
        other = self.other_wi()
        rc, out = self.addendum(f"1. {other}", "--wi", other)
        self.assertEqual(rc, 0, out)
        add = [e for e in Ledger(self.s).events() if e["type"] == "day.planned"][-1]
        self.assertEqual((add["payload"]["workday"], add["payload"]["items"]), (False, [other]))
        appr = workday.approve(self.s, "desk", "ok", by="DUREH", addendum=True)
        self.assertFalse(appr["payload"]["workday"])
        d = workday.day(self.s)
        self.assertEqual((d["phase"], d["plan_items"]), ("on-duty", sorted([self.wi, other])))
        text = render_status(status(self.s))
        self.assertIn(f"Addendum {add['id']}", text)
        self.assertEqual(workday.status(self.s)["day"]["addenda"][0]["approval"], appr["id"])

    def write_plan(self, text):
        p = self.tmp / "plan.md"
        p.write_text(text)
        return p


if __name__ == "__main__":
    unittest.main()
