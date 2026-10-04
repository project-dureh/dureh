"""UR-20261001-07: py-writer may regenerate a module's uv.lock, nothing outside its module."""
import unittest

from dureh_core.org import HARNESS_ROOT, Org


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


if __name__ == "__main__":
    unittest.main()
