"""Review 2026-10-07 #11: the manager's re-dispatch count and the report line limits in generated agent files and
briefs come from org/tiers.yaml limits (config `limits` overrides), not hardcoded text."""
import json

from helpers import SiteCase

from dureh_core.runtimes import claude_code


class AgentLimits(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()

    def agent(self, name):
        claude_code.generate(self.site())
        return (self.site_root / ".claude" / "agents" / f"{name}.md").read_text()

    def test_defaults(self):
        text = self.agent("pm-modules")
        self.assertIn("Re-dispatch an item at most twice after failed checks", text)
        self.assertIn("Return to the desk at most 30 lines", text)
        self.assertIn("Return at most 30 lines with numbers", self.agent("py-writer"))

    def test_config_limits_override(self):
        path = self.site_root / "config.json"
        cfg = json.loads(path.read_text())
        cfg["limits"] = {**(cfg.get("limits") or {}), "max_redispatch": 3, "manager_report_lines": 20,
                         "worker_report_lines": 25}
        path.write_text(json.dumps(cfg))
        text = self.agent("pm-modules")
        self.assertIn("Re-dispatch an item at most 3 times after failed checks", text)
        self.assertIn("Return to the desk at most 20 lines", text)
        self.assertIn("Return at most 25 lines with numbers", self.agent("py-writer"))
