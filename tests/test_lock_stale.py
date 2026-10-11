"""Release 51 (UR-20261011-01): an environment whose interpreter is in place can still miss packages or extras
the lock declares (the main tree's `.venv` was synced without an extra; `worktree repair --target` said `env ok`,
and `verify --wi` shared that environment because the lock was byte-identical). `worktree repair` now also asks
`uv sync --check` (workflow.lock_stale) once per environment, rebuilds a lock-stale one alone, and never shares a
lock-stale main environment with a worktree (checks_env_plan, provision_worktree_env). `--force`, `--dry-run` and
`--json` go with `--target` and `--wi` too. No network: `uv` is a fake script on PATH."""
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

from dureh_core import cli
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError
from tests.helpers import SiteCase

# A fake `uv`: `uv sync ... --check` logs and exits as $DUREH_TEST_LOCK says (stale: 1 with a message, else 0);
# a plain `uv sync ...` logs and makes a virtualenv at $UV_PROJECT_ENVIRONMENT or $PWD/.venv.
FAKE_UV = '''#!/bin/sh
echo "$PWD $* nosync=${UV_NO_SYNC-unset}" >> "$DUREH_TEST_LOG"
case " $* " in
  *" --check "*)
    if [ "$DUREH_TEST_LOCK" = stale ]; then
      echo "Resolved 12 packages in 3ms" >&2
      echo "The environment is outdated: matplotlib==3.9.2 would be installed" >&2
      exit 1
    fi
    exit 0 ;;
esac
V="${UV_PROJECT_ENVIRONMENT:-$PWD/.venv}"
mkdir -p "$V/bin"
printf 'home = %s\\n' "$(dirname "$DUREH_TEST_PY")" > "$V/pyvenv.cfg"
ln -sf "$DUREH_TEST_PY" "$V/bin/python"
'''


