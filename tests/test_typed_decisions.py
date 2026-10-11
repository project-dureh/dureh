"""Release 52, proposals/typed-decisions-and-model-routing.md A1-A2: the optional typed block on decision records
(question, options, chosen, confidence, action, facts), its flags on `ledger add decision` and `day decide`, and
the shape of records written before it (no block) staying valid."""
import contextlib
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from helpers import SiteCase

from dureh_core import workday
from dureh_core.cli import main
from dureh_core.records import Ledger, parse_fact, typed_block, typed_line
from dureh_core.util import DurehError
from dureh_core.validate import check_record, errors, schema_for

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=timezone(timedelta(hours=-4)))
ALL_WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def record(payload: dict) -> dict:
    return {"schema_version": 1, "id": "dec-20261010T120000Z-abcd1234", "kind": "decision", "type": "triage",
            "ts": "2026-10-10T12:00:00Z", "actor": "desk", "wi": None, "refs": [], "payload": payload}


TYPED = {"question": "Is the request a duplicate of an open one?", "options": ["duplicate", "new", "unclear"],
         "chosen": "new", "confidence": "medium", "action": "acted",
         "facts": {"same_component": True, "same_symptom": False}}


class Schema(unittest.TestCase):
    def test_old_record_without_block_is_valid(self):
        check_record(record({"subject": "s", "disposition": "record", "by": "desk", "memo": ""}))
        check_record(record({"subject": "s", "disposition": "approve", "by": "Director", "binding": {"via": "x"}}))

    def test_block_validates(self):
        check_record(record({"subject": "s", "disposition": "record", "by": "desk", "typed": TYPED}))
        check_record(record({"subject": "s", "disposition": "record", "by": "desk",
                             "typed": {k: v for k, v in TYPED.items() if k != "facts"}}))

    def test_block_rejects_bad_shapes(self):
        schema = schema_for("decision")
        for bad in ({**TYPED, "confidence": "sure"}, {**TYPED, "action": "done"}, {**TYPED, "options": ["one"]},
                    {**TYPED, "facts": {"x": "yes"}}, {**TYPED, "extra": 1}, {k: v for k, v in TYPED.items() if k != "chosen"}):
            problems = errors(record({"subject": "s", "disposition": "record", "by": "desk", "typed": bad}), schema)
            self.assertTrue(problems, bad)
            self.assertTrue(all(".typed" in p for p in problems), problems)

    def test_validator_additional_properties_schema(self):
        self.assertEqual(errors({"a": True}, {"type": "object", "additionalProperties": {"type": "boolean"}}), [])
        self.assertEqual(len(errors({"a": 1}, {"type": "object", "additionalProperties": {"type": "boolean"}})), 1)


class Block(unittest.TestCase):
    def test_rules(self):
        b = typed_block("q?", ["a", "b"], "b", "high", "acted", {"f": True}, "desk", "desk")
        self.assertEqual(b, {"question": "q?", "options": ["a", "b"], "chosen": "b", "confidence": "high",
                             "action": "acted", "facts": {"f": True}})
        self.assertNotIn("facts", typed_block("q?", ["a", "b"], "a"))
        for args in (("", ["a", "b"], "a"), ("q", ["a"], "a"), ("q", ["a", "a"], "a"), ("q", ["a", "b"], "c"),
                     ("q", ["a", "b"], "a", "sure"), ("q", ["a", "b"], "a", "low", "acted"),
                     ("q", ["a", "b"], "a", "medium", "escalated", None, "desk", "desk"),
                     ("q", ["a", "b"], "a", "medium", "escalated", None, "", "desk")):
            with self.assertRaises(DurehError, msg=args):
                typed_block(*args)
        self.assertEqual(typed_block("q", ["a", "b"], "a", "low", "provisional")["action"], "provisional")
        self.assertEqual(typed_block("q", ["a", "b"], "a", "low", "escalated", None, "Director", "desk")["action"],
                         "escalated")

    def test_facts_and_line(self):
        self.assertEqual(parse_fact("same_rev=yes"), ("same_rev", True))
        self.assertEqual(parse_fact("x=No"), ("x", False))
        for bad in ("x", "x=", "=yes", "x=maybe"):
            with self.assertRaises(DurehError):
                parse_fact(bad)
        self.assertEqual(typed_line(TYPED), "new (of duplicate | new | unclear), confidence medium, acted; "
                                            "facts same_component=yes, same_symptom=no")
        self.assertEqual(typed_line(None), "")


