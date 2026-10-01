"""TOOL_NAMES must list every user-facing harness subcommand."""
import argparse
import unittest

from dureh_core import cli
from dureh_core.site import TOOL_NAMES

NOT_TOOLS = {"install", "adapter", "hook"}
UNCLASSIFIED = {"site", "authorize-write", "event", "artifact"}  # not covered by UR-20260930-07


class ToolNames(unittest.TestCase):
    def test_every_subcommand_is_a_listed_tool(self):
        p = cli.parser()
        sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
        commands = set(sub.choices) - NOT_TOOLS - UNCLASSIFIED
        self.assertEqual(commands, set(TOOL_NAMES))

    def test_help_description_names_every_tool(self):
        desc = cli.parser().description
        for t in TOOL_NAMES:
            self.assertIn(t, desc)


if __name__ == "__main__":
    unittest.main()
