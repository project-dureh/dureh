"""pipeline-runner writes only what a workflow run produces: checkpoints, and the project notebooks and README
"data usage" entries each run regenerates (Director, 2026-10-04); other project code and data stay out."""
import unittest

from dureh_core.org import HARNESS_ROOT, Org


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


if __name__ == "__main__":
    unittest.main()
