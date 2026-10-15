"""UR-20261001-01: bind each agent to the brief in its own prompt; stale briefs are never candidates."""
import json
import unittest
from pathlib import Path

from helpers import SiteCase, denied, hook

from dureh_core import workflow as wf
from dureh_core.records import Ledger


class ExactBinding(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)
        self.session = self.tmp / "projects" / "sess-1.jsonl"
        self.session.parent.mkdir(parents=True)
        self.session.write_text("")
        self.bundle = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        self.a, self.b = [wf.wi_new(self.s, "desk", f"item {n}", "g", "py-writer", "modules/demo", ["ok"],
                                    parent=self.bundle)["id"] for n in (1, 2)]
        for wi in (self.bundle, self.a, self.b):
            wf.wi_approve(self.s, "desk", wi, "ok")
        self.pm_brief = wf.dispatch_brief(self.s, self.bundle, "desk")
        self.spawn("desk", "pm-modules", self.pm_brief["brief"])
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")

    def spawn(self, caller, target, brief, agent_id=None):
        data = dict(hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
                    tool_input={"subagent_type": target, "prompt": brief, "description": "x"})
        if caller != "desk":
            data.update(agent_type=caller, agent_id=agent_id)
        self.assertIsNone(hook(self.s, **data))

    def worker(self, wi):
        """Prepare and spawn a py-writer for wi; returns its brief."""
        b = wf.dispatch_brief(self.s, wi, "pm-modules")
        self.spawn("pm-modules", "py-writer", b["brief"], "a-pm")
        return b

    def transcript(self, agent_id, brief):
        p = self.session.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": brief}}) + "\n")

    def start(self, agent_id, with_session=True):
        data = dict(hook_event_name="SubagentStart", agent_type="py-writer", agent_id=agent_id)
        if with_session:
            data["transcript_path"] = str(self.session)
        hook(self.s, **data)

    def write(self, agent_id, path):
        return hook(self.s, hook_event_name="PreToolUse", tool_name="Write", agent_type="py-writer", agent_id=agent_id,
                    transcript_path=str(self.session), cwd=str(self.site_root),
                    tool_input={"file_path": str(path), "content": "x"})

    def bound(self):
        return {e["payload"]["agent_id"]: (e["wi"], e["payload"]["brief"], e["payload"]["bound_by"])
                for e in self.led.load("event") if e["type"] == "agent.started" and e["actor"] == "py-writer" and e["wi"]}

    def digest(self, brief):
        return brief["brief"].split("brief=")[1][:16]

    def test_spawn_the_hook_never_recorded_binds_late_by_brief(self):
        # UR-20261015-01: the manager's Agent hook timed out (fail-open), so no wi.dispatched was recorded
        ba = wf.dispatch_brief(self.s, self.a, "pm-modules")  # prepared, never spawned through the hook
        self.start("a-late")
        self.transcript("a-late", ba["brief"])
        self.assertIsNone(self.write("a-late", Path(ba["worktree"]) / "src" / "demo.py"))
        self.assertEqual(self.bound()["a-late"], (self.a, self.digest(ba), "brief-late"))
        disp = [e for e in self.led.load("event") if e["type"] == "wi.dispatched" and e["wi"] == self.a]
        self.assertEqual(len(disp), 1)
        self.assertEqual((disp[0]["actor"], disp[0]["payload"]["recorded"]), ("pm-modules", "late"))
        # a second agent with the same brief cannot take it: the brief is used now
        self.start("a-dup")
        self.transcript("a-dup", ba["brief"])
        self.assertTrue(denied(self.write("a-dup", Path(ba["worktree"]) / "src" / "demo.py")))

    def test_late_binding_needs_a_brief_prepared_for_this_role(self):
        self.start("a-x")  # a py-writer whose prompt carries the manager's (pm-modules) brief
        self.transcript("a-x", self.pm_brief["brief"])
        self.assertTrue(denied(self.write("a-x", self.site_root / "worktrees" / self.a / "src" / "demo.py")))
        self.assertNotIn("a-x", self.bound())

    def test_start_without_transcript_defers_then_binds_by_brief(self):
        ba = self.worker(self.a)
        self.start("a-1")  # the runtime has not written the transcript yet
        self.assertNotIn("a-1", self.bound())
        self.transcript("a-1", ba["brief"])
        self.assertIsNone(self.write("a-1", Path(ba["worktree"]) / "src" / "demo.py"))
        self.assertEqual(self.bound()["a-1"], (self.a, self.digest(ba), "brief"))
        ev = [e for e in self.led.load("event") if e["type"] == "agent.started" and e["payload"]["agent_id"] == "a-1"][-1]
        self.assertEqual(ev["payload"]["prompt_brief"], self.digest(ba))

    def test_order_fallback_records_that_no_prompt_brief_was_found(self):
        ba = self.worker(self.a)
        self.start("a-1", with_session=False)
        self.write("a-1", Path(ba["worktree"]) / "src" / "demo.py")
        ev = [e for e in self.led.load("event") if e["type"] == "agent.started" and e["payload"]["agent_id"] == "a-1"][-1]
        self.assertEqual((ev["payload"]["bound_by"], ev["payload"]["prompt_brief"]), ("order", None))

    def test_agent_that_used_no_tool_is_bound_at_stop(self):
        ba = self.worker(self.a)
        self.start("a-1")
        p = self.transcript("a-1", ba["brief"])
        hook(self.s, hook_event_name="SubagentStop", agent_type="py-writer", agent_id="a-1",
             agent_transcript_path=str(self.session.with_suffix("") / "subagents" / "agent-a-1.jsonl"))
        self.assertEqual(self.bound()["a-1"][2], "brief")
        fin = [e for e in self.led.load("event") if e["type"] == "agent.finished" and e["payload"]["agent_id"] == "a-1"]
        self.assertEqual(fin[-1]["wi"], self.a)

    def test_stopped_dispatch_is_not_a_candidate(self):
        self.worker(self.a)  # spawned, agent never starts
        wf.dispatch_stop(self.s, "desk", self.a, "never started")
        bb = self.worker(self.b)
        self.start("a-1", with_session=False)
        self.write("a-1", Path(bb["worktree"]) / "src" / "demo.py")
        self.assertEqual(self.bound()["a-1"], (self.b, self.digest(bb), "order"))

    def test_superseded_dispatch_is_not_a_candidate(self):
        self.worker(self.a)
        wf.dispatch_stop(self.s, "desk", self.a, "never started")
        wf.wi_approve(self.s, "desk", self.a, "again")
        second = self.worker(self.a)
        self.start("a-1", with_session=False)
        self.write("a-1", Path(second["worktree"]) / "src" / "demo.py")
        self.assertEqual(self.bound()["a-1"], (self.a, self.digest(second), "order"))

    def test_binds_by_brief_from_the_session_transcript_layout(self):
        ba, bb = self.worker(self.a), self.worker(self.b)
        self.transcript("a-2", bb["brief"])
        self.transcript("a-1", ba["brief"])
        self.start("a-2")
        self.start("a-1")
        self.assertEqual(self.bound(), {"a-2": (self.b, self.digest(bb), "brief"),
                                        "a-1": (self.a, self.digest(ba), "brief")})

    def test_ambiguous_start_defers_binding_to_the_first_tool_call(self):
        ba, bb = self.worker(self.a), self.worker(self.b)
        self.start("a-2")  # transcript not written yet, two candidates: no guess
        self.assertNotIn("a-2", self.bound())
        self.transcript("a-2", bb["brief"])
        self.assertIsNone(self.write("a-2", Path(bb["worktree"]) / "src" / "demo.py"))
        self.assertEqual(self.bound()["a-2"], (self.b, self.digest(bb), "brief"))
        self.assertTrue(denied(self.write("a-2", Path(ba["worktree"]) / "src" / "demo.py")))

    def test_unbound_worker_cannot_write_any_worktree_and_denial_names_briefs(self):
        ba, bb = self.worker(self.a), self.worker(self.b)
        self.start("a-9")
        self.assertTrue(denied(self.write("a-9", Path(ba["worktree"]) / "src" / "demo.py")))
        ev = [e for e in self.led.load("event") if e["type"] == "guard.denied"][-1]["payload"]
        self.assertIn("not bound", ev["reason"])
        self.assertIn("bound_brief", ev)
        self.assertIn("prompt_brief", ev)


if __name__ == "__main__":
    unittest.main()
