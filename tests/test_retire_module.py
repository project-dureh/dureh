"""Release 55 (UR-20261011-02, option A): `dureh site retire-module` is the mirror of link-module."""
import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stdout

from helpers import SiteCase, hook

from dureh_core import cli, sitelinks
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class RetireModule(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.src = self.tmp / "sources" / "demo"
        self.src.mkdir(parents=True)
        (self.src / "demo.py").write_text("x = 1\n")
        git(self.src, "init", "-q", "-b", "main")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "init")
        self.s = self.site()
        self.entry = sitelinks.link_module(self.s, "desk", "demo", str(self.src), "main")
        self.mod = self.site_root / "modules" / "demo"
        self.s = self.site()

    def config(self):
        return json.loads((self.site_root / "config.json").read_text())

    def events(self, kind="site.module-retired"):
        return [e for e in Ledger(self.s).events() if e["type"] == kind]

    def stopped_wi_with_worktree(self) -> str:
        bundle = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        wi = wf.wi_new(self.s, "desk", "item", "g", "py-writer", "modules/demo", ["ok"], parent=bundle)["id"]
        for w in (bundle, wi):
            wf.wi_approve(self.s, "desk", w, "ok")
        brief = wf.dispatch_brief(self.s, bundle, "desk")["brief"]
        hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.s.root),
             tool_input={"subagent_type": "pm-modules", "prompt": brief, "description": "x"})
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        wf.dispatch_brief(self.s, wi, "pm-modules")  # creates the worktree
        self.assertTrue((self.s.worktrees / wi).is_dir())
        for w in (wi, bundle):
            wf.dispatch_stop(self.s, "desk", w, "retired")
        return wi

    def test_refuses_unknown_id_and_non_desk(self):
        with self.assertRaisesRegex(DurehError, "not a registered module"):
            sitelinks.retire_module(self.s, "desk", "nope")
        with self.assertRaisesRegex(DurehError, "only the desk"):
            sitelinks.retire_module(self.s, "py-writer", "demo")
        self.assertTrue(self.mod.is_dir())

    def test_refuses_live_work_item_on_the_target(self):
        wi = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        with self.assertRaisesRegex(DurehError, f"live work items on the target: {wi} \\(proposed\\)"):
            sitelinks.retire_module(self.s, "desk", "demo")
        other = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "site", ["ok"])["id"]
        wf.dispatch_stop(self.s, "desk", wi, "x")
        out = sitelinks.retire_module(self.s, "desk", "demo", dry_run=True)
        self.assertIsNone(out["refusal"], out)
        self.assertNotIn(other, out["steps"][1]["work_items"], "a WI on another target is not on this one")

    def test_refuses_dirty_checkout(self):
        (self.mod / "scratch.txt").write_text("untracked\n")
        with self.assertRaisesRegex(DurehError, "uncommitted changes"):
            sitelinks.retire_module(self.s, "desk", "demo", force=True)
        self.assertTrue(self.mod.is_dir())
        self.assertEqual(self.events(), [])

    def test_refuses_unpushed_commit_even_with_force(self):
        (self.mod / "demo.py").write_text("x = 2\n")
        git(self.mod, "commit", "-q", "-am", "local only")
        with self.assertRaisesRegex(DurehError, "not reachable from .* push it first"):
            sitelinks.retire_module(self.s, "desk", "demo", force=True)
        self.assertTrue(self.mod.is_dir())
        self.assertIn("demo", [e["id"] for e in self.config()["modules"]])

    def test_refuses_unreachable_source(self):
        cfg = self.config()
        next(e for e in cfg["modules"] if e["id"] == "demo")["source"] = str(self.tmp / "gone")
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        with self.assertRaisesRegex(DurehError, "source is unreachable"):
            sitelinks.retire_module(self.site(), "desk", "demo", force=True)

    def test_source_moved_on_is_still_reachable(self):
        (self.src / "demo.py").write_text("x = 3\n")
        git(self.src, "commit", "-q", "-am", "newer at the source")
        out = sitelinks.retire_module(self.s, "desk", "demo", dry_run=True)
        self.assertIsNone(out["refusal"], out)
        self.assertFalse(out["steps"][2]["unpushed"])

    def test_structural_reference_refused_unless_forced_history_listed(self):
        other = self.site_root / "modules" / "other"
        other.mkdir()
        (other / "pyproject.toml").write_text('[tool.uv.sources]\ndemo = { path = "../modules/demo" }\n')
        (self.site_root / "notes").mkdir(exist_ok=True)
        (self.site_root / "notes" / "history.md").write_text("modules/demo was absorbed in 2.0.0b1\n")
        with self.assertRaisesRegex(DurehError, "1 structural reference"):
            sitelinks.retire_module(self.s, "desk", "demo")
        out = sitelinks.retire_module(self.s, "desk", "demo", dry_run=True)
        refs = out["steps"][3]
        self.assertEqual(len(refs["structural"]), 1)
        self.assertIn("modules/other/pyproject.toml", refs["structural"][0])
        self.assertTrue(any("notes/history.md" in h for h in refs["history"]), refs)
        self.assertEqual(out["refusal"], refs["refusal"])
        out = sitelinks.retire_module(self.s, "desk", "demo", force=True)
        self.assertIsNone(out["refusal"])
        self.assertFalse(self.mod.exists())

    def test_config_json_outside_own_entry_is_structural(self):
        cfg = self.config()
        cfg["env"] = {"vars": {"DEMO_ROOT": "${SITE}/modules/demo"}}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        out = sitelinks.retire_module(self.site(), "desk", "demo", dry_run=True)
        self.assertEqual(len(out["steps"][3]["structural"]), 1, out["steps"][3])
        self.assertIn("env.vars.DEMO_ROOT", out["steps"][3]["structural"][0])
        self.assertIn("structural reference", out["refusal"])

    def test_dry_run_lists_steps_and_changes_nothing(self):
        wi = self.stopped_wi_with_worktree()
        before = (self.site_root / "config.json").read_text()
        n = len(Ledger(self.s).events())
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["--site", str(self.site_root), "site", "retire-module", "--id", "demo", "--dry-run"])
        self.assertEqual(code, 0, buf.getvalue())
        out = json.loads(buf.getvalue())
        self.assertTrue(out["dry_run"])
        self.assertEqual([s["step"] for s in out["steps"]],
                         ["registered", "no-live-work", "checkout", "references", "record", "remove", "regenerate"])
        self.assertEqual(out["steps"][5]["directory"], str(self.mod))
        self.assertEqual(out["steps"][5]["worktrees"], [str(self.s.worktrees / wi)])
        self.assertIn(f"link-module --id demo --source {self.src} --rev {git(self.mod, 'rev-parse', 'HEAD')}", out["compensation"])
        self.assertEqual((self.site_root / "config.json").read_text(), before)
        self.assertTrue(self.mod.is_dir())
        self.assertTrue((self.s.worktrees / wi).is_dir())
        self.assertEqual(len(Ledger(self.s).events()), n)

    def test_retire_removes_records_regenerates_and_round_trips(self):
        wi = self.stopped_wi_with_worktree()
        wt = self.s.worktrees / wi
        branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD")
        head = git(self.mod, "rev-parse", "HEAD")
        manifest = self.site_root / ".claude" / ".dureh-generated.json"
        os.utime(manifest, (1, 1))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["--site", str(self.site_root), "site", "retire-module", "--id", "demo"])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("Restore with: dureh site link-module --id demo", buf.getvalue())
        self.assertNotIn("demo", [e["id"] for e in self.config()["modules"]])
        self.assertEqual(self.config()["site"]["name"], "test-site", "other keys kept")
        self.assertFalse(self.mod.exists())
        self.assertFalse(wt.exists())
        ev = self.events()
        self.assertEqual(len(ev), 1)
        p = ev[0]["payload"]
        self.assertEqual((p["id"], p["path"], p["source"], p["branch"], p["head"], p["rev"]),
                         ("demo", "modules/demo", str(self.src), "main", head, self.entry["rev"]))
        self.assertEqual(p["removed"], {"config_entry": True, "worktrees": [str(wt)], "branches": [branch], "directory": True})
        self.assertEqual(p["compensation"], f"dureh site link-module --id demo --source {self.src} --rev {head}")
        self.assertGreater(manifest.stat().st_mtime, 1, "adapter files regenerated")
        for rel in json.loads(manifest.read_text())["files"]:
            if (self.site_root / rel).is_file():
                self.assertNotIn("modules/demo", (self.site_root / rel).read_text(), rel)
        # purge of the stopped WI still works after its worktree went with the module
        wf.purge(self.site(), "desk", wi)
        # round trip: the compensation command restores the entry at the recorded head
        restored = sitelinks.link_module(self.site(), "desk", "demo", str(self.src), head)
        self.assertEqual(restored["rev"], head)
        self.assertEqual(git(self.site_root / "modules" / "demo", "rev-parse", "HEAD"), head)
        self.assertIn("demo", [e["id"] for e in self.config()["modules"]])


if __name__ == "__main__":
    unittest.main()
