"""Nothing a site depends on may break when the site or the package directory moves."""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from helpers import SiteCase, hook

from dureh_core import workflow as wf
from dureh_core.paths import target_root
from dureh_core.runtimes.claude_code import generate
from dureh_core.site import Site


class Relocatable(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()

    def set_config(self, **changes):
        cfg = json.loads(self.s.config_path.read_text())
        for key, value in changes.items():
            cfg[key] = value
        self.s.config_path.write_text(json.dumps(cfg, indent=2))
        self.s = Site(self.site_root)

    def move(self):
        new = self.tmp / "moved" / "site"
        new.parent.mkdir()
        shutil.move(str(self.site_root), str(new))
        self.site_root = new
        self.s = Site(new)
        return new

    def test_env_vars_expand_against_the_site(self):
        self.set_config(env={"path_prepend": ["$DUREH_SITE/tools/bin"],
                             "vars": {"CACHE": "$DUREH_SITE/cache/containers", "X": "${DUREH_NOT_SET:-fallback}"}})
        env = self.s.env()
        self.assertEqual(env["CACHE"], f"{self.site_root}/cache/containers")
        self.assertEqual(env["X"], "fallback")
        self.assertTrue(env["PATH"].startswith(f"{self.site_root}/tools/bin{os.pathsep}"))

    def test_settings_env_holds_expanded_values(self):
        self.set_config(env={"vars": {"CACHE": "$DUREH_SITE/cache/containers"}})
        generate(self.s)
        env = json.loads((self.site_root / ".claude" / "settings.json").read_text())["env"]
        self.assertEqual(env["CACHE"], f"{self.site_root}/cache/containers")

    def test_bin_dureh_and_hooks_work_after_the_site_moves(self):
        self.assertNotIn(str(self.site_root), (self.site_root / "bin" / "dureh").read_text())
        new = self.move()
        r = subprocess.run([str(new / "bin" / "dureh"), "status"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"DUREH site {new}", r.stdout)
        cmd = json.loads((new / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn("CLAUDE_PROJECT_DIR", cmd)
        event = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "x"},
                            "cwd": str(new)})
        r = subprocess.run(["sh", "-c", cmd], input=event, capture_output=True, text=True,
                           env=dict(os.environ, CLAUDE_PROJECT_DIR=str(new)))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([str(new / "bin" / "dureh"), "adapter", "sync"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_package_source_may_be_relative_or_use_env_vars(self):
        startup = dict(self.s.config["startup"])
        pkg = Path(startup["source"])
        os.environ["DUREH_TEST_PKG"] = str(pkg)
        self.set_config(startup={**startup, "source": "${DUREH_TEST_PKG}"})
        self.assertEqual(target_root(self.s, "package"), pkg)
        rel = os.path.relpath(pkg, self.site_root)
        self.set_config(startup={**startup, "source": rel})
        self.assertEqual(target_root(self.s, "package"), (self.site_root / rel).resolve())

    def test_spawn_checks_the_worktree_where_the_site_is_now(self):
        self.make_module()
        b = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        w = wf.wi_new(self.s, "desk", "W", "g", "py-writer", "modules/demo", ["ok"], parent=b)["id"]
        for x in (b, w):
            wf.wi_approve(self.s, "desk", x, "ok")
        spawn = lambda caller, target, brief, aid=None: hook(self.s, hook_event_name="PreToolUse", tool_name="Agent",
                                                             cwd=str(self.s.root), **({"agent_type": caller, "agent_id": aid} if aid else {}),
                                                             tool_input={"subagent_type": target, "prompt": brief})
        self.assertIsNone(spawn("desk", "pm-modules", wf.dispatch_brief(self.s, b, "desk")["brief"]))
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        brief = wf.dispatch_brief(self.s, w, "pm-modules")["brief"]
        self.move()
        wf.worktree_repair(self.s, "desk", w)
        self.assertIsNone(spawn("pm-modules", "py-writer", brief, "a-pm"))


if __name__ == "__main__":
    unittest.main()
