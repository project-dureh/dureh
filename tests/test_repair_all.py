"""Site folder-name independence, step 3 (proposals/site-rename-independence.md rows 13-14): after the site
directory is renamed, `worktree repair --all` re-links every WI worktree and rebuilds every stale virtualenv the
site owns, in one pass that never stops at an error; a dry run changes nothing; a second run finds nothing to do."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import SiteCase, hook

from dureh_core import cli
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.site import Site
from dureh_core.util import DurehError

# A fake env_rebuild: records its argv and environment, then writes a virtualenv whose files name the directory
# it runs in (`$PWD/.venv`, or $VIRTUAL_ENV when the wrapper names one: an exec tree's sibling).
REBUILD = '''#!/bin/sh
echo "$PWD $*" >> "$DUREH_TEST_LOG"
V="${UV_PROJECT_ENVIRONMENT:-$PWD/.venv}"
mkdir -p "$V/bin"
printf 'home = %s\\n' "$(dirname "$DUREH_TEST_PY")" > "$V/pyvenv.cfg"
printf "VIRTUAL_ENV='%s'\\nexport VIRTUAL_ENV\\n" "$V" > "$V/bin/activate"
printf '#!%s/bin/python\\nprint(1)\\n' "$V" > "$V/bin/pytest"
ln -sf "$DUREH_TEST_PY" "$V/bin/python"
'''


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def fake_venv(venv: Path, named: Path, home: str | None = None):
    """A virtualenv at `venv` whose files name the location `named` (the same path: valid; another: stale)."""
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "pyvenv.cfg").write_text(f"home = {home or os.path.dirname(sys.executable)}\nversion = 3.12\n")
    (venv / "bin" / "activate").write_text(f"VIRTUAL_ENV='{named}'\nexport VIRTUAL_ENV\n")
    (venv / "bin" / "pytest").write_text(f"#!{named}/bin/python\nprint(1)\n")  # scripts run the venv's python
    py = venv / "bin" / "python"
    if py.is_symlink() or py.exists():
        py.unlink()
    os.symlink(sys.executable, py)  # as in a real venv: a link to the base interpreter


class RepairAll(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.mod = self.make_module()
        self.s = self.site()
        self.log = self.tmp / "rebuild.log"
        os.environ["DUREH_TEST_LOG"] = str(self.log)
        os.environ["DUREH_TEST_PY"] = sys.executable
        self.rebuild = self.tmp / "rebuild.sh"
        self.rebuild.write_text(REBUILD)
        self.rebuild.chmod(0o755)
        b = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        self.wi = wf.wi_new(self.s, "desk", "W", "g", "py-writer", "modules/demo", ["ok"], parent=b)["id"]
        for x in (b, self.wi):
            wf.wi_approve(self.s, "desk", x, "ok")
        hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
             tool_input={"subagent_type": "pm-modules", "prompt": wf.dispatch_brief(self.s, b, "desk")["brief"]})
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        self.wt = Path(wf.dispatch_brief(self.s, self.wi, "pm-modules")["worktree"])  # a real `git worktree add`
        wf.dispatch_stop(self.s, "desk", self.wi, "before the move")
        self.assertEqual(git(self.wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
                         f"wi/{self.s.site_id}/{self.wi}")

    def configure(self, env_rebuild=True):
        cfg = json.loads(self.s.config_path.read_text())
        cfg["modules"] = [{"id": "demo", "path": "modules/demo", **({"env_rebuild": str(self.rebuild)} if env_rebuild else {})}]
        self.s.config_path.write_text(json.dumps(cfg))
        self.s = Site(self.site_root)

    def rename(self):
        new = self.site_root.parent / "site-renamed"
        os.rename(self.site_root, new)
        self.old, self.site_root = self.site_root, new
        self.s = Site(new)
        self.wt = new / "worktrees" / self.wi
        return new

    def runs(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def rows(self, r, kind=None):
        return {row["name"]: row for row in r["items"] if kind is None or row["kind"] == kind}

    def test_rename_then_repair_all(self):
        fake_venv(self.wt / ".venv", self.wt / ".venv")  # valid before the rename
        fake_venv(self.mod / ".venv", self.mod / ".venv")
        self.configure()
        (self.wt / "src" / "demo.py").write_text("def f():\n    return 2\n")
        git(self.wt, "commit", "-qam", "work in progress")
        head = git(self.wt, "rev-parse", "HEAD").stdout.strip()
        new = self.rename()
        self.assertNotEqual(git(self.wt, "status", check=False).returncode, 0)  # broken by the rename
        self.assertTrue(wf.stale_venv(self.wt))
        self.assertTrue(wf.stale_venv(new / "modules" / "demo"))

        # dry run: says what would change, changes nothing
        events = len(Ledger(self.s).events())
        d = wf.worktree_repair_all(self.s, "desk", dry_run=True)
        rows = self.rows(d)
        self.assertEqual((rows[self.wi]["state"], rows[self.wi]["env"]), ("repaired", "rebuilt"))
        self.assertEqual(rows["modules/demo"]["state"], "rebuilt")
        self.assertEqual(d["summary"]["worktrees"]["repaired"], 1)
        self.assertEqual(d["summary"]["venvs"]["rebuilt"], 2)
        self.assertEqual(self.runs(), [])
        self.assertNotEqual(git(self.wt, "status", check=False).returncode, 0)
        self.assertEqual(len(Ledger(self.s).events()), events)

        r = wf.worktree_repair_all(self.s, "desk")
        rows = self.rows(r)
        self.assertEqual((rows[self.wi]["state"], rows[self.wi]["env"]), ("repaired", "rebuilt"))
        self.assertEqual(rows["modules/demo"]["state"], "rebuilt")
        self.assertEqual(r["summary"]["worktrees"],
                         {"repaired": 1, "pruned": 0, "unchanged": 0, "skipped": 0, "error": 0})
        self.assertEqual(r["summary"]["venvs"], {"rebuilt": 2, "unchanged": 0, "skipped": 0, "error": 0})
        # both pointers name the new location and git works in the worktree again
        self.assertEqual((self.wt / ".git").read_text().strip(),
                         f"gitdir: {new / 'modules' / 'demo' / '.git' / 'worktrees' / self.wi}")
        self.assertEqual(Path(new / "modules" / "demo" / ".git" / "worktrees" / self.wi / "gitdir").read_text().strip(),
                         str(self.wt / ".git"))
        self.assertEqual(git(self.wt, "status").returncode, 0)
        self.assertEqual(git(self.wt, "rev-parse", "HEAD").stdout.strip(), head)
        self.assertNotIn("prunable", git(new / "modules" / "demo", "worktree", "list").stdout)
        # the configured env_rebuild ran once per stale environment, in that environment's tree
        self.assertEqual(sorted(self.runs()), sorted([f"{self.wt} ", f"{new / 'modules' / 'demo'} "]))
        self.assertIsNone(wf.stale_venv(self.wt))
        self.assertIsNone(wf.stale_venv(new / "modules" / "demo"))
        led = Ledger(self.s)
        self.assertEqual([e["payload"]["state"] for e in led.events(self.wi) if e["type"] == "worktree.repaired"],
                         ["repaired"])
        self.assertEqual(len([e for e in led.events() if e["type"] == "env.repaired"]), 1)
        alls = [e for e in led.events() if e["type"] == "worktree.repair-all"]
        self.assertEqual(len(alls), 1)
        self.assertEqual(alls[0]["payload"]["worktrees"]["repaired"], 1)
        self.assertEqual(alls[0]["payload"]["venvs"]["rebuilt"], 2)

        # a second run finds nothing to do and writes no repair records
        r2 = wf.worktree_repair_all(self.s, "desk")
        rows = self.rows(r2)
        self.assertEqual((rows[self.wi]["state"], rows[self.wi]["env"]), ("unchanged", "unchanged"))
        self.assertEqual(rows["modules/demo"]["state"], "unchanged")
        self.assertEqual(r2["summary"]["worktrees"]["unchanged"], 1)
        self.assertEqual(r2["summary"]["venvs"]["unchanged"], 2)
        self.assertEqual(len(self.runs()), 2)
        self.assertEqual(len([e for e in led.events() if e["type"] in ("worktree.repaired", "env.repaired")]), 2)
        self.assertEqual(len([e for e in led.events() if e["type"] == "worktree.repair-all"]), 2)

    def test_a_venv_already_at_the_new_location_is_not_rebuilt(self):
        self.configure()
        new = self.rename()
        fake_venv(self.wt / ".venv", self.wt / ".venv")  # written after the rename: already correct
        fake_venv(new / "modules" / "demo" / ".venv", new / "modules" / "demo" / ".venv")
        r = wf.worktree_repair_all(self.s, "desk")
        rows = self.rows(r)
        self.assertEqual((rows[self.wi]["state"], rows[self.wi]["env"]), ("repaired", "unchanged"))
        self.assertEqual(rows["modules/demo"]["state"], "unchanged")
        self.assertEqual(self.runs(), [])
        # --force rebuilds them anyway
        r = wf.worktree_repair_all(self.s, "desk", force=True)
        self.assertEqual(r["summary"]["venvs"]["rebuilt"], 2)
        self.assertEqual(len(self.runs()), 2)

    def test_activate_alone_marks_stale_and_no_env_rebuild_is_skipped(self):
        new = self.rename()
        fake_venv(self.wt / ".venv", self.wt / ".venv")
        (self.wt / ".venv" / "bin" / "pytest").write_text(f"#!{sys.executable}\n")  # a system shebang...
        self.assertIsNone(wf.stale_venv(self.wt))
        (self.wt / ".venv" / "bin" / "activate").write_text(f"VIRTUAL_ENV=\"{self.old / 'worktrees' / self.wi / '.venv'}\"\n")
        self.assertEqual(wf.stale_venv(self.wt), str(self.old / "worktrees" / self.wi / ".venv"))  # ...but activate is old
        fake_venv(new / "modules" / "demo" / ".venv", new / "modules" / "demo" / ".venv", home=str(self.old / "py"))
        self.assertEqual(wf.stale_venv(new / "modules" / "demo"), str(self.old / "py"))  # pyvenv.cfg home is gone
        r = wf.worktree_repair_all(self.s, "desk")  # no env_rebuild configured
        rows = self.rows(r)
        self.assertEqual((rows[self.wi]["state"], rows[self.wi]["env"]), ("repaired", "skipped"))
        self.assertEqual(rows[self.wi]["env_reason"], "no env_rebuild")
        self.assertEqual((rows["modules/demo"]["state"], rows["modules/demo"]["reason"]), ("skipped", "no env_rebuild"))
        self.assertEqual(r["summary"]["venvs"]["skipped"], 2)
        self.assertEqual(r["summary"]["venvs"]["error"], 0)

    def test_gone_worktree_is_pruned_and_errors_do_not_stop_the_pass(self):
        import shutil

        self.configure()
        new = self.rename()
        shutil.rmtree(self.wt)  # registered in modules/demo at the old path, gone on disk
        (new / "worktrees" / "WI-9999").mkdir()  # a directory with no record
        exec_dir = new / "scratch" / "exec"
        tree = exec_dir / f"{self.wi}-demo-abcdef0-20261006T120000000000Z"
        tree.mkdir(parents=True)
        fake_venv(Path(f"{tree}.venv"), self.old / "scratch" / "exec" / f"{tree.name}.venv")  # stale exec sibling
        other = exec_dir / f"{self.wi}-ghost-abcdef0-20261006T120000000000Z"
        fake_venv(Path(f"{other}.venv"), Path(f"{other}.venv"))  # its tree is gone
        r = wf.worktree_repair_all(self.s, "desk")
        rows = self.rows(r)
        self.assertEqual(rows[self.wi]["state"], "pruned")
        self.assertNotIn(self.wi, git(new / "modules" / "demo", "worktree", "list").stdout)
        self.assertEqual((rows["WI-9999"]["state"], rows["WI-9999"]["error"]), ("error", "no work-item record"))
        ex = rows[f"scratch/exec/{tree.name}.venv"]
        self.assertEqual((ex["state"], ex["target"]), ("rebuilt", "modules/demo"))
        self.assertEqual(self.runs(), [f"{tree} "])
        self.assertIsNone(wf.venv_stale(Path(f"{tree}.venv")))
        self.assertEqual(rows[f"scratch/exec/{other.name}.venv"]["state"], "skipped")
        self.assertEqual(r["summary"]["worktrees"], {"repaired": 0, "pruned": 1, "unchanged": 0, "skipped": 0, "error": 1})
        self.assertEqual(r["summary"]["venvs"]["rebuilt"], 1)
        self.assertEqual(r["summary"]["venvs"]["skipped"], 1)

    def test_failed_rebuild_is_an_error_row_not_a_stop(self):
        cfg = json.loads(self.s.config_path.read_text())
        cfg["modules"] = [{"id": "demo", "path": "modules/demo", "env_rebuild": "echo no network >&2; exit 3"}]
        self.s.config_path.write_text(json.dumps(cfg))
        new = self.rename()
        fake_venv(self.wt / ".venv", self.old / "worktrees" / self.wi / ".venv")
        fake_venv(new / "modules" / "demo" / ".venv", self.old / "modules" / "demo" / ".venv")
        r = wf.worktree_repair_all(self.s, "desk")
        rows = self.rows(r)
        self.assertEqual(rows[self.wi]["state"], "error")
        self.assertIn("no network", rows[self.wi]["error"])
        self.assertEqual(rows["modules/demo"]["state"], "error")
        self.assertEqual(r["summary"]["venvs"]["error"], 1)
        self.assertEqual(r["summary"]["worktrees"]["error"], 1)
        self.assertEqual(git(self.wt, "status").returncode, 0)  # the git half of the worktree repair happened

    def test_envs_only_and_no_envs(self):
        self.configure()
        new = self.rename()
        fake_venv(self.wt / ".venv", self.old / "worktrees" / self.wi / ".venv")
        r = wf.worktree_repair_all(self.s, "desk", envs=False)
        rows = self.rows(r)
        self.assertEqual(rows[self.wi]["state"], "repaired")
        self.assertNotIn("env", rows[self.wi])
        self.assertEqual(self.runs(), [])
        self.assertEqual([x["kind"] for x in r["items"]], ["worktree"])
        r = wf.worktree_repair_all(self.s, "desk", envs_only=True)
        rows = self.rows(r)
        self.assertEqual(rows[f"worktrees/{self.wi}"]["state"], "rebuilt")
        self.assertEqual(r["summary"]["worktrees"], {"repaired": 0, "pruned": 0, "unchanged": 0, "skipped": 0, "error": 0})
        self.assertEqual(self.runs(), [f"{self.wt} "])

    def test_cli_prints_a_table_and_a_summary(self):
        import io
        from contextlib import redirect_stdout

        self.configure()
        new = self.rename()
        fake_venv(self.wt / ".venv", self.old / "worktrees" / self.wi / ".venv")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["--site", str(new), "worktree", "repair", "--all", "--dry-run"]), 0)
        text = out.getvalue()
        self.assertIn("would repair", text)
        self.assertIn("env would rebuild", text)
        self.assertIn("dry run: nothing was changed or recorded", text)
        self.assertEqual(self.runs(), [])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["--site", str(new), "worktree", "repair", "--all"]), 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith(f"worktree  {self.wi}"), lines)
        self.assertIn("repaired", lines[0])
        self.assertIn("env rebuilt", lines[0])
        self.assertTrue(lines[-1].startswith("summary: worktrees 1 repaired"), lines[-1])
        self.assertIn("environments 1 rebuilt", lines[-1])
        with self.assertRaisesRegex(DurehError, "neither --wi nor --target"):
            a = cli.parser().parse_args(["--site", str(new), "worktree", "repair", "--all", "--wi", self.wi])
            a.func(a)

    def test_stale_venv_is_recreated_from_scratch_not_synced_in_place(self):
        """Release 43 gap 1: a sync into the old directory keeps the stale console scripts; the repair moves the
        stale environment aside and the rebuild starts from nothing (the old contents are gone afterwards)."""
        self.configure()
        venv = self.wt / ".venv"
        fake_venv(venv, Path("/old/site/worktrees") / self.wi / ".venv", home="/old/site/py/bin")
        (venv / "lib").mkdir()
        (venv / "lib" / "old-package.txt").write_text("installed before the move\n")  # what `uv sync` would keep
        old_venv = f"/old/site/worktrees/{self.wi}/.venv"
        self.assertEqual(wf.stale_venv(self.wt), old_venv)  # activate, the first bin/ entry, names the old place
        r = wf.worktree_repair(self.s, "desk", self.wi)
        self.assertEqual((r["state"], r["env"]), ("ok", "rebuilt"))
        self.assertEqual(r["stale_interpreter"], old_venv)
        self.assertTrue(r["moved_aside"].startswith(f"{venv}.stale-"), r)
        self.assertTrue(r["moved_aside"].endswith(" (removed)"), r)
        self.assertEqual(self.runs(), [f"{self.wt} "])
        self.assertIsNone(wf.stale_venv(self.wt))
        self.assertFalse((venv / "lib" / "old-package.txt").exists())  # recreated, not patched in place
        self.assertEqual([p.name for p in self.wt.iterdir() if p.name.startswith(".venv")], [".venv"])  # no leftover
        ev = [e for e in Ledger(self.s).events(self.wi) if e["type"] == "worktree.repaired"][-1]
        self.assertEqual(ev["payload"]["env"], "rebuilt")
        self.assertIn("moved_aside", ev["payload"])

    def test_failed_rebuild_keeps_the_stale_copy_and_names_it(self):
        cfg = json.loads(self.s.config_path.read_text())
        cfg["modules"] = [{"id": "demo", "path": "modules/demo", "env_rebuild": "echo no network >&2; exit 3"}]
        self.s.config_path.write_text(json.dumps(cfg))
        self.s = Site(self.site_root)
        venv = self.wt / ".venv"
        fake_venv(venv, Path("/old/site/worktrees") / self.wi / ".venv")
        with self.assertRaisesRegex(DurehError, r"no network.*moved to .*\.venv\.stale-") as cm:
            wf.worktree_repair(self.s, "desk", self.wi)
        aside = [p for p in self.wt.iterdir() if p.name.startswith(".venv.stale-")]
        self.assertEqual(len(aside), 1, aside)
        self.assertIn(str(aside[0]), str(cm.exception))
        self.assertTrue((aside[0] / "bin" / "pytest").is_file())  # the old environment is still there to inspect
        self.assertFalse(venv.exists())
        # once a rebuild succeeds (here: the network is back), the leftover copy goes too
        self.configure()
        r = wf.worktree_repair(self.s, "desk", self.wi)
        self.assertEqual((r["env"], r["missing"]), ("rebuilt", True))
        self.assertEqual([p.name for p in self.wt.iterdir() if p.name.startswith(".venv")], [".venv"])

    def test_missing_worktree_venv_is_created(self):
        """Release 43 gap 2: a worktree with no .venv at all gets one (through the worktree pass and --envs-only),
        a dry run only says so, and without env_rebuild nothing is created."""
        venv = self.wt / ".venv"
        self.assertFalse(venv.exists())
        r = wf.worktree_repair_all(self.s, "desk")  # no env_rebuild configured: not a problem to report
        rows = self.rows(r)
        self.assertEqual((rows[self.wi]["state"], rows[self.wi]["env"]), ("unchanged", "unchanged"))
        self.assertEqual(self.runs(), [])
        self.assertFalse(venv.exists())

        self.configure()
        d = wf.worktree_repair_all(self.s, "desk", dry_run=True)
        rows = self.rows(d)
        self.assertEqual((rows[self.wi]["state"], rows[self.wi]["env"]), ("unchanged", "rebuilt"))
        self.assertEqual(rows[self.wi]["env_reason"], "created (no .venv)")
        self.assertFalse(venv.exists())
        self.assertEqual(self.runs(), [])

        r = wf.worktree_repair_all(self.s, "desk")
        rows = self.rows(r)
        self.assertEqual((rows[self.wi]["state"], rows[self.wi]["env"]), ("unchanged", "rebuilt"))
        self.assertEqual(rows[self.wi]["env_reason"], "created (no .venv)")
        self.assertEqual(r["summary"]["venvs"]["rebuilt"], 1)
        self.assertTrue(venv.is_dir())
        self.assertIsNone(wf.stale_venv(self.wt))
        self.assertEqual(self.runs(), [f"{self.wt} "])
        led = Ledger(self.s)
        ev = [e for e in led.events(self.wi) if e["type"] == "worktree.repaired"]
        self.assertEqual((ev[-1]["payload"]["env"], ev[-1]["payload"]["missing"]), ("rebuilt", True))
        # a second run finds nothing to do
        r2 = wf.worktree_repair_all(self.s, "desk")
        self.assertEqual(self.rows(r2)[self.wi]["env"], "unchanged")
        self.assertEqual(len(self.runs()), 1)

        # the env pass alone (--envs-only) lists a worktree without a .venv too
        import shutil
        shutil.rmtree(venv)
        r3 = wf.worktree_repair_all(self.s, "desk", envs_only=True)
        row = self.rows(r3)[f"worktrees/{self.wi}"]
        self.assertEqual((row["state"], row["detail"]), ("rebuilt", "created (no .venv)"))
        self.assertTrue(venv.is_dir())
        self.assertEqual(len(self.runs()), 2)
        self.assertEqual(len([e for e in Ledger(self.s).events() if e["type"] == "env.repaired"]), 1)

    def test_only_the_desk(self):
        with self.assertRaisesRegex(DurehError, "only the desk"):
            wf.worktree_repair_all(self.s, "py-writer")


if __name__ == "__main__":
    unittest.main()
