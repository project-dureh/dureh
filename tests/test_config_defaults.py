"""UP-0069: company values (manifest name, pipeline version key, derivative naming, container cache variable) are
config keys with generic harness defaults; a startup package seeds its own through `config_defaults` in startup.yaml,
merged at install below the preset and added to an existing site's config.json by `adapter sync` (missing keys only)."""
import json
import os
from pathlib import Path

from dureh_core import containers, exectree, install
from dureh_core.site import CONTAINER_DEFAULTS, PROJECT_DEFAULTS
from dureh_core.util import DurehError
from tests.helpers import SiteCase

# the block startup-packages/camri-org/startup.yaml needs (proposals/module-submit-contract.md (b))
CAMRI_DEFAULTS = """config_defaults:
  project:
    manifest: camri-manifest.yaml
    pipeline_version_key: camri_version
    derivative_entry: "{family}_camri_{species}_{suffix}"
  containers:
    cache_env: CAMRI_CONTAINER_CACHE
    manifest_globs: ["*manifest*.yaml", "*manifest*.yml", "*manifest*.json"]
  modules:
    - id: camri
      submit:
        record_arg: --submission-record
        log_arg: --log-dir
        after_arg: --after
        items_arg: --items
"""


class ConfigDefaults(SiteCase):
    def package_defaults(self, block=CAMRI_DEFAULTS):
        m = self.package / "startup.yaml"
        m.write_text(m.read_text() + block)

    def cfg(self):
        return json.loads((self.site_root / "config.json").read_text())

    def test_harness_defaults_are_generic(self):
        self.install()
        s = self.site()
        self.assertEqual(s.project_manifest(), "manifest.yaml")
        self.assertEqual(s.project_setting("pipeline_version_key"), "version")
        self.assertEqual(s.project_setting("derivative_entry"), "{family}_{species}_{suffix}")
        self.assertEqual(containers.cache_env(s), "DUREH_CONTAINER_CACHE")
        self.assertEqual(containers.manifest_globs(s), ["manifest.yaml", "*manifest*.yaml", "*manifest*.yml", "*manifest*.json"])
        self.assertNotIn("project", self.cfg())
        os.environ["DUREH_CONTAINER_CACHE"] = str(self.tmp / "cc")
        self.assertEqual(containers.cache_dir(s), self.tmp / "cc")
        os.environ["CAMRI_CONTAINER_CACHE"] = str(self.tmp / "other")
        self.assertEqual(containers.cache_dir(s), self.tmp / "cc", "a company's variable is not read unless configured")
        for key in PROJECT_DEFAULTS:
            self.assertIsNotNone(s.project_setting(key))
        self.assertEqual(set(CONTAINER_DEFAULTS), {"cache_env", "manifest_globs"})

    def test_install_merges_config_defaults_below_the_preset(self):
        self.package_defaults()
        self.install(containers={"module": "", "cache": "images"})
        c = self.cfg()
        self.assertEqual(c["project"], {"manifest": "camri-manifest.yaml", "pipeline_version_key": "camri_version",
                                        "derivative_entry": "{family}_camri_{species}_{suffix}"})
        self.assertEqual(c["containers"], {"module": "", "cache": "images", "cache_env": "CAMRI_CONTAINER_CACHE",
                                           "manifest_globs": ["*manifest*.yaml", "*manifest*.yml", "*manifest*.json"]},
                         "the preset's keys win; the package adds the ones it lacks")
        self.assertEqual(c["modules"], [{"id": "camri", "submit": {"record_arg": "--submission-record", "log_arg": "--log-dir",
                                                                   "after_arg": "--after", "items_arg": "--items"}}])
        self.assertEqual(c["slurm"], {"account": "acct-x", "partition": "general", "each_level": "any"},
                         "untouched keys as before (each_level any is the test preset's)")
        s = self.site()
        self.assertEqual(s.project_manifest(), "camri-manifest.yaml")
        self.assertEqual(containers.manifest_globs(s), ["camri-manifest.yaml", "*manifest*.yaml", "*manifest*.yml", "*manifest*.json"])
        os.environ.pop("DUREH_CONTAINER_CACHE", None)
        os.environ["CAMRI_CONTAINER_CACHE"] = str(self.tmp / "cc")
        self.assertEqual(containers.cache_dir(s), self.site_root / "images", "containers.cache wins over the variable")
        c["containers"].pop("cache")
        (self.site_root / "config.json").write_text(json.dumps(c))
        self.assertEqual(containers.cache_dir(self.site()), self.tmp / "cc")

    def test_link_module_keeps_the_seeded_submit_block(self):
        self.package_defaults()
        self.install()
        from dureh_core.sitelinks import _config_add

        s = self.site()
        _config_add(s, "modules", {"id": "camri", "path": "modules/camri", "source": "/src/camri", "rev": "a" * 40})
        entry = [m for m in self.cfg()["modules"] if m["id"] == "camri"]
        self.assertEqual(len(entry), 1)
        self.assertEqual((entry[0]["path"], entry[0]["submit"]["items_arg"]), ("modules/camri", "--items"))

    def test_adapter_sync_adds_missing_keys_once_and_keeps_values(self):
        self.install(project={"manifest": "my-manifest.yaml"})
        self.package_defaults()  # the package source gains config_defaults after the site was installed
        s = self.site()
        added = install.apply_config_defaults(s)
        self.assertEqual(added, ["project.pipeline_version_key", "project.derivative_entry", "containers",
                                 "modules[camri]"])
        c = self.cfg()
        self.assertEqual(c["project"]["manifest"], "my-manifest.yaml", "the site's value stands")
        self.assertEqual(c["project"]["pipeline_version_key"], "camri_version")
        self.assertEqual(c["containers"]["cache_env"], "CAMRI_CONTAINER_CACHE")
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
        (self.proj / "camri-manifest.yaml").write_text("provenance:\n  tool_versions:\n    demo: \"1.0+def5678\"\n")

    def with_project(self, **project):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["project"] = project
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        return self.site()

    def test_pin_follows_the_configured_name(self):
        self.assertEqual(exectree.manifest_pin(self.site(), "projects/lna", "demo"), ("abc1234", "projects/lna/manifest.yaml"))
        s = self.with_project(manifest="camri-manifest.yaml")
        self.assertEqual(exectree.manifest_pin(s, "projects/lna", "demo"), ("def5678", "projects/lna/camri-manifest.yaml"))
        s = self.with_project(manifest=None)
        self.assertEqual(exectree.manifest_pin(s, "projects/lna", "demo"), (None, None))
        self.assertIsNone(s.project_manifest())
        mod = self.make_module("demo")
        with self.assertRaisesRegex(DurehError, "project.manifest is unset.*pass --rev"):
            exectree.prepare(s, "WI-0001", "projects/lna", "demo", None, Path(mod / "tests" / "test_demo.py"), dry_run=True)

    def test_projectgen_uses_the_configured_keys(self):
        from dureh_core import projectgen as pg

        self.assertEqual(pg._derivative_entry("{family}_{species}_{suffix}", {"family": "f", "species": "rat", "suffix": "bold"}),
                         "f_rat_bold")
        self.assertEqual(pg._derivative_entry("{family}_camri_{species}_{suffix}", {"family": "f", "species": "rat", "suffix": "bold"}),
                         "f_camri_rat_bold")
        self.assertEqual(pg._derivative_entry("{family}_{nope}", {"family": "f"}), "f_?", "an unknown field never crashes")
        self.assertIsNone(pg._derivative_entry("", {}))
        s = self.with_project(manifest=None)
        self.assertEqual(pg._manifest(s, self.proj), {}, "feature off: no manifest is read")
        s = self.with_project()  # back to the harness default
        self.assertEqual(pg._manifest(s, self.proj)["provenance"]["tool_versions"]["demo"], "1.0+abc1234")

    def test_container_manifest_globs_are_config(self):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["containers"] = {"manifest_globs": ["pins.json"]}
        cfg["project"] = {"manifest": "camri-manifest.yaml"}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(containers.manifest_globs(self.site()), ["camri-manifest.yaml", "pins.json"])
        (self.proj / "pins.json").write_text("{}")
        self.assertEqual([p.name for p in containers._manifests(self.site())], ["camri-manifest.yaml", "pins.json"])
        cfg["containers"] = {"manifest_globs": "x"}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        with self.assertRaisesRegex(DurehError, "manifest_globs must be a list"):
            containers.manifest_globs(self.site())
