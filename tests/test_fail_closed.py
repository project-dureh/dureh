"""Release 78 (improvement plan v0.4 bundle 1): fail-closed asks never proceed on a fallback or a provisional choice,
and an approval record always names its approver (`--by` has no default)."""
import contextlib
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from helpers import SiteCase
from test_github import GithubBase

from dureh_core import workday
from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.records import FAIL_CLOSED_RULE, Ledger, fail_closed_check, fallback_lines, typed_block
from dureh_core.util import DurehError

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=timezone(timedelta(hours=-4)))
ALL_WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def run(site_root, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["--site", str(site_root), *argv])
    return code, out.getvalue(), err.getvalue()


class FallbackLines(unittest.TestCase):
    def test_named_fallbacks_are_found(self):
        for text in ("Fallback after 30 minutes: Option 2", "30-minute fallback: Option 1 (keep the old dataset)",
                     "30분 무응답이면 Option 2로 진행", "폴백: 2번", "If no answer, Option 1 applies"):
            self.assertTrue(fallback_lines(text), text)

    def test_no_fallback_lines_pass(self):
        for text in ("Fallback: none, this waits for the Director (Option 1 needs a push)", "Option 1: push now",
                     "30분 fallback 없음 (Option 2는 대표 조치)", "We wait 30 minutes for the job", "", "fallback: none"):
            self.assertEqual(fallback_lines(text), [], text)

    def test_check(self):
        with self.assertRaisesRegex(DurehError, "no 30-minute fallback and no provisional"):
            fail_closed_check("provisional", "Director", "desk")
        for by in ("", "desk", "Desk"):
            with self.assertRaisesRegex(DurehError, "only by its approver's explicit answer"):
                fail_closed_check("acted", by, "desk")
        fail_closed_check("acted", "Director", "desk")
        fail_closed_check("escalated", "Director", "desk")


FC_THREE = ("--decide", "새 remote로 push할까요?", "--recommend", "아니오, 로컬에 둡니다.",
            "--otherwise", "기다립니다 (Fallback: none).")


class Ask(GithubBase):
    def body(self, text):
        f = self.tmp / "ask.md"
        f.write_text(text)
        return str(f)

    def test_fail_closed_ask_is_labelled_and_recorded(self):
        code, out, err = self.run_cli("item", "ask", "--title", "Push to a new remote",
                                      "--body-file", self.body("Push WI-0001 to a new remote?\nFallback: none."),
                                      "--options", "push now", "keep local", "--fail-closed", *FC_THREE)
        self.assertEqual(code, 0, err)
        create = next(c for c in self.gh.calls if c["argv"][1:3] == ["issue", "create"])
        argv = create["argv"]
        labels = [argv[i + 1] for i, x in enumerate(argv) if x == "--label"]
        self.assertEqual(labels, ["kind:decision", "fail-closed"])
        self.assertIn("**Fail-closed**: no 30-minute fallback", create["input"])
        ev = [e for e in self.events() if e["type"] == "github.ask.created"][0]
        self.assertTrue(ev["payload"]["fail_closed"])
        from dureh_core import github

        self.assertTrue(github.ask_fail_closed(self.site(), json.loads(out)["url"]))
        self.assertFalse(github.ask_fail_closed(self.site(), "https://github.com/x/y/issues/1"))

    def test_fail_closed_ask_naming_a_fallback_is_refused_before_anything(self):
        for body, opts in (("Link dataset ds-1?\n30-minute fallback: Option 2", ["link", "skip"]),
                           ("Link dataset ds-1?", ["link", "skip (fallback after 30 min: Option 2)"])):
            self.gh.calls.clear()
            code, _, err = self.run_cli("item", "ask", "--title", "Link", "--body-file", self.body(body),
                                        "--options", *opts, "--fail-closed")
            self.assertEqual(code, 2)
            self.assertIn("a fail-closed ask names no fallback option", err)
            self.assertEqual(self.gh.calls, [])
        self.assertEqual([e for e in self.events() if e["type"] == "github.ask.created"], [])

    def test_fail_closed_otherwise_naming_a_fallback_is_refused(self):
        code, _, err = self.run_cli("item", "ask", "--title", "Push", "--body-file", self.body("Push?"),
                                    "--options", "push", "keep", "--fail-closed", "--decide", "push할까요?",
                                    "--recommend", "예", "--otherwise", "fallback after 30 min: Option 2")
        self.assertEqual(code, 2)
        self.assertIn("rewrite --otherwise", err)
        self.assertEqual(self.gh.calls, [])

    def test_ordinary_ask_unchanged(self):
        code, out, err = self.run_cli("item", "ask", "--title", "Order", "--body-file",
                                      self.body("Which first?\nFallback after 30 minutes: Option 1"),
                                      "--options", "A", "B", "--decide", "A 먼저?", "--recommend", "A",
                                      "--otherwise", "30분 뒤 Option 1")
        self.assertEqual(code, 0, err)
        create = next(c for c in self.gh.calls if c["argv"][1:3] == ["issue", "create"])
        self.assertNotIn("fail-closed", create["argv"])
        self.assertNotIn("fail_closed", [e for e in self.events() if e["type"] == "github.ask.created"][0]["payload"])


