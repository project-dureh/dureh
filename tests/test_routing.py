"""Release 54 (D3): model routing per work-unit kind. `models.routing` maps a kind (check, doc, triage, build) to a
tier or null; `dispatch brief` derives the kind, resolves the model, and the spawn hook refuses another model.
Off by default: without the key every brief is what it was."""
import contextlib
import io
import json
import re
import subprocess
import unittest

from helpers import SiteCase, denied, hook

from dureh_core import cli, ops
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.runtimes.claude_code import model_matches
from dureh_core.site import Site, routing_errors
from dureh_core.util import DurehError

OPUS, SONNET, HAIKU = "anthropic:claude-opus-5-5", "anthropic:claude-sonnet-5", "anthropic:claude-haiku-4-5-20251001"


def _body(brief: str) -> str:
    """The brief without the parts that differ between two preparations (digest, Prepared time)."""
    return "\n".join(ln for ln in brief.splitlines() if not ln.startswith(("DUREH-BRIEF ", "Prepared: ")))


class Routing(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)

    def configure(self, **models) -> Site:
        cfg = json.loads(self.s.config_path.read_text())
        for k, v in models.items():
            if v is None:
                cfg["models"].pop(k, None)
            else:
                cfg["models"][k] = v
        self.s.config_path.write_text(json.dumps(cfg))
        self.s = Site(self.site_root)
        self.led = Ledger(self.s)
        return self.s

    def item(self, role="researcher", target="site", **kw) -> str:
        wi = wf.wi_new(self.s, "desk", f"{role} item", "do it", role, target, ["done"], **kw)["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        return wi

    def staged(self, role="researcher") -> str:
        wi = self.item(role)
        b = wf.dispatch_brief(self.s, wi, "desk")
        self.assertIsNone(self.spawn("desk", role, b["brief"]))
        wf.wi_stage(self.s, role, wi, "staged in test")
        return wi

    def spawn(self, caller, target, brief, model=None):
        ti = {"subagent_type": target, "prompt": brief, "description": "x"}
        if model:
            ti["model"] = model
        data = dict(hook_event_name="PreToolUse", tool_name="Agent", tool_input=ti, cwd=str(self.site_root))
        if caller != "desk":
            data.update(agent_type=caller, agent_id=f"a-{caller}")
        return hook(self.s, **data)

    # ---------------------------------------------------------------- kinds
    def test_kind_derivation(self):
        self.assertEqual(wf.dispatch_brief(self.s, self.item("researcher"), "desk")["kind"], "build")
        self.assertEqual(wf.dispatch_brief(self.s, self.item("doc-writer", "modules/demo"), "desk")["kind"], "doc")
        self.assertEqual(wf.dispatch_brief(self.s, self.item("researcher", lane="desk"), "desk")["kind"], "triage")
        wi = self.staged()
        self.assertEqual(wf.dispatch_brief(self.s, wi, "desk", "checker")["kind"], "check")

    def test_routing_doc_roles_configurable(self):
        self.configure(routing_doc_roles=["researcher"])
        self.assertEqual(wf.dispatch_brief(self.s, self.item("researcher"), "desk")["kind"], "doc")
        self.assertEqual(wf.dispatch_brief(self.s, self.item("doc-writer", "modules/demo"), "desk")["kind"], "build")

    # ---------------------------------------------------------------- model resolution
    def test_routing_absent_keeps_the_brief_and_model(self):
        wi = self.item("researcher")
        before = wf.dispatch_brief(self.s, wi, "desk")
        self.assertEqual(before["model"], SONNET)
        self.assertNotIn("kind", before["brief"], "the brief body does not change; kind is on the spawn line")
        self.configure(routing={"check": None, "doc": None, "triage": None, "build": None})
        after = wf.dispatch_brief(self.s, wi, "desk")
        self.assertEqual(after["model"], SONNET)
        self.assertEqual(_body(after["brief"]), _body(before["brief"]))
        self.assertEqual(self.s.routing(), {"check": None, "doc": None, "triage": None, "build": None})

    def test_routing_present_picks_the_tier_model_and_null_keeps_the_role_tier(self):
        self.configure(routing={"doc": "recount", "triage": "recount", "build": None})
        wi = self.item("doc-writer", "modules/demo")
        before = _body(wf.dispatch_brief(self.s, wi, "desk")["brief"])
        self.configure(routing=None)
        plain = wf.dispatch_brief(self.s, wi, "desk")
        self.configure(routing={"doc": "recount", "triage": "recount", "build": None})
        routed = wf.dispatch_brief(self.s, wi, "desk")
        self.assertEqual((plain["model"], routed["model"]), (SONNET, HAIKU))
        self.assertEqual(_body(routed["brief"]), before, "routing changes the model, not the brief body")
        self.assertEqual(wf.dispatch_brief(self.s, self.item("researcher", lane="desk"), "desk")["model"], HAIKU)
        self.assertEqual(wf.dispatch_brief(self.s, self.item("researcher"), "desk")["model"], SONNET, "build -> null")
        ev = [e for e in self.led.events(wi) if e["type"] == "dispatch.prepared"][-1]["payload"]
        self.assertEqual((ev["kind"], ev["model"]), ("doc", HAIKU))

    def test_escalations_keep_precedence_over_routing(self):
        self.configure(routing={"build": "recount"})
        academic = self.item("researcher", academic=True)
        self.assertEqual(wf.dispatch_brief(self.s, academic, "desk")["model"], OPUS, "academic -> writer-academic")
        wi = self.item("researcher")
        for _ in range(2):  # two reworks reach the worker tier's rework_limit
            self.led.event("wi.dispatched", "desk", wi, {"role": "researcher"})
            self.led.event("rework.requested", "desk", wi, {"why": "again"})
        self.assertEqual(self.led.wi_status(wi)["rework"], 2)
        r = wf.dispatch_brief(self.s, wi, "desk")
        self.assertEqual(r["model"], OPUS, "rework escalation -> pm")
        self.assertEqual(wf.dispatch_brief(self.s, self.item("researcher"), "desk")["model"], HAIKU, "routed otherwise")

    # ---------------------------------------------------------------- validation
    def test_unknown_kind_and_unknown_tier_are_refused(self):
        self.configure(routing={"review": "recount"})
        errs = ops.config_errors(self.s)
        self.assertEqual(len(errs), 1)
        self.assertIn("unknown kind(s) review", errs[0])
        self.assertIn("release 54", errs[0])
        with self.assertRaisesRegex(DurehError, "unknown kind"):
            wf.dispatch_brief(self.s, self.item("researcher"), "desk")
        self.configure(routing={"doc": "tiny"})
        errs = ops.config_errors(self.s)
        self.assertEqual(len(errs), 1)
        self.assertIn("doc -> 'tiny' names no tier defined under models", errs[0])
        with self.assertRaisesRegex(DurehError, "names no tier"):
            wf.dispatch_brief(self.s, self.item("researcher"), "desk")
        self.assertTrue(routing_errors({"models": {"worker": {"model": SONNET}, "routing": {"doc": "routing"}}}),
                        "meta keys are not tiers")
        self.assertTrue(routing_errors({"models": {"routing_doc_roles": "doc-writer"}}))
        self.assertEqual(routing_errors({"models": {"worker": {"model": SONNET}}}), [])
        self.assertEqual(ops.config_errors(self.configure(routing=None)), [])

    def test_meta_keys_are_not_tiers(self):
        self.configure(routing={"doc": "recount"}, routing_doc_roles=["doc-writer"])
        self.assertNotIn("routing", self.s.model_tiers())
        with self.assertRaisesRegex(DurehError, "no model mapping for tier 'routing'"):
            self.s.model_for_tier("routing")

    # ---------------------------------------------------------------- events and the hook
    def test_dispatched_events_carry_kind_and_model(self):
        wi = self.staged("researcher")
        ev = [e for e in self.led.events(wi) if e["type"] == "wi.dispatched"][-1]["payload"]
        self.assertEqual((ev["kind"], ev["model"]), ("build", SONNET))
        cb = wf.dispatch_brief(self.s, wi, "desk", "checker")
        self.assertIsNone(self.spawn("desk", "checker", cb["brief"]))
        ev = [e for e in self.led.events(wi) if e["type"] == "checker.dispatched"][-1]["payload"]
        self.assertEqual((ev["kind"], ev["model"]), ("check", HAIKU))

    def test_hook_refuses_a_spawn_whose_model_differs_from_the_brief(self):
        wi = self.item("researcher")
        b = wf.dispatch_brief(self.s, wi, "desk")
        out = self.spawn("desk", "researcher", b["brief"], model="haiku")
        self.assertTrue(denied(out))
        self.assertIn(f"the brief is for model {SONNET}, not haiku", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(self.spawn("desk", "researcher", b["brief"], model="sonnet"), "the family alias matches")
        b = wf.dispatch_brief(self.s, self.item("researcher"), "desk")
        self.assertIsNone(self.spawn("desk", "researcher", b["brief"], model="claude-sonnet-5"), "the id matches")
        # routed away from the role's own tier: a spawn without `model` runs the agent file's model; allowed, warned
        self.configure(routing={"build": "recount"})
        wi = self.item("researcher")
        b = wf.dispatch_brief(self.s, wi, "desk")
        self.assertEqual(b["model"], HAIKU)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = self.spawn("desk", "researcher", b["brief"])
        self.assertFalse(denied(out))
        self.assertEqual(out, {"systemMessage": f"brief model {HAIKU} not passed; pass model=claude-haiku-4-5-20251001"
                                                f" (this spawn runs {SONNET})"})
        self.assertIn("dureh: warning: brief model", err.getvalue())
        ev = [e for e in self.led.events(wi) if e["type"] == "wi.dispatched"][-1]["payload"]
        self.assertEqual((ev["kind"], ev["model"], ev["model_unstated"]), ("build", HAIKU, True))
        b = wf.dispatch_brief(self.s, self.item("researcher"), "desk")
        self.assertIsNone(self.spawn("desk", "researcher", b["brief"], model="haiku"), "stated: no warning")

    def test_model_matches(self):
        self.assertTrue(model_matches("opus", OPUS))
        self.assertTrue(model_matches("claude-opus-5-5", OPUS))
        self.assertTrue(model_matches(OPUS, OPUS))
        self.assertTrue(model_matches("haiku", HAIKU))
        self.assertFalse(model_matches("sonnet", OPUS))
        self.assertFalse(model_matches("", OPUS))
        self.assertFalse(model_matches("claude-opus-4", OPUS))

    # ---------------------------------------------------------------- usage
    def test_usage_counts_spawns_per_kind_per_model(self):
        self.configure(routing={"triage": "recount"})
        wi = self.staged("researcher")
        cb = wf.dispatch_brief(self.s, wi, "desk", "checker")
        self.assertIsNone(self.spawn("desk", "checker", cb["brief"]))
        t = self.item("researcher", lane="desk")
        self.assertIsNone(self.spawn("desk", "researcher", wf.dispatch_brief(self.s, t, "desk")["brief"], model="haiku"))
        u = ops.usage(self.s)["spawns_today"]
        self.assertEqual(u, {"kinds": {"build": {SONNET: 1}, "check": {HAIKU: 1}, "triage": {HAIKU: 1}}, "total": 3})
        self.led.event("wi.dispatched", "desk", self.item("researcher"), {"role": "researcher", "model": SONNET})
        self.assertEqual(ops.spawn_table(self.led.events())["kinds"]["unknown"], {SONNET: 1}, "pre-54 event")
        self.assertEqual(ops.spawn_table(self.led.events(), day="2000-01-01")["total"], 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["--site", str(self.site_root), "usage"]), 0)
        self.assertEqual(json.loads(buf.getvalue())["spawns_today"]["total"], 4)

    def test_brief_command_prints_kind_and_model(self):
        wi = self.item("researcher")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["--site", str(self.site_root), "dispatch", "brief", "--wi", wi, "--actor", "desk"]), 0)
        first = buf.getvalue().splitlines()[0]
        self.assertRegex(first, rf"^# spawn subagent_type=researcher model={re.escape(SONNET)} kind=build;")


if __name__ == "__main__":
    unittest.main()
