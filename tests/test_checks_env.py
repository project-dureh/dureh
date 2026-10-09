"""Release 46 (company finding 2026-10-09): a new WI worktree has no `.venv` (it is untracked), so declared checks
ran a bare `python` until the desk repaired the worktree by hand. `verify --wi` now picks the checks' environment
itself (workflow.checks_env_plan): the worktree's own `.venv`; else the target main tree's `.venv` when the
dependencies are the same (`shared-venv`, never synced, the worktree's code first on PYTHONPATH); else the target's
`env_rebuild` builds the worktree's own `.venv` before the checks (in the checks job); else nothing (as before)."""
import json
import subprocess
import sys
from pathlib import Path

from dureh_core import workflow as wf
from tests.helpers import SiteCase

PROBE = "printf '%s|%s|%s|%s' \"$VIRTUAL_ENV\" \"$PYTHONPATH\" \"$UV_NO_SYNC\" \"$(command -v python)\""


def fake_venv(root: Path) -> Path:
    v = root / ".venv"
    (v / "bin").mkdir(parents=True)
    (v / "pyvenv.cfg").write_text(f"home = {Path(sys.executable).parent}\n")
    (v / "bin" / "python").symlink_to(sys.executable)
    return v


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ChecksEnv(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.mod = self.make_module("demo")
        (self.mod / "uv.lock").write_text("lock v1\n")
        (self.mod / ".gitignore").write_text(".venv/\n")
        git(self.mod, "add", "-A")
        git(self.mod, "commit", "-qm", "lock")
        self.main_venv = fake_venv(self.mod)
        self.s = self.site()
        self.w = wf.wi_new(self.s, "desk", "Docs", "g", "doc-writer", "modules/demo", ["ok"])["id"]
        self.wt = Path(wf._create_worktree(self.s, self.w, "modules/demo")["worktree"])

    def probe(self, plan=None):
        r = wf._run_checks_local(self.s, self.w, self.wt, self.mod, [PROBE], 60, plan)
        self.assertEqual(r[0]["exit"], 0, r)
        return r[0]["tail"].split("|")

    def set_rebuild(self, cmd):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["modules"] = [{"id": "demo", "path": "modules/demo", "env_rebuild": cmd}]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()

    def test_new_worktree_shares_the_main_env_when_the_lock_is_the_same(self):
        self.assertFalse((self.wt / ".venv").exists(), "a new worktree has no environment")
        plan = wf.checks_env_plan(self.s, self.w, self.wt, self.mod)
        self.assertEqual(plan["mode"], "shared-venv")
        venv, pypath, nosync, python = self.probe()
        self.assertEqual(venv, str(self.main_venv))
        self.assertEqual(pypath.split(":")[0], str(self.wt / "src"), "the worktree's code is imported first")
        self.assertEqual(nosync, "1", "the main tree's environment is never synced from a check")
        self.assertEqual(python, str(self.main_venv / "bin" / "python"))
        self.assertFalse((self.wt / ".venv").exists(), "nothing was created in the worktree")

    def test_changed_dependencies_build_the_worktrees_own_env(self):
        (self.wt / "uv.lock").write_text("lock v2\n")
        self.assertEqual(wf.checks_env_plan(self.s, self.w, self.wt, self.mod)["mode"], "none",
                         "no env_rebuild: as before")
        self.set_rebuild(f"mkdir -p .venv/bin && printf 'home = {Path(sys.executable).parent}\\n' > .venv/pyvenv.cfg"
                         f" && ln -s {sys.executable} .venv/bin/python")
        plan = wf.checks_env_plan(self.s, self.w, self.wt, self.mod)
        self.assertEqual(plan["mode"], "rebuild")
        venv, _, _, python = self.probe()
        self.assertEqual(venv, str(self.wt / ".venv"))
        self.assertEqual(python, str(self.wt / ".venv" / "bin" / "python"))
        self.assertEqual(wf.checks_env_plan(self.s, self.w, self.wt, self.mod)["mode"], "own", "built once, then its own")

    def test_a_stale_main_env_is_not_shared(self):
        (self.main_venv / "pyvenv.cfg").write_text("home = /nonexistent/old-site/bin\n")
        self.assertEqual(wf.checks_env_plan(self.s, self.w, self.wt, self.mod)["mode"], "none")

    def test_the_checks_job_script_carries_the_plan(self):
        plan = wf.checks_env_plan(self.s, self.w, self.wt, self.mod)
        text = wf._checks_script(self.w, self.wt, self.mod, self.site_root, self.tmp / "p", ["true"], plan)
        self.assertIn(f"export VIRTUAL_ENV={self.main_venv}", text)
        self.assertIn("export UV_NO_SYNC=1", text)
        self.assertIn(f"export PYTHONPATH={self.wt / 'src'}", text)
        (self.wt / "uv.lock").write_text("lock v2\n")
        self.set_rebuild("uv sync --frozen")
        plan = wf.checks_env_plan(self.s, self.w, self.wt, self.mod)
        text = wf._checks_script(self.w, self.wt, self.mod, self.site_root, self.tmp / "p", ["true"], plan)
        self.assertIn("sh -c 'uv sync --frozen'", text)
        self.assertIn("exit 95", text)
        self.assertLess(text.index("uv sync"), text.index("check 1"), "the env is built before the checks")

    def test_the_main_tree_and_an_own_env_are_used_as_they_are(self):
        self.assertEqual(wf.checks_env_plan(self.s, self.w, self.mod, self.mod)["mode"], "own")
        fake_venv(self.wt)
        self.assertEqual(wf.checks_env_plan(self.s, self.w, self.wt, self.mod)["mode"], "own")

    def test_verify_runs_checks_in_the_shared_env_and_records_it(self):
        from dureh_core.records import Ledger

        led = Ledger(self.s)
        wf.wi_amend(self.s, "desk", self.w, "ok", {"checks": [f"test \"$VIRTUAL_ENV\" = {self.main_venv}"]})
        wf.wi_approve(self.s, "desk", self.w, "ok")
        brief = wf.dispatch_brief(self.s, self.w, "desk")
        led.event("wi.dispatched", "desk", self.w, {"role": "doc-writer", "brief": brief["digest"] if "digest" in brief
                                                    else "0" * 16, "by": "desk"})
        led.event("wi.staged", "desk", self.w, {"summary": "done"})
        r = wf.verify_wi(self.s, "checker", self.w)
        self.assertEqual(r["result"], "pass", r)
        self.assertEqual(r["checks_env"], "shared-venv")
