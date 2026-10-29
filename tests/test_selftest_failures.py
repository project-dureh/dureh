"""Release 71 (UR-20261016-02): `verify --self` names each failing or erroring unit test with the end of its report,
in `problems` and in the run record, bounded to 10 tests and 5 lines each."""
import subprocess
import unittest
from unittest import mock

from helpers import SiteCase

from dureh_core import selftest
from dureh_core.records import Ledger

SEP = "=" * 70
DASH = "-" * 70


def report(n_fail=1, n_error=1, long_line=False):
    blocks = []
    for i in range(n_fail):
        blocks.append(f"{SEP}\nFAIL: test_f{i} (tests.test_x.Case.test_f{i})\n{DASH}\nTraceback (most recent call last):\n"
                      f"  File \"tests/test_x.py\", line {10 + i}, in test_f{i}\n    self.assertEqual(1, 2)\n"
                      f"AssertionError: 1 != 2{' ' + 'x' * 500 if long_line else ''}\n")
    for i in range(n_error):
        blocks.append(f"{SEP}\nERROR: test_e{i} (tests.test_y.Other.test_e{i})\n{DASH}\nTraceback (most recent call last):\n"
                      f"  File \"tests/test_y.py\", line 5, in test_e{i}\n    boom()\nNameError: name 'boom' is not defined\n")
    total = n_fail + n_error
    return ("..F.E\n" + "\n".join(blocks) + f"\n{DASH}\nRan 12 tests in 1.0s\n\nFAILED (failures={n_fail}, errors={n_error})\n"
            if total else "....\n" + f"{DASH}\nRan 4 tests in 0.1s\n\nOK\n")


class Summary(unittest.TestCase):
    """unittest's closing lines are the summary even when buffered test stdout lands after them (10-29)."""

    def test_ok_with_trailing_stdout(self):
        text = report(0, 0) + '{"type": "workday.switched", "x": 1}\ngithub token refresh failed: POST x: HTTP 502\n'
        self.assertEqual(selftest.unit_summary(text), "Ran 4 tests in 0.1s | OK")

    def test_failed(self):
        self.assertEqual(selftest.unit_summary(report(1, 1)), "Ran 12 tests in 1.0s | FAILED (failures=1, errors=1)")

    def test_no_summary_falls_back_to_tail(self):
        self.assertEqual(selftest.unit_summary("a\nb\nc\nd\n"), "b | c | d")


class Parse(unittest.TestCase):
    def test_each_failure_and_error_is_named_with_its_tail(self):
        got = selftest.unit_failures(report())
        self.assertEqual([(f["kind"], f["test"]) for f in got],
                         [("FAIL", "tests.test_x.Case.test_f0"), ("ERROR", "tests.test_y.Other.test_e0")])
        self.assertEqual(got[0]["tail"][-1], "AssertionError: 1 != 2")
        self.assertEqual(len(got[0]["tail"]), 4)
        self.assertNotIn("Ran 12 tests", " ".join(got[1]["tail"]))

    def test_older_python_test_ids_and_bounds(self):
        text = f"{SEP}\nFAIL: test_a (tests.test_x.Case)\n{DASH}\n" + "".join(f"line {i}\n" for i in range(20))
        got = selftest.unit_failures(text)
        self.assertEqual(got[0]["test"], "tests.test_x.Case.test_a")
        self.assertEqual(got[0]["tail"], [f"line {i}" for i in range(15, 20)])
        long = selftest.unit_failures(report(long_line=True, n_error=0))[0]["tail"][-1]
        self.assertEqual(len(long), selftest.FAILED_LINE_CHARS)
        self.assertTrue(long.endswith("..."))

    def test_a_passing_run_has_none(self):
        self.assertEqual(selftest.unit_failures(report(0, 0)), [])


class RunSelf(SiteCase):
    def run_with(self, stderr, code=1):
        self.install()
        proc = subprocess.CompletedProcess([], code, "", stderr)
        with mock.patch.object(selftest.subprocess, "run", return_value=proc):
            return selftest.run_self(self.site(), unit=True)

    def test_problems_and_the_run_record_name_the_failing_tests(self):
        r = self.run_with(report())
        self.assertEqual(r["result"], "fail")
        self.assertTrue(r["problems"][0].startswith("unit tests failed: "))
        self.assertIn("FAIL tests.test_x.Case.test_f0: Traceback (most recent call last): | ", r["problems"][1])
        self.assertTrue(r["problems"][1].endswith("AssertionError: 1 != 2"))
        self.assertTrue(r["problems"][2].startswith("ERROR tests.test_y.Other.test_e0: "))
        run = [x for x in Ledger(self.site()).load("run") if x["payload"].get("run_type") == "selftest"][-1]
        self.assertEqual(run["payload"]["problems"][:3], r["problems"][:3])
        self.assertEqual([f["test"] for f in run["payload"]["counts"]["unit"]["failures"]],
                         ["tests.test_x.Case.test_f0", "tests.test_y.Other.test_e0"])

    def test_at_most_ten_tests_are_listed(self):
        r = self.run_with(report(n_fail=8, n_error=5))
        named = [p for p in r["problems"] if p.startswith(("FAIL ", "ERROR "))]
        self.assertEqual(len(named), selftest.MAX_FAILED_TESTS)
        self.assertIn("... and 3 more failing test(s)", r["problems"])
        self.assertEqual(r["counts"]["unit"]["failed_tests"], 13)

    def test_a_passing_run_adds_nothing(self):
        r = self.run_with(report(0, 0), code=0)
        self.assertNotIn("failures", r["counts"]["unit"])
        self.assertFalse(any(p.startswith(("FAIL ", "ERROR ", "unit tests")) for p in r["problems"]))
