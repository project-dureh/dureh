"""project-dureh/dureh#35 (a company site, 2026-10-19): a manager that ran `<site>/bin/dureh` in a way the hook did not
rewrite was taken for the desk. The actor comes from the session or agent identity, never from how dureh was called."""
import argparse
import io
import json
import os
import subprocess
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import SiteCase, denied, hook

from dureh_core.cli import _actor, main
from dureh_core.runtimes import claude_code
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


MANAGER, AGENT = "pm-modules", "m1"


class IdentityWhateverTheCwd(SiteCase):
    """UR-20261026-02 (a company site, 2026-10-26): a manager's later `bin/dureh` calls were recorded as the desk and
    its checker spawn was refused ("prepared by desk, not pm-modules"). Every spelling of the site's dureh, from every
    cwd, carries the identity bound at spawn: the role and the agent id (whose `agent.started` gives the WI)."""

    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        self.saved = {k: os.environ.pop(k, None) for k in ("DUREH_ACTOR", "DUREH_AGENT_ID", "DUREH_SITE")}
        Ledger(self.s).event("agent.started", MANAGER, None, {"agent_id": AGENT})
        self.cwds = {"site root": self.site_root, "project": self.site_root / "projects" / "demo",
                     "worktree": self.site_root / "worktrees" / "WI-0009", "unrelated": self.tmp / "elsewhere" / "deep"}
        for d in self.cwds.values():
            d.mkdir(parents=True, exist_ok=True)
        self.link = self.tmp / "site-link"
        self.link.symlink_to(self.site_root)

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        super().tearDown()

    def forms(self, cwd: Path) -> list[str]:
        dureh = self.site_root / "bin" / "dureh"
        rel = os.path.relpath(dureh, cwd)
        return [f"{dureh} {{}}", f"{rel} {{}}", f"cd {self.site_root} && bin/dureh {{}}",
                f"cd {cwd} && {rel} {{}}", f"{self.link}/bin/dureh {{}}", f"cd {self.link} && ./bin/dureh {{}}"]

    def rewrite(self, command: str, cwd: Path) -> str:
        out = hook(self.s, hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": command},
                   cwd=str(cwd), agent_type=MANAGER, agent_id=AGENT)
        self.assertFalse(denied(out), (command, cwd, out))
        new = out["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertIn(f"DUREH_ACTOR={MANAGER}", new, (command, cwd))
        self.assertIn(f"DUREH_AGENT_ID={AGENT}", new, (command, cwd))
        return new

    def test_every_path_form_from_every_cwd_gets_the_identity(self):
        for name, cwd in self.cwds.items():
            for form in self.forms(cwd):
                with self.subTest(cwd=name, form=form):
                    self.rewrite(form.format("dispatch brief --wi WI-0001 --role checker"), cwd)

    def test_the_cli_takes_the_identity_whatever_its_cwd(self):
        """The rewritten environment gives the manager's role from any cwd, and so does the agent id alone."""
        args = argparse.Namespace(site=str(self.site_root), actor=None)
        here = os.getcwd()
        try:
            for cwd in self.cwds.values():
                os.chdir(cwd)
                os.environ.update(DUREH_ACTOR=MANAGER, DUREH_AGENT_ID=AGENT)
                self.assertEqual(_actor(args), MANAGER)
                os.environ.pop("DUREH_ACTOR")
                self.assertEqual(_actor(args), MANAGER, "the agent id alone is the agent's recorded role")
        finally:
            os.chdir(here)
            os.environ.pop("DUREH_AGENT_ID", None)

    def test_recorded_actor_end_to_end(self):
        """The rewritten command, run by a shell in each cwd, records the finding as the manager, never the desk."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("DUREH_")}
        for i, (name, cwd) in enumerate(self.cwds.items()):
            form = self.forms(cwd)[i % 4 if name != "unrelated" else 0]
            new = self.rewrite(form.format(f"ledger add finding --summary probe-{name.replace(' ', '-')}"), cwd)
            proc = subprocess.run(["bash", "-c", new], cwd=cwd, env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, (name, form, proc.stderr))
        found = {f["payload"]["summary"]: f["actor"] for f in Ledger(self.site()).load("finding")}
        self.assertEqual(found, {f"probe-{n.replace(' ', '-')}": MANAGER for n in self.cwds})


class GuardBudget(SiteCase):
    """UR-20261026-02, the measured cause: the guard hook timed out (30 s, `hook_cancelled` timedOut in the managers'
    transcripts) and Claude Code ran the call without the hook's rewrite, so it was recorded as the desk and its
    refusal ("desk may not dispatch py-writer") left no guard.denied. The guard now answers within its budget."""

    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        self.data = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "agent_type": MANAGER, "agent_id": AGENT,
                     "cwd": str(self.site_root), "tool_input": {"command": "bin/dureh dispatch brief --wi WI-0001"}}

    def denials(self):
        return [e for e in Ledger(self.site()).events() if e["type"] == "guard.denied"]

    def test_a_slow_check_is_refused_within_the_budget_and_recorded(self):
        real = claude_code.pre_tool_use

        def slow(site, data):
            time.sleep(3)
            return real(site, data)

        with mock.patch.object(claude_code, "pre_tool_use", slow):
            t0 = time.monotonic()
            out = claude_code.handle_within(self.s, self.data, timeout=1.0, age=0.0)
            spent = time.monotonic() - t0
        self.assertTrue(denied(out), out)
        self.assertLess(spent, 2.5, "answered within the budget, not after the slow check")
        self.assertIn("did not finish within", out["hookSpecificOutput"]["permissionDecisionReason"])
        ev = [e for e in self.denials() if e["payload"].get("over_budget")]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["actor"], ev[0]["payload"]["role"], ev[0]["payload"]["agent_id"]),
                         (MANAGER, MANAGER, AGENT))
        self.assertIn("dispatch brief", ev[0]["payload"]["target"])

    def test_a_hook_already_past_its_budget_refuses_at_once(self):
        out = claude_code.handle_within(self.s, self.data, timeout=30, age=25.0)
        self.assertTrue(denied(out))
        self.assertEqual(len(self.denials()), 1)

    def test_in_time_the_answer_is_the_checks_answer(self):
        out = claude_code.handle_within(self.s, self.data, timeout=30, age=0.0)
        self.assertFalse(denied(out), out)
        self.assertIn(f"DUREH_ACTOR={MANAGER} DUREH_AGENT_ID={AGENT}", out["hookSpecificOutput"]["updatedInput"]["command"])
        bad = dict(self.data, tool_input={"command": "sbatch x.sh"})
        self.assertTrue(denied(claude_code.handle_within(self.s, bad, timeout=30, age=0.0)))
        self.assertEqual(len(self.denials()), 1, "the child's own refusal is recorded once")

    def test_a_crash_in_the_checks_fails_closed(self):
        with mock.patch.object(claude_code, "pre_tool_use", side_effect=RuntimeError("boom")):
            out = claude_code.handle_within(self.s, self.data, timeout=30, age=0.0)
        self.assertTrue(denied(out))
        self.assertIn("boom", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_spawns_and_other_events_are_not_cut_off(self):
        self.assertFalse(claude_code.guarded("PreToolUse", "Agent"))
        self.assertFalse(claude_code.guarded("SubagentStart", None))
        self.assertTrue(claude_code.guarded("PreToolUse", "Bash"))
        self.assertTrue(claude_code.guarded("PreToolUse", "Write"))

    def test_settings_give_the_guard_its_timeout_and_the_cli_takes_it(self):
        pre = json.loads((self.site_root / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
        guard = [h for h in pre if "Bash" in h["matcher"]][0]["hooks"][0]
        self.assertEqual(guard["timeout"], claude_code.GUARD_HOOK_TIMEOUT)
        self.assertTrue(guard["command"].endswith(f"hook claude-code --timeout {claude_code.GUARD_HOOK_TIMEOUT}"))
        spawn = [h for h in pre if h["matcher"] == "Agent|Task"][0]["hooks"][0]
        self.assertNotIn("--timeout", spawn["command"])
        seen = {}
        with mock.patch.object(claude_code, "handle_within", lambda site, data, timeout=None: seen.update(t=timeout)), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(self.data))), redirect_stdout(io.StringIO()):
            main(["--site", str(self.site_root), "hook", "claude-code", "--timeout", "60"])
        self.assertEqual(seen["t"], 60.0)


class CliRefusalsAreRecorded(SiteCase):
    """UR-20261026-02: a refusal on the CLI identity path leaves guard.denied like the hook's."""

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

    def denials(self):
        return [e for e in Ledger(self.site()).events() if e["type"] == "guard.denied"]

    def test_unknown_agent_and_conflicting_actor(self):
        os.environ["DUREH_AGENT_ID"] = "nobody"
        with self.assertRaises(DurehError):
            _actor(self.args)
        os.environ.update(DUREH_ACTOR="py-writer", DUREH_AGENT_ID="a1")
        self.args.actor = "desk"
        with self.assertRaises(DurehError):
            _actor(self.args)
        ev = self.denials()
        self.assertEqual([(e["actor"], e["payload"]["tool"], e["payload"]["agent_id"]) for e in ev],
                         [("desk", "cli", "nobody"), ("py-writer", "cli", "a1")])
        self.assertIn("no recorded role", ev[0]["payload"]["reason"])
        self.assertIn("conflicts", ev[1]["payload"]["reason"])

    def test_desk_may_not_dispatch_a_worker_is_recorded(self):
        from dureh_core import workflow as wf

        site = self.site()
        bundle = wf.wi_new(site, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        wi = wf.wi_new(site, "desk", "w", "g", "py-writer", "modules/demo", ["ok"], parent=bundle)["id"]
        with self.assertRaises(DurehError) as cm:
            wf.dispatch_brief(site, wi, "desk")
        self.assertIn("desk may not dispatch py-writer", str(cm.exception))
        ev = self.denials()
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["actor"], ev[0]["wi"], ev[0]["payload"]["tool"]), ("desk", wi, "cli"))


if __name__ == "__main__":
    unittest.main()
