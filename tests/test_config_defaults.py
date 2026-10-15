"""UP-0069: company values (manifest name, container cache variable, module submit options) are config keys with
generic harness defaults; a startup package seeds its own through `config_defaults` in startup.yaml, merged at install
below the preset and added to an existing site's config.json by `adapter sync` (missing keys only; release 46: read
from the installed package, which `adapter sync` re-copies from the committed source first)."""
import json
import os
from pathlib import Path

from dureh_core import containers, exectree, install
from dureh_core.site import CONTAINER_DEFAULTS, PROJECT_DEFAULTS
from dureh_core.util import DurehError
from tests.helpers import SiteCase

# the kind of block a package needs (proposals/module-submit-contract.md (b)), with neutral names
EXAMPLE_DEFAULTS = """config_defaults:
  project:
    manifest: example-manifest.yaml
  containers:
    cache_env: EXAMPLE_IMAGE_CACHE
  modules:
    - id: exmod
      submit:
        record_arg: --submission-record
        log_arg: --log-dir
        after_arg: --after
        items_arg: --items
"""


class ConfigDefaults(SiteCase):
    def package_defaults(self, block=EXAMPLE_DEFAULTS):
        m = self.package / "startup.yaml"
        m.write_text(m.read_text() + block)

    def cfg(self):
        return json.loads((self.site_root / "config.json").read_text())

    def test_harness_defaults_are_generic(self):
        self.install()
        s = self.site()
        self.assertEqual(s.project_manifest(), "manifest.yaml")
        self.assertEqual(containers.cache_env(s), "DUREH_CONTAINER_CACHE")
        self.assertNotIn("project", self.cfg())
        os.environ["DUREH_CONTAINER_CACHE"] = str(self.tmp / "cc")
        self.assertEqual(containers.cache_dir(s), self.tmp / "cc")
        os.environ["EXAMPLE_IMAGE_CACHE"] = str(self.tmp / "other")
        self.assertEqual(containers.cache_dir(s), self.tmp / "cc", "a package's variable is not read unless configured")
        for key in PROJECT_DEFAULTS:
            self.assertIsNotNone(s.project_setting(key))
        self.assertEqual(set(PROJECT_DEFAULTS), {"manifest"}, "release 46: no data-flow keys")
        self.assertEqual(set(CONTAINER_DEFAULTS), {"cache_env"}, "release 46: no image pinning keys")

    def test_install_merges_config_defaults_below_the_preset(self):
        self.package_defaults()
        self.install(containers={"module": "", "cache": "images"})
        c = self.cfg()
        self.assertEqual(c["project"], {"manifest": "example-manifest.yaml"})
        self.assertEqual(c["containers"], {"module": "", "cache": "images", "cache_env": "EXAMPLE_IMAGE_CACHE"},
                         "the preset's keys win; the package adds the ones it lacks")
        self.assertEqual(c["modules"], [{"id": "exmod", "submit": {"record_arg": "--submission-record", "log_arg": "--log-dir",
                                                                   "after_arg": "--after", "items_arg": "--items"}}])
        self.assertEqual(c["slurm"], {"account": "acct-x", "partition": "general", "each_level": "any",
                                      "external_jobs": False, "watch": "on-demand"},
                         "untouched keys as before (each_level any and external_jobs false are the test preset's; "
                         "release 46: a new site watches jobs on demand)")
        s = self.site()
        self.assertEqual(s.project_manifest(), "example-manifest.yaml")
        os.environ.pop("DUREH_CONTAINER_CACHE", None)
        os.environ["EXAMPLE_IMAGE_CACHE"] = str(self.tmp / "cc")
        self.assertEqual(containers.cache_dir(s), self.site_root / "images", "containers.cache wins over the variable")
        c["containers"].pop("cache")
        (self.site_root / "config.json").write_text(json.dumps(c))
        self.assertEqual(containers.cache_dir(self.site()), self.tmp / "cc")

    def test_link_module_keeps_the_seeded_submit_block(self):
        self.package_defaults()
        self.install()
        from dureh_core.sitelinks import _config_add

        s = self.site()
        _config_add(s, "modules", {"id": "exmod", "path": "modules/exmod", "source": "/src/exmod", "rev": "a" * 40})
        entry = [m for m in self.cfg()["modules"] if m["id"] == "exmod"]
        self.assertEqual(len(entry), 1)
        self.assertEqual((entry[0]["path"], entry[0]["submit"]["items_arg"]), ("modules/exmod", "--items"))

    def test_adapter_sync_adds_missing_keys_once_and_keeps_values(self):
        self.install(project={"manifest": "my-manifest.yaml"})
        self.package_defaults()  # the package source gains config_defaults after the site was installed
        s = self.site()
        self.assertEqual(install.apply_config_defaults(s), [], "only the installed package is read, not the source")
        install.sync_package(s)  # `adapter sync` re-copies the package first
        added = install.apply_config_defaults(self.site())
        self.assertEqual(added, ["containers", "modules[exmod]"])
        c = self.cfg()
        self.assertEqual(c["project"]["manifest"], "my-manifest.yaml", "the site's value stands")
        self.assertEqual(c["containers"]["cache_env"], "EXAMPLE_IMAGE_CACHE")
        self.assertEqual(install.apply_config_defaults(self.site()), [], "nothing to add the second time")
        from dureh_core.cli import main
        import contextlib, io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["--site", str(self.site_root), "adapter", "sync"]), 0)
        self.assertNotIn("config_defaults", buf.getvalue())
        self.assertEqual(self.cfg()["project"]["manifest"], "my-manifest.yaml")

    def test_bad_blocks_are_refused(self):
        for block, msg in (("config_defaults: [1]\n", "must be a mapping"),
                           ("config_defaults:\n  site:\n    id: x\n", "not a key a package may seed"),
                           ("config_defaults:\n  project: nope\n", "project must be a mapping"),
                           ("config_defaults:\n  modules:\n    - submit: {}\n", "list of {id")):
            m = self.package / "startup.yaml"
            text = m.read_text()
            m.write_text(text + block)
            with self.assertRaisesRegex(DurehError, msg):
                install.validate_package(self.package)
            m.write_text(text)

    def test_merge_rules(self):
        merged, added = install.merge_config_defaults(
            {"a": {"x": 1, "n": None}, "modules": [{"id": "m", "path": "p"}], "list": [1]},
            {"a": {"x": 2, "y": 3, "n": 4}, "b": {"k": "v"}, "list": [9], "modules": [{"id": "m", "submit": {"q": "-q"}}, {"id": "z"}]})
        self.assertEqual(merged["a"], {"x": 1, "n": 4, "y": 3}, "a null value is filled, a set one kept")
        self.assertEqual(merged["b"], {"k": "v"})
        self.assertEqual(merged["list"], [1], "lists are not merged")
        self.assertEqual(merged["modules"], [{"id": "m", "path": "p", "submit": {"q": "-q"}}, {"id": "z"}])
        self.assertEqual(added, ["a.y", "a.n", "b", "modules[m].submit", "modules[z]"])


