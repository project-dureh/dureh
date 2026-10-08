"""Release 45: config `slurm.watch` (monitor | on-demand | off) sets what the generated text says about job ends.
`monitor` (the default, a site without the key) keeps the standing `slurm watch` Monitor text; `on-demand` and
`off` drop it from the desk text, the role files, the refused-wait hint and the verify hints."""
import json

from helpers import SiteCase, denied, hook

from dureh_core import ops
from dureh_core.runtimes import claude_code
from dureh_core.util import DurehError


class WatchModes(SiteCase):
    def setUp(self):
        super().setUp()
        manifest = self.package / "startup.yaml"
        manifest.write_text(manifest.read_text()
                            .replace("managers: [pm-modules]", "managers: [pm-modules, pm-projects]")
                            .replace("workers: [py-writer,", "workers: [pipeline-runner, py-writer,"))
        self.install()

    def set_mode(self, mode):
        path = self.site_root / "config.json"
        cfg = json.loads(path.read_text())
        cfg["slurm"] = {**(cfg.get("slurm") or {}), "watch": mode}
        path.write_text(json.dumps(cfg))

    def generated(self):
        claude_code.generate(self.site())
        return ((self.site_root / "CLAUDE.md").read_text(),
                (self.site_root / ".claude" / "agents" / "pipeline-runner.md").read_text())

    def refusal(self):
        out = hook(self.site(), hook_event_name="PreToolUse", tool_name="Bash", cwd=str(self.site_root),
                   agent_type="py-writer", agent_id="a-1", tool_input={"command": "squeue -j 4300 -h",
                                                                        "run_in_background": True})
        self.assertTrue(denied(out), out)
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_default_is_monitor_with_todays_text(self):
        self.assertEqual(ops.watch_mode(self.site()), "monitor")
        md, runner = self.generated()
        self.assertIn("slurm watch` running as a Monitor", md)
        self.assertIn("When a line arrives, re-dispatch the owning WI", md)
        self.assertIn("`slurm watch` reports the verify job's end", md)
        self.assertIn("the desk re-dispatches you when `dureh slurm watch` reports the job", runner)
        self.assertIn(claude_code.WAIT_HINT, self.refusal())

    def test_on_demand_has_no_standing_monitor(self):
        self.set_mode("on-demand")
        md, runner = self.generated()
        self.assertNotIn("slurm watch` running as a Monitor", md)
        self.assertNotIn("as a Monitor reports it", md)
        self.assertIn("No standing job Monitor (config `slurm.watch: on-demand`)", md)
        self.assertIn("slurm wait --run <id>`", md)
        self.assertIn("slurm status --run <id>` at your next turn or when the Director asks", md)
        self.assertIn("When a job has ended, re-dispatch the owning WI", md)
        self.assertIn("re-dispatch the checker (`dispatch brief --wi <WI> --role checker`)", md)
        self.assertNotIn("slurm watch` reports", runner)
        self.assertIn("the desk re-dispatches you once the job has ended", runner)
        reason = self.refusal()
        self.assertIn(claude_code.WAIT_HINT_UNWATCHED, reason)
        self.assertNotIn("slurm watch", reason)

    def test_off_names_only_slurm_status(self):
        self.set_mode("off")
        md, runner = self.generated()
        self.assertNotIn("as a Monitor (one line per tracked job", md)
        self.assertNotIn("slurm watch --once", md)
        self.assertNotIn("slurm wait --run <id>` for a job", md)
        self.assertIn("they are not watched (config `slurm.watch: off`)", md)
        self.assertIn("slurm status --run <id>`", md)
        self.assertNotIn("slurm watch` reports", runner)

    def test_invalid_mode_fails_generation(self):
        self.set_mode("sometimes")
        with self.assertRaises(DurehError) as cm:
            claude_code.generate(self.site())
        self.assertIn("monitor, on-demand, off", str(cm.exception))

    def test_tool_hints_follow_the_mode(self):
        self.assertIn("slurm watch", ops.job_end_phrase(self.site()))
        self.set_mode("off")
        self.assertNotIn("slurm watch", ops.job_end_phrase(self.site()))
        self.assertIn("slurm status", ops.job_end_phrase(self.site()))


if __name__ == "__main__":
    import unittest

    unittest.main()
