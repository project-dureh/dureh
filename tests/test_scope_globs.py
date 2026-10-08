"""Review 2026-10-07 #7: write-scope globs are segment-aware. `*` and `?` never cross `/`; `**` spans depth."""
import unittest

from dureh_core.org import HARNESS_ROOT, Org, scope_match


class ScopeGlobMatching(unittest.TestCase):
    def test_star_and_question_stay_in_one_segment(self):
        self.assertTrue(scope_match("projects/p/README.md", "projects/*/README.md"))
        self.assertFalse(scope_match("projects/p/sub/README.md", "projects/*/README.md"))
        self.assertTrue(scope_match("modules/ab/x", "modules/a?/x"))
        self.assertFalse(scope_match("modules/a/b/x", "modules/a?b/x"))

    def test_double_star(self):
        self.assertTrue(scope_match("projects/p/derivatives/a/b.nii.gz", "projects/*/derivatives/**"))
        self.assertFalse(scope_match("projects/p/derivatives", "projects/*/derivatives/**"))
        self.assertTrue(scope_match("a/b", "a/**/b"))
        self.assertTrue(scope_match("a/x/y/b", "a/**/b"))
        self.assertFalse(scope_match("a/x/y/c", "a/**/b"))

    def test_literal_characters_are_not_regex(self):
        self.assertFalse(scope_match("projects/p/READMExmd", "projects/*/README.md"))


class ScopesDoNotCrossDirectories(unittest.TestCase):
    def setUp(self):
        self.org = Org.load(HARNESS_ROOT)

    def test_data_steward_cannot_reach_sourcedata_through_star(self):
        for rel in ("projects/p/sourcedata/sub-01/derivatives/x.nii.gz",
                    "projects/p/sourcedata/sub-01/code/containers/x.def",
                    "projects/p/sourcedata/sub-01/dataset_description.json",
                    "projects/p/sub-01/.datalad/config"):
            self.assertFalse(self.org.scope_allows("data-steward", rel), rel)

    def test_py_writer_single_level_files_stay_single_level(self):
        for rel in ("modules/m/sub/src/x.py", "modules/m/data/uv.lock", "modules/m/data/pyproject.toml"):
            self.assertFalse(self.org.scope_allows("py-writer", rel), rel)

    def test_intended_patterns_still_allowed(self):
        allowed = {
            "data-steward": ["projects/p/derivatives/ica/a/b.nii.gz", "projects/p/dataset_description.json",
                             "projects/p/.datalad/config", "projects/p/sourcedata/atlases/a/b.nii.gz",
                             "projects/p/code/containers/fsl.def"],
            "py-writer": ["modules/m/src/pkg/x.py", "modules/m/tests/t.py", "modules/m/uv.lock",
                          "projects/p/code/notebooks/a/b.py"],
            "pipeline-runner": ["projects/p/checkpoints/a/b.json", "projects/p/README.md"],
            "desk": ["projects/p/README.md", "package/a/b"],
        }
        for role, paths in allowed.items():
            if role not in self.org.roles:
                continue
            for rel in paths:
                self.assertTrue(self.org.scope_allows(role, rel), (role, rel))

    def test_site_scopes_are_segment_aware_too(self):
        self.assertTrue(self.org.site_scope_allows("desk", "notes/a/b.md"))
        self.assertFalse(self.org.site_scope_allows("desk", "state/records/x"))


if __name__ == "__main__":
    unittest.main()
