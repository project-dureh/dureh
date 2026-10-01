"""UR-20260930-04: `dureh status` flags dispatched work items whose agent is gone."""
import unittest

from helpers import SiteCase, hook

from dureh_core import workflow as wf
from dureh_core.ops import render_status, status


class Orphans(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.bundle = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        self.items = [wf.wi_new(self.s, "desk", f"item {n}", "g", "py-writer", "modules/demo", ["ok"],
                                parent=self.bundle)["id"] for n in (1, 2)]
        for wi in [self.bundle, *self.items]:
            wf.wi_approve(self.s, "desk", wi, "ok")
        self.spawn("desk", "pm-modules", wf.dispatch_brief(self.s, self.bundle, "desk")["brief"])
        self.event("SubagentStart", "pm-modules", "a-pm")

    def spawn(self, caller, target, brief, agent_id=None):
        data = dict(hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
                    tool_input={"subagent_type": target, "prompt": brief, "description": "x"})
        if caller != "desk":
            data.update(agent_type=caller, agent_id=agent_id)
        self.assertIsNone(hook(self.s, **data))

    def event(self, name, role, agent_id):
        hook(self.s, hook_event_name=name, agent_type=role, agent_id=agent_id)

    def orphans(self):
        return {o["wi"]: o for o in status(self.s)["orphaned"]}

    def test_running_agents_are_not_orphans(self):
        self.assertEqual(self.orphans(), {})

    def test_manager_that_handed_back_while_its_worker_runs(self):
        self.spawn("pm-modules", "py-writer", wf.dispatch_brief(self.s, self.items[0], "pm-modules")["brief"], "a-pm")
        self.event("SubagentStart", "py-writer", "a-w1")
        self.event("SubagentStop", "pm-modules", "a-pm")
        o = self.orphans()
        self.assertEqual(set(o), {self.bundle})
        self.assertEqual(o[self.bundle]["agent"], "ended")
        self.assertEqual(o[self.bundle]["children_running"], [self.items[0]])
        text = render_status(status(self.s))
        self.assertIn(f"{self.bundle} pm-modules agent ended; still running: {self.items[0]}", text)

    def test_dispatched_item_whose_agent_never_started(self):
        self.spawn("pm-modules", "py-writer", wf.dispatch_brief(self.s, self.items[1], "pm-modules")["brief"], "a-pm")
        o = self.orphans()
        self.assertEqual(o[self.items[1]]["agent"], "not started")
        self.assertIn("dispatch stop", o[self.items[1]]["action"])

    def test_ended_manager_can_be_re_adopted(self):
        self.event("SubagentStop", "pm-modules", "a-pm")
        self.assertIn("dispatch brief", self.orphans()[self.bundle]["action"])
        wf.dispatch_brief(self.s, self.bundle, "desk")


if __name__ == "__main__":
    unittest.main()
