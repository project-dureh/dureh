"""D2 (release 53): UR severity is derived from four facts; `--severity` is an override that states why;
send/export refuse a UR whose Facts line is missing or disagrees with its Severity."""
import contextlib
import io
import json
import os
import unittest
from pathlib import Path

from helpers import SiteCase

from dureh_core import cli
from dureh_core import upstream as up
from dureh_core.records import Ledger
from dureh_core.site import Site
from dureh_core.util import DurehError


def facts(job, data, workaround, all_):
    return {"blocks_job": job, "loses_data": data, "workaround": workaround, "affects_all": all_}


class Derivation(unittest.TestCase):
    def test_the_six_table_rows(self):
        # blocks job | loses data | workaround | affects all -> severity (proposal table)
        self.assertEqual(up.derive_severity(facts(True, False, True, False)), "blocking")
        self.assertEqual(up.derive_severity(facts(False, True, True, False)), "blocking")
        self.assertEqual(up.derive_severity(facts(False, False, False, True)), "blocking")
        self.assertEqual(up.derive_severity(facts(False, False, False, False)), "degrading")
        self.assertEqual(up.derive_severity(facts(False, False, True, True)), "degrading")
        self.assertEqual(up.derive_severity(facts(False, False, True, False)), "minor")

    def test_any_means_any(self):
        for wa in (True, False):
            for all_ in (True, False):
                self.assertEqual(up.derive_severity(facts(True, True, wa, all_)), "blocking")
                self.assertEqual(up.derive_severity(facts(True, False, wa, all_)), "blocking")
                self.assertEqual(up.derive_severity(facts(False, True, wa, all_)), "blocking")

    def test_derive_needs_all_four(self):
        with self.assertRaisesRegex(DurehError, "missing facts affects_all"):
            up.derive_severity({"blocks_job": False, "loses_data": False, "workaround": True})

    def test_parse_facts_round_trip(self):
        f = facts(False, False, True, False)
        line = up.facts_line(f)
        self.assertEqual(line, "blocks_job=no, loses_data=no, workaround=yes, affects_all=no")
        self.assertEqual(up.parse_facts(line), f)
        self.assertEqual(up.parse_facts("blocks_job=YES, workaround=?"), {"blocks_job": True})
        with self.assertRaisesRegex(DurehError, "expected one of"):
            up.parse_facts("blocks_job=yes, colour=no")
        with self.assertRaisesRegex(DurehError, "yes or no"):
            up.parse_facts("blocks_job=maybe")


