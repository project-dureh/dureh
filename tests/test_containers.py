"""`dureh container build|status` (UR-20261004-02): a sanctioned SIF build as a scheduler job, with fake
sbatch/sacct/apptainer only; the real scheduler and apptainer are never called."""
import json
import os
import subprocess
import unittest
from pathlib import Path

from dureh_core import containers
from dureh_core import workflow as wf
from dureh_core.paths import write_decision
from dureh_core.records import Ledger
from dureh_core.util import DurehError
from tests.helpers import SiteCase, denied, hook

RECIPE = "projects/lna/code/containers/camri.def"


def _job_env(fakes, **kw):
    """The job's environment for a local run: the fake apptainer first, no exported shell functions (Lmod)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BASH_FUNC_")}
    env.update(PATH=f"{fakes}:{os.environ['PATH']}", SLURM_JOB_ID="4242", **kw)
    return env


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ContainerBuild(SiteCase):
    def setUp(self):
        super().setUp()
        manifest = self.package / "startup.yaml"
        manifest.write_text(manifest.read_text()
                            .replace("managers: [pm-modules]", "managers: [pm-modules, pm-projects]")
                            .replace("workers: [py-writer,", "workers: [data-steward, pipeline-runner, py-writer,"))
        self.install()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        (self.fake / "sbatch").write_text("#!/bin/sh\necho \"$@\" >> \"$(dirname \"$0\")/sbatch.args\"\necho 4242\n")
        (self.fake / "sacct").write_text("#!/bin/sh\ncat \"$(dirname \"$0\")/sacct.out\" 2>/dev/null || echo 'RUNNING|0:0|00:01'\n")
        for f in ("sbatch", "sacct"):
            (self.fake / f).chmod(0o755)
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["env"] = {"path_prepend": [str(self.fake)], "vars": {"CAMRI_CONTAINER_CACHE": "$DUREH_SITE/cache/containers"}}
        cfg["containers"] = {"module": ""}  # never load the real apptainer module in a test
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.cache = self.site_root / "cache" / "containers"
        self.cache.mkdir(parents=True)
        self.proj = self.site_root / "projects" / "lna"
        (self.proj / "code" / "containers").mkdir(parents=True)
        (self.proj / "code" / "containers" / "camri.def").write_text(
            "Bootstrap: docker\nFrom: python:3.11-slim\n%files\n    src /opt/camri/src\n")
        _git(self.proj, "init", "-q", "-b", "main")
        _git(self.proj, "add", "-A")
        _git(self.proj, "commit", "-q", "-m", "recipe")
        self.mod = self.make_module("camri")
        (self.mod / "pyproject.toml").write_text('[project]\nname = "camri"\nversion = "2.0.0a1"\n')
        _git(self.mod, "add", "-A")
        _git(self.mod, "commit", "-q", "-m", "version")
        self.rev = _git(self.mod, "rev-parse", "HEAD")
        self.s = self.site()
        self.led = Ledger(self.s)
        self.bundle = self.wi("Bundle", "pm-projects")
        self.w = self.wi("Build camri", "pipeline-runner")

    def wi(self, title, role, target="projects/lna"):
        parent = None if role == "pm-projects" else self.bundle
        w = wf.wi_new(self.s, "desk", title, "g", role, target, ["ok"], parent=parent)["id"]
        wf.wi_approve(self.s, "desk", w, "ok")
        self.led.event("wi.dispatched", "desk", w, {"role": role, "brief": "0" * 16, "by": "desk"})
        return w

    def build(self, actor="pipeline-runner", **kw):
        args = dict(wi=self.w, recipe=RECIPE, module="camri", rev=self.rev[:7])
        args.update(kw)
        return containers.build(self.s, actor, **args)

    def target(self):
        return f"camri_2.0.0a1-{self.rev[:7]}.sif"

    def test_build_writes_script_and_submits_through_slurm(self):
        run = self.build()
        p = run["payload"]
        self.assertEqual(p["job_id"], "4242")
        self.assertEqual(p["container"]["target"], self.target())
        self.assertEqual(p["container"]["rev"], self.rev)
        self.assertEqual(p["container"]["version"], "2.0.0a1")
        self.assertEqual(p["container"]["recipe"], RECIPE)
        self.assertEqual(p["container"]["recipe_blob"], _git(self.proj, "rev-parse", "HEAD:code/containers/camri.def"))
        script = Path(p["script"])
        self.assertTrue(str(script).startswith(str(self.proj / "checkpoints" / f"{self.w}-container-")))
        text = script.read_text()
        self.assertIn(f"git -C {self.mod} archive {self.rev} | tar -x -C \"$CTX\"", text)
        self.assertIn("apptainer build --fakeroot \"$PARTIAL\" camri.def", text)
        self.assertIn("mv -n \"$PARTIAL\" \"$TARGET\"", text)
        self.assertIn("APPTAINER_TMPDIR=\"${TMPDIR:-", text)
        self.assertEqual((script.parent / "camri.def").read_text(), (self.proj / RECIPE.split("lna/")[1]).read_text())
        args = (self.fake / "sbatch.args").read_text()
        self.assertIn(f"--job-name=dureh-{self.w}", args)
        self.assertIn(f"--output={self.site_root}/logs/slurm/{self.w}/", args)
        self.assertEqual(p["script_sha256"], containers.sha256_file(script))

    def test_refusals(self):
        with self.assertRaisesRegex(DurehError, "only by pipeline-runner"):
            self.build(actor="py-writer")
        steward = self.wi("Steward", "data-steward")
        with self.assertRaisesRegex(DurehError, "not a pipeline-runner work item"):
            self.build(wi=steward)
        with self.assertRaisesRegex(DurehError, "code/containers"):
            self.build(recipe="projects/lna/code/run.def")
        with self.assertRaisesRegex(DurehError, "code/containers"):
            self.build(recipe="projects/lna/code/containers/../../x.def")
        with self.assertRaisesRegex(DurehError, "does not resolve"):
            self.build(rev="nope")
        with self.assertRaisesRegex(DurehError, "module is missing"):
            self.build(module="ghost")
        (self.proj / "code" / "containers" / "new.def").write_text("Bootstrap: docker\nFrom: x\n")
        with self.assertRaisesRegex(DurehError, "not tracked"):
            self.build(recipe="projects/lna/code/containers/new.def")
        (self.proj / "code" / "containers" / "camri.def").write_text("changed\n")
        with self.assertRaisesRegex(DurehError, "not tracked"):
            self.build()
        self.assertFalse((self.fake / "sbatch.args").exists(), "nothing was submitted")

    def test_annexed_recipe(self):
        """DataLad projects keep recipes in git-annex (locked: a symlink into .git/annex/objects)."""
        import hashlib

        data = b"Bootstrap: docker\nFrom: python:3.11-slim\n"
        key = f"SHA256E-s{len(data)}--{hashlib.sha256(data).hexdigest()}.def"
        obj = self.proj / ".git" / "annex" / "objects" / "Ab" / "Cd" / key / key
        obj.parent.mkdir(parents=True)
        obj.write_bytes(data)
        link = self.proj / "code" / "containers" / "annexed.def"
        link.symlink_to(os.path.relpath(obj, link.parent))
        _git(self.proj, "add", "code/containers/annexed.def")
        _git(self.proj, "commit", "-q", "-m", "annexed recipe")
        run = self.build(recipe="projects/lna/code/containers/annexed.def")
        c = run["payload"]["container"]
        self.assertEqual(c["recipe_annex_key"], key)
        self.assertEqual(c["recipe_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual((Path(run["payload"]["script"]).parent / "annexed.def").read_bytes(), data)
        obj.write_bytes(b"tampered content of other size\n")
        with self.assertRaisesRegex(DurehError, "not tracked"):
            self.build(recipe="projects/lna/code/containers/annexed.def", version="9")

    def test_existing_target_is_never_overwritten(self):
        (self.cache / self.target()).write_bytes(b"old image")
        with self.assertRaisesRegex(DurehError, "never overwritten"):
            self.build()
        run = self.build(version="2.0.0a1.post1")
        self.assertEqual(run["payload"]["container"]["target"], f"camri_2.0.0a1.post1-{self.rev[:7]}.sif")

    def test_no_cache_configured(self):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["env"]["vars"] = {}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        os.environ.pop("CAMRI_CONTAINER_CACHE", None)
        with self.assertRaisesRegex(DurehError, "no container cache configured"):
            containers.build(self.site(), "pipeline-runner", self.w, RECIPE, "camri", self.rev)

    def test_generic_key_wins_over_env_var(self):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["containers"] = {"module": "", "cache": "images", "build_args": ["--fakeroot", "--ignore-fakeroot-command"]}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        (self.site_root / "images").mkdir()
        run = containers.build(self.site(), "desk", self.w, RECIPE, "camri", self.rev, name="camri-rt")
        self.assertEqual(run["payload"]["container"]["cache"], str(self.site_root / "images"))
        self.assertIn("--fakeroot --ignore-fakeroot-command", Path(run["payload"]["script"]).read_text())

    def test_job_script_runs_with_fake_tools_and_status_records_once(self):
        run = self.build()
        script = Path(run["payload"]["script"])
        fakes = self.tmp / "jobbin"
        fakes.mkdir()
        (fakes / "apptainer").write_text(
            "#!/bin/sh\nif [ \"$1\" = --version ]; then echo 'apptainer version 1.5.0'; exit 0; fi\n"
            "# build [flags] <out> <recipe>: check the context, write a fake image\n"
            "for a; do out=$prev; prev=$a; done\n"
            "test -f src/demo.py && test -f camri.def || exit 9\n"
            "echo image-bytes > \"$out\"\n")
        (fakes / "apptainer").chmod(0o755)
        env = _job_env(fakes, TMPDIR=str(self.tmp / "nodelocal"))
        proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sif = self.cache / self.target()
        self.assertEqual(sif.read_text(), "image-bytes\n")
        prov = json.loads((self.cache / f"{self.target()}.provenance.json").read_text())
        self.assertEqual(prov["job_id"], "4242")
        self.assertEqual(prov["rev"], self.rev)
        self.assertEqual(prov["recipe"], RECIPE)
        self.assertEqual(prov["sha256"], containers.sha256_file(sif))
        self.assertEqual(prov["apptainer_version"], "apptainer version 1.5.0")
        self.assertFalse(list(self.cache.glob(".*partial*")))
        # a second run of the same job never overwrites the image
        proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, cwd=self.tmp)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(sif.read_text(), "image-bytes\n")
        out = containers.status(self.s, "pipeline-runner", self.w)
        self.assertEqual(out["builds"][0]["state"], "recorded")
        self.assertTrue(out["builds"][0]["new"])
        again = containers.status(self.s, "pipeline-runner", self.w)
        self.assertNotIn("new", again["builds"][0])
        built = [e for e in self.led.events(self.w) if e["type"] == "container.built"]
        self.assertEqual(len(built), 1)
        self.assertEqual(built[0]["payload"]["sha256"], prov["sha256"])
        self.assertEqual(built[0]["refs"], [run["id"]])
        # the image now exists: the same build is refused
        with self.assertRaisesRegex(DurehError, "never overwritten"):
            self.build()

    def test_target_appearing_during_the_build_leaves_the_partial(self):
        run = self.build()
        script = Path(run["payload"]["script"])
        fakes = self.tmp / "jobbin"
        fakes.mkdir()
        target = self.cache / self.target()
        (fakes / "apptainer").write_text(
            "#!/bin/sh\nif [ \"$1\" = --version ]; then echo v; exit 0; fi\n"
            "for a; do out=$prev; prev=$a; done\necho new > \"$out\"\n"
            f"echo racer > {target}\n")
        (fakes / "apptainer").chmod(0o755)
        env = _job_env(fakes)
        proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, cwd=self.tmp)
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertEqual(target.read_text(), "racer\n")
        self.assertTrue((self.cache / f".{self.target()}.partial-4242").is_file())
        self.assertFalse((self.cache / f"{self.target()}.provenance.json").exists())
        (self.fake / "sacct.out").write_text("FAILED|4:0|00:01\n")
        out = containers.status(self.s, "pipeline-runner", self.w)
        self.assertEqual(out["builds"][0]["state"], "FAILED")
        self.assertIn("partial image", out["builds"][0]["note"])
        self.assertFalse([e for e in self.led.events(self.w) if e["type"] == "container.built"])

    def test_status_before_the_job_ends_records_nothing(self):
        self.build()
        out = containers.status(self.s, "pipeline-runner", self.w)
        self.assertEqual(out["builds"][0]["state"], "RUNNING")
        self.assertFalse([e for e in self.led.events(self.w) if e["type"] == "container.built"])
        with self.assertRaisesRegex(DurehError, "already being built"):
            self.build()

    def test_cache_is_written_by_no_role(self):
        target = self.cache / "x.sif"
        for role in self.s.org.roles:
            ok, why = write_decision(self.s, role, target)
            self.assertFalse(ok, role)
            self.assertIn("container cache", why)

    def test_bash_guard(self):
        def bash(command, role):
            data = dict(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": command},
                        cwd=str(self.site_root))
            if role != "desk":
                data.update(agent_type=role, agent_id="a1")
            return hook(self.s, **data)

        for role in ("pipeline-runner", "desk"):
            out = bash(f"{self.site_root}/bin/dureh container build --wi {self.w} --recipe {RECIPE} --module camri"
                       f" --rev {self.rev[:7]}", role)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow", role)
            for raw in ("apptainer build --fakeroot x.sif camri.def", "singularity build x.sif camri.def",
                        f"cp img.sif {self.cache}/camri.sif", "echo x > cache/containers/a.sif"):
                self.assertTrue(denied(bash(raw, role)), (role, raw))


class ModuleRecipeBuild(ContainerBuild):
    """UR-20261004-03 item 2: without --recipe the recipe is the module's own containers/<name>.def inside
    `git archive <rev>`; the WI may target that module or a project."""

    def setUp(self):
        super().setUp()
        (self.mod / "containers").mkdir()
        (self.mod / "containers" / "camri.def").write_text(
            "Bootstrap: docker\nFrom: python:3.11-slim\n%files\n    src /opt/camri/src\n")
        (self.mod / "containers" / "fsl.def").write_text("Bootstrap: docker\nFrom: ubuntu:22.04\n")
        _git(self.mod, "add", "-A")
        _git(self.mod, "commit", "-q", "-m", "recipes")
        self.old = self.rev
        self.rev = _git(self.mod, "rev-parse", "HEAD")

    def mbuild(self, actor="pipeline-runner", **kw):
        args = dict(wi=self.w, recipe=None, module="camri", rev=self.rev[:7])
        args.update(kw)
        return containers.build(self.s, actor, **args)

    def test_module_recipe_on_a_project_wi(self):
        import hashlib

        run = self.mbuild()
        c = run["payload"]["container"]
        self.assertEqual(c["recipe_source"], "module")
        self.assertEqual(c["recipe_path"], "modules/camri/containers/camri.def")
        self.assertEqual(c["recipe"], "modules/camri/containers/camri.def")
        self.assertEqual(c["recipe_rev"], self.rev)
        self.assertEqual(c["rev"], self.rev)
        self.assertEqual(c["recipe_blob"], _git(self.mod, "rev-parse", f"{self.rev}:containers/camri.def"))
        self.assertEqual(c["recipe_sha256"], hashlib.sha256((self.mod / "containers" / "camri.def").read_bytes()).hexdigest())
        self.assertEqual(c["wi_target"], "projects/lna")
        self.assertEqual(c["project"], "projects/lna")
        self.assertEqual(c["target"], self.target())
        script = Path(run["payload"]["script"])
        self.assertTrue(str(script).startswith(str(self.proj / "checkpoints" / f"{self.w}-container-")))
        text = script.read_text()
        self.assertIn("apptainer build --fakeroot \"$PARTIAL\" containers/camri.def", text)
        self.assertNotIn("cp ", text)

    def test_module_recipe_on_a_module_wi(self):
        w = self.wi("Build in module", "pipeline-runner", target="modules/camri")
        run = self.mbuild(wi=w, name="fsl")
        c = run["payload"]["container"]
        self.assertEqual(c["wi_target"], "modules/camri")
        self.assertIsNone(c["project"])
        self.assertEqual(c["target"], f"fsl_2.0.0a1-{self.rev[:7]}.sif")
        self.assertEqual(c["recipe_path"], "modules/camri/containers/fsl.def")
        script = Path(run["payload"]["script"])
        self.assertTrue(str(script).startswith(str(self.site_root / "logs" / "containers" / f"{w}-container-fsl_")))
        self.assertEqual(_git(self.mod, "status", "--porcelain"), "", "the module tree stays clean")

    def test_module_recipe_refusals(self):
        with self.assertRaisesRegex(DurehError, "modules/camri/containers/ghost.def is not in modules/camri at"):
            self.mbuild(name="ghost")
        with self.assertRaisesRegex(DurehError, "containers/camri.def is not in modules/camri at"):
            self.mbuild(rev=self.old)  # the recipe was committed after that rev
        (self.mod / "containers" / "new.def").write_text("Bootstrap: docker\nFrom: x\n")  # untracked
        with self.assertRaisesRegex(DurehError, "is not in modules/camri"):
            self.mbuild(name="new")
        other = self.make_module("other")
        _git(other, "commit", "-q", "--allow-empty", "-m", "x")
        w = self.wi("Wrong module", "pipeline-runner", target="modules/other")
        with self.assertRaisesRegex(DurehError, "needs a WI for modules/camri or for a project"):
            self.mbuild(wi=w)
        with self.assertRaisesRegex(DurehError, "only by pipeline-runner"):
            self.mbuild(actor="py-writer")
        self.assertFalse((self.fake / "sbatch.args").exists(), "nothing was submitted")

    def test_module_job_script_builds_from_the_archive(self):
        run = self.mbuild()
        script = Path(run["payload"]["script"])
        fakes = self.tmp / "jobbin"
        fakes.mkdir()
        (fakes / "apptainer").write_text(
            "#!/bin/sh\nif [ \"$1\" = --version ]; then echo 'apptainer version 1.5.0'; exit 0; fi\n"
            "for a; do out=$prev; prev=$a; done\n"
            "test -f src/demo.py && test \"$prev\" = containers/camri.def && test -f containers/camri.def || exit 9\n"
            "echo image-bytes > \"$out\"\n")
        (fakes / "apptainer").chmod(0o755)
        proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=_job_env(fakes), cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        prov = json.loads((self.cache / f"{self.target()}.provenance.json").read_text())
        self.assertEqual(prov["recipe_source"], "module")
        self.assertEqual(prov["recipe_rev"], self.rev)
        self.assertEqual(prov["recipe_path"], "modules/camri/containers/camri.def")
        out = containers.status(self.s, "pipeline-runner", self.w)
        self.assertEqual(out["builds"][0]["state"], "recorded")


if __name__ == "__main__":
    unittest.main()
