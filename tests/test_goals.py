"""Release 98 (dureh-team#161): the goal mechanism. A goal keeps the Director's words verbatim with criteria that name
their deliverables; work items link to it and Gate 1 refuses an uncovered criterion; a goal closes only with evidence
for every deliverable and the Director's acceptance (fail-closed); status and the day plan raise a site without an
open goal; `goal check` shows criteria against evidence, read-only."""
import contextlib
import io
import json
import os

from helpers import SiteCase

from dureh_core import goals
from dureh_core import workday
from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.ops import render_status, status
from dureh_core.records import Ledger
from dureh_core.util import DurehError

WORDS = "Run the pipeline end to end and   report it,\nwith several methods compared.\n"
SOURCE = "https://github.com/example-org/office/issues/7"
CRITERIA = ["every method has run => run summary",
            "the methods are compared => comparison document; summary figure"]
DECIDED = "github.decision.changed"


class GoalCase(SiteCase):
    def setUp(self):
        super().setUp()
        os.environ.pop("CLAUDECODE", None)
        os.environ.pop("DUREH_AGENT_ID", None)
        os.environ["DUREH_USAGE_CACHE"] = str(self.tmp / "usage-cache.json")
        self.install()
        self.make_module()
        (self.tmp / "transcripts").mkdir()
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["workday"] = {"transcripts": [str(self.tmp / "transcripts")]}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        self.led = Ledger(self.s)

    def goal(self, source=SOURCE, criteria=None) -> str:
        return goals.new(self.s, "desk", WORDS, criteria or CRITERIA, source, "Director")["payload"]["goal"]

    def wi(self, acceptance, gid=None, **kw) -> str:
        return wf.wi_new(self.s, "desk", "t", "g", "doc-writer", "modules/demo", acceptance, goal_id=gid, **kw)["id"]

    def cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["--site", str(self.site_root), *argv])
        finally:
            os.environ.pop("DUREH_OBSERVE", None)  # `--observe` sets it for the process
        return rc, out.getvalue(), err.getvalue()

    def decide(self, to="Approve 1", url=SOURCE) -> dict:
        return self.led.event(DECIDED, "desk", None, {"item_id": "x", "url": url, "from": None, "to": to})

    def write(self, rel, data=b"data") -> str:
        p = self.site_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return rel


class GoalRecord(GoalCase):
    def test_verbatim_text_criteria_and_ids(self):
        g1 = self.goal()
        g2 = self.goal(source="example-org/office#8")
        self.assertEqual((g1, g2), ("G-0001", "G-0002"))
        g = goals.get_goal(self.led, g1)
        self.assertEqual(g["text"], WORDS)  # stored exactly, whitespace and newline included
        self.assertEqual(g["criteria"][1], {"id": "C2", "text": "the methods are compared",
                                            "deliverables": ["comparison document", "summary figure"]})
        self.assertEqual((g["source"], g["by"], g["state"]), (SOURCE, "Director", "open"))

    def test_refusals(self):
        for crit, msg in ((["no deliverable"], "names its deliverables"), (["x => "], "names its deliverables"),
                          (["x => a; a"], "twice"), ([], "at least one")):
            with self.assertRaisesRegex(DurehError, msg):
                goals.new(self.s, "desk", WORDS, crit, SOURCE, "Director")
        with self.assertRaisesRegex(DurehError, "--source"):
            goals.new(self.s, "desk", WORDS, CRITERIA, "somewhere", "Director")
        with self.assertRaisesRegex(DurehError, "--by is required"):
            goals.new(self.s, "desk", WORDS, CRITERIA, SOURCE, None)
        with self.assertRaisesRegex(DurehError, "--by must be one of"):
            goals.new(self.s, "desk", WORDS, CRITERIA, SOURCE, "desk")
        with self.assertRaisesRegex(DurehError, "verbatim"):
            goals.new(self.s, "desk", "  ", CRITERIA, SOURCE, "Director")
        with self.assertRaisesRegex(DurehError, "only the desk"):
            goals.new(self.s, "doc-writer", WORDS, CRITERIA, SOURCE, "Director")
        self.goal()
        with self.assertRaisesRegex(DurehError, "still open"):
            self.goal()

    def test_cli_new_list_show(self):
        words = self.tmp / "words.txt"
        words.write_text(WORDS)
        rc, out, _ = self.cli("goal", "new", "--text-file", str(words), "--criterion", CRITERIA[0],
                              "--criterion", CRITERIA[1], "--source", SOURCE, "--by", "DUREH", "--title", "end to end")
        self.assertEqual(rc, 0)
        self.assertIn("G-0001 recorded (open): end to end", out)
        self.assertIn("C2 the methods are compared => comparison document; summary figure", out)
        self.assertEqual(goals.get_goal(self.led, "G-0001")["text"], WORDS)
        rc, out, _ = self.cli("--observe", "goal", "list")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^G-0001  open")
        rc, out, _ = self.cli("--observe", "goal", "show", "--goal", "G-0001")
        self.assertEqual(json.loads(out)["by"], "DUREH")
        rc, _, err = self.cli("--observe", "goal", "new", "--text", "x", "--criterion", "a => b", "--source", "#1",
                              "--by", "Director")
        self.assertEqual(rc, 2)
        self.assertIn("observe mode", err)

    def test_goal_events_and_decisions_are_reserved(self):
        gid = self.goal()
        rc, _, err = self.cli("ledger", "add", "event", "--type", "goal.closed", "--payload", json.dumps({"goal": gid}))
        self.assertEqual(rc, 2)
        self.assertIn("goal.closed events are written only", err)
        rc, _, err = self.cli("ledger", "add", "decision", "--type", "goal-acceptance", "--summary", "s",
                              "--disposition", "approve", "--by", "Director")
        self.assertEqual(rc, 2)
        self.assertIn("written only by `dureh goal close`", err)


