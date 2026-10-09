"""A scratch role (isolation scratch, no WI worktree) writes only its declared scope in the target's main tree:
review 2026-10-07 #8, proved since release 46 on the example package's `runner` (submit_jobs, scratch)."""
import subprocess
import unittest

from helpers import ExampleSiteCase, example_org

from dureh_core.paths import write_decision


class ScratchRoleScope(unittest.TestCase):
    def setUp(self):
        self.org = example_org()

    def test_declared_files_are_in_scope(self):
        for rel in ("projects/p/checkpoints/step-a/run.json", "projects/p/checkpoints/a/b/c.txt", "projects/p/README.md"):
            self.assertTrue(self.org.scope_allows("runner", rel), rel)

    def test_other_project_code_and_data_stay_refused(self):
        # scope globs are segment-aware: `projects/*/README.md` is the project's top-level README only
        for rel in ("projects/p/code/run.sh", "projects/p/inputs/a.txt", "projects/p/published/x", "README.md",
                    "projects/p/inputs/README.md", "projects/p/catalog.yaml", "modules/m/src/x.py"):
            self.assertFalse(self.org.scope_allows("runner", rel), rel)


class ScratchRoleWrites(ExampleSiteCase):
    """The runner has no WI worktree, so its scope must hold through write_decision in the git-tracked project's main
    tree; nothing else in that tree opens, and worktree roles still write tracked files only in their worktree."""

    def setUp(self):
        super().setUp()
        self.install()
        self.proj = self.site_root / "projects" / "p"
        (self.proj / "code").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        self.s = self.site()

    def decide(self, role, rel):
        return write_decision(self.s, role, self.proj / rel)

    def test_scope_paths_are_writable_in_the_main_tree(self):
        for rel in ("README.md", "checkpoints/a/run.json", "checkpoints/a/b/fig.txt"):
            ok, why = self.decide("runner", rel)
            self.assertTrue(ok, f"{rel}: {why}")

    def test_everything_else_stays_refused(self):
        for rel in ("code/run.sh", "inputs/a.txt", "published/x", "sub/README.md", "manifest.yaml", ".gitattributes"):
            ok, why = self.decide("runner", rel)
            self.assertFalse(ok, rel)
        for role, rel in (("builder", "code/x.py"), ("publisher", "catalog.yaml")):
            ok, why = self.decide(role, rel)
            self.assertFalse(ok, (role, rel))
            self.assertIn("only inside the WI worktree", why)


if __name__ == "__main__":
    unittest.main()
