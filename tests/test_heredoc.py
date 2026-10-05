"""UR-20260930-06: a heredoc body is data, not commands, unless it feeds an interpreter."""
import os
import unittest

from helpers import SiteCase, denied, hook

from dureh_core.records import Ledger
from dureh_core.runtimes.claude_code import _strip_heredocs


class Heredoc(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()

    def bash(self, command, role="py-writer"):
        data = dict(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": command},
                    cwd=str(self.site_root))
        if role != "desk":
            data.update(agent_type=role, agent_id="a1")
        return hook(self.s, **data)

    def test_commit_message_body_with_arrow_is_not_a_redirect(self):
        cmd = "wc -l <<'EOF'\nAdd docs\n\nDocument the finding -> UR -> forward flow\nEOF"
        self.assertFalse(denied(self.bash(cmd)))

    def test_redirect_on_the_heredoc_line_is_still_checked(self):
        self.assertTrue(denied(self.bash("cat <<EOF > notes/x.md\nhello\nEOF")))
        tmp = os.environ["TMPDIR"]
        self.assertFalse(denied(self.bash(f"cat <<EOF > {tmp}/x.md\nhello\nEOF")))

    def test_commands_after_the_delimiter_are_still_checked(self):
        self.assertTrue(denied(self.bash("cat <<EOF\nbody\nEOF\necho x > notes/y.md")))

    def test_heredoc_into_an_interpreter_is_refused(self):
        for c in ("bash <<EOF\necho x > notes/z.md\nEOF", "python3 - <<'PY'\nprint(1)\nPY", "sh <<< 'rm x'"):
            self.assertTrue(denied(self.bash(c)), c)

    def test_fake_heredoc_inside_quotes_hides_nothing(self):
        cmd = 'echo "\n: <<X\n"\necho x > notes/w.md\nX'
        self.assertTrue(denied(self.bash(cmd)))

    def test_strip_keeps_the_operator_line_and_tab_stripped_delimiters(self):
        self.assertEqual(_strip_heredocs("cat <<-END | wc -l\n\tbody > x\n\tEND\nls"), "cat <<-END | wc -l\nls")

    def test_denial_records_the_full_command(self):
        long = "echo " + "a" * 1500 + " > notes/long.md"
        self.assertTrue(denied(self.bash(long)))
        ev = [e for e in Ledger(self.s).load("event") if e["type"] == "guard.denied"][-1]
        self.assertEqual(ev["payload"]["target"], long)


if __name__ == "__main__":
    unittest.main()
