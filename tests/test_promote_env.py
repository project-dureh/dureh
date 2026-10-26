"""Release 81 (finding c3350543): `wi promote` checks the target main tree's `.venv` after the merge, as `worktree
repair --target` does, and rebuilds it with the target's env_rebuild when it is stale (for a `uv sync`: when `uv
sync --check` says it does not match the lock; for another env_rebuild: when the merge changed a dependency file).
No env_rebuild, or no main `.venv`: nothing runs, as before. A failed rebuild leaves the promotion standing. No
network: `uv` is a fake script on PATH."""
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

from dureh_core import cli
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from tests.helpers import SiteCase, hook
from tests.test_lock_stale import FAKE_UV, fake_venv, git

# another env_rebuild (not a `uv sync`, so no lock check): logs and makes a virtualenv in $PWD/.venv
FAKE_REBUILD = '''#!/bin/sh
echo "$PWD rebuild" >> "$DUREH_TEST_LOG"
[ -n "$DUREH_TEST_FAIL" ] && { echo "no network" >&2; exit 3; }
mkdir -p .venv/bin
printf 'home = %s\\n' "$(dirname "$DUREH_TEST_PY")" > .venv/pyvenv.cfg
ln -sf "$DUREH_TEST_PY" .venv/bin/python
'''