class ProjectManifestKey(SiteCase):
    """exectree reads the pin from the configured manifest name; null turns the feature off."""

    def setUp(self):
        super().setUp()
        self.install()
        self.proj = self.site_root / "projects" / "lna"
        self.proj.mkdir(parents=True)
        (self.proj / "manifest.yaml").write_text("provenance:\n  tool_versions:\n    demo: \"1.0+abc1234\"\n")
        (self.proj / "example-manifest.yaml").write_text("provenance:\n  tool_versions:\n    demo: \"1.0+def5678\"\n")

    def with_project(self, **project):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["project"] = project
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        return self.site()

    def test_pin_follows_the_configured_name(self):
        self.assertEqual(exectree.manifest_pin(self.site(), "projects/lna", "demo"), ("abc1234", "projects/lna/manifest.yaml"))
        s = self.with_project(manifest="example-manifest.yaml")
        self.assertEqual(exectree.manifest_pin(s, "projects/lna", "demo"), ("def5678", "projects/lna/example-manifest.yaml"))
        s = self.with_project(manifest=None)
        self.assertEqual(exectree.manifest_pin(s, "projects/lna", "demo"), (None, None))
        self.assertIsNone(s.project_manifest())
        mod = self.make_module("demo")
        with self.assertRaisesRegex(DurehError, "project.manifest is unset.*pass --rev"):
            exectree.prepare(s, "WI-0001", "projects/lna", "demo", None, Path(mod / "tests" / "test_demo.py"), dry_run=True)


