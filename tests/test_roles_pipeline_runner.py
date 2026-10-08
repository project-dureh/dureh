"""pipeline-runner writes only what a workflow run produces: checkpoints, and the project notebooks and README
"data usage" entries each run regenerates (Director, 2026-10-04); other project code and data stay out."""
import subprocess
import unittest

from helpers import SiteCase

from dureh_core.org import HARNESS_ROOT, Org
from dureh_core.paths import write_decision


class PipelineRunnerScope(unittest.TestCase):
    def setUp(self):
        self.org = Org.load(HARNESS_ROOT)

    def test_regenerated_files_are_in_scope(self):
        for rel in ("projects/lna/checkpoints/phase-d/run.json", "projects/lna/code/notebooks/camri_preproc.py",
                    "projects/lna/code/notebooks/sub/figures.py", "projects/lna/README.md"):
            self.assertTrue(self.org.scope_allows("pipeline-runner", rel), rel)

    def test_other_project_code_and_data_stay_refused(self):
        # scope globs are segment-aware: `projects/*/README.md` is the project's top-level README only
        for rel in ("projects/lna/code/run.sh", "projects/lna/code/containers/fsl.def", "projects/lna/code/readme.txt",
                    "projects/lna/sourcedata/sub-01/anat.nii.gz", "projects/lna/derivatives/x",
                    "README.md", "projects/lna/sourcedata/README.md", "projects/lna/code/notebooks",
                    "modules/camri/src/x.py", "projects/lna/camri-manifest.yaml"):
            self.assertFalse(self.org.scope_allows("pipeline-runner", rel), rel)


class PipelineRunnerWrites(SiteCase):
    """Review 2026-10-07 #8: the runner has no WI worktree (isolation scratch), so its README/notebook scope must hold
    through write_decision in the git-tracked project's main tree; nothing else in that tree opens."""

    def setUp(self):
        super().setUp()
        manifest = self.package / "startup.yaml"
        manifest.write_text(manifest.read_text()
                            .replace("managers: [pm-modules]", "managers: [pm-modules, pm-projects]")
                            .replace("workers: [py-writer,", "workers: [data-steward, pipeline-runner, py-writer,"))
        self.install()
        self.proj = self.site_root / "projects" / "lna"
        (self.proj / "code" / "notebooks").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        self.s = self.site()

    def decide(self, role, rel):
        return write_decision(self.s, role, self.proj / rel)

    def test_scope_paths_are_writable_in_the_main_tree(self):
        for rel in ("README.md", "code/notebooks/preproc.py", "code/notebooks/sub/fig.py", "checkpoints/a/run.json"):
            ok, why = self.decide("pipeline-runner", rel)
            self.assertTrue(ok, f"{rel}: {why}")

    def test_everything_else_stays_refused(self):
        for rel in ("code/run.sh", "code/containers/x.def", "sourcedata/sub-01/a.nii.gz", "derivatives/x",
                    "sub/README.md", "manifest.yaml", ".gitattributes"):
            ok, why = self.decide("pipeline-runner", rel)
            self.assertFalse(ok, rel)
        # worktree roles still write a git-tracked target only inside their WI worktree
        for role, rel in (("py-writer", "code/notebooks/x.py"), ("data-steward", "dataset_description.json")):
            ok, why = self.decide(role, rel)
            self.assertFalse(ok, (role, rel))
            self.assertIn("only inside the WI worktree", why)


if __name__ == "__main__":
    unittest.main()
