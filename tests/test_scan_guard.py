"""Release 27: the Bash guard refuses whole-filesystem scans. Evidence: WI-0115 left `find /` plus wait loops
running for hours after its agent had finished (2026-10-04)."""
import os
import unittest

from helpers import SiteCase, denied, hook

from dureh_core.org import HARNESS_ROOT
from dureh_core.runtimes.claude_code import _SEPARATORS, _segments, scan_roots


class ScanGuard(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.wt = self.site_root / "worktrees" / "WI-0001"
        self.wt.mkdir(parents=True)

    def bash(self, command, role="py-writer", cwd=None):
        data = dict(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": command},
                    cwd=str(cwd or self.site_root))
        if role != "desk":
            data.update(agent_type=role, agent_id="a1")
        return hook(self.s, **data)

    def reason(self, out):
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_roots_of_scan_commands(self):
        self.assertEqual(scan_roots("find", ["find", "/", "-name", "x"]), ["/"])
        self.assertEqual(scan_roots("find", ["find", "-L", "/work", "/nas", "-type", "f"]), ["/work", "/nas"])
        self.assertEqual(scan_roots("find", ["find", "-name", "x"]), ["."])
        self.assertEqual(scan_roots("grep", ["grep", "-rn", "foo", "/proj"]), ["/proj"])
        self.assertEqual(scan_roots("grep", ["grep", "-r", "--include=*.py", "-e", "foo", "src", "tests"]), ["src", "tests"])
        self.assertEqual(scan_roots("grep", ["grep", "-A", "3", "-r", "foo"]), ["."])
        self.assertIsNone(scan_roots("grep", ["grep", "foo", "/etc/passwd"]), "not recursive")
        self.assertEqual(scan_roots("rg", ["rg", "-t", "py", "foo", "/home"]), ["/home"])
        self.assertEqual(scan_roots("rg", ["rg", "--files", "/users"]), ["/users"])
        self.assertEqual(scan_roots("du", ["du", "-sh", "/work"]), ["/work"])
        self.assertEqual(scan_roots("ls", ["ls", "-laR", "/"]), ["/"])
        self.assertIsNone(scan_roots("ls", ["ls", "-la", "/work"]), "a plain listing is not a scan")
        self.assertEqual(scan_roots("tree", ["tree", "-L", "2", "/nas"]), ["/nas"])
        self.assertIsNone(scan_roots("cat", ["cat", "/etc/hosts"]))

    def test_whole_filesystem_scans_are_refused_with_a_hint(self):
        for c in ("find / -name '*.dat'", "find /work -name x", "find /nas -type d", "find /proj -maxdepth 2",
                  "find /home -name x", "find /users -name x", "grep -r foo /work", "grep -rn foo /",
                  "rg foo /proj", "rg --files /nas", "du -sh /work", "ls -R /", "ls -laR /users", "tree /",
                  "tree -L 2 /home", "find ~ -name x", "find $HOME -name x", "find /work/users -name x",
                  "cd / && find . -name x"):
            out = self.bash(c)
            self.assertTrue(denied(out), c)
            why = self.reason(out)
            self.assertIn("scans outside the allowed roots", why, c)
            self.assertIn(str(self.site_root / "worktrees"), why, c)
            self.assertIn(str(HARNESS_ROOT), why, c)
        for c in ("find . -name x | head", "grep -r foo", "du", "ls -R", "rg foo"):  # the cwd is the root
            out = self.bash(c, cwd="/")
            self.assertIn("scans outside the allowed roots", self.reason(out), c)

    def test_redirections_are_not_scan_roots(self):
        """Release 50: `2>/dev/null` reached scan_roots as `2`, `>`, `/dev/null` and the guard refused
        `grep ... -l . 2>/dev/null` for scanning /dev/null (three guard.denied records at a company, 2026-10-10)."""
        def roots(command):
            seg = [s for s in _segments(command) if s[0] not in _SEPARATORS][0]
            return scan_roots(seg[0], seg)

        denied_cmd = (r'grep -rn "entry_points\|entry-points" --include="*.py" -l . 2>/dev/null'
                      r' | grep -v worktrees | grep -v "\.venv" | head -20')
        self.assertEqual(roots(denied_cmd), ["."])
        self.assertEqual(roots("find . -name x 2>/dev/null"), ["."])
        self.assertEqual(roots("find . 2>/dev/null -name x"), ["."])
        self.assertEqual(roots("rg foo 2>&1"), ["."])
        self.assertEqual(roots("rg foo &>log src"), ["src"])
        self.assertEqual(roots("grep -r foo src > out.txt"), ["src"])
        self.assertEqual(roots("grep -r foo src >> out.txt 2>&1"), ["src"])
        self.assertEqual(roots("grep -r foo src 2> err.txt"), ["src"])
        self.assertEqual(roots("tree < in"), ["."])
        self.assertEqual(roots("ls -R src 3>trace"), ["src"])
        self.assertEqual(roots("du -sh /work 2>/dev/null"), ["/work"], "a redirect does not hide a real outside root")
        # through the hook: the company command passes from a worktree, the outside root is still refused
        out = self.bash(denied_cmd, cwd=self.wt)
        self.assertFalse(denied(out), out)
        out = self.bash("grep -r foo src > out.txt", cwd=self.wt)  # the write guard still sees out.txt
        self.assertNotIn("scans outside", self.reason(out) if denied(out) else "")
        self.assertFalse(denied(self.bash("find . -name x 2>/dev/null", cwd=self.wt)))
        out = self.bash("du -sh /work 2>/dev/null")
        self.assertTrue(denied(out))
        self.assertIn("`du /work` scans outside the allowed roots", self.reason(out))

    def test_scans_inside_the_allowed_roots_pass(self):
        tmp = os.environ["TMPDIR"]
        for c, cwd in ((f"find {self.site_root} -name x", None), ("find . -name x", self.wt),
                       ("grep -rn foo src", self.wt), ("grep -r foo .", self.wt),
                       (f"rg foo {self.site_root}/modules/demo", None), (f"du -sh {self.site_root}/modules/demo", None),
                       ("ls -R modules", None), (f"tree {self.wt}", None), (f"find {tmp} -name x", None),
                       ("find /tmp -name x", None), (f"rg foo {HARNESS_ROOT}", None), ("find $DUREH_SITE -name x", None),
                       (f"find $TMPDIR -name x", None), ("grep foo /etc/hosts", None), ("ls -la /work", None),
                       ("du -sh .", self.wt), ("find $UNKNOWN_VAR -name x", None)):
            out = self.bash(c, cwd=cwd)
            self.assertFalse(denied(out), (c, out))

    def test_desk_keeps_its_freedom(self):
        self.assertIsNone(self.bash("find / -name x", role="desk"))

    def test_scan_denial_is_audited(self):
        self.bash("find / -name x")
        from dureh_core.records import Ledger

        ev = [e for e in Ledger(self.s).load("event") if e["type"] == "guard.denied"][-1]
        self.assertEqual(ev["payload"]["target"], "find / -name x")
        self.assertIn("scans outside", ev["payload"]["reason"])


if __name__ == "__main__":
    unittest.main()
