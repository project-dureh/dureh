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


if __name__ == "__main__":
    unittest.main()