class Draft(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        self.led = Ledger(self.s)
        self.finding = self.led.append("finding", "finding", "desk", None,
                                       {"summary": "the harness cannot do X", "evidence_level": "measured"})["id"]

    def test_facts_line_written_and_severity_derived(self):
        r = up.draft(self.s, "desk", self.finding, "bug", facts=facts(False, False, True, False))
        self.assertEqual(r["severity"], "minor")
        text = Path(r["path"]).read_text(encoding="utf-8")
        self.assertIn("- Severity: minor\n- Facts: blocks_job=no, loses_data=no, workaround=yes, affects_all=no\n", text)
        ev = [e for e in self.led.events() if e["type"] == "upstream.drafted"][-1]
        self.assertEqual(ev["payload"]["facts"], facts(False, False, True, False))
        self.assertNotIn("override_why", ev["payload"])

    def test_missing_fact_refused(self):
        with self.assertRaisesRegex(DurehError, r"missing facts: --fact affects_all=yes\|no"):
            up.draft(self.s, "desk", self.finding, "bug",
                     facts={"blocks_job": False, "loses_data": False, "workaround": True})
        with self.assertRaisesRegex(DurehError, "unknown: colour"):
            up.draft(self.s, "desk", self.finding, "bug", facts={**facts(False, False, True, False), "colour": True})
        self.assertFalse(list(up.notes_dir(self.s).glob("UR-*.md")) if up.notes_dir(self.s).is_dir() else [])

    def test_override_requires_why(self):
        with self.assertRaisesRegex(DurehError, "--why"):
            up.draft(self.s, "desk", self.finding, "bug", "blocking", facts=facts(False, False, True, False))
        with self.assertRaisesRegex(DurehError, "--why goes with --severity"):
            up.draft(self.s, "desk", self.finding, "bug", facts=facts(False, False, True, False), why="x")

    def test_override_written_with_why_and_derived(self):
        r = up.draft(self.s, "desk", self.finding, "bug", "blocking", facts=facts(False, False, True, False),
                     why="the Director wants it fixed first")
        self.assertEqual(r["severity"], "blocking")
        text = Path(r["path"]).read_text(encoding="utf-8")
        self.assertIn("- Severity: blocking (override: the Director wants it fixed first; derived minor)\n"
                      "- Facts: blocks_job=no, loses_data=no, workaround=yes, affects_all=no\n", text)
        ev = [e for e in self.led.events() if e["type"] == "upstream.drafted"][-1]
        self.assertEqual(ev["payload"]["override_why"], "the Director wants it fixed first")
        self.assertEqual(ev["payload"]["severity"], "blocking")
        self.assertEqual(up.list_requests(self.s)[0]["severity"].split()[0], "blocking")
        up.export(self.s, "desk", r["ur"])  # the override is accepted when the UR leaves the site

    def test_override_with_incomplete_facts_marks_them(self):
        r = up.draft(self.s, "desk", self.finding, "bug", "degrading", facts={"blocks_job": False}, why="judged")
        text = Path(r["path"]).read_text(encoding="utf-8")
        self.assertIn("- Severity: degrading (override: judged; facts incomplete)\n"
                      "- Facts: blocks_job=no, loses_data=?, workaround=?, affects_all=?\n", text)
        up.export(self.s, "desk", r["ur"])

    def test_cli_facts_and_override(self):
        run = lambda *argv: cli.main(["--site", str(self.site_root), "upstream", "draft", "--finding", self.finding,
                                      "--type", "bug", *argv])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(run("--fact", "blocks_job=no", "--fact", "loses_data=no", "--fact", "workaround=no"), 2)
            self.assertEqual(run("--fact", "blocks_job=no", "--fact", "loses_data=no", "--fact", "workaround=no",
                                 "--fact", "affects_all=no", "--severity", "minor"), 2)
            self.assertEqual(run("--fact", "blocks_job=no", "--fact", "loses_data=no", "--fact", "workaround=no",
                                 "--fact", "affects_all=no"), 0)
            self.assertEqual(run("--fact", "blocks_job=no", "--fact", "loses_data=no", "--fact", "workaround=no",
                                 "--fact", "affects_all=no", "--severity", "minor", "--why", "edge case"), 0)
        self.assertIn("affects_all", err.getvalue())
        rows = up.list_requests(self.s)
        self.assertEqual([r["severity"].split()[0] for r in rows], ["degrading", "minor"])
        self.assertIn("(override: edge case; derived degrading)", rows[1]["severity"])


class Export(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()

    def write(self, ur_id, severity_field, facts_line):
        d = up.notes_dir(self.s)
        d.mkdir(parents=True, exist_ok=True)
        fl = f"- Facts: {facts_line}\n" if facts_line is not None else ""
        (d / f"{ur_id}.md").write_text(f"# {ur_id}: t\n\n- Type: bug\n- Severity: {severity_field}\n{fl}- Site: s\n"
                                       f"- Status: open\n\n## Evidence (measured)\ne\n\n## Request\nfix it\n\n"
                                       f"## Impact at this site\nx\n")

    def test_refuses_missing_facts_line(self):
        self.write("UR-20261011-01", "minor", None)
        with self.assertRaisesRegex(DurehError, r"UR-20261011-01: no '- Facts:' line"):
            up.export(self.s, "desk", "UR-20261011-01")

    def test_refuses_disagreeing_facts(self):
        self.write("UR-20261011-01", "minor", "blocks_job=yes, loses_data=no, workaround=yes, affects_all=no")
        with self.assertRaisesRegex(DurehError, "Severity minor disagrees with its facts \\(derived blocking\\)"):
            up.export(self.s, "desk", "UR-20261011-01")
        self.write("UR-20261011-02", "minor", "blocks_job=no, loses_data=no, workaround=?, affects_all=no")
        with self.assertRaisesRegex(DurehError, "leaves workaround unknown"):
            up.export(self.s, "desk", "UR-20261011-02")

    def test_accepts_agreeing_facts_and_override(self):
        self.write("UR-20261011-01", "blocking", "blocks_job=yes, loses_data=no, workaround=yes, affects_all=no")
        up.export(self.s, "desk", "UR-20261011-01")
        self.write("UR-20261011-02", "blocking (override: Director; derived minor)",
                   "blocks_job=no, loses_data=no, workaround=yes, affects_all=no")
        up.export(self.s, "desk", "UR-20261011-02")


class Send(SiteCase):
    """`send --kind request` applies the same check to every open UR, next to the placeholder refusal."""

    def setUp(self):
        super().setUp()
        self.install()
        self.x = self.tmp / "exchange"
        self.x.mkdir()
        os.environ["DUREH_TEST_EXCHANGE"] = str(self.x)
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["upstream"] = {"exchange": "${DUREH_TEST_EXCHANGE}"}
        (self.site_root / "config.json").write_text(json.dumps(cfg, indent=2))
        self.s = Site(self.site_root)

    def test_send_refuses_missing_or_disagreeing_and_accepts_override(self):
        d = up.notes_dir(self.s)
        d.mkdir(parents=True, exist_ok=True)
        body = "- Site: s\n- Status: open\n\n## Request\nfix it\n\n## Impact at this site\nx\n"
        (d / "UR-20261011-01.md").write_text("# UR-20261011-01: t\n\n- Type: bug\n- Severity: minor\n" + body)
        with self.assertRaisesRegex(DurehError, "UR-20261011-01: no '- Facts:' line"):
            up.send(self.s, "desk", "request")
        (d / "UR-20261011-01.md").write_text("# UR-20261011-01: t\n\n- Type: bug\n- Severity: minor\n"
                                             "- Facts: blocks_job=no, loses_data=yes, workaround=yes, affects_all=no\n"
                                             + body)
        with self.assertRaisesRegex(DurehError, "disagrees with its facts \\(derived blocking\\)"):
            up.send(self.s, "desk", "request")
        (d / "UR-20261011-01.md").write_text("# UR-20261011-01: t\n\n- Type: bug\n- Severity: minor (override: w; "
                                             "derived blocking)\n- Facts: blocks_job=no, loses_data=yes, "
                                             "workaround=yes, affects_all=no\n" + body)
        self.assertEqual(up.send(self.s, "desk", "request")["refs"], ["UR-20261011-01"])


if __name__ == "__main__":
    unittest.main()