class Cli(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        os.environ.pop("CLAUDECODE", None)

    def cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--site", str(self.site_root), *args])
        return rc, buf.getvalue()

    def decisions(self, type_=None):
        return [r for r in Ledger(self.s).load("decision") if type_ is None or r["type"] == type_]

    def test_ledger_add_round_trip(self):
        rc, out = self.cli("ledger", "add", "decision", "--type", "triage", "--summary", "UR triage",
                           "--question", TYPED["question"], "--option", "duplicate", "--option", "new",
                           "--option", "unclear", "--chosen", "new", "--confidence", "medium",
                           "--fact", "same_component=yes", "--fact", "same_symptom=no", "--ref", "fnd-x")
        self.assertEqual(rc, 0, out)
        rec = json.loads(out)
        self.assertEqual(rec["type"], "triage")
        self.assertEqual(rec["refs"], ["fnd-x"])
        self.assertEqual(rec["payload"]["typed"], TYPED)
        stored = Ledger(self.s).get("decision", rec["id"])  # re-read and re-checked against the schema
        self.assertEqual(stored["payload"]["typed"], TYPED)
        rows = Ledger(self.s).sql("select payload from records where kind = 'decision' and type = 'triage'")
        self.assertEqual(json.loads(rows[0]["payload"])["typed"]["chosen"], "new")

    def test_ledger_add_without_block_keeps_old_shape(self):
        rc, out = self.cli("ledger", "add", "decision", "--summary", "plain")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("typed", json.loads(out)["payload"])

    def test_ledger_add_refuses_bad_block(self):
        base = ("ledger", "add", "decision", "--summary", "s", "--question", "q", "--option", "a", "--option", "b")
        self.assertEqual(self.cli(*base, "--chosen", "c")[0], 2)
        self.assertEqual(self.cli(*base, "--chosen", "a", "--confidence", "low")[0], 2)
        self.assertEqual(self.cli(*base, "--chosen", "a", "--action", "escalated")[0], 2)  # --by is the desk
        self.assertEqual(self.cli(*base, "--chosen", "a", "--action", "escalated", "--by", "Director")[0], 0)
        self.assertEqual(self.cli("ledger", "add", "decision", "--summary", "s", "--option", "a", "--option", "b",
                                  "--chosen", "a")[0], 2)  # no question
        self.assertEqual(len(self.decisions()), 1)


class DayDecide(SiteCase):
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
        workday.plan(self.s, "desk", "finish W", "1. W", None)
        workday.approve(self.s, "desk", "ok")

    def cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--site", str(self.site_root), *args])
        return rc, buf.getvalue()

    def test_plain_decide_unchanged(self):
        r = workday.decide(self.s, "desk", "lock strategy", "regenerate", "pyproject changed", [])
        self.assertNotIn("decision", r)
        self.assertEqual([r["type"] for r in Ledger(self.s).load("decision")], [])
        st = workday.status(self.s)
        self.assertEqual(st["decisions"], [{"id": r["record"], "title": "lock strategy", "commit": None}])

    def test_typed_decide_writes_day_choice_record(self):
        rc, out = self.cli("day", "decide", "--title", "lock strategy", "--choice", "regenerate",
                           "--why", "pyproject changed", "--option", "regenerate", "--option", "keep",
                           "--confidence", "high", "--fact", "lock_stale=yes")
        self.assertEqual(rc, 0, out)
        r = json.loads(out)
        ev = Ledger(self.s).get("event", r["record"])
        self.assertEqual(ev["type"], "day.decision")
        self.assertNotIn("typed", ev["payload"])  # the event keeps its shape: it stays the rollback point
        dec = Ledger(self.s).get("decision", r["decision"])
        self.assertEqual(dec["type"], "day-choice")
        self.assertIn(r["record"], dec["refs"])
        self.assertEqual(dec["payload"]["typed"], {"question": "lock strategy", "options": ["regenerate", "keep"],
                                                   "chosen": "regenerate", "confidence": "high", "action": "acted",
                                                   "facts": {"lock_stale": True}})
        self.assertEqual(dec["payload"]["by"], "desk")
        self.assertEqual(dec["payload"]["memo"], "pyproject changed")
        d = workday.status(self.s)["decisions"][0]
        self.assertEqual((d["decision"], d["typed"]["chosen"]), (r["decision"], "regenerate"))

    def test_typed_decide_refuses_choice_outside_options(self):
        rc, _ = self.cli("day", "decide", "--title", "t", "--choice", "other", "--why", "w",
                         "--option", "a", "--option", "b")
        self.assertEqual(rc, 2)
        self.assertEqual(workday.status(self.s)["decisions"], [])  # nothing recorded, no event either

    def test_escalated_names_the_director(self):
        rc, _ = self.cli("day", "decide", "--title", "t", "--choice", "a", "--why", "w", "--option", "a",
                         "--option", "b", "--confidence", "low", "--action", "escalated")
        self.assertEqual(rc, 2)
        rc, out = self.cli("day", "decide", "--title", "t", "--choice", "a", "--why", "w", "--option", "a",
                           "--option", "b", "--confidence", "low", "--action", "escalated", "--by", "Director")
        self.assertEqual(rc, 0, out)
        dec = Ledger(self.s).get("decision", json.loads(out)["decision"])
        self.assertEqual((dec["payload"]["by"], dec["payload"]["typed"]["action"]), ("Director", "escalated"))


if __name__ == "__main__":
    unittest.main()
