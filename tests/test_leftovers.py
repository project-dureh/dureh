"""Release 27: leftover processes of a finished agent are killed and recorded. Evidence: WI-0115 left `find /`
and wait loops running for hours after its agent had finished. Fake processes (`sleep`) only; SLURM_JOB_ID is
never the real session job (the helper unsets it)."""
import os
import subprocess
import time
import unittest

from helpers import SiteCase, denied, hook

from dureh_core import procs
from dureh_core import workflow as wf
from dureh_core.records import Ledger


def _wait_gone(proc, seconds=5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return proc.poll() is not None


class Procs(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.pop("SLURM_JOB_ID", None)
        self.spawned = []

    def tearDown(self):
        for p in self.spawned:
            if p.poll() is None:
                p.kill()
                p.wait()
        os.environ.clear()
        os.environ.update(self._env)

    def sleep(self, **env):
        p = subprocess.Popen(["sleep", "300"], env={**os.environ, **env})
        self.spawned.append(p)
        return p

    @unittest.skipUnless(procs.PROC.is_dir(), "needs /proc")
    def test_only_the_agents_marked_processes_of_this_session_die(self):
        mine = self.sleep(DUREH_AGENT_ID="t-agent-1")
        mine_child = subprocess.Popen(["sh", "-c", "sleep 300"], env={**os.environ, "DUREH_AGENT_ID": "t-agent-1"})
        self.spawned.append(mine_child)
        other = self.sleep(DUREH_AGENT_ID="t-agent-2")
        unmarked = self.sleep()
        other_job = self.sleep(DUREH_AGENT_ID="t-agent-1", SLURM_JOB_ID="999999")  # a submitted job's process
        time.sleep(0.2)
        found = {p["pid"] for p in procs.marked_processes("t-agent-1", None, set())}
        self.assertIn(mine.pid, found)
        self.assertIn(mine_child.pid, found)
        self.assertNotIn(other.pid, found)
        self.assertNotIn(unmarked.pid, found)
        self.assertNotIn(other_job.pid, found, "another SLURM_JOB_ID is a scheduler job, not session work")
        r = procs.kill_leftovers("t-agent-1", grace=1.0)
        self.assertEqual(r["method"], "environ-marker")
        self.assertTrue({mine.pid, mine_child.pid} <= {k["pid"] for k in r["killed"]}, r)
        self.assertTrue(_wait_gone(mine) and _wait_gone(mine_child))
        self.assertIsNone(other.poll())
        self.assertIsNone(unmarked.poll())
        self.assertIsNone(other_job.poll())
        self.assertEqual(procs.kill_leftovers("t-agent-1")["killed"], [], "nothing left")

    @unittest.skipUnless(procs.PROC.is_dir(), "needs /proc")
    def test_protected_pids_and_the_own_tree_are_never_killed(self):
        p = self.sleep(DUREH_AGENT_ID="t-agent-3")
        time.sleep(0.1)
        r = procs.kill_leftovers("t-agent-3", protect={p.pid})
        self.assertEqual(r["killed"], [])
        self.assertIsNone(p.poll())
        self.assertIn(os.getpid(), procs.ancestors())
        self.assertNotIn(os.getpid(), {x["pid"] for x in procs.marked_processes("t-agent-3", None, set(procs.ancestors()))})
        rp = procs.runtime_pid()
        self.assertTrue(rp is None or rp in procs.ancestors())

    def test_sigkill_after_the_grace_period(self):
        stubborn = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 300"], env={**os.environ, "DUREH_AGENT_ID": "t-agent-4"})
        self.spawned.append(stubborn)
        time.sleep(0.2)
        r = procs.kill_leftovers("t-agent-4", grace=0.5)
        self.assertEqual([k["signal"] for k in r["killed"] if k["pid"] == stubborn.pid], ["KILL"])
        self.assertTrue(_wait_gone(stubborn))


class HookCleanup(SiteCase):
    """The marker is set on every agent command; SubagentStop kills what still carries it and records it."""

    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)
        self.spawned = []
        self.bundle = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        self.item = wf.wi_new(self.s, "desk", "I", "g", "py-writer", "modules/demo", ["ok"], parent=self.bundle)["id"]
        for wi in (self.bundle, self.item):
            wf.wi_approve(self.s, "desk", wi, "ok")
        brief = wf.dispatch_brief(self.s, self.bundle, "desk")["brief"]
        self.assertIsNone(hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
                               tool_input={"subagent_type": "pm-modules", "prompt": brief, "description": "x"}))

    def tearDown(self):
        for p in self.spawned:
            if p.poll() is None:
                p.kill()
                p.wait()
        super().tearDown()

    def bash(self, command, agent_id="a-pm", role="pm-modules"):
        return hook(self.s, hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": command},
                    cwd=str(self.site_root), agent_type=role, agent_id=agent_id)

    def test_every_agent_command_carries_the_marker(self):
        out = self.bash("ls modules")
        self.assertFalse(denied(out))
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["command"], "DUREH_AGENT_ID=a-pm ls modules")
        out = self.bash("cd modules && ls")
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["command"], "export DUREH_AGENT_ID=a-pm\ncd modules && ls")
        out = self.bash("for f in a b; do echo $f; done")
        self.assertTrue(out["hookSpecificOutput"]["updatedInput"]["command"].startswith("export DUREH_AGENT_ID=a-pm\n"))
        # dureh lines keep their identity prefix; the desk is not marked
        out = self.bash("bin/dureh status")
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["command"], "DUREH_ACTOR=pm-modules DUREH_AGENT_ID=a-pm bin/dureh status")
        self.assertIsNone(hook(self.s, hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": "ls"},
                               cwd=str(self.site_root)))

    def test_start_records_the_runtime_pid(self):
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        ev = [e for e in self.led.load("event") if e["type"] == "agent.started"][-1]
        self.assertIn("runtime_pid", ev["payload"])
        rp = ev["payload"]["runtime_pid"]
        self.assertTrue(rp is None or rp in procs.ancestors(), "the runtime pid is one of this process's ancestors")

    @unittest.skipUnless(procs.PROC.is_dir(), "needs /proc")
    def test_stop_kills_the_agents_leftovers_and_records_them(self):
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        # what the marked command would have left behind: a marked sleep, and a bystander of another agent
        left = subprocess.Popen(["sleep", "300"], env={**os.environ, "DUREH_AGENT_ID": "a-pm"})
        bystander = subprocess.Popen(["sleep", "300"], env={**os.environ, "DUREH_AGENT_ID": "a-other"})
        self.spawned += [left, bystander]
        time.sleep(0.2)
        hook(self.s, hook_event_name="SubagentStop", agent_type="pm-modules", agent_id="a-pm")
        self.assertTrue(_wait_gone(left), "the finished agent's process is gone")
        self.assertIsNone(bystander.poll(), "another agent's process lives on")
        events = self.led.load("event")
        cleaned = [e for e in events if e["type"] == "agent.cleaned"]
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["actor"], "pm-modules")
        self.assertEqual(cleaned[0]["wi"], self.bundle)
        self.assertEqual([k["pid"] for k in cleaned[0]["payload"]["killed"]], [left.pid])
        self.assertIn("sleep 300", cleaned[0]["payload"]["killed"][0]["cmd"])
        fin = [e for e in events if e["type"] == "agent.finished"][-1]
        self.assertEqual(fin["payload"]["leftovers_killed"], 1)

    def test_stop_without_leftovers_records_no_cleanup(self):
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        hook(self.s, hook_event_name="SubagentStop", agent_type="pm-modules", agent_id="a-pm")
        events = self.led.load("event")
        self.assertEqual([e for e in events if e["type"] == "agent.cleaned"], [])
        self.assertEqual([e for e in events if e["type"] == "agent.finished"][-1]["payload"]["leftovers_killed"], 0)


if __name__ == "__main__":
    unittest.main()