class GoalLink(GoalCase):
    def test_wi_new_links_and_tags(self):
        gid = self.goal()
        wi = self.wi(["[C1] the run summary is written"], gid)
        self.assertEqual(self.led.get("work-item", wi)["payload"]["goal_id"], gid)
        with self.assertRaisesRegex(DurehError, "unknown goal G-0009"):
            self.wi(["ok"], "G-0009")
        with self.assertRaisesRegex(DurehError, "tags C7"):
            self.wi(["[C7] something"], gid)
        with self.assertRaisesRegex(DurehError, "not both"):
            self.wi(["ok"], gid, no_goal="x")
        with self.assertRaisesRegex(DurehError, "needs the reason"):
            self.wi(["ok"], no_goal=" ")
        legacy = self.wi(["ok"])  # a library caller naming neither: a legacy item
        self.assertNotIn("goal_id", self.led.get("work-item", legacy)["payload"])
        fix = self.wi(["ok"], fix_for=wi)  # inherits the goal of the item it fixes
        self.assertEqual(self.led.get("work-item", fix)["payload"]["goal_id"], gid)
        none = self.wi(["ok"], no_goal="urgent repair")
        self.assertEqual(self.led.get("work-item", none)["payload"]["no_goal"], "urgent repair")

    def test_cli_requires_a_goal_choice(self):
        base = ["wi", "new", "--title", "t", "--goal", "g", "--role", "doc-writer", "--target", "modules/demo"]
        rc, _, err = self.cli(*base, "--acceptance", "ok")
        self.assertEqual(rc, 2)
        self.assertIn("names the goal it serves", err)
        rc, out, _ = self.cli(*base, "--acceptance", "ok", "--no-goal", "housekeeping")
        self.assertEqual(rc, 0)
        self.assertIn("no goal: housekeeping", out)
        gid = self.goal()
        rc, out, _ = self.cli(*base, "--acceptance", "[C1] summary", "--goal-id", gid)
        self.assertEqual(rc, 0)
        self.assertIn(f"goal {gid}", out)

    def test_gate1_refuses_an_uncovered_criterion(self):
        gid = self.goal()
        a = self.wi(["[C1] the run summary is written", "runs complete"], gid)
        n = len(self.led.load("decision"))
        with self.assertRaises(DurehError) as cm:
            wf.wi_approve(self.s, "desk", a, "ok", by="Director")
        msg = str(cm.exception)
        self.assertIn("criteria C2 of goal G-0001 are covered by no work item", msg)
        self.assertIn("C2 the methods are compared (deliverables: comparison document; summary figure) -> NOT COVERED",
                      msg)
        self.assertIn(f"C1 every method has run (deliverables: run summary) -> {a} (t) [proposed] (this item)", msg)
        self.assertEqual(len(self.led.load("decision")), n)  # nothing recorded
        self.assertEqual(self.led.wi_status(a)["status"], "proposed")
        b = self.wi(["[C2] the comparison document and its summary figure"], gid)
        ev = wf.wi_approve(self.s, "desk", a, "ok", by="Director")
        self.assertEqual(ev["payload"]["goal"], gid)
        dec = self.led.get("decision", ev["payload"]["decision"])
        self.assertEqual(dec["payload"]["binding"], {"goal": gid, "coverage": {"C1": [a], "C2": [b]}})

    def test_stopped_item_covers_nothing(self):
        gid = self.goal()
        a = self.wi(["[C1] summary"], gid)
        b = self.wi(["[C2] comparison"], gid)
        self.led.event("wi.stopped", "desk", b, {"reason": "dropped"})
        with self.assertRaisesRegex(DurehError, "criteria C2"):
            wf.wi_approve(self.s, "desk", a, "ok", by="Director")

    def test_cli_approve_prints_the_table_and_legacy_warns(self):
        gid = self.goal(criteria=["it is written => document"])
        a = self.wi(["[C1] the document"], gid)
        rc, out, _ = self.cli("wi", "approve", "--wi", a, "--by", "Director", "--memo", "ok")
        self.assertEqual(rc, 0)
        self.assertIn(f"Goal {gid} (", out)
        self.assertIn(f"C1 it is written (deliverables: document) -> {a} (t) [approved] (this item)", out)
        legacy = self.wi(["ok"])
        rc, out, _ = self.cli("wi", "approve", "--wi", legacy, "--by", "desk", "--memo", "ok")
        self.assertEqual(rc, 0)
        self.assertIn(f"Warning: {legacy} names no goal", out)
        self.assertTrue(self.led.events(legacy)[-1]["payload"]["legacy_no_goal"])

    def test_amend_links_a_legacy_item(self):
        gid = self.goal(criteria=["it is written => document"])
        legacy = self.wi(["ok"])
        with self.assertRaisesRegex(DurehError, "tags C3"):
            wf.wi_amend(self.s, "desk", legacy, "link", {"goal_id": gid, "acceptance": ["[C3] x"]}, by="Director")
        wf.wi_amend(self.s, "desk", legacy, "link", {"goal_id": gid, "acceptance": ["[C1] the document"]}, by="Director")
        self.assertEqual(self.led.get("work-item", legacy)["payload"]["goal_id"], gid)
        other = self.goal(source="#9", criteria=["x => y"])
        with self.assertRaisesRegex(DurehError, "already serves goal"):
            wf.wi_amend(self.s, "desk", legacy, "move", {"goal_id": other}, by="Director")
        with self.assertRaisesRegex(DurehError, "tags C2"):
            wf.wi_amend(self.s, "desk", legacy, "x", {"acceptance": ["[C2] x"]}, by="Director")


