import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import demo  # noqa: E402


class Present(unittest.TestCase):
    def test_reads_manifest(self):
        path = os.path.join(tempfile.mkdtemp(), "m.json")
        with open(path, "w") as s:
            json.dump({"runs": 2}, s)
        self.assertEqual(demo.load_manifest(path), {"runs": 2})