class PromoteEnv(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.mod = self.make_module("demo")
        (self.mod / "uv.lock").write_text("lock v1\n")
        (self.mod / ".gitignore").write_text(".venv/\n.venv.*\n")
        (self.mod / "docs").mkdir()
        (self.mod / "docs" / "note.md").write_text("line one\n")
        git(self.mod, "add", "-A")
        git(self.mod, "commit", "-qm", "lock")
        self.main_venv = fake_venv(self.mod)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for name, body in (("uv", FAKE_UV), ("rebuild-env", FAKE_REBUILD)):
            (bindir / name).write_text(body)
            (bindir / name).chmod(0o755)
        os.environ["PATH"] = os.pathsep.join([str(bindir), os.environ.get("PATH", "")])
        self.log = self.tmp / "uv.log"
        os.environ["DUREH_TEST_LOG"] = str(self.log)
        os.environ["DUREH_TEST_PY"] = sys.executable
        os.environ["DUREH_TEST_LOCK"] = "ok"
        os.environ.pop("DUREH_TEST_FAIL", None)
        self.configure("uv sync --frozen --all-extras")

    def configure(self, cmd):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["modules"] = [{"id": "demo", "path": "modules/demo", **({"env_rebuild": cmd} if cmd else {})}]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        self.led = Ledger(self.s)

    def verified(self, rel="docs/note.md", text="line one, by the worker\n"):
        """A verified doc-writer item on modules/demo whose branch changed `rel`. The verification is recorded
        directly (bound to the head as verify records it): what is measured here is the promotion."""
        wi = wf.wi_new(self.s, "desk", "t", "g", "doc-writer", "modules/demo", ["ok"], checks=["true"])["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        b = wf.dispatch_brief(self.s, wi, "desk")
        hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
             tool_input={"subagent_type": "doc-writer", "prompt": b["brief"]})
        wt = Path(b["worktree"])
        (wt / rel).write_text(text)
        git(wt, "add", rel)
        git(wt, "commit", "-qm", "work")
        self.led.event("wi.staged", "desk", wi, {"summary": "s", "head": git(wt, "rev-parse", "HEAD")})
        self.led.event("wi.verified", "desk", wi, {"head": git(wt, "rev-parse", "HEAD")})
        if self.log.exists():
            self.log.unlink()  # only what the promotion runs
        return wi

    def runs(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def promoted(self, wi):
        return [e for e in self.led.events(wi) if e["type"] == "wi.promoted"][-1]["payload"]

    def repaired(self, wi):
        return [e for e in self.led.events(wi) if e["type"] == "env.repaired"]

    def test_a_matching_env_is_checked_and_left_alone(self):
        wi = self.verified()
        wf.wi_promote(self.s, "desk", wi, "ok")
        env = self.promoted(wi)["env"]
        self.assertEqual((env["env"], env["lock_check"]), ("ok", "ok"))
        self.assertEqual([r for r in self.runs() if "--check" not in r], [], "no rebuild")
        self.assertEqual(len([r for r in self.runs() if "--check" in r]), 1, "one lock check")
        self.assertEqual(self.repaired(wi), [])
        self.assertTrue((self.main_venv / "bin" / "python").exists())

    def test_a_lock_stale_env_is_rebuilt_after_the_merge(self):
        wi = self.verified("uv.lock", "lock v2\n")
        os.environ["DUREH_TEST_LOCK"] = "stale"  # the merged lock is not what is installed
        ev = wf.wi_promote(self.s, "desk", wi, "ok")
        env = self.promoted(wi)["env"]
        self.assertEqual((env["env"], env["deps_changed"]), ("rebuilt", ["uv.lock"]))
        self.assertIn("would be installed", env["stale_lock"])
        self.assertNotIn("forced", env, "a uv sync env_rebuild is asked, not forced")
        rebuilds = [r for r in self.runs() if "--check" not in r]
        self.assertEqual(rebuilds, [f"{self.mod} sync --frozen --all-extras nosync=unset"])
        rep = self.repaired(wi)
        self.assertEqual(len(rep), 1)
        self.assertEqual((rep[0]["payload"]["target"], rep[0]["payload"]["after_promote"]), ("modules/demo", True))
        self.assertEqual(list(self.mod.glob(".venv.stale-*")), [], "the moved-aside copy is removed")
        self.assertEqual(git(self.mod, "rev-parse", "HEAD"), ev["payload"]["merge"])

    def test_another_env_rebuild_is_forced_only_when_a_dependency_file_changed(self):
        self.configure("rebuild-env")
        wi = self.verified()  # docs only: nothing to ask, nothing to rebuild
        wf.wi_promote(self.s, "desk", wi, "ok")
        env = self.promoted(wi)["env"]
        self.assertEqual((env["env"], env["lock_check"]), ("ok", "unavailable"))
        self.assertEqual(self.runs(), [])
        wi = self.verified("pyproject.toml", "[project]\nname = 'demo'\nversion = '2'\n")
        wf.wi_promote(self.s, "desk", wi, "ok")
        env = self.promoted(wi)["env"]
        self.assertEqual((env["env"], env["deps_changed"], env["forced"]), ("rebuilt", ["pyproject.toml"], True))
        self.assertEqual(self.runs(), [f"{self.mod} rebuild"])

    def test_a_failed_rebuild_leaves_the_promotion_standing(self):
        self.configure("rebuild-env")
        wi = self.verified("pyproject.toml", "[project]\nname = 'demo'\n")
        os.environ["DUREH_TEST_FAIL"] = "1"
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["--site", str(self.site_root), "wi", "promote", "--wi", wi, "--memo", "ok", "--actor", "desk"])
        self.assertIn("main env: error (dependency files changed: pyproject.toml)", out.getvalue())
        self.assertIn("next: dureh worktree repair --target modules/demo", out.getvalue())
        self.assertEqual(self.led.wi_status(wi)["status"], "promoted")
        env = self.promoted(wi)["env"]
        self.assertEqual((env["env"], env["next"]), ("error", "dureh worktree repair --target modules/demo"))
        self.assertIn("no network", env["error"])
        self.assertEqual(self.repaired(wi), [])

    def test_no_env_rebuild_or_no_main_env_runs_nothing(self):
        self.configure(None)
        wi = self.verified("uv.lock", "lock v2\n")
        wf.wi_promote(self.s, "desk", wi, "ok")
        self.assertNotIn("env", self.promoted(wi))
        self.configure("uv sync --frozen")
        os.environ["DUREH_TEST_LOCK"] = "stale"
        wi = self.verified("uv.lock", "lock v3\n")
        for p in sorted(self.main_venv.rglob("*"), reverse=True):
            p.unlink() if p.is_file() or p.is_symlink() else p.rmdir()
        self.main_venv.rmdir()
        wf.wi_promote(self.s, "desk", wi, "ok")
        self.assertNotIn("env", self.promoted(wi))
        self.assertFalse(self.main_venv.exists(), "promote does not create a main environment")
        self.assertEqual(self.runs(), [])