class GoalClose(GoalCase):
    def setUp(self):
        super().setUp()
        self.gid = self.goal()
        self.a = self.wi(["[C1] summary", "[C2] comparison and figure"], self.gid)

    def evidence(self):
        goals.add_evidence(self.s, "desk", self.gid, "C1", "path", self.write("notes/out/summary.md"))
        goals.add_evidence(self.s, "desk", self.gid, "C2", "path", self.write("notes/out/compare.md"), "1")
        goals.add_evidence(self.s, "desk", self.gid, "C2", "figure", self.write("notes/out/fig1.png"),
                           "summary figure")

    def finish_items(self):
        self.led.event("wi.stopped", "desk", self.a, {"reason": "test: no work needed"})

    def test_evidence_rules(self):
        with self.assertRaisesRegex(DurehError, "inside the site"):
            goals.add_evidence(self.s, "desk", self.gid, "C1", "path", str(self.tmp / "package"))
        with self.assertRaisesRegex(DurehError, "does not exist"):
            goals.add_evidence(self.s, "desk", self.gid, "C1", "path", "notes/none.md")
        with self.assertRaisesRegex(DurehError, "say which with --deliverable"):
            goals.add_evidence(self.s, "desk", self.gid, "C2", "path", self.write("notes/x.md"))
        with self.assertRaisesRegex(DurehError, "no deliverable 'poster'"):
            goals.add_evidence(self.s, "desk", self.gid, "C2", "path", "notes/x.md", "poster")
        with self.assertRaisesRegex(DurehError, "image or PDF"):
            goals.add_evidence(self.s, "desk", self.gid, "C2", "figure", "notes/x.md", "summary figure")
        with self.assertRaisesRegex(DurehError, "https URL"):
            goals.add_evidence(self.s, "desk", self.gid, "C1", "link", "http://x")
        with self.assertRaisesRegex(DurehError, "no criterion 'C9'"):
            goals.add_evidence(self.s, "desk", self.gid, "C9", "link", "https://x")
        with self.assertRaisesRegex(DurehError, "only the desk"):
            goals.add_evidence(self.s, "doc-writer", self.gid, "C1", "link", "https://x")
        self.write("notes/dir/a.txt")
        ev = goals.add_evidence(self.s, "desk", self.gid, "C1", "path", "notes/dir")
        self.assertEqual(ev["payload"]["fingerprint"]["type"], "dir")

    def test_check_shows_missing_changed_and_open_items(self):
        r = goals.check(self.s, self.gid)
        self.assertFalse(r["ready"])
        self.assertIn("C1 deliverable 'run summary': no evidence recorded", r["problems"])
        self.assertIn(f"{self.a} (t) is still proposed", r["problems"])
        self.evidence()
        self.finish_items()
        self.assertTrue(goals.check(self.s, self.gid)["ready"])
        self.write("notes/out/summary.md", b"changed")
        r = goals.check(self.s, self.gid)
        self.assertEqual(r["problems"], ["C1 deliverable 'run summary': evidence notes/out/summary.md changed"
                                         " (record it again)"])
        text = goals.render_check(r)
        self.assertIn("[ ] run summary: path notes/out/summary.md changed", text)
        self.assertIn("[x] summary figure: figure notes/out/fig1.png ok", text)
        self.assertIn("Check: NOT READY (1 problem)", text)
        rc, out, _ = self.cli("--observe", "goal", "check", "--goal", self.gid)
        self.assertEqual(rc, 1)
        self.assertIn("Check: NOT READY", out)
        old = goals.goals(self.led)[self.gid]["evidence"][0]["id"]
        goals.withdraw_evidence(self.s, "desk", self.gid, old, "replaced")
        goals.add_evidence(self.s, "desk", self.gid, "C1", "path", "notes/out/summary.md")
        rc, out, _ = self.cli("--observe", "goal", "check", "--goal", self.gid)
        self.assertEqual(rc, 0, out)
        self.assertIn("Check: READY for the Director's acceptance", out)

    def test_close_refuses_without_evidence(self):
        self.decide()
        with self.assertRaisesRegex(DurehError, "not ready to close"):
            goals.close(self.s, "desk", self.gid, approval=SOURCE)
        self.assertEqual(goals.get_goal(self.led, self.gid)["state"], "open")

    def test_close_is_fail_closed(self):
        self.evidence()
        self.finish_items()
        os.environ["CLAUDECODE"] = "1"  # the desk's agent session
        with self.assertRaisesRegex(DurehError, "fail-closed"):
            goals.close(self.s, "desk", self.gid, by="Director", memo="the Director said so")
        os.environ.pop("CLAUDECODE")
        with self.assertRaisesRegex(DurehError, "fail-closed"):
            goals.close(self.s, "desk", self.gid, by="DUREH", memo="relayed")
        with self.assertRaisesRegex(DurehError, "fail-closed"):
            goals.close(self.s, "desk", self.gid, by="desk", memo="done")
        with self.assertRaisesRegex(DurehError, "no recorded board Decision"):
            goals.close(self.s, "desk", self.gid, approval=SOURCE)
        self.decide("Discuss")
        with self.assertRaisesRegex(DurehError, "not an approval"):
            goals.close(self.s, "desk", self.gid, approval=SOURCE)
        other = "https://github.com/example-org/office/issues/99"
        self.decide(url=other)
        with self.assertRaisesRegex(DurehError, "not on goal G-0001's board item"):
            goals.close(self.s, "desk", self.gid, approval=other)
        first = self.decide()
        self.decide("Rejected")
        with self.assertRaisesRegex(DurehError, "changed since"):
            goals.close(self.s, "desk", self.gid, approval=first["id"])
        with self.assertRaisesRegex(DurehError, "not an approval"):
            goals.close(self.s, "desk", self.gid, approval=SOURCE)
        self.decide()
        goals.add_evidence(self.s, "desk", self.gid, "C1", "link", "https://github.com/example-org/office/issues/12")
        with self.assertRaisesRegex(DurehError, "older than the goal's latest evidence"):
            goals.close(self.s, "desk", self.gid, approval=SOURCE)
        self.assertEqual(goals.get_goal(self.led, self.gid)["state"], "open")
        self.assertEqual([d for d in self.led.load("decision") if d["type"] == goals.ACCEPTANCE], [])

    def test_close_with_the_board_decision(self):
        self.evidence()
        report = "https://github.com/example-org/office/issues/12"
        goals.add_evidence(self.s, "desk", self.gid, "C2", "link", report, "comparison document")
        self.finish_items()
        self.decide(url=report)  # the Decision on the report item given as evidence
        os.environ["CLAUDECODE"] = "1"  # allowed inside the desk's session: the Decision is the Director's
        ev = goals.close(self.s, "desk", self.gid, approval=report, memo="Approve 1 on the report")
        dec = self.led.get("decision", ev["payload"]["decision"])
        self.assertEqual((dec["type"], dec["payload"]["by"], dec["payload"]["binding"]["url"]),
                         (goals.ACCEPTANCE, "Director", report))
        self.assertEqual(dec["payload"]["binding"]["digest"], ev["payload"]["digest"])
        self.assertEqual(goals.get_goal(self.led, self.gid)["state"], "closed")
        with self.assertRaisesRegex(DurehError, "is closed"):
            self.wi(["[C1] more"], self.gid)
        with self.assertRaisesRegex(DurehError, "is closed"):
            goals.add_evidence(self.s, "desk", self.gid, "C1", "link", report)
        self.assertIn("Closed", goals.render_check(goals.check(self.s, self.gid)))
        self.goal()  # the same board item may carry a new goal once the old one is closed

    def test_director_terminal_close_and_drop(self):
        self.evidence()
        self.finish_items()
        rc, out, err = self.cli("goal", "close", "--goal", self.gid, "--by", "Director", "--memo", "accepted, 11-10")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"{self.gid} closed by Director", out)
        g2 = self.goal(source="#5")
        os.environ["DUREH_ACTOR"] = "desk"
        rc, _, err = self.cli("goal", "drop", "--goal", g2, "--by", "Director", "--memo", "x")
        self.assertEqual(rc, 2)
        self.assertIn("fail-closed", err)
        os.environ.pop("DUREH_ACTOR")
        self.decide(url="https://github.com/example-org/office/issues/5")
        rc, out, err = self.cli("goal", "drop", "--goal", g2, "--approval",
                                "https://github.com/example-org/office/issues/5", "--memo", "withdrawn")
        self.assertEqual(rc, 0, err)
        self.assertEqual(goals.get_goal(self.led, g2)["state"], "dropped")


