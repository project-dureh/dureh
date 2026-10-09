"""Review 2026-10-07 #7: write-scope globs are segment-aware. `*` and `?` never cross `/`; `**` spans depth."""
import unittest

from helpers import example_org

from dureh_core.org import scope_match


class ScopeGlobMatching(unittest.TestCase):
    def test_star_and_question_stay_in_one_segment(self):
        self.assertTrue(scope_match("projects/p/README.md", "projects/*/README.md"))
        self.assertFalse(scope_match("projects/p/sub/README.md", "projects/*/README.md"))
        self.assertTrue(scope_match("modules/ab/x", "modules/a?/x"))
        self.assertFalse(scope_match("modules/a/b/x", "modules/a?b/x"))

    def test_double_star(self):
        self.assertTrue(scope_match("projects/p/published/a/b.dat", "projects/*/published/**"))
        self.assertFalse(scope_match("projects/p/published", "projects/*/published/**"))
        self.assertTrue(scope_match("a/b", "a/**/b"))
        self.assertTrue(scope_match("a/x/y/b", "a/**/b"))
        self.assertFalse(scope_match("a/x/y/c", "a/**/b"))

    def test_literal_characters_are_not_regex(self):
        self.assertFalse(scope_match("projects/p/READMExmd", "projects/*/README.md"))


class ScopesDoNotCrossDirectories(unittest.TestCase):
    def setUp(self):
        self.org = example_org()

    def test_publisher_cannot_reach_inputs_through_star(self):
        for rel in ("projects/p/inputs/catalog.yaml", "projects/p/inputs/a/catalog.yaml", "projects/p/a/catalog.yaml"):
            self.assertFalse(self.org.scope_allows("publisher", rel), rel)

    def test_builder_single_level_files_stay_single_level(self):
        for rel in ("modules/m/sub/src/x.py", "modules/m/data/pyproject.toml"):
            self.assertFalse(self.org.scope_allows("builder", rel), rel)

    def test_intended_patterns_still_allowed(self):
        allowed = {
            "publisher": ["projects/p/catalog.yaml"],
            "builder": ["modules/m/src/pkg/x.py", "modules/m/tests/t.py", "modules/m/pyproject.toml"],
            "runner": ["projects/p/checkpoints/a/b.json", "projects/p/README.md"],
            "worker": ["modules/m/src/x.py", "modules/m/docs/a.md"],
            "desk": ["projects/p/README.md"],
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