class Decide(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.projects = self.tmp / "transcripts"
        (self.projects / "p").mkdir(parents=True)
        os.environ["DUREH_USAGE_CACHE"] = str(self.tmp / "usage-cache.json")
        os.environ.pop("CLAUDECODE", None)
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["workday"] = {"transcripts": [str(self.projects)], "phase": "production", "weekdays": ALL_WEEK}
        (self.site_root / "config.json").write_text(json.dumps(cfg, indent=2))
        os.environ["DUREH_FAKE_NOW"] = T0.isoformat()
        self.s = self.site()
        workday.switch(self.s, True, "trial")

    def open_day(self):
        workday.plan(self.s, "desk", "finish W", "1. W", None)
        workday.approve(self.s, "desk", "ok", by="Director")

    def decide(self, *extra):
        return run(self.site_root, "day", "decide", "--title", "dataset", "--choice", "link", "--why", "w", *extra)

    def test_provisional_refused_on_a_fail_closed_decision(self):
        self.open_day()
        code, _, err = self.decide("--option", "link", "--option", "skip", "--confidence", "low",
                                   "--action", "provisional", "--fail-closed")
        self.assertEqual(code, 2)
        self.assertIn("no 30-minute fallback and no provisional choice", err)
        code, _, err = self.decide("--fail-closed")  # untyped, acted by the desk itself
        self.assertEqual(code, 2)
        self.assertIn("only by its approver's explicit answer", err)
        self.assertEqual([e for e in Ledger(self.s).load("event") if e["type"] == "day.decision"], [])
        code, out, err = self.decide("--fail-closed", "--by", "Director")
        self.assertEqual(code, 0, err)
        ev = [e for e in Ledger(self.s).load("event") if e["type"] == "day.decision"][-1]
        self.assertEqual((ev["payload"]["fail_closed"], ev["payload"]["by"]), (True, "Director"))
        code, _, err = self.decide("--option", "link", "--option", "wait", "--action", "escalated", "--by",
                                   "Director", "--fail-closed")
        self.assertEqual(code, 0, err)

    def test_ask_url_makes_the_decision_fail_closed(self):
        self.open_day()
        url = "https://github.com/example-org/office/issues/7"
        Ledger(self.s).event("github.ask.created", "desk", None, {"url": url, "item_id": "PVTI_7", "layer": 3,
                                                                  "fail_closed": True})
        code, _, err = self.decide("--option", "link", "--option", "skip", "--confidence", "low",
                                   "--action", "provisional", "--ask", url)
        self.assertEqual(code, 2)
        self.assertIn(url, err)
        # an ordinary ask keeps the release-56 path, and the decision names the ask
        other = "https://github.com/example-org/office/issues/8"
        Ledger(self.s).event("github.ask.created", "desk", None, {"url": other, "item_id": "PVTI_8", "layer": 3})
        code, _, err = self.decide("--option", "link", "--option", "skip", "--confidence", "low",
                                   "--action", "provisional", "--ask", other)
        self.assertEqual(code, 0, err)
        ev = [e for e in Ledger(self.s).load("event") if e["type"] == "day.decision"][-1]
        self.assertEqual(ev["payload"]["ask"], other)
        self.assertNotIn("fail_closed", ev["payload"])

    def test_python_api_provisional_without_flag_unchanged(self):
        self.open_day()
        prov = typed_block("q", ["a", "b"], "a", "low", "provisional")
        workday.decide(self.s, "desk", "t", "a", "w", [], typed=prov)
        with self.assertRaises(DurehError):
            workday.decide(self.s, "desk", "t", "a", "w", [], typed=prov, fail_closed=True, action="provisional")

    def test_plan_refuses_a_fail_closed_choice_with_a_fallback(self):
        with self.assertRaisesRegex(DurehError, "a fail-closed choice names no fallback"):
            workday.plan(self.s, "desk", "g", "1. W\n- push to new remote (fail-closed): fallback after 30 min Option 2", None)
        workday.plan(self.s, "desk", "g", "1. W\n- push to new remote (fail-closed): fallback none, waits\n"
                                          "- order of W: fallback after 30 min Option 1", None)

    def test_day_approve_needs_by(self):
        workday.plan(self.s, "desk", "g", "1. W", None)
        code, _, err = run(self.site_root, "day", "approve", "--memo", "ok")
        self.assertEqual(code, 2)
        self.assertIn("--by is required for `day approve`", err)
        code, _, err = run(self.site_root, "day", "approve", "--memo", "ok", "--by", "desk")
        self.assertEqual(code, 2)
        self.assertIn("--by must be one of Director, DUREH", err)
        code, _, err = run(self.site_root, "day", "approve", "--memo", "ok", "--by", "DUREH")
        self.assertEqual(code, 0, err)


class Approver(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        self.wi = Ledger(self.s).work_items()[0]["id"]

    def test_wi_approve_needs_by(self):
        code, _, err = run(self.site_root, "wi", "approve", "--wi", self.wi, "--memo", "ok")
        self.assertEqual(code, 2)
        self.assertIn("--by is required for `wi approve`", err)
        self.assertIn("never assumed", err)
        code, _, err = run(self.site_root, "wi", "approve", "--wi", self.wi, "--memo", "ok", "--by", "the boss")
        self.assertEqual(code, 2)
        self.assertIn("--by must be one of Director, DUREH, desk", err)
        self.assertEqual([e for e in Ledger(self.s).load("event") if e["type"] == "wi.approved"], [])
        code, out, err = run(self.site_root, "wi", "approve", "--wi", self.wi, "--memo", "plan item", "--by", "desk")
        self.assertEqual(code, 0, err)
        self.assertIn("by desk", out)
        dec = [d for d in Ledger(self.s).load("decision") if d["type"] == "gate1"][-1]
        self.assertEqual(dec["payload"]["by"], "desk")
        with self.assertRaisesRegex(DurehError, "--by is required"):
            wf.wi_approve(self.s, "desk", self.wi, "ok", by="")

    def test_old_records_load(self):
        Ledger(self.s).append("decision", "gate1", "desk", self.wi, {"subject": "Gate 1", "disposition": "approve",
                                                                    "by": "Director", "memo": "old"})
        code, out, err = run(self.site_root, "wi", "show", "--wi", self.wi)
        self.assertEqual(code, 0, err)

    def test_push_approve_needs_by(self):
        code, _, err = run(self.site_root, "push", "--approve", "--repo", str(self.site_root), "--remote", "origin",
                           "--ref", "HEAD", "--memo", "m")
        self.assertEqual(code, 2)
        self.assertIn("--by is required for `push --approve`", err)

    def test_purge_by_goes_with_project(self):
        code, _, err = run(self.site_root, "purge", "--wi", self.wi, "--by", "Director")
        self.assertEqual(code, 2)
        self.assertIn("--by goes with --project", err)

    def test_desk_text(self):
        from dureh_core.runtimes.claude_code import _claude_md

        text = _claude_md(self.s)
        self.assertIn(FAIL_CLOSED_RULE, text)
        self.assertIn("anything about credentials", text)
        self.assertIn("Independent work continues meanwhile", text)
        self.assertIn("wi approve --wi <WI> --by Director --memo", text)
        self.assertIn("day approve --by Director --memo", text)
        self.assertIn("--approve --by Director --memo", text)


if __name__ == "__main__":
    unittest.main()
