"""UR-20261008-02: every WI id shown to the Director carries a short title, `WI-xxxx (title)`, cut to 40
characters unless `wi new --label` sets one; ids inside backquotes (commands) stay bare."""
import contextlib
import io
import json
import os

from helpers import SiteCase, hook

from dureh_core import upstream as up
from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.ops import render_status, status
from dureh_core.records import Ledger, short_title, titled_text, wi_label
from dureh_core.runtimes import claude_code
from dureh_core.util import DurehError

LONG = "Write the framework design document with all requirements and examples"


class Titles(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)
        self.a = wf.wi_new(self.s, "desk", LONG, "g", "doc-writer", "modules/demo", ["ok"])["id"]
        self.b = wf.wi_new(self.s, "desk", "Tiny", "g", "doc-writer", "modules/demo", ["ok"], label="design doc")["id"]

    def test_short_title_and_label(self):
        self.assertEqual(short_title({"title": LONG}), LONG[:37].rstrip() + "...")
        self.assertLessEqual(len(short_title({"title": LONG})), 40)
        self.assertEqual(wi_label(self.b, self.led.get("work-item", self.b)["payload"]), f"{self.b} (design doc)")
        for bad in ("", " ", "x" * 41):
            with self.assertRaises(DurehError):
                wf.wi_new(self.s, "desk", "t", "g", "doc-writer", "modules/demo", ["ok"], label=bad)

    def test_titled_text_leaves_commands_and_titled_ids(self):
        labels = self.led.labels()
        text = f"Gate 1: {self.b}, run `dispatch brief --wi {self.b}`; {self.a} (already) and WI-9999"
        out = titled_text(text, labels)
        self.assertIn(f"Gate 1: {self.b} (design doc),", out)
        self.assertIn(f"`dispatch brief --wi {self.b}`", out)
        self.assertIn(f"{self.a} (already)", out)
        self.assertIn("WI-9999", out)

    def test_status_gate_lines_and_orphans_carry_titles(self):
        text = render_status(status(self.s))
        self.assertIn(f", {wi_label(self.a, {'title': LONG})}, {self.b} (design doc)", text)
        self.assertIn("Gate 1 (Director approval) pending: WI-0001 (", text)
        wf.wi_approve(self.s, "desk", self.b, "ok")
        brief = wf.dispatch_brief(self.s, self.b, "desk")["brief"]
        hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
             tool_input={"subagent_type": "doc-writer", "prompt": brief, "description": "x"})
        hook(self.s, hook_event_name="SubagentStart", agent_type="doc-writer", agent_id="a-1")
        self.assertIn(f"Lane: {self.b} (design doc) doc-writer", render_status(status(self.s)))
        hook(self.s, hook_event_name="SubagentStop", agent_type="doc-writer", agent_id="a-1")
        text = render_status(status(self.s))
        self.assertIn(f"Orphaned: {self.b} (design doc) doc-writer agent ended", text)
        self.assertIn(f"`dispatch brief --wi {self.b}`", text)

    def test_wi_show_summary_and_new(self):
        os.environ["DUREH_SITE"] = str(self.site_root)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["wi", "show", "--wi", self.b, "--summary"])
        self.assertEqual(json.loads(out.getvalue())["label"], f"{self.b} (design doc)")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["wi", "new", "--title", "t", "--goal", "g", "--role", "doc-writer", "--target", "modules/demo",
                  "--acceptance", "ok", "--label", "short"])
        self.assertRegex(out.getvalue(), r"^WI-\d{4} \(short\) created")

    def test_upstream_message_and_slack_text(self):
        notes = self.site_root / "notes" / "upstream"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / "UR-20261008-09.md").write_text(f"# UR-20261008-09: {self.b} blocked\n\n- Type: bug\n- Severity: minor\n"
                                                 "- Status: open\n\n## Request\nfix it\n")
        self.assertIn(f"{self.b} (design doc) blocked", up.message(self.s))
        from dureh_core import slack

        self.assertEqual(slack._titled(self.s, f"done: {self.b}"), f"done: {self.b} (design doc)")

    def test_day_plan_records_titles(self):
        from dureh_core import workday

        os.environ["DUREH_USAGE_CACHE"] = str(self.tmp / "usage-cache.json")
        os.environ.pop("CLAUDECODE", None)  # the Director's terminal
        (self.tmp / "transcripts").mkdir()
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["workday"] = {"transcripts": [str(self.tmp / "transcripts")]}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        s = self.site()
        workday.switch(s, True, "trial")
        ev = workday.plan(s, "desk", "goal", f"1. {self.b}\n2. {self.a}\n")
        self.assertEqual(ev["payload"]["titles"], {self.a: short_title({"title": LONG}), self.b: "design doc"})

    def test_desk_instruction_line(self):
        claude_code.generate(self.s)
        self.assertIn("When you name a WI to the Director, add its short title", (self.site_root / "CLAUDE.md").read_text())
