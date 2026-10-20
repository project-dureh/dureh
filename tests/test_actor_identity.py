"""project-dureh/dureh#35 (a company site, 2026-10-19): a manager that ran `<site>/bin/dureh` in a way the hook did not
rewrite was taken for the desk. The actor comes from the session or agent identity, never from how dureh was called."""
import argparse
import os
import unittest

from helpers import SiteCase, denied, hook

from dureh_core.cli import _actor
from dureh_core.records import Ledger
from dureh_core.util import DurehError


class HookPaths(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()

    def bash(self, command, cwd=None):
        return hook(self.s, hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": command},
                    cwd=str(cwd or self.site_root), agent_type="py-writer", agent_id="a1")

    def assert_identity(self, out, command):
        self.assertFalse(denied(out), (command, out))
        self.assertIn("DUREH_ACTOR=py-writer", out["hookSpecificOutput"]["updatedInput"]["command"], command)

    def test_absolute_relative_and_path_lookup_get_the_identity(self):
        sub = self.site_root / "notes"
        sub.mkdir(exist_ok=True)
        for command, cwd in ((f"{self.site_root}/bin/dureh status", None), ("bin/dureh status", None),
                             ("./bin/dureh status", None), ("dureh status", None), ("../bin/dureh status", sub),
                             (f"{self.site_root}/notes/../bin/dureh status", None)):
            self.assert_identity(self.bash(command, cwd), command)

    def test_absolute_path_through_a_symlink(self):
        link = self.tmp / "site-link"
        link.symlink_to(self.site_root)
        command = f"{link}/bin/dureh status"
        self.assert_identity(self.bash(command), command)

    def test_another_dureh_is_still_refused(self):
        other = self.tmp / "elsewhere" / "bin"
        other.mkdir(parents=True)
        (other / "dureh").write_text("#!/bin/sh\n")
        self.assertTrue(denied(self.bash(f"{other}/dureh status")))


class ActorFromIdentity(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.args = argparse.Namespace(site=str(self.site_root), actor=None)
        self.saved = {k: os.environ.pop(k, None) for k in ("DUREH_ACTOR", "DUREH_AGENT_ID")}

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        super().tearDown()

    def test_no_identity_is_the_desk(self):
        self.assertEqual(_actor(self.args), "desk")

    def test_agent_process_without_actor_is_its_recorded_role(self):
        Ledger(self.site()).event("agent.started", "py-writer", None, {"agent_id": "a7"})
        os.environ["DUREH_AGENT_ID"] = "a7"
        self.assertEqual(_actor(self.args), "py-writer")
        self.args.actor = "desk"
        with self.assertRaises(DurehError):
            _actor(self.args)

    def test_unknown_agent_is_refused_not_the_desk(self):
        os.environ["DUREH_AGENT_ID"] = "nobody"
        with self.assertRaises(DurehError):
            _actor(self.args)

    def test_harness_identity_wins(self):
        os.environ.update(DUREH_ACTOR="py-writer", DUREH_AGENT_ID="a1")
        self.assertEqual(_actor(self.args), "py-writer")


if __name__ == "__main__":
    unittest.main()
