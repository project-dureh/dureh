"""The YAML subset parser: flow mappings, and loud failure instead of guessing."""
import unittest

from dureh_core import yamlsub
from dureh_core.util import DurehError


def manifest_v2(links=50):
    """A project manifest in the shape company manifests use (nested mappings, flow mappings, comments, quoted and
    null scalars, long link lists), with neutral names."""
    rows = [f"inputs/s-{i:03d}" for i in range(links)]
    link_lines = "".join(f'  - {{path: "{r}", link_target: "/proj/lab/{r}", external: true, traversed: false, size_bytes: 10}}\n'
                         for r in rows)
    return ("schema_version: 2\nproject:\n  name: \"p1\"\n"
            "execution:\n  mode: \"slurm\"\n  slurm:\n    account: \"acct\"\n    partition: \"general\"\n"
            "source:\n  type: \"raw\"   # note\n  external_root: \"/proj/lab/p1\"\n  size_bytes: 17490933543\n"
            "pipelines:\n  - family: \"pipe\"\n    axes: {}\n    tool_version: null\n    workflows:\n"
            "      - name: \"pipe.default\"\n        default: true\n"
            "provenance:\n  tool_versions:\n    exmod: \"2.0.0a0+68b4d2d\"\n    containers:\n"
            "      - {name: \"a\", digest: \"sha256:aaa\"}\n"
            f"external_links:   # {len(rows)} entries\n{link_lines}")


class FlowMappings(unittest.TestCase):
    def test_flow_mapping_items_in_a_sequence(self):
        text = ('links:   # a comment\n'
                '  - {path: "inputs/sub-1", link_target: "/proj/x/sub-1", external: true, size_bytes: 45}\n'
                '  - {name: "afni", digest: "sha256:fb4f"}\n')
        self.assertEqual(yamlsub.loads(text)["links"], [
            {"path": "inputs/sub-1", "link_target": "/proj/x/sub-1", "external": True, "size_bytes": 45},
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
        text = manifest_v2(50)
        self.assertEqual(yamlsub.loads(text), yaml.safe_load(text))

    def test_block_mapping_items_still_work(self):
        self.assertEqual(yamlsub.loads("- name: x\n  role: y\n"), [{"name": "x", "role": "y"}])


if __name__ == "__main__":
    unittest.main()
