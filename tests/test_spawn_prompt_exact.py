"""Release 97 (a second site's UR-20261109-01, option 2a): the spawn hook admits a worker only with the printed brief
as its whole prompt, byte for byte (trailing whitespace aside): `dispatch.prepared` records `prompt_sha256` and a
retyped, shortened or extended prompt is refused, even with the right brief id."""
from helpers import SiteCase, denied, hook

from dureh_core import workflow as wf
from dureh_core.records import Ledger


class SpawnPromptExact(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)
        self.wi = wf.wi_new(self.s, "desk", "t", "g", "doc-writer", "modules/demo", ["ok"], checks=["true"])["id"]
        wf.wi_approve(self.s, "desk", self.wi, "ok")
        self.b = wf.dispatch_brief(self.s, self.wi, "desk")

    def spawn(self, prompt):
        return hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
                    tool_input={"subagent_type": "doc-writer", "prompt": prompt})

    def dispatched(self):
        return [e for e in self.led.events(self.wi) if e["type"] == "wi.dispatched"]

    def test_the_brief_records_its_prompt_hash(self):
        p = [e for e in self.led.events(self.wi) if e["type"] == "dispatch.prepared"][-1]["payload"]
        self.assertEqual(p["prompt_sha256"], wf.prompt_sha256(self.b["brief"]))

    def test_a_changed_prompt_is_refused(self):
        brief = self.b["brief"]
        line = brief.splitlines()[3]
        for prompt in (brief.replace(line, line + " (retyped)"), brief[: len(brief) // 2],
                       "Context first.\n" + brief, brief + "\nAlso do X."):
            out = self.spawn(prompt)
            self.assertTrue(denied(out), prompt[-40:])
            reason = out["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertIn("not the brief", reason)
            self.assertIn(f"`dispatch brief --wi {self.wi}` unchanged", reason)
        self.assertFalse(self.dispatched(), "nothing recorded")

    def test_the_printed_brief_is_admitted(self):
        out = self.spawn(self.b["brief"].rstrip("\n"))  # a dropped final newline is not a change
        self.assertFalse(denied(out), out)
        self.assertEqual(len(self.dispatched()), 1)

    def test_a_record_without_the_hash_keeps_the_id_check(self):
        """A brief prepared before release 97 has no prompt_sha256: the id check alone applies, as before."""
        self.led.event("dispatch.prepared", "desk", self.wi,
                       {**[e for e in self.led.events(self.wi) if e["type"] == "dispatch.prepared"][-1]["payload"],
                        "prompt_sha256": None})
        self.assertFalse(denied(self.spawn(self.b["brief"] + "\nextra")))
