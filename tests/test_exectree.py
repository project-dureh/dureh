"""`dureh slurm submit --module` (UR-20261004-04): jobs run against a pinned, read-only `git archive` tree of
modules/<m>, never the live checkout. Fake sbatch only; a temporary git repository is the module."""
import json
import os
import stat
import subprocess
from pathlib import Path

from dureh_core import ops
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError
from tests.helpers import SiteCase, add_runner_roles


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ModuleExec(SiteCase):
    def setUp(self):
        super().setUp()
        add_runner_roles(self.package)
        self.install()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        (self.fake / "sbatch").write_text("#!/bin/sh\necho \"$@\" >> \"$(dirname \"$0\")/sbatch.args\"\n"
                                          "echo 4242\n")
        (self.fake / "sbatch").chmod(0o755)
        self.mod = self.site_root / "modules" / "exmod"
        (self.mod / "src" / "exmod").mkdir(parents=True)
        (self.mod / "src" / "exmod" / "__init__.py").write_text("V = 1\n")
        (self.mod / "pyproject.toml").write_text('[project]\nname = "exmod"\nversion = "2.0.0a1"\n')
        (self.mod / "uv.lock").write_text("version = 1\n# lock A\n")
        _git(self.mod, "init", "-q", "-b", "main")
        _git(self.mod, "add", "-A")
        _git(self.mod, "commit", "-q", "-m", "a")
        self.rev_a = _git(self.mod, "rev-parse", "HEAD")
        (self.mod / "src" / "exmod" / "__init__.py").write_text("V = 2\n")
        _git(self.mod, "commit", "-qam", "b")
        self.rev_b = _git(self.mod, "rev-parse", "HEAD")
        (self.mod / ".venv" / "bin").mkdir(parents=True)
        self.proj = self.site_root / "projects" / "lna"
        self.proj.mkdir(parents=True)
        self.pin(self.rev_a[:7])
        _git(self.proj, "init", "-q", "-b", "main")
        _git(self.proj, "add", "-A")
        _git(self.proj, "commit", "-q", "-m", "m")
        self.config(root_env="EXMOD_PROJECT")
        self.s = self.site()
        self.led = Ledger(self.s)
        b = wf.wi_new(self.s, "desk", "Bundle", "g", "coordinator", "projects/lna", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", b, "ok")
        self.led.event("wi.dispatched", "desk", b, {"role": "coordinator", "brief": "0" * 16, "by": "desk"})
        self.w = wf.wi_new(self.s, "desk", "Run", "g", "runner", "projects/lna", ["ok"], parent=b)["id"]
        wf.wi_approve(self.s, "desk", self.w, "ok")
        self.led.event("wi.dispatched", "desk", self.w, {"role": "runner", "brief": "0" * 16, "by": "desk"})
        self.script = self.proj / "checkpoints" / "run.sh"
        self.script.parent.mkdir()
        self.script.write_text("#!/bin/bash\n#SBATCH --time=00:10:00\n#SBATCH --mem=1G\n"
                               "echo \"ROOT=$DUREH_MODULE_ROOT REV=$DUREH_MODULE_REV CP=$EXMOD_PROJECT PP=$PYTHONPATH\"\n"
                               "touch \"$DUREH_MODULE_ROOT/x\" 2>/dev/null && echo WRITABLE || echo READONLY\n")

    def pin(self, value):
        (self.proj / "exmod-manifest.yaml").write_text(
            "project:\n  name: lna\nprovenance:\n  tool_versions:\n    brkraw: null\n"
            f"    exmod: \"2.0.0a1+{value}\"   # pinned\n")

    def config(self, **entry):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["env"] = {"path_prepend": [str(self.fake)]}
        cfg["containers"] = {"module": ""}
        cfg["project"] = {"manifest": "exmod-manifest.yaml"}  # UP-0069: the manifest name is config, not code
        cfg["modules"] = [{"id": "exmod", "path": "modules/exmod", "env_rebuild": "echo REBUILD-RAN", **entry}]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()

    def submit(self, **kw):
        return ops.slurm_submit(self.s, "runner", self.w, str(self.script), module="exmod", **kw)

    def test_default_rev_from_manifest_and_read_only_tree(self):
        run = self.submit()
        m = run["payload"]["module"]
        self.assertEqual(m["name"], "exmod")
        self.assertEqual(m["rev"], self.rev_a)
        self.assertFalse(m["rev_override"])
        self.assertEqual(m["env_mode"], "shared-venv")
        tree = Path(m["tree"])
        self.assertEqual(tree.parent, self.site_root / "scratch" / "exec")
        self.assertTrue(tree.name.startswith(f"{self.w}-exmod-{self.rev_a[:7]}-"))
        self.assertEqual((tree / "src" / "exmod" / "__init__.py").read_text(), "V = 1\n")
        for root, dirs, files in os.walk(tree):
            for n in dirs + files + ["."]:
                self.assertFalse(os.stat(os.path.join(root, n)).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        self.assertFalse((self.mod / ".git" / "worktrees").exists(), "no git worktree")
        self.assertEqual(run["payload"]["job_id"], "4242")
        self.assertEqual(run["payload"]["script"], str(self.script))
        args = (self.fake / "sbatch.args").read_text()
        self.assertTrue(args.strip().endswith(run["payload"]["wrapper"]))
        text = Path(run["payload"]["wrapper"]).read_text()
        self.assertIn("#SBATCH --time=00:10:00", text)
        # Director 2026-10-04: a configured root_env replaces the DUREH_* names; no rev_env, no rev export
        self.assertNotIn("DUREH_MODULE_ROOT=", text)
        self.assertNotIn("DUREH_MODULE_REV=", text)
        self.assertIn(f"export EXMOD_PROJECT={tree}", text)
        self.assertEqual(m["env"], {"root": "EXMOD_PROJECT", "rev": None})
        self.assertIn(f"export VIRTUAL_ENV={self.mod / '.venv'}", text)
        self.assertIn(f"export PYTHONPATH={tree / 'src'}", text)
        self.assertIn("trap _dureh_exec_cleanup EXIT", text)
        self.assertIn("chmod -R u+w", text)
        self.assertNotIn("REBUILD-RAN", text)

    def test_override_recorded_and_refusals(self):
        run = self.submit(rev=self.rev_b[:9])
        self.assertTrue(run["payload"]["module"]["rev_override"])
        self.assertEqual(run["payload"]["module"]["rev"], self.rev_b)
        self.assertEqual(run["payload"]["module"]["manifest_pin"], self.rev_a)
        same = self.submit(rev=self.rev_a)
        self.assertFalse(same["payload"]["module"]["rev_override"])
        with self.assertRaisesRegex(DurehError, "does not resolve"):
            self.submit(rev="deadbeef")
        (self.proj / "exmod-manifest.yaml").write_text("project:\n  name: lna\n")
        with self.assertRaisesRegex(DurehError, "no usable provenance.tool_versions.exmod.*--rev"):
            self.submit()
        self.assertEqual(self.submit(rev="main")["payload"]["module"]["rev"], self.rev_b)
        with self.assertRaisesRegex(DurehError, "--rev needs --module"):
            ops.slurm_submit(self.s, "runner", self.w, str(self.script), rev="main")

    def test_rebuild_when_lock_differs(self):
        (self.mod / "uv.lock").write_text("version = 1\n# lock B\n")
        _git(self.mod, "commit", "-qam", "lock b")
        run = self.submit()  # pinned rev A has lock A; the live checkout has lock B
        m = run["payload"]["module"]
        self.assertEqual(m["env_mode"], "rebuild")
        text = Path(run["payload"]["wrapper"]).read_text()
        self.assertIn(f"export VIRTUAL_ENV={m['tree']}.venv", text)
        self.assertIn('export UV_PROJECT_ENVIRONMENT="$VIRTUAL_ENV"', text)
        self.assertIn("&& echo REBUILD-RAN )", text)
        self.assertNotIn("PYTHONPATH", text)
        # no .venv at all also means rebuild; and no env_rebuild configured is refused before submitting
        (self.mod / "uv.lock").write_text("version = 1\n# lock A\n")
        _git(self.mod, "commit", "-qam", "lock a")
        os.rmdir(self.mod / ".venv" / "bin")
        os.rmdir(self.mod / ".venv")
        self.assertEqual(self.submit()["payload"]["module"]["env_mode"], "rebuild")
        cfg = json.loads((self.site_root / "config.json").read_text())
        del cfg["modules"][0]["env_rebuild"]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        before = sorted((self.site_root / "scratch" / "exec").iterdir())
        with self.assertRaisesRegex(DurehError, "env_rebuild"):
            self.submit()
        self.assertEqual(sorted((self.site_root / "scratch" / "exec").iterdir()), before, "refusal leaves nothing")

    def test_wrapper_runs_and_cleans_up(self):
        run = self.submit()
        tree = Path(run["payload"]["module"]["tree"])
        env = {k: v for k, v in os.environ.items() if not k.startswith("BASH_FUNC_")}
        env.pop("EXMOD_PROJECT", None)
        p = subprocess.run(["bash", run["payload"]["wrapper"]], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"ROOT= REV= CP={tree} PP={tree / 'src'}", p.stdout)
        self.assertIn("READONLY", p.stdout)
        self.assertFalse(tree.exists(), "the EXIT trap removes the tree")

    def test_live_root_assignment_warns_and_no_root_env_warns(self):
        self.script.write_text("#!/bin/bash\nEXMOD_PROJECT=\"$DUREH_SITE/modules/exmod\"\n"
                               f"export OTHER={self.site_root}/modules/exmod\necho hi\n")
        run = self.submit()
        warns = run["payload"]["warnings"]
        self.assertTrue(any("line 2" in w and "overrides the pinned tree" in w for w in warns), warns)
        self.assertTrue(any("line 3" in w for w in warns), warns)
        self.assertFalse((self.script.read_text()).count("DUREH_MODULE_ROOT"), "the script is never edited")
        self.config()  # no root_env
        self.script.write_text("#!/bin/bash\necho hi\n")
        run = self.submit()
        text = Path(run["payload"]["wrapper"]).read_text()
        self.assertNotIn("EXMOD_PROJECT", text)
        self.assertTrue(any("no root_env" in w for w in run["payload"]["warnings"]))

    def test_without_module_unchanged(self):
        run = ops.slurm_submit(self.s, "runner", self.w, str(self.script))
        self.assertNotIn("module", run["payload"])
        self.assertNotIn("wrapper", run["payload"])
        self.assertEqual(run["payload"]["command"][-1], str(self.script))
        self.assertFalse((self.site_root / "scratch").exists())

    def test_failed_sbatch_removes_tree(self):
        (self.fake / "sbatch").write_text("#!/bin/sh\necho nope >&2\nexit 1\n")
        with self.assertRaisesRegex(DurehError, "sbatch failed"):
            self.submit()
        self.assertEqual(list((self.site_root / "scratch" / "exec").iterdir()), [])

    def test_purge_removes_leftover_trees(self):
        run = self.submit()
        tree = Path(run["payload"]["module"]["tree"])
        (Path(f"{tree}.venv") / "bin").mkdir(parents=True)
        other = self.site_root / "scratch" / "exec" / "WI-9999-exmod-abcdef0-x"
        other.mkdir()
        wf.dispatch_stop(self.s, "desk", self.w, "test")
        out = wf.purge(self.s, "desk", self.w)
        self.assertIn(str(tree), out["removed"])
        self.assertFalse(tree.exists())
        self.assertFalse(Path(f"{tree}.venv").exists())
        self.assertFalse(Path(run["payload"]["wrapper"]).exists())
        self.assertTrue(other.is_dir(), "other WIs' trees stay")

    def test_each_array_shares_the_tree_until_every_task_ended(self):
        # release 26: array tasks share one pinned tree; no task removes it, `slurm wait|status` does at the end
        self.script.write_text("#!/bin/bash\necho \"ITEM=$DUREH_ITER IDX=$DUREH_ITER_INDEX ROOT=$EXMOD_PROJECT\"\n")
        run = self.submit(each="sub-01,sub-02")
        tree = Path(run["payload"]["module"]["tree"])
        self.assertIn("--array=0-1", " ".join(run["payload"]["command"]))
        self.assertEqual(run["payload"]["command"][-1], run["payload"]["wrapper"])
        env = {k: v for k, v in os.environ.items() if not k.startswith("BASH_FUNC_")}
        env["SLURM_ARRAY_TASK_ID"] = "1"
        p = subprocess.run(["bash", run["payload"]["wrapper"]], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"ITEM=sub-02 IDX=1 ROOT={tree}", p.stdout)
        self.assertTrue(tree.is_dir(), "a task never removes the shared tree")
        (Path(f"{tree}.venv-0") / "bin").mkdir(parents=True)  # a rebuilt per-task env left by a killed task
        (self.fake / "sacct").write_text("#!/bin/sh\necho '4242_0|RUNNING|0:0'\necho '4242_1|COMPLETED|0:0'\n")
        (self.fake / "sacct").chmod(0o755)
        ops.slurm_status(self.s, "runner", run["id"])
        self.assertTrue(tree.is_dir())
        (self.fake / "sacct").write_text("#!/bin/sh\necho '4242_0|COMPLETED|0:0'\necho '4242_1|COMPLETED|0:0'\n")
        ev = ops.slurm_wait(self.s, "runner", run["id"], timeout=5, interval=1)
        self.assertEqual(ev["payload"]["scheduler_state"], "COMPLETED")
        self.assertFalse(tree.exists() or Path(f"{tree}.venv-0").exists() or Path(run["payload"]["wrapper"]).exists())

    def test_each_rebuild_uses_a_venv_per_task(self):
        (self.mod / "uv.lock").write_text("version = 1\n# lock B\n")
        run = self.submit(each="a,b")
        text = Path(run["payload"]["wrapper"]).read_text()
        self.assertEqual(run["payload"]["module"]["env_mode"], "rebuild")
        self.assertIn('.venv-"$SLURM_ARRAY_TASK_ID"', text)
        self.assertIn('export VIRTUAL_ENV="$DUREH_EXEC_VENV"', text)

    def test_resubmit_failed_keeps_the_module_revision(self):
        run = self.submit(each="a,b", rev=self.rev_b)
        (self.fake / "sacct").write_text("#!/bin/sh\necho '4242_0|COMPLETED|0:0'\necho '4242_1|FAILED|1:0'\n")
        (self.fake / "sacct").chmod(0o755)
        again = ops.slurm_resubmit_failed(self.s, "runner", run["id"])
        self.assertEqual(again["payload"]["module"]["rev"], self.rev_b)
        self.assertEqual(again["payload"]["array"]["items"], ["b"])

    def test_configured_rev_env_and_fallback_names(self):
        self.config(root_env="EXMOD_PROJECT", rev_env="EXMOD_REV")
        self.script.write_text("#!/bin/bash\necho \"CP=$EXMOD_PROJECT CR=$EXMOD_REV DR=$DUREH_MODULE_ROOT\"\n")
        run = self.submit()
        tree = Path(run["payload"]["module"]["tree"])
        self.assertEqual(run["payload"]["module"]["env"], {"root": "EXMOD_PROJECT", "rev": "EXMOD_REV"})
        env = {k: v for k, v in os.environ.items() if not k.startswith("BASH_FUNC_")}
        env.pop("DUREH_MODULE_ROOT", None)
        p = subprocess.run(["bash", run["payload"]["wrapper"]], capture_output=True, text=True, env=env)
        self.assertIn(f"CP={tree} CR={self.rev_a} DR=", p.stdout)
        self.config()  # no root_env: the DUREH_* fallbacks
        run = self.submit()
        text = Path(run["payload"]["wrapper"]).read_text()
        self.assertEqual(run["payload"]["module"]["env"], {"root": "DUREH_MODULE_ROOT", "rev": "DUREH_MODULE_REV"})
        self.assertIn("export DUREH_MODULE_ROOT=", text)
        self.assertIn(f"export DUREH_MODULE_REV={self.rev_a}", text)
        self.config(rev_env="EXMOD_REV")
        with self.assertRaisesRegex(DurehError, "rev_env needs root_env"):
            self.submit()
        self.config(root_env="EXMOD_PROJECT", rev_env="bad name")
        with self.assertRaisesRegex(DurehError, "rev_env is not a variable name"):
            self.submit()
