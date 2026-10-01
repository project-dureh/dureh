"""`dureh upstream message`: one paste-ready message for the Director to forward upstream."""
import contextlib
import io
import os
import unittest

from helpers import SiteCase

from dureh_core import cli
from dureh_core import upstream as up


def ur(title, type_, severity, status, request="Do the thing."):
    return (f"# {{id}}: {title}\n\n- Type: {type_}\n- Severity: {severity}\n- Site: s\n- Status: {status}\n\n"
            f"## Evidence (measured)\nlong evidence\n\n## Request\n{request}\nmore detail\n\n## Impact at this site\nx\n")


class Message(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        d = up.notes_dir(self.s)
        d.mkdir(parents=True, exist_ok=True)
        files = {
            "UR-20261001-01": ur("minor thing", "bug", "minor", "open"),
            "UR-20261001-02": ur("binding is wrong", "bug", "blocking", "open, not yet sent upstream", "Bind by hash."),
            "UR-20260930-01": ur("roles.yaml has no writer", "decision", "blocking", "sent 2026-09-30; revised, D3 pending"),
            "UR-20260930-02": ur("brief size", "bug", "degrading", "landed d9a807d"),
            "UR-20260930-05": ur("false alarm", "bug", "minor", "WITHDRAWN 2026-09-30 (false positive)"),
        }
        for k, v in files.items():
            (d / f"{k}.md").write_text(v.replace("{id}", k))

    def test_message_lists_new_then_awaiting_and_omits_closed(self):
        text = up.message(self.s)
        new = text.index("NEW")
        waiting = text.index("AWAITING REPLY OR LANDING")
        self.assertLess(text.index("UR-20261001-02 (bug, blocking): binding is wrong"), text.index("UR-20261001-01"))
        self.assertTrue(new < text.index("UR-20261001-02") < waiting < text.index("UR-20260930-01"))
        self.assertIn("Request: Bind by hash.", text)
        self.assertIn("status: sent 2026-09-30; revised, D3 pending", text)
        for closed in ("UR-20260930-02", "UR-20260930-05"):
            self.assertNotIn(closed, text)
        self.assertIn(str(up.notes_dir(self.s)), text)
        self.assertIn("harness rev", text)
        self.assertTrue(text.rstrip().endswith("give the harness rev when a change lands."))

    def test_cli_prints_it_in_observe_mode_without_writing(self):
        before = sorted(os.listdir(up.notes_dir(self.s)))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--site", str(self.site_root), "--observe", "upstream", "message"])
        self.assertEqual(code, 0)
        self.assertIn("UR-20261001-02", out.getvalue())
        self.assertEqual(sorted(os.listdir(up.notes_dir(self.s))), before)

    def test_nothing_to_forward(self):
        for p in up.notes_dir(self.s).glob("UR-*.md"):
            p.unlink()
        self.assertIn("No open upstream requests", up.message(self.s))

    def test_desk_instructions_require_the_paste_ready_message(self):
        text = (self.site_root / "CLAUDE.md").read_text()
        self.assertIn("upstream message", text)
        self.assertIn("one fenced code block", text)


if __name__ == "__main__":
    unittest.main()