def fake_venv(root: Path) -> Path:
    """A valid (for venv_stale) environment at <root>/.venv."""
    v = root / ".venv"
    (v / "bin").mkdir(parents=True)
    (v / "pyvenv.cfg").write_text(f"home = {Path(sys.executable).parent}\n")
    (v / "bin" / "python").symlink_to(sys.executable)
    return v


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class LockStale(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.mod = self.make_module("demo")
        (self.mod / "uv.lock").write_text("lock v1\n")
        (self.mod / ".gitignore").write_text(".venv/\n.venv.*\n")
        git(self.mod, "add", "-A")
        git(self.mod, "commit", "-qm", "lock")
        self.main_venv = fake_venv(self.mod)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "uv").write_text(FAKE_UV)
        (bindir / "uv").chmod(0o755)
        os.environ["PATH"] = os.pathsep.join([str(bindir), os.environ.get("PATH", "")])
        self.log = self.tmp / "uv.log"
        os.environ["DUREH_TEST_LOG"] = str(self.log)
        os.environ["DUREH_TEST_PY"] = sys.executable
        os.environ["DUREH_TEST_LOCK"] = "ok"
        self.configure("uv sync --frozen --all-extras")
        self.w = wf.wi_new(self.s, "desk", "Docs", "g", "doc-writer", "modules/demo", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", self.w, "ok")
        self.wt = self.s.worktrees / self.w

    def configure(self, cmd):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["modules"] = [{"id": "demo", "path": "modules/demo", "env_rebuild": cmd}]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()

    def runs(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["--site", str(self.site_root), "worktree", "repair", *args])
        return rc, out.getvalue()

    # --- the helper -------------------------------------------------------------------------------------------
    def test_check_command_is_the_rebuild_plus_check(self):
        for cmd, want in [("uv sync --frozen", "uv sync --frozen --check"), ("uv sync", "uv sync --check"),
                          ("cd sub && uv sync --all-groups", "cd sub && uv sync --all-groups --check"),
                          ("UV_CACHE_DIR=/c uv sync --extra report", "UV_CACHE_DIR=/c uv sync --extra report --check"),
                          ("uv sync --frozen && echo done", None), ("uv sync; true", None), ("uv pip sync r.txt", None),
                          ("pip install -e .", None), ("/x/rebuild.sh", None), ("", None), (None, None)]:
            self.assertEqual(wf.lock_check_command(cmd), want, cmd)

    def test_lock_stale_runs_the_check_in_the_root_without_uv_no_sync(self):
        seen = []

        def fake(cmd, root, env, timeout):
            seen.append((cmd, root, env, timeout))
            return 1, "Resolved\nThe environment is outdated\n"

        os.environ["UV_NO_SYNC"] = "1"
        os.environ["VIRTUAL_ENV"] = "/elsewhere/.venv"
        self.s = self.site()
        old = wf.run_lock_check
        wf.run_lock_check = fake
        try:
            reason = wf.lock_stale(self.s, self.mod, "modules/demo")
        finally:
            wf.run_lock_check = old
        self.assertEqual(len(seen), 1)
        cmd, root, env, timeout = seen[0]
        self.assertEqual((cmd, root, timeout), ("uv sync --frozen --all-extras --check", self.mod, wf.LOCK_CHECK_TIMEOUT))
        self.assertNotIn("UV_NO_SYNC", env, "the check must be allowed to answer")
        self.assertNotIn("VIRTUAL_ENV", env, "never the caller's environment")
        self.assertEqual(reason, "`uv sync --frozen --all-extras --check` exit 1: Resolved / The environment is outdated")
        # an environment elsewhere than <root>/.venv is named to uv, as the rebuild names it
        wf.run_lock_check = lambda cmd, root, env, timeout: (seen.append(env), (0, ""))[1]
        try:
            self.assertIsNone(wf.lock_stale(self.s, self.mod, "modules/demo", venv=self.tmp / "x.venv"))
        finally:
            wf.run_lock_check = old
        self.assertEqual(seen[-1]["UV_PROJECT_ENVIRONMENT"], str(self.tmp / "x.venv"))

    # --- worktree repair --target -------------------------------------------------------------------------------
    def test_a_lock_stale_main_env_is_rebuilt_alone_and_the_result_says_why(self):
        r = wf.env_repair(self.s, "desk", "modules/demo")
        self.assertEqual(r, {"target": "modules/demo", "env": "ok", "lock_check": "ok"})
        self.assertEqual(self.runs(), [f"{self.mod} sync --frozen --all-extras --check nosync=unset"])
        os.environ["DUREH_TEST_LOCK"] = "stale"
        r = wf.env_repair(self.s, "desk", "modules/demo")
        self.assertEqual((r["env"], r["stale_interpreter"], r["lock_check"]), ("rebuilt", None, "stale"))
        self.assertIn("exit 1: Resolved 12 packages in 3ms / The environment is outdated: matplotlib==3.9.2", r["stale_lock"])
        self.assertEqual(self.runs()[1:], [f"{self.mod} sync --frozen --all-extras --check nosync=unset",
                                           f"{self.mod} sync --frozen --all-extras nosync=unset"], "one check, one rebuild")
        self.assertTrue((self.main_venv / "bin" / "python").exists())
        self.assertFalse(list(self.mod.glob(".venv.stale-*")), "the copy moved aside is removed after success")
        ev = [e for e in Ledger(self.s).events() if e["type"] == "env.repaired"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["payload"]["lock_check"], "stale")
        self.assertIn("exit 1", ev[0]["payload"]["stale_lock"])

    def test_a_non_uv_rebuild_command_keeps_the_old_behaviour(self):
        self.configure(str(self.tmp / "rebuild.sh"))
        r = wf.env_repair(self.s, "desk", "modules/demo")
        self.assertEqual(r, {"target": "modules/demo", "env": "ok", "lock_check": "unavailable"})
        self.assertEqual(self.runs(), [], "nothing ran")
        (self.main_venv / "pyvenv.cfg").write_text("home = /nonexistent/old/bin\n")  # the interpreter check still decides
        r = wf.env_repair(self.s, "desk", "modules/demo", dry_run=True)
        self.assertEqual((r["env"], r["stale_interpreter"], r.get("stale_lock"), r.get("lock_check")),
                         ("rebuilt", "/nonexistent/old/bin", None, None))

    def test_force_and_dry_run_with_target(self):
        r = wf.env_repair(self.s, "desk", "modules/demo", dry_run=True)
        self.assertEqual(r["env"], "ok")
        os.environ["DUREH_TEST_LOCK"] = "stale"
        r = wf.env_repair(self.s, "desk", "modules/demo", dry_run=True)
        self.assertEqual((r["env"], r["dry_run"], r["lock_check"]), ("rebuilt", True, "stale"))
        self.assertEqual(len(self.runs()), 2, "two checks, no rebuild")
        self.assertFalse(list(self.mod.glob(".venv.stale-*")))
        self.assertEqual([e for e in Ledger(self.s).events() if e["type"] == "env.repaired"], [], "a dry run records nothing")
        os.environ["DUREH_TEST_LOCK"] = "ok"
        r = wf.env_repair(self.s, "desk", "modules/demo", force=True)
        self.assertEqual((r["env"], r["stale_interpreter"], r["stale_lock"]), ("rebuilt", None, None))
        self.assertNotIn("lock_check", r, "a forced rebuild does not ask")
        self.assertEqual(self.runs()[2:], [f"{self.mod} sync --frozen --all-extras nosync=unset"])

    def test_cli_force_dry_run_and_json_with_target_and_wi(self):
        rc, out = self.cli("--target", "modules/demo", "--dry-run")
        self.assertEqual((rc, json.loads(out)["env"]), (0, "ok"))
        os.environ["DUREH_TEST_LOCK"] = "stale"
        rc, out = self.cli("--target", "modules/demo", "--dry-run", "--json")
        self.assertEqual((rc, json.loads(out)["env"], json.loads(out)["dry_run"]), (0, "rebuilt", True))
        self.assertEqual(len(self.runs()), 2, "no rebuild ran")
        rc, out = self.cli("--target", "modules/demo", "--force")
        self.assertEqual((rc, json.loads(out)["env"]), (0, "rebuilt"))
        self.assertEqual(self.runs()[-1], f"{self.mod} sync --frozen --all-extras nosync=unset")
        # --wi: the worktree's own environment, forced
        os.environ["DUREH_TEST_LOCK"] = "ok"
        wf._create_worktree(self.s, self.w, "modules/demo")
        fake_venv(self.wt)
        rc, out = self.cli("--wi", self.w, "--dry-run")
        self.assertEqual((rc, json.loads(out)["state"], json.loads(out)["env"]), (0, "ok", "ok"))
        rc, out = self.cli("--wi", self.w, "--force")
        r = json.loads(out)
        self.assertEqual((rc, r["state"], r["env"]), (0, "ok", "rebuilt"))
        self.assertEqual(self.runs()[-1], f"{self.wt} sync --frozen --all-extras nosync=unset")
        ev = [e for e in Ledger(self.s).events(self.w) if e["type"] == "worktree.repaired"]
        self.assertEqual((len(ev), ev[0]["payload"]["env"]), (1, "rebuilt"))
        # --force without --all, --target or --wi still fails; --envs-only/--no-envs stay --all-only
        with self.assertRaisesRegex(DurehError, "exactly one of --wi"):
            a = cli.parser().parse_args(["--site", str(self.site_root), "worktree", "repair", "--force"])
            a.func(a)
        with self.assertRaisesRegex(DurehError, "--envs-only and --no-envs go with --all"):
            a = cli.parser().parse_args(["--site", str(self.site_root), "worktree", "repair", "--target", "modules/demo",
                                         "--envs-only"])
            a.func(a)

    # --- the sharing rule ---------------------------------------------------------------------------------------
    def test_checks_env_plan_does_not_share_a_lock_stale_main_env(self):
        wf._create_worktree(self.s, self.w, "modules/demo")
        self.assertEqual(wf.checks_env_plan(self.s, self.w, self.wt, self.mod)["mode"], "shared-venv")
        os.environ["DUREH_TEST_LOCK"] = "stale"
        plan = wf.checks_env_plan(self.s, self.w, self.wt, self.mod)
        self.assertEqual((plan["mode"], plan["command"]), ("rebuild", "uv sync --frozen --all-extras"))
        self.assertTrue(plan["main_env"].startswith("lock-stale: `uv sync --frozen --all-extras --check` exit 1"), plan)
        self.assertEqual(self.runs(), [f"{self.mod} sync --frozen --all-extras --check nosync=unset"] * 2,
                         "the check runs in the main tree, once per plan")

    def test_dispatch_does_not_share_a_lock_stale_main_env(self):
        os.environ["DUREH_TEST_LOCK"] = "stale"
        r = wf.dispatch_brief(self.s, self.w, "desk")
        self.assertEqual(r["worktree_env"]["env"], "rebuilt")
        self.assertIn("lock-stale", r["worktree_env"]["main_env"])
        self.assertFalse((self.wt / ".venv" / wf.SHARED_MARK).exists())
        self.assertEqual(self.runs(), [f"{self.mod} sync --frozen --all-extras --check nosync=unset",
                                       f"{self.wt} sync --frozen --all-extras nosync=unset"])
        self.assertEqual(wf.checks_env_plan(self.s, self.w, self.wt, self.mod)["mode"], "own")

    def test_repair_all_rebuilds_only_the_lock_stale_env(self):
        """The company case: one main environment lacking an extra among valid ones (62 rebuilt for one)."""
        other = self.make_module("other")
        fake_venv(other)
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["modules"].append({"id": "other", "path": "modules/other", "env_rebuild": str(self.tmp / "rebuild.sh")})
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        wf.dispatch_brief(self.s, self.w, "desk")  # the worktree gets the shared form
        self.assertTrue((self.wt / ".venv" / wf.SHARED_MARK).is_file())
        os.environ["DUREH_TEST_LOCK"] = "stale"
        r = wf.worktree_repair_all(self.s, "desk", dry_run=True)
        rows = {row["name"]: row for row in r["items"]}
        self.assertEqual(rows["modules/demo"]["state"], "rebuilt")
        self.assertIn("exit 1", rows["modules/demo"]["stale_lock"])
        self.assertEqual(rows["modules/other"]["state"], "unchanged")  # no uv sync: lock check unavailable
        self.assertEqual((rows[self.w]["state"], rows[self.w]["env"]), ("unchanged", "unchanged"))  # the shared form is not asked
        self.assertEqual(r["summary"]["venvs"]["rebuilt"], 1)
        self.assertEqual(len(self.runs()), 2, "the check ran once for the main env (dispatch) and once in the pass")
        r = wf.worktree_repair_all(self.s, "desk")
        self.assertEqual(r["summary"]["venvs"]["rebuilt"], 1)
        self.assertEqual(self.runs()[-1], f"{self.mod} sync --frozen --all-extras nosync=unset")
        out = "\n".join(cli.repair_all_lines(r))
        self.assertIn("rebuilt", out)
        self.assertIn("The environment is outdated", out)
