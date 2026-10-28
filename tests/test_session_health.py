"""Harness check 2026-10-28 (H3, H4): a company's session job ended OUT_OF_MEMORY with heavy work inside it, and
the job the Director started by hand afterwards (7001475) had no session record, so `site session status` and `day
close` saw only the dead job. Status now names how a job ended, a session near its memory limit takes dureh commands
only (the agreed rule: heavy work runs as Slurm jobs), and session-like jobs dureh did not start are shown."""
import os
import subprocess
import unittest
from unittest import mock

from helpers import SiteCase, denied, hook

from dureh_core import sessions
from dureh_core.records import Ledger


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class SessionHealth(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        Ledger(self.s).event("session.started", "desk", None, {"job_id": "100", "purpose": "desk"})

    def fake(self, squeue_all="", squeue_one=None, sacct="", sstat=""):
        def run(argv, **kw):
            if argv[0] == "squeue" and "-u" in argv:
                return done(squeue_all)
            if argv[0] == "squeue":
                return squeue_one or done("")
            if argv[0] == "sacct":
                return done(sacct)
            if argv[0] == "sstat":
                return done(sstat)
            return done()
        return mock.patch.object(sessions.subprocess, "run", side_effect=run)

    def test_an_ended_job_shows_its_end_and_the_slurm_rule(self):
        gone = done("", 1, "slurm_load_jobs error: Invalid job id specified")
        with self.fake(squeue_one=gone, sacct="OUT_OF_MEMORY\n"):
            text = sessions.render_status(sessions.status(self.s))
        self.assertIn("job 100: not in squeue (ended OUT_OF_MEMORY)", text)
        self.assertIn("heavy work ran inside the session instead of a Slurm job", text)
        self.assertIn("dureh site session start --force", text)
        self.assertNotIn("squeue unavailable", text)

    def test_a_hand_started_session_job_is_shown(self):
        root = str(self.site_root)
        rows = (f"200|{self.s.session_name}|RUNNING|12:00:00|{root}\n"
                f"201|cc-{self.s.session_name}-overtime|PENDING|1:00:00|{root}\n"
                f"202|{self.s.session_name}|RUNNING|1:00:00|/elsewhere\n"
                f"203|other-run|RUNNING|1:00:00|{root}\n"
                f"100|cc-{self.s.session_name}|RUNNING|1:00:00|{root}\n")
        with self.fake(squeue_all=rows, squeue_one=done("RUNNING|1:00:00\n"), sstat="1000K\n"):
            st = sessions.status(self.s)
        self.assertEqual([u["job_id"] for u in st["unrecorded"]], ["200", "201"])
        text = sessions.render_status(st)
        self.assertIn("job 200", text)
        self.assertIn("was not started by dureh", text)
        self.assertIn("scancel 200", text)

    def test_memory_warning_on_a_running_session(self):
        with self.fake(squeue_one=done("RUNNING|1:00:00\n"), sstat="15G\n"):
            text = sessions.render_status(sessions.status(self.s))
        self.assertIn("uses 94% of its --mem 16G", text)


class MemoryGate(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        env = mock.patch.dict(os.environ, {"SLURM_JOB_ID": "300", "SLURM_MEM_PER_NODE": "16384"})
        env.start()
        self.addCleanup(env.stop)

    def bash(self, command, share):
        with mock.patch.object(sessions, "_mem_use", return_value={"share": share, "rss": 0, "mem": "16384M"}):
            cache = self.s.index_path().parent / "memory-300.json"
            if cache.exists():
                cache.unlink()
            return hook(self.s, hook_event_name="PreToolUse", tool_name="Bash", cwd=str(self.site_root),
                        tool_input={"command": command}, agent_type="py-writer", agent_id="a1")

    def test_near_the_limit_only_dureh_commands_run(self):
        out = self.bash("python3 -c 'print(1)'", 0.95)
        self.assertTrue(denied(out))
        self.assertIn("heavy work belongs in a Slurm job", str(out))
        self.assertFalse(denied(self.bash("bin/dureh status", 0.95)))

    def test_below_the_limit_nothing_changes(self):
        self.assertFalse(denied(self.bash("ls", 0.5)))

    def test_the_reading_is_cached_for_a_minute(self):
        with mock.patch.object(sessions, "_mem_use", return_value={"share": 0.95, "rss": 0, "mem": "x"}) as m:
            sessions.own_job_memory(self.s)
            sessions.own_job_memory(self.s)
        self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main()
