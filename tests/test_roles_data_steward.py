"""UR-20260930-01 option A: data-steward may write promoted derivatives, never sourcedata or raw BIDS."""
import unittest

from dureh_core.org import HARNESS_ROOT, Org


class DataStewardScope(unittest.TestCase):
    def setUp(self):
        self.org = Org.load(HARNESS_ROOT)

    def test_derivatives_are_in_scope(self):
        self.assertTrue(self.org.scope_allows("data-steward", "projects/lna/derivatives/group-ica/melodic_IC.nii.gz"))

    def test_source_and_raw_data_stay_out_of_scope(self):
        for rel in ("projects/lna/sourcedata/raw/sub-01.nii.gz", "projects/lna/rawbids/sub-01/anat/x.nii.gz",
                    "projects/lna/sub-01/anat/sub-01_T2w.nii.gz"):
            # hook paths are resolved by classify() before scope checks, so ".." never reaches scope_allows
            self.assertFalse(self.org.scope_allows("data-steward", rel), rel)

    def test_no_other_role_gains_derivatives(self):
        others = [r for r in self.org.roles if r != "data-steward"
                  and self.org.scope_allows(r, "projects/lna/derivatives/x")]
        self.assertEqual(others, [])

    def test_never_line_names_what_stays_forbidden(self):
        never = self.org.role("data-steward").never
        for word in ("sourcedata", "raw BIDS", "pipeline execution", "code"):
            self.assertIn(word, never)


if __name__ == "__main__":
    unittest.main()
