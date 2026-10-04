"""UR-20261001-07: py-writer may regenerate a module's uv.lock, nothing outside its module."""
import json
import unittest

from dureh_core import workflow as wf
from dureh_core.org import HARNESS_ROOT, Org
from dureh_core.paths import write_decision
from dureh_core.records import Ledger
from tests.helpers import SiteCase


class PyWriterScope(unittest.TestCase):
    def setUp(self):
        self.org = Org.load(HARNESS_ROOT)

    def test_module_lock_is_in_scope(self):
        self.assertTrue(self.org.scope_allows("py-writer", "modules/camri/uv.lock"))

    def test_other_locks_stay_out_of_scope(self):
        # fnmatch "*" spans "/", so nested paths under a module match too; .venv/ is kept out of the
        # changed list by changed_paths (UP-0023), not by the scope.
        for rel in ("uv.lock", "modules/uv.lock", "projects/lna/uv.lock", "pyproject.toml"):
            self.assertFalse(self.org.scope_allows("py-writer", rel), rel)


class Notebooks(unittest.TestCase):
    # UR-20261003-06 (UNC_Lee_CAMRI-ORG): one marimo notebook per pipeline in the project's code/ folder
    def test_project_notebooks_in_scope_other_project_code_not(self):
        from dureh_core.org import HARNESS_ROOT, Org

        org = Org.load(HARNESS_ROOT)
        self.assertTrue(org.scope_allows("py-writer", "projects/lna/code/notebooks/camri_preproc.py"))
        for rel in ("projects/lna/code/containers/fsl.def", "projects/lna/code/run.sh", "projects/lna/derivatives/x",
                    "projects/lna/README.md"):
            self.assertFalse(org.scope_allows("py-writer", rel), rel)


class ModuleContainerRecipes(SiteCase):
    """UR-20261004-03 item 1: a module's container recipes (modules/<m>/containers/) are py-writer's, written in
    the WI worktree of a module WI; no other worker gains them and the image cache stays closed to every role."""

    def setUp(self):
        super().setUp()
        self.install()
        self.make_module("camri")
        self.s = self.site()
        led = Ledger(self.s)
        bundle = wf.wi_new(self.s, "desk", "Bundle", "g", "pm-modules", "modules/camri", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", bundle, "ok")
        self.w = wf.wi_new(self.s, "desk", "Recipe", "g", "py-writer", "modules/camri", ["ok"], parent=bundle)["id"]
        wf.wi_approve(self.s, "desk", self.w, "ok")
        led.event("wi.dispatched", "desk", self.w, {"role": "py-writer", "brief": "0" * 16, "by": "desk"})
        self.recipe = self.site_root / "worktrees" / self.w / "containers" / "camri.def"
        self.recipe.parent.mkdir(parents=True)

    def test_py_writer_writes_module_recipes_in_its_worktree(self):
        self.assertTrue(self.s.org.scope_allows("py-writer", "modules/camri/containers/camri.def"))
        ok, why = write_decision(self.s, "py-writer", self.recipe, agent_wi=self.w)
        self.assertTrue(ok, why)
        ok, _ = write_decision(self.s, "py-writer", self.recipe, agent_wi="WI-9999")
        self.assertFalse(ok, "only in its own WI worktree")

    def test_other_roles_are_refused(self):
        for role in self.s.org.roles:
            if role == "py-writer":
                continue
            ok, why = write_decision(self.s, role, self.recipe)
            self.assertFalse(ok, (role, why))
            self.assertFalse(self.s.org.scope_allows(role, "modules/camri/containers/camri.def"), role)

    def test_cache_stays_protected(self):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["containers"] = {"module": "", "cache": "cache/containers"}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        (self.site_root / "cache" / "containers").mkdir(parents=True)
        s = self.site()
        ok, why = write_decision(s, "py-writer", self.site_root / "cache" / "containers" / "camri.def", agent_wi=self.w)
        self.assertFalse(ok)
        self.assertIn("container cache", why)


if __name__ == "__main__":
    unittest.main()
