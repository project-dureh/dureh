"""UR-20261001-06: stale virtualenvs are caught before checks, rebuilt from configuration, and kept out of `changed`."""
import json
import os
import sys
import unittest
from pathlib import Path

from helpers import SiteCase, hook

from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.site import Site
from dureh_core.util import DurehError

# A fake env_rebuild that writes a fresh .venv (the repair moves a stale one aside first, so it starts from nothing).
FIX = f"mkdir -p .venv/bin && printf '#!%s\\n' {sys.executable} > .venv/bin/pytest"


class Venv(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.mod = self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)
        b = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        self.wi = wf.wi_new(self.s, "desk", "W", "g", "py-writer", "modules/demo", ["ok"], parent=b,
                            checks=["true"])["id"]
        for x in (b, self.wi):
            wf.wi_approve(self.s, "desk", x, "ok")
        hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
             tool_input={"subagent_type": "pm-modules", "prompt": wf.dispatch_brief(self.s, b, "desk")["brief"]})
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        wb = wf.dispatch_brief(self.s, self.wi, "pm-modules")
        hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root), agent_type="pm-modules",
             agent_id="a-pm", tool_input={"subagent_type": "py-writer", "prompt": wb["brief"]})
        self.wt = Path(wb["worktree"])

    def venv(self, root, interpreter):
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (root / ".venv" / "bin" / "pytest").write_text(f"#!{interpreter}\nprint(1)\n")

    def configure(self, **entry):
        cfg = json.loads(self.s.config_path.read_text())
        cfg["modules"] = [{"id": "demo", "path": "modules/demo", **entry}]
        self.s.config_path.write_text(json.dumps(cfg))
        self.s = Site(self.site_root)

    def stage(self):
        import subprocess

        (self.wt / "src" / "new file.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "src/new file.py"], cwd=self.wt, check=True)
        subprocess.run(["git", "commit", "-qm", "add"], cwd=self.wt, check=True)
        self.led.event("wi.staged", "desk", self.wi, {"summary": "s"})

    def test_changed_paths_are_exact_and_exclude_the_venv(self):
        self.venv(self.wt, sys.executable)
        (self.wt / ".venv" / "lib" / "site-packages").mkdir(parents=True)
        (self.wt / ".venv" / "lib" / "site-packages" / "Transparent Busy.ani").write_text("x")
        self.stage()
        paths, _ = wf.changed_paths(self.s, self.wi)
        self.assertEqual(paths, ["src/new file.py"])

    def test_changed_paths_exclude_stale_venv_copies_too(self):
        self.venv(self.wt, sys.executable)
        (self.wt / ".venv" / "y").write_text("x")
        (self.wt / ".venv.stale-20261006" / "bin").mkdir(parents=True)  # the rename step's copy
        (self.wt / ".venv.stale-20261006" / "x").write_text("x")
        (self.wt / ".venv.stale-20261007T133434" / "lib").mkdir(parents=True)  # a repair's copy
        (self.wt / ".venv.stale-20261007T133434" / "lib" / "z").write_text("x")
        (self.wt / ".venvs").mkdir()  # not a venv name the harness knows: still an output
        (self.wt / ".venvs" / "w").write_text("x")
        self.stage()
        paths, _ = wf.changed_paths(self.s, self.wi)
        self.assertEqual(paths, [".venvs/w", "src/new file.py"])
        for name in (".venv/y", "a/.venv.stale-20261006/x", ".venv.stale-x/bin/pytest", "b/.venv", ".venv.stale-1"):
            self.assertTrue(wf.CACHE_RE.search(name), name)
        for name in (".venvs/w", "x/.venv-old/y", ".venv.stale", "venv/x"):
            self.assertFalse(wf.CACHE_RE.search(name), name)

    def test_verify_refuses_a_stale_venv_without_recording_a_failure(self):
        self.venv(self.wt, "/old/site/worktrees/W/.venv/bin/python")
        self.stage()
        runs = len(self.led.load("run"))
        with self.assertRaisesRegex(DurehError, r"stale virtualenv.*/old/site/worktrees/W/\.venv/bin/python.*worktree repair"):
            wf.verify_wi(self.s, "checker", self.wi)
        self.assertEqual(len(self.led.load("run")), runs)
        self.assertEqual(self.led.wi_status(self.wi)["status"], "staged")

    def test_verify_runs_with_a_valid_venv(self):
        self.venv(self.wt, sys.executable)
        self.stage()
        self.assertEqual(wf.verify_wi(self.s, "checker", self.wi)["result"], "pass")

    def test_repair_rebuilds_the_venv_with_the_configured_command(self):
        self.venv(self.wt, "/old/python")
        self.configure(env_rebuild=FIX)
        r = wf.worktree_repair(self.s, "desk", self.wi)
        self.assertEqual(r["env"], "rebuilt")
        self.assertIsNone(wf.stale_venv(self.wt))
        ev = [e for e in self.led.events(self.wi) if e["type"] == "worktree.repaired"][-1]
        self.assertEqual(ev["payload"]["env"], "rebuilt")

    def test_repair_without_a_configured_command_reports(self):
        self.venv(self.wt, "/old/python")
        r = wf.worktree_repair(self.s, "desk", self.wi)
        self.assertEqual(r["env"], "stale")
        self.assertIn("env_rebuild", r["hint"])

    def test_site_wide_default_and_target_main_tree(self):
        self.venv(self.mod, "/old/python")
        cfg = json.loads(self.s.config_path.read_text())
        cfg.setdefault("env", {})["rebuild"] = FIX
        self.s.config_path.write_text(json.dumps(cfg))
        self.s = Site(self.site_root)
        r = wf.env_repair(self.s, "desk", "modules/demo")
        self.assertEqual(r["env"], "rebuilt")
        self.assertIsNone(wf.stale_venv(self.mod))

    def test_repair_creates_a_missing_venv(self):
        self.assertFalse((self.wt / ".venv").exists())
        self.assertEqual(wf.worktree_repair(self.s, "desk", self.wi)["env"], "ok")  # no env_rebuild: nothing to do
        self.configure(env_rebuild=FIX)
        r = wf.worktree_repair(self.s, "desk", self.wi)
        self.assertEqual((r["env"], r["missing"], r["stale_interpreter"]), ("rebuilt", True, None))
        self.assertTrue((self.wt / ".venv" / "bin" / "pytest").is_file())
        self.assertEqual(wf.worktree_repair(self.s, "desk", self.wi)["env"], "ok")

    def test_failed_rebuild_names_the_error(self):
        self.venv(self.wt, "/old/python")
        self.configure(env_rebuild="echo no network >&2; exit 3")
        with self.assertRaisesRegex(DurehError, "no network"):
            wf.worktree_repair(self.s, "desk", self.wi)


if __name__ == "__main__":
    unittest.main()
