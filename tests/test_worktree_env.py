"""Release 47 (UR-20261009-01): a new WI worktree of a target with `env_rebuild` gets its Python environment at
`dispatch brief`, so the worker's own commands have one: the shared form of the main tree's environment when the
lock is the same and the main env is not stale (a small `.venv` layered on it; installs land in the worktree's own
`.venv`), else its own `.venv` built with `env_rebuild` (bounded by `env.rebuild_timeout`; a failure refuses the
brief with the `worktree repair` hint and is recorded). `verify --wi` refuses a module target with `env_rebuild`
and no environment instead of running a bare python. No network: the fake env_rebuild makes a `python -m venv`."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError
from tests.helpers import SiteCase

# A fake env_rebuild: logs where it ran, then makes a real (pip-less, offline) virtualenv in $PWD/.venv.
REBUILD = f'''#!/bin/sh
echo "$PWD" >> "$DUREH_TEST_LOG"
{sys.executable} -m venv --without-pip .venv
'''


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def purelib(venv: Path) -> Path:
    return Path(subprocess.run([str(venv / "bin" / "python"), "-I", "-c",
                                "import sysconfig; print(sysconfig.get_path('purelib'))"],
                               check=True, capture_output=True, text=True).stdout.strip())


def py(venv: Path, code: str, cwd: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")}
    return subprocess.run([str(venv / "bin" / "python"), "-c", code], cwd=cwd, check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


class WorktreeEnv(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.mod = self.make_module("demo")
        (self.mod / "uv.lock").write_text("lock v1\n")
        (self.mod / ".gitignore").write_text(".venv/\n.venv.*\n")
        git(self.mod, "add", "-A")
        git(self.mod, "commit", "-qm", "lock")
        # the main tree's environment: a real venv with one dependency and an editable install of the main tree
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(self.mod / ".venv")], check=True)
        self.main_sp = purelib(self.mod / ".venv")
        (self.main_sp / "depmod.py").write_text("NAME = 'dependency from the main env'\n")
        (self.main_sp / "_editable_demo.pth").write_text(f"{self.mod / 'src'}\n")
        self.log = self.tmp / "rebuild.log"
        os.environ["DUREH_TEST_LOG"] = str(self.log)
        self.rebuild = self.tmp / "rebuild.sh"
        self.rebuild.write_text(REBUILD)
        self.rebuild.chmod(0o755)
        self.configure(str(self.rebuild))
        self.w = wf.wi_new(self.s, "desk", "Docs", "g", "doc-writer", "modules/demo", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", self.w, "ok")
        self.wt = self.s.worktrees / self.w

    def configure(self, cmd=None, timeout=None):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["modules"] = [{"id": "demo", "path": "modules/demo", **({"env_rebuild": cmd} if cmd else {})}]
        if timeout is not None:
            cfg.setdefault("env", {})["rebuild_timeout"] = timeout
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()

    def runs(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def events(self, type_):
        return [e for e in Ledger(self.s).events(self.w) if e["type"] == type_]

    def stage(self, checks):
        led = Ledger(self.s)
        wf.wi_amend(self.s, "desk", self.w, "ok", {"checks": checks})
        led.event("wi.dispatched", "desk", self.w, {"role": "doc-writer", "brief": "0" * 16, "by": "desk"})
        led.event("wi.staged", "desk", self.w, {"summary": "done"})

    def test_same_lock_gives_the_worktree_the_shared_env_at_dispatch(self):
        r = wf.dispatch_brief(self.s, self.w, "desk")
        venv = self.wt / ".venv"
        self.assertEqual(r["worktree_env"]["env"], "shared")
        self.assertTrue((venv / wf.SHARED_MARK).is_file())
        self.assertFalse(venv.is_symlink(), "a real virtualenv of its own, not a link to the main env")
        self.assertEqual(self.runs(), [], "env_rebuild did not run: the main env is shared")
        self.assertIn(f"Python env: {venv} shares the target main tree's environment", r["brief"])
        self.assertEqual(self.events("dispatch.prepared")[-1]["payload"]["worktree_env"]["env"], "shared")
        # the worker's own commands: the worktree's code first, the main env's dependencies behind it
        (self.wt / "src" / "demo.py").write_text("def f():\n    return 2\n")
        self.assertEqual(py(venv, "import demo, depmod; print(demo.f(), depmod.NAME)", self.tmp),
                         "2 dependency from the main env")
        # an installer in the worktree targets the worktree's own prefix and site-packages, never the main env
        self.assertEqual(py(venv, "import sys; print(sys.prefix)", self.tmp), str(venv))
        self.assertNotEqual(purelib(venv), self.main_sp)
        self.assertNotIn(str(self.mod / ".venv"), (venv / "pyvenv.cfg").read_text())
        self.assertIsNone(wf.stale_venv(self.wt))
        # verify uses the main env as release 46 does, with UV_NO_SYNC
        plan = wf.checks_env_plan(self.s, self.w, self.wt, self.mod)
        self.assertEqual((plan["mode"], plan["worktree_env"], plan["exports"]["UV_NO_SYNC"]),
                         ("shared-venv", "shared", "1"))
        # a re-dispatch keeps the worktree's environment
        r2 = wf.dispatch_brief(self.s, self.w, "desk")
        self.assertEqual((r2["worktree_env"]["env"], r2["worktree_env"]["env_mode"]), ("existing", "shared-venv"))

    def test_verify_records_shared_venv_for_the_shared_form(self):
        wf.dispatch_brief(self.s, self.w, "desk")
        self.stage([f"test \"$VIRTUAL_ENV\" = {self.mod / '.venv'}"])
        r = wf.verify_wi(self.s, "checker", self.w)
        self.assertEqual((r["result"], r["checks_env"]), ("pass", "shared-venv"), r)

    def test_different_lock_builds_the_worktrees_own_env_before_the_brief(self):
        (self.mod / "uv.lock").write_text("lock v2 (uncommitted in the main tree)\n")
        r = wf.dispatch_brief(self.s, self.w, "desk")
        venv = self.wt / ".venv"
        self.assertEqual((r["worktree_env"]["env"], r["worktree_env"]["command"]), ("rebuilt", str(self.rebuild)))
        self.assertEqual(self.runs(), [str(self.wt)])
        self.assertTrue((venv / "bin" / "python").exists())
        self.assertFalse((venv / wf.SHARED_MARK).exists())
        self.assertIn("this worktree's own; built with env_rebuild", r["brief"])
        self.assertEqual(wf.checks_env_plan(self.s, self.w, self.wt, self.mod)["mode"], "own")

    def test_a_stale_main_env_is_not_shared(self):
        (self.mod / ".venv" / "pyvenv.cfg").write_text("home = /nonexistent/old-site/bin\n")
        r = wf.dispatch_brief(self.s, self.w, "desk")
        self.assertEqual(r["worktree_env"]["env"], "rebuilt")

    def test_no_env_rebuild_creates_nothing(self):
        self.configure(None)
        r = wf.dispatch_brief(self.s, self.w, "desk")
        self.assertNotIn("worktree_env", r)
        self.assertFalse((self.wt / ".venv").exists())
        self.assertNotIn("Python env:", r["brief"])

    def test_failed_build_refuses_the_brief_with_the_repair_hint_and_is_recorded(self):
        (self.mod / "uv.lock").write_text("lock v2\n")
        self.configure("mkdir -p .venv/bin; echo no network >&2; exit 3")
        with self.assertRaisesRegex(DurehError, r"no network.*No brief was prepared.*worktree repair --wi") as cm:
            wf.dispatch_brief(self.s, self.w, "desk")
        self.assertIn("partial environment was moved to", str(cm.exception))
        self.assertFalse((self.wt / ".venv").exists(), "no half-built .venv is left for the worker")
        self.assertEqual(self.events("dispatch.prepared"), [])
        ev = self.events("worktree.env-failed")
        self.assertEqual(len(ev), 1)
        self.assertIn("no network", ev[0]["payload"]["error"])
        # once the cause is fixed, the repair builds it and the leftover goes
        self.configure(str(self.rebuild))
        rep = wf.worktree_repair(self.s, "desk", self.w)
        self.assertEqual((rep["env"], rep["missing"], rep["env_mode"]), ("rebuilt", True, "rebuild"))
        self.assertEqual([p.name for p in self.wt.iterdir() if p.name.startswith(".venv")], [".venv"])
        self.assertEqual(wf.dispatch_brief(self.s, self.w, "desk")["worktree_env"]["env"], "existing")

    def test_the_build_is_bounded_by_env_rebuild_timeout(self):
        (self.mod / "uv.lock").write_text("lock v2\n")
        self.configure("sleep 5", timeout=1)
        with self.assertRaisesRegex(DurehError, r"timed out after 1 s \(config env.rebuild_timeout\)"):
            wf.dispatch_brief(self.s, self.w, "desk")

    def test_a_stale_shared_form_is_refused_by_verify_and_repaired(self):
        wf.dispatch_brief(self.s, self.w, "desk")
        venv = self.wt / ".venv"
        main_cfg = (self.mod / ".venv" / "pyvenv.cfg").read_text()
        (self.mod / ".venv" / "pyvenv.cfg").write_text("home = /nonexistent/old-site/bin\n")
        self.assertEqual(wf.stale_venv(self.wt), "/nonexistent/old-site/bin", "stale through the shared env")
        self.stage(["true"])
        with self.assertRaisesRegex(DurehError, r"stale virtualenv.*worktree repair --wi"):
            wf.verify_wi(self.s, "checker", self.w)
        # main env still stale: the repair gives the worktree its own env
        rep = wf.worktree_repair(self.s, "desk", self.w)
        self.assertEqual((rep["env"], rep["env_mode"]), ("rebuilt", "rebuild"))
        self.assertFalse((venv / wf.SHARED_MARK).exists())
        self.assertEqual([p.name for p in self.wt.iterdir() if p.name.startswith(".venv")], [".venv"])
        # main env repaired, worktree env removed: the repair shares again
        (self.mod / ".venv" / "pyvenv.cfg").write_text(main_cfg)
        shutil.rmtree(venv)
        rep = wf.worktree_repair(self.s, "desk", self.w)
        self.assertEqual((rep["env"], rep["env_mode"], rep["missing"]), ("rebuilt", "shared-venv", True))
        self.assertTrue((venv / wf.SHARED_MARK).is_file())
        self.assertEqual(len(self.runs()), 1)
        ev = self.events("worktree.repaired")
        self.assertEqual(ev[-1]["payload"]["env_mode"], "shared-venv")

    def test_a_moved_worktree_marks_the_shared_form_stale(self):
        wf.dispatch_brief(self.s, self.w, "desk")
        mark = self.wt / ".venv" / wf.SHARED_MARK
        data = json.loads(mark.read_text())
        data["root"] = "/old/site/worktrees/" + self.w
        mark.write_text(json.dumps(data))
        self.assertEqual(wf.stale_venv(self.wt), data["root"])
        rep = wf.worktree_repair(self.s, "desk", self.w)
        self.assertEqual(rep["env_mode"], "shared-venv")
        self.assertIsNone(wf.stale_venv(self.wt))

    def test_a_changed_lock_replaces_the_shared_form_for_the_checks(self):
        wf.dispatch_brief(self.s, self.w, "desk")
        (self.wt / "uv.lock").write_text("lock v2 (the worker added a dependency)\n")
        plan = wf.checks_env_plan(self.s, self.w, self.wt, self.mod)
        self.assertEqual((plan["mode"], plan.get("replace")), ("rebuild", True))
        text = wf._checks_script(self.w, self.wt, self.mod, self.site_root, self.tmp / "p", ["true"], plan)
        self.assertLess(text.index(f"rm -rf -- {self.wt / '.venv'}"), text.index(str(self.rebuild)))
        r = wf._run_checks_local(self.s, self.w, self.wt, self.mod, ['printf %s "$VIRTUAL_ENV"'], 60, plan)
        self.assertEqual(r[0]["tail"], str(self.wt / ".venv"))
        self.assertFalse((self.wt / ".venv" / wf.SHARED_MARK).exists(), "its own environment now")
        self.assertEqual(self.runs(), [str(self.wt)])

    def test_verify_refuses_a_module_target_without_any_env(self):
        self.stage(["python -c 'import demo'"])
        shutil.rmtree(self.mod / ".venv")  # no worktree, no main env: the checks would run a bare python
        self.assertEqual(wf.checks_env_plan(self.s, self.w, self.mod, self.mod)["mode"], "missing")
        with self.assertRaisesRegex(DurehError, r"worktree env missing: run worktree repair"):
            wf.verify_wi(self.s, "checker", self.w)
        self.assertEqual([r for r in Ledger(self.s).load("run") if r["wi"] == self.w], [], "nothing recorded")
        self.configure(None)  # without env_rebuild: as before
        self.assertEqual(wf.checks_env_plan(self.s, self.w, self.mod, self.mod)["mode"], "own")

    def test_cli_names_the_worktree_env(self):
        import contextlib
        import io

        from dureh_core import cli

        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = cli.main(["--site", str(self.site_root), "dispatch", "brief", "--wi", self.w])
        self.assertEqual(code, 0, err.getvalue())
        out = out.getvalue()
        self.assertIn(f"# worktree env: {self.wt / '.venv'} shares {self.mod / '.venv'}", out)