class SyncChecksTheIncomingPackage(SiteCase):
    """Release 65: `adapter sync [--package-rev R]` merges R's config_defaults INTO mapping keys the site already has
    (site values win per leaf, lists are not merged) and checks the config as R leaves it against R's files, so a site
    whose office_hours block lacks the profiles R supplies is not refused, and a profile file only R has passes."""

    def setUp(self):
        super().setUp()
        import subprocess

        def git(*args):
            return subprocess.run(["git", *args], cwd=self.package, check=True, capture_output=True,
                                  text=True).stdout.strip()

        self.git = git
        git("init", "-q", "-b", "main")
        git("add", "-A")
        git("commit", "-qm", "package 1")
        self.install()
        cfg = self.cfg()
        cfg["office_hours"] = {"enabled": True, "partition": "gpu", "server": {"kind": "ollama", "port": 11999}}
        (self.site_root / "config.json").write_text(json.dumps(cfg))

    def cfg(self):
        return json.loads((self.site_root / "config.json").read_text())

    def sync(self, *extra):
        import contextlib
        import io

        from dureh_core.cli import main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(["--site", str(self.site_root), "adapter", "sync", *extra])
            except DurehError as exc:
                return 1, str(exc)
        return code, out.getvalue() + err.getvalue()

    def package_rev(self, profile_file="profiles/coder.Modelfile", write_file=True):
        (self.package / "profiles").mkdir(exist_ok=True)
        if write_file:
            (self.package / profile_file).write_text("FROM base\nPARAMETER num_ctx 8192\n")
        m = self.package / "startup.yaml"
        m.write_text(m.read_text() + "config_defaults:\n  office_hours:\n    enabled: false\n    server:\n"
                     "      port: 1\n      bin: srv\n    profiles:\n"
                     f"      - {{name: coder-x, file: {profile_file}, base: base}}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "package 2")
        return self.git("rev-parse", "HEAD")

    def test_profiles_arrive_into_the_existing_block(self):
        rev = self.package_rev()
        self.assertFalse((self.site_root / "startup" / "profiles" / "coder.Modelfile").exists())
        code, out = self.sync("--package-rev", rev)
        self.assertEqual(code, 0, out)
        oh = self.cfg()["office_hours"]
        self.assertEqual(oh["profiles"], [{"name": "coder-x", "file": "profiles/coder.Modelfile", "base": "base"}])
        self.assertEqual((oh["enabled"], oh["partition"]), (True, "gpu"), "the site's values win")
        self.assertEqual(oh["server"], {"kind": "ollama", "port": 11999, "bin": "srv"}, "merged per leaf")
        self.assertIn("office_hours.profiles", out)
        self.assertTrue((self.site_root / "startup" / "profiles" / "coder.Modelfile").is_file())

    def test_a_profile_missing_from_the_incoming_package_refuses_and_changes_nothing(self):
        before = self.cfg()["startup"]["rev"]
        rev = self.package_rev(profile_file="profiles/missing.Modelfile", write_file=False)
        code, out = self.sync("--package-rev", rev)
        self.assertNotEqual(code, 0)
        self.assertIn("profiles/missing.Modelfile is not in the package being installed", out)
        self.assertEqual(self.cfg()["startup"]["rev"], before)
        self.assertNotIn("profiles", self.cfg()["office_hours"])
        self.assertFalse((self.site_root / "startup.new").exists())

    def test_without_the_package_profiles_the_old_refusal_stands(self):
        code, out = self.sync()
        self.assertNotEqual(code, 0)
        self.assertIn("office_hours.enabled needs at least one profile", out)
