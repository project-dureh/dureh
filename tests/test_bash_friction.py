"""Friction seen at a company site on 2026-10-03 (upstream health check, no UR):
`cd X && bin/dureh ...`, `bin/dureh ... | head` and Claude Code's `git commit -m "$(cat <<'EOF' ...)"` were refused."""
import unittest

from helpers import SiteCase, denied, hook


class BashFriction(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        self.dureh = str(self.site_root / "bin" / "dureh")

    def bash(self, command, role="py-writer"):
        data = dict(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": command},
                    cwd=str(self.site_root))
        if role != "desk":
            data.update(agent_type=role, agent_id="a1")
        return hook(self.s, **data)

    def allowed_command(self, out):
        self.assertFalse(denied(out), out)
        return out["hookSpecificOutput"]["updatedInput"]["command"]

    def test_cd_then_dureh(self):
        cmd = self.allowed_command(self.bash(f"cd {self.site_root} && {self.dureh} wi stage --wi WI-0001 --summary x"))
        self.assertTrue(cmd.startswith("( export DUREH_ACTOR=py-writer DUREH_AGENT_ID=a1\n"))
        self.assertTrue(cmd.endswith("\n)"))

    def test_dureh_piped_into_read_only_filters(self):
        self.allowed_command(self.bash("bin/dureh status 2>&1 | head -60"))
        self.allowed_command(self.bash("bin/dureh wi show --wi WI-0001 | grep status | wc -l", role="desk"))

    def test_single_dureh_keeps_the_plain_prefix(self):
        self.assertEqual(self.allowed_command(self.bash("bin/dureh status")), "DUREH_ACTOR=py-writer DUREH_AGENT_ID=a1 bin/dureh status")

    def test_still_refused(self):
        for c in ("bin/dureh status > notes/x.txt", "bin/dureh status; rm notes/x",
                  "bin/dureh status && touch notes/y",
                  "for w in a b; do bin/dureh wi show --wi $w > notes/$w.txt; done",
                  "if true; then bin/dureh status --actor desk; fi", "{ bin/dureh status --site /elsewhere; }",
                  "(bin/dureh status &)", "for w in a b; do $B wi show --wi $w; done"):
            self.assertTrue(denied(self.bash(c)), c)

    def test_any_checked_combination_keeps_the_identity(self):
        # release 14: every segment is checked by its own rules; each dureh in the line gets the identity
        for c in ("bin/dureh wi show --wi WI-0001 | python3 -c 'import json,sys; print(1)'",
                  "echo start\nbin/dureh slurm --help\necho ---\nbin/dureh status",
                  f"cd {self.site_root} && ls && bin/dureh status"):
            cmd = self.allowed_command(self.bash(c))
            self.assertTrue(cmd.startswith("( export DUREH_ACTOR=py-writer"), c)

    def test_variable_holding_the_dureh_path_in_the_same_line(self):
        # release 18: agents kept writing `B=<site>/bin/dureh; $B ...`
        cmd = self.allowed_command(self.bash(f"B={self.dureh}; $B wi show --wi WI-0001 | head -20; $B status"))
        self.assertTrue(cmd.startswith("( export DUREH_ACTOR=py-writer"))
        self.assertTrue(denied(self.bash("$B status")), "unknown variable")
        self.assertTrue(denied(self.bash("B=/bin/rm; $B -rf x")), "a variable naming something else")

    def test_pwd_substitution_is_allowed(self):
        self.assertFalse(denied(self.bash('ls "$(pwd)/src"')))
        self.assertTrue(denied(self.bash('ls "$(id)"')))

    def test_git_archive_and_branch_listing_are_reads(self):
        import os
        scratch = os.environ["TMPDIR"]
        self.assertFalse(denied(self.bash(f"git -C {self.site_root} branch -a --contains abc123")))
        self.assertFalse(denied(self.bash(f"git -C {self.site_root} archive HEAD | tar -x -C {scratch}")))
        self.assertFalse(denied(self.bash(f"git -C {self.site_root} archive -o {scratch}/a.tar HEAD")))
        self.assertTrue(denied(self.bash(f"git -C {self.site_root} archive -o {self.site_root}/notes/a.tar HEAD")))

    def test_hidden_harness_calls_cannot_borrow_the_desk_identity(self):
        # without the hook's prefix the CLI's default actor is the desk
        for c in ("python3 /x/dureh.py wi approve --wi WI-0001 --memo ok", "python3 -m dureh_core.cli status",
                  "/other/path/bin/dureh wi approve --wi WI-0001 --memo ok", "$B status"):
            self.assertTrue(denied(self.bash(c)), c)

    def test_dureh_inside_loops_conditions_and_groups_gets_the_workers_identity(self):
        # release 20: the line-wide `( export DUREH_ACTOR=... )` wrapper covers every dureh in the line,
        # so a dureh after a shell keyword or an opening parenthesis is checked and allowed like any other
        for c in ("for w in A B; do echo x; bin/dureh wi show --wi $w; done",  # replay pair
                  "for w in A B; do bin/dureh wi show --wi $w; done",
                  "for w in a b; do bin/dureh wi show --wi $w; done",
                  "(bin/dureh wi approve --wi WI-0001 --memo ok)",
                  "if true; then bin/dureh wi approve --wi WI-0001 --memo ok; fi",
                  "{ bin/dureh status; }",
                  "while false; do bin/dureh status; done"):
            cmd = self.allowed_command(self.bash(c))
            self.assertTrue(cmd.startswith("( export DUREH_ACTOR=py-writer DUREH_AGENT_ID=a1\n"), (c, cmd))
            self.assertTrue(cmd.endswith("\n)"), c)

    def test_variable_dureh_runs_with_the_workers_identity(self):
        cmd = self.allowed_command(self.bash("X=bin/dureh; $X wi approve --wi WI-0001 --memo ok"))
        self.assertTrue(cmd.startswith("( export DUREH_ACTOR=py-writer DUREH_AGENT_ID=a1\n"), "not the desk")

    def test_desk_keeps_its_freedom(self):
        self.assertFalse(denied(self.bash("python3 -m dureh_core.cli status", role="desk")))

    def test_commit_message_from_a_quoted_heredoc(self):
        wt = self.site_root  # the check under test is the substitution, not the worktree rule
        cmd = (f"cd {wt} && git status --short && git commit -m \"$(cat <<'EOF'\n"
               "camri: add step\n\nUses $(not run) and `backticks` literally.\nEOF\n)\"")
        out = self.bash(cmd)  # a worker: other rules may still apply (worktree), the substitution no longer does
        self.assertNotIn("command substitution", str(out))
        self.assertIn("command substitution", str(self.bash("git commit -m \"$(date)\"")))

    def test_unquoted_heredoc_or_other_substitution_still_refused(self):
        for c in ("git commit -m \"$(cat <<EOF\nmsg $(id)\nEOF\n)\"", "git commit -m \"$(date)\"",
                  "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\" $(rm -rf x)"):
            self.assertTrue(denied(self.bash(c)), c)


if __name__ == "__main__":
    unittest.main()