class GoalAlert(GoalCase):
    def test_status_lines(self):
        text = render_status(status(self.s))
        self.assertIn("\nGoal: none open\n", text + "\n")
        workday.plan(self.s, "desk", "today", "1. tidy the notes\n")
        text = render_status(status(self.s))
        self.assertIn("Goal: none open (on duty): propose the next goal to the Director", text)
        gid = self.goal()
        self.wi(["[C1] summary"], gid)
        legacy = self.wi(["ok"])
        text = render_status(status(self.s))
        self.assertIn(f"Goal: {gid} open: Run the pipeline end to end and report it, (criteria 2, deliverables with"
                      " evidence 0/3, work items 1); not covered by any work item: C2", text)
        line = next(x for x in text.splitlines() if x.startswith("Goal: ") and "name no goal" in x)
        self.assertRegex(line, r"^Goal: 2 open work items name no goal \(legacy or --no-goal\): WI-0001 .*" + legacy)
        self.assertNotIn("Goal: none open", text)
        rc, out, _ = self.cli("--observe", "status")
        self.assertEqual(rc, 0)
        self.assertIn(f"Goal: {gid} open", out)

    def test_day_plan_starts_with_the_next_goal(self):
        ev = workday.plan(self.s, "desk", "today", "1. tidy the notes\n")
        self.assertTrue(ev["payload"]["plan"].startswith(goals.NEXT_GOAL_LINE + "\n1. tidy the notes"))
        self.assertTrue(ev["payload"]["no_open_goal"])
        ev = workday.plan(self.s, "desk", "today", "1. Propose the next goal: a comparison\n")
        self.assertEqual(ev["payload"]["plan"], "1. Propose the next goal: a comparison\n")
        self.goal()
        ev = workday.plan(self.s, "desk", "today", "1. tidy the notes\n")
        self.assertEqual(ev["payload"]["plan"], "1. tidy the notes\n")
        self.assertNotIn("no_open_goal", ev["payload"])
