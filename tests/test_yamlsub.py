"""The YAML subset parser: flow mappings, and loud failure instead of guessing."""
import unittest

from dureh_core import yamlsub
from dureh_core.util import DurehError


class FlowMappings(unittest.TestCase):
    def test_flow_mapping_items_in_a_sequence(self):
        text = ('links:   # a comment\n'
                '  - {path: "rawbids/sub-1", link_target: "/proj/x/sub-1", external: true, size_bytes: 45}\n'
                '  - {name: "afni", digest: "sha256:fb4f"}\n')
        self.assertEqual(yamlsub.loads(text)["links"], [
            {"path": "rawbids/sub-1", "link_target": "/proj/x/sub-1", "external": True, "size_bytes": 45},
            {"name": "afni", "digest": "sha256:fb4f"}])

    def test_flow_mapping_value_and_empty(self):
        self.assertEqual(yamlsub.loads("a: {x: 1, y: \"p, q\"}\nb: {}\n"), {"a": {"x": 1, "y": "p, q"}, "b": {}})

    def test_quoted_colons_and_commas_stay_in_values(self):
        v = yamlsub.loads('- {t: "2026-04-02T17:31:20Z", s: "a, b: c"}\n')
        self.assertEqual(v, [{"t": "2026-04-02T17:31:20Z", "s": "a, b: c"}])

    def test_unsupported_forms_fail_loudly(self):
        for text in ('- {a: {b: 1}}\n', '- {a: [1, 2]}\n', '- {a 1}\n', '- {a: 1\n', 'k: {a: 1, a: 2}\n'):
            with self.assertRaises(DurehError, msg=text):
                yamlsub.loads(text)

    def test_matches_pyyaml_on_a_v2_manifest(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed: not run")
        from test_projectgen import manifest_v2

        text = manifest_v2(50)
        self.assertEqual(yamlsub.loads(text), yaml.safe_load(text))

    def test_block_mapping_items_still_work(self):
        self.assertEqual(yamlsub.loads("- name: x\n  role: y\n"), [{"name": "x", "role": "y"}])


if __name__ == "__main__":
    unittest.main()
