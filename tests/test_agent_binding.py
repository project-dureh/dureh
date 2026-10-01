"""UR-20260930-03: concurrently spawned workers must each bind to their own WI."""
import json
import multiprocessing as mp
import unittest

from helpers import SiteCase, hook

from dureh_core import workflow as wf
from dureh_core.records import Ledger


def _start(site_root, agent_id, barrier, session=None):
    from dureh_core.site import Site

    barrier.wait()
    extra = {"transcript_path": session} if session else {}
    hook(Site(site_root), hook_event_name="SubagentStart", agent_type="py-writer", agent_id=agent_id, **extra)


class AgentBinding(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)
        self.bundle = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        self.items = [wf.wi_new(self.s, "desk", f"item {n}", "g", "py-writer", "modules/demo", ["ok"],
                                parent=self.bundle)["id"] for n in (1, 2)]
        for wi in [self.bundle, *self.items]:
            wf.wi_approve(self.s, "desk", wi, "ok")
        self.spawn("desk", "pm-modules", wf.dispatch_brief(self.s, self.bundle, "desk")["brief"])
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        self.briefs = {}
        for wi in self.items:
            b = wf.dispatch_brief(self.s, wi, "pm-modules")["brief"]
            self.assertIsNone(self.spawn("pm-modules", "py-writer", b, "a-pm"))
            self.briefs[wi] = b

    def spawn(self, caller, target, brief, agent_id=None):
        data = dict(hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
                    tool_input={"subagent_type": target, "prompt": brief, "description": "x"})
        if caller != "desk":
            data.update(agent_type=caller, agent_id=agent_id)
        return hook(self.s, **data)

    def bound(self):
        return {e["payload"]["agent_id"]: e["wi"] for e in self.led.load("event")
                if e["type"] == "agent.started" and e["actor"] == "py-writer" and e["wi"]}

    def test_binds_by_the_brief_in_the_agent_transcript(self):
        second = self.items[1]
        transcript = self.tmp / "agent-b.jsonl"
        transcript.write_text('{"role": "user", "content": ' + repr(self.briefs[second][:200]).replace("'", '"') + "}\n")
        hook(self.s, hook_event_name="SubagentStart", agent_type="py-writer", agent_id="a-b",
             agent_transcript_path=str(transcript))
        hook(self.s, hook_event_name="SubagentStart", agent_type="py-writer", agent_id="a-a")
        self.assertEqual(self.bound(), {"a-b": second}, "a-a has no transcript yet: deferred, not guessed")
        deferred = [e for e in self.led.load("event") if e["type"] == "agent.started" and not e["wi"]
                    and e["actor"] == "py-writer"]
        self.assertEqual([e["payload"]["agent_id"] for e in deferred], ["a-a"])

    def concurrent(self, session=None):
        ctx = mp.get_context("fork")
        barrier = ctx.Barrier(2)
        procs = [ctx.Process(target=_start, args=(str(self.site_root), aid, barrier, session)) for aid in ("a-1", "a-2")]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)

    def test_concurrent_starts_with_transcripts_bind_each_to_its_own_brief(self):
        session = self.tmp / "sess.jsonl"
        for aid, wi in (("a-1", self.items[1]), ("a-2", self.items[0])):
            t = session.with_suffix("") / "subagents" / f"agent-{aid}.jsonl"
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(json.dumps({"message": {"content": self.briefs[wi]}}) + "\n")
        self.concurrent(str(session))
        self.assertEqual(self.bound(), {"a-1": self.items[1], "a-2": self.items[0]})

    def test_concurrent_starts_without_transcripts_never_guess(self):
        self.concurrent()
        self.assertEqual(self.bound(), {})

if __name__ == "__main__":
    unittest.main()
