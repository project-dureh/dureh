"""Phase 1 criterion 5 plus the Claude Code adapter's dispatch and guard contract."""
import json
import os
import subprocess
import unittest
from pathlib import Path

from helpers import SiteCase, denied, hook

from dureh_core.records import Ledger
from dureh_core.util import DurehError
from dureh_core import workflow as wf


class Bundle(SiteCase):
    """desk -> pm-modules -> py-writer on a git module target."""

    def setUp(self):
        super().setUp()
        self.install()
        self.mod = self.make_module()
        self.s = self.site()
        self.led = Ledger(self.s)
        b = wf.wi_new(self.s, "desk", "Bundle", "Improve demo", "pm-modules", "modules/demo", ["items verified"])
        self.bundle = b["id"]
        c = wf.wi_new(self.s, "desk", "Add g()", "Add g returning 2", "py-writer", "modules/demo", ["g() == 2"],
                      parent=self.bundle, checks=["python3 -m unittest discover -s tests -q"], outputs=["src/demo.py"])
        self.item = c["id"]

    def spawn(self, caller, target, brief, agent_id=None):
        data = dict(hook_event_name="PreToolUse", tool_name="Agent",
                    tool_input={"subagent_type": target, "prompt": brief, "description": "x"}, cwd=str(self.site_root))
        if caller != "desk":
            data.update(agent_type=caller, agent_id=agent_id or f"a-{caller}")
        return hook(self.s, **data)

    def start(self, role, agent_id):
        hook(self.s, hook_event_name="SubagentStart", agent_type=role, agent_id=agent_id)

    def write(self, role, agent_id, path):
        data = dict(hook_event_name="PreToolUse", tool_name="Write", tool_input={"file_path": str(path), "content": "x"},
                    cwd=str(self.site_root))
        if role != "desk":
            data.update(agent_type=role, agent_id=agent_id)
        return hook(self.s, **data)

    def test_full_lifecycle_and_gates(self):
        with self.assertRaisesRegex(DurehError, "only approved"):
            wf.dispatch_brief(self.s, self.bundle, "desk")
        wf.wi_approve(self.s, "desk", self.bundle, "Director approved in test")
        wf.wi_approve(self.s, "desk", self.item, "Director approved in test")
        # spawn without a brief is refused
        self.assertTrue(denied(self.spawn("desk", "pm-modules", "please do it")))
        # a non-DUREH subagent type is refused
        self.assertTrue(denied(self.spawn("desk", "general-purpose", "x")))
        b = wf.dispatch_brief(self.s, self.bundle, "desk")
        self.assertIsNone(self.spawn("desk", "pm-modules", b["brief"]))
        self.assertEqual(self.led.wi_status(self.bundle)["status"], "dispatched")
        self.assertTrue(denied(self.spawn("desk", "pm-modules", b["brief"])), "a brief is single-use")
        self.start("pm-modules", "a-pm")
        # manager dispatches its worker
        with self.assertRaisesRegex(DurehError, "may not dispatch"):
            wf.dispatch_brief(self.s, self.item, "desk")
        wb = wf.dispatch_brief(self.s, self.item, "pm-modules")
        self.assertIn("DUREH-BRIEF", wb["brief"])
        self.assertLessEqual(len(wb["brief"].encode()), 2048 + 200)
        wt = Path(wb["worktree"])
        self.assertTrue((wt / "src" / "demo.py").is_file())
        self.assertTrue(denied(self.spawn("desk", "py-writer", wb["brief"])), "desk did not prepare this brief")
        self.assertIsNone(self.spawn("pm-modules", "py-writer", wb["brief"], "a-pm"))
        self.start("py-writer", "a-py")
        # worker writes: allowed in its worktree scope, denied elsewhere
        self.assertIsNone(self.write("py-writer", "a-py", wt / "src" / "demo.py"))
        self.assertTrue(denied(self.write("py-writer", "a-py", wt / "README.md")))
        self.assertTrue(denied(self.write("py-writer", "a-py", self.mod / "src" / "demo.py")), "main tree of a git target")
        self.assertTrue(denied(self.write("py-writer", "a-py", self.site_root / "state/records/event/x.json")))
        self.assertTrue(denied(self.write("py-writer", "a-py", self.site_root / "references/old-org/docs/old.md")))
        self.assertTrue(denied(self.write("py-writer", "a-py", self.site_root / "state/memory/x.md")))
        self.assertTrue(denied(self.spawn("py-writer", "py-writer", wb["brief"], "a-py")), "workers cannot spawn")
        # the worker's edit (performed here directly) and commit
        (wt / "src" / "demo.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
        with self.assertRaisesRegex(DurehError, "uncommitted"):
            wf.wi_stage(self.s, "py-writer", self.item, "added g")
        subprocess.run(["git", "commit", "-qam", "add g"], cwd=wt, check=True)
        wf.wi_stage(self.s, "py-writer", self.item, "added g")
        with self.assertRaisesRegex(DurehError, "not verified"):
            wf.wi_promote(self.s, "desk", self.item, "too early")
        cb = wf.dispatch_brief(self.s, self.item, "pm-modules", "checker")
        self.assertIsNone(self.spawn("pm-modules", "checker", cb["brief"], "a-pm"))
        self.assertEqual(self.led.wi_status(self.item)["status"], "staged", "checker spawn is not a transition")
        r = wf.verify_wi(self.s, "checker", self.item)
        self.assertEqual(r["result"], "pass", r)
        self.assertEqual(r["changed"], ["src/demo.py"])
        ev = wf.wi_promote(self.s, "desk", self.item, "Gate 2 in test")
        self.assertIn("g()", (self.mod / "src" / "demo.py").read_text().replace("def g", "g()"))
        self.assertEqual(self.led.wi_status(self.item)["status"], "promoted")
        self.assertIn("git revert", ev["payload"]["compensation"])
        # the bundle passes once every child is verified or promoted, then promotes
        wf.wi_stage(self.s, "pm-modules", self.bundle, "bundle done")
        r = wf.verify_wi(self.s, "checker", self.bundle)
        self.assertEqual(r["result"], "pass", r)
        ev = wf.wi_promote(self.s, "desk", self.bundle, "bundle Gate 2")
        self.assertEqual(ev["payload"]["children"], [], "child already promoted individually")
        self.assertEqual(self.led.wi_status(self.bundle)["status"], "promoted")
        p = wf.purge(self.s, "desk", self.item)
        self.assertFalse(wt.exists())
        self.assertIn(str(wt), p["removed"])

    def test_stopped_children_are_not_counted_in_the_bundle_recount(self):
        # UNC_Lee_CAMRI-ORG finding fnd-20261003T184341577481Z-02e9dca5: superseded items made WI-0014 fail forever
        old = wf.wi_new(self.s, "desk", "old try", "Add g", "py-writer", "modules/demo", ["g"], parent=self.bundle)["id"]
        for w in (self.bundle, self.item, old):
            wf.wi_approve(self.s, "desk", w, "ok")
        wf.dispatch_stop(self.s, "desk", old, "superseded by the item")
        self.assertIsNone(self.spawn("desk", "pm-modules", wf.dispatch_brief(self.s, self.bundle, "desk")["brief"]))
        self.start("pm-modules", "a-pm")
        wb = wf.dispatch_brief(self.s, self.item, "pm-modules")
        self.assertIsNone(self.spawn("pm-modules", "py-writer", wb["brief"], "a-pm"))
        self.start("py-writer", "a-py")
        wt = Path(wb["worktree"])
        (wt / "src" / "demo.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
        subprocess.run(["git", "commit", "-qam", "add g"], cwd=wt, check=True)
        wf.wi_stage(self.s, "py-writer", self.item, "added g")
        hook(self.s, hook_event_name="SubagentStop", agent_type="py-writer", agent_id="a-py")
        self.assertIsNone(self.spawn("pm-modules", "checker", wf.dispatch_brief(self.s, self.item, "pm-modules", "checker")["brief"], "a-pm"))
        self.assertEqual(wf.verify_wi(self.s, "checker", self.item)["result"], "pass")
        wf.wi_stage(self.s, "pm-modules", self.bundle, "bundle done")
        r = wf.verify_wi(self.s, "checker", self.bundle)
        self.assertEqual(r["result"], "pass", r)
        self.assertIn(f"not counted (stopped): {old}", r["note"])

    def test_mkdir_of_a_scope_root_is_allowed(self):
        # UR-20261003-04: `mkdir -p <dir>` where <dir>/** is the role's scope
        for w in (self.bundle, self.item):
            wf.wi_approve(self.s, "desk", w, "ok")
        self.assertIsNone(self.spawn("desk", "pm-modules", wf.dispatch_brief(self.s, self.bundle, "desk")["brief"]))
        self.start("pm-modules", "a-pm")
        wb = wf.dispatch_brief(self.s, self.item, "pm-modules")
        self.assertIsNone(self.spawn("pm-modules", "py-writer", wb["brief"], "a-pm"))
        self.start("py-writer", "a-py")
        wt = Path(wb["worktree"])

        def bash(cmd):
            return hook(self.s, hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": cmd},
                        cwd=str(wt), agent_type="py-writer", agent_id="a-py")
        (wt / "tests").rename(wt / "tests-old")
        self.assertFalse(denied(bash(f"mkdir -p {wt}/tests")), "scope root")
        self.assertFalse(denied(bash(f"mkdir -p {wt}/src/pkg/sub")))
        self.assertTrue(denied(bash(f"mkdir -p {wt}/docs")), "outside every scope")
        # UR-20261003-05: fd redirections are not write targets
        for c in (f"mkdir -p {wt}/src/a 2>/dev/null", f"touch {wt}/src/a/x.py 2>&1", f"chmod +x {wt}/src/a/x.py >&2",
                  f"mkdir -p {wt}/src/b 1>&2", f"ls {wt} &>/dev/null", f"mkdir -p {wt}/src/c 2> /dev/null"):
            self.assertFalse(denied(bash(c)), c)
        self.assertTrue(denied(bash(f"ls {wt} 2>{wt}/docs/err.txt")), "a real file after 2> is still a write")
        self.assertTrue(denied(bash(f"echo x > {wt}/README.md")))

    def test_bundle_with_only_stopped_children_fails(self):
        wf.wi_approve(self.s, "desk", self.bundle, "ok")
        wf.wi_approve(self.s, "desk", self.item, "ok")
        wf.dispatch_stop(self.s, "desk", self.item, "dropped")
        self.assertIsNone(self.spawn("desk", "pm-modules", wf.dispatch_brief(self.s, self.bundle, "desk")["brief"]))
        wf.wi_stage(self.s, "pm-modules", self.bundle, "nothing left")
        self.assertEqual(wf.verify_wi(self.s, "checker", self.bundle)["result"], "fail")

    def test_verify_catches_out_of_scope_and_missing_output(self):
        for w in (self.bundle, self.item):
            wf.wi_approve(self.s, "desk", w, "ok")
        b = wf.dispatch_brief(self.s, self.bundle, "desk")
        self.spawn("desk", "pm-modules", b["brief"])
        wb = wf.dispatch_brief(self.s, self.item, "pm-modules")
        wt = Path(wb["worktree"])
        (wt / "NOTES.txt").write_text("stray output outside the role scope\n")
        (wt / "src" / "demo.py").unlink()
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-qm", "bad"], cwd=wt, check=True)
        self.spawn("pm-modules", "py-writer", wb["brief"], "a-pm")
        wf.wi_stage(self.s, "py-writer", self.item, "done?")
        r = wf.verify_wi(self.s, "checker", self.item)
        self.assertEqual(r["result"], "fail")
        self.assertIn("NOTES.txt", r["violations"])
        self.assertEqual(r["missing_outputs"], ["src/demo.py"])
        self.assertEqual(self.led.wi_status(self.item)["status"], "rework")
        wf.wi_stage(self.s, "pm-modules", self.bundle, "premature")
        self.assertEqual(wf.verify_wi(self.s, "checker", self.bundle)["result"], "fail", "open child blocks the bundle")

    def test_invalid_transitions_and_authority(self):
        with self.assertRaisesRegex(DurehError, "invalid transition"):
            self.led.event("wi.promoted", "desk", self.item, {})
        with self.assertRaisesRegex(DurehError, "only the desk"):
            wf.wi_new(self.s, "py-writer", "t", "g", "py-writer", "modules/demo", ["a"])
        with self.assertRaisesRegex(DurehError, "target must be"):
            wf.wi_new(self.s, "desk", "t", "g", "py-writer", "/etc", ["a"])
        with self.assertRaisesRegex(DurehError, "not authorized"):
            self.led.event("wi.approved", "py-writer", self.item, {})

    def test_resume_after_agent_finished(self):
        w = wf.wi_new(self.s, "desk", "r", "research", "researcher", "site", ["summary"])["id"]
        wf.wi_approve(self.s, "desk", w, "ok")
        self.assertIsNone(self.spawn("desk", "researcher", wf.dispatch_brief(self.s, w, "desk")["brief"]))
        with self.assertRaisesRegex(DurehError, "no agent has started for it yet"):  # release 30: refused within the grace
            wf.dispatch_brief(self.s, w, "desk")
        self.start("researcher", "a-r1")
        with self.assertRaisesRegex(DurehError, "has not finished"):
            wf.dispatch_brief(self.s, w, "desk")
        hook(self.s, hook_event_name="SubagentStop", agent_type="researcher", agent_id="a-r1")
        again = wf.dispatch_brief(self.s, w, "desk")
        self.assertIsNone(self.spawn("desk", "researcher", again["brief"]))
        self.assertEqual(self.led.wi_status(w)["status"], "dispatched")

    def test_lane_limit(self):
        ids = []
        for i in range(3):
            w = wf.wi_new(self.s, "desk", f"r{i}", "research", "researcher", "site", ["summary"])
            wf.wi_approve(self.s, "desk", w["id"], "ok")
            ids.append(w["id"])
        for n, w in enumerate(ids[:2]):
            self.spawn("desk", "researcher", wf.dispatch_brief(self.s, w, "desk")["brief"])
            self.start("researcher", f"a-r{n}")  # release 30: only a live agent (or a running job) holds a lane
            hook(self.s, hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": "ls"},
                 cwd=str(self.site_root), agent_type="researcher", agent_id=f"a-r{n}")  # first tool call binds it
        with self.assertRaisesRegex(DurehError, "lane limit"):
            wf.dispatch_brief(self.s, ids[2], "desk")
        hook(self.s, hook_event_name="SubagentStop", agent_type="researcher", agent_id="a-r0")
        wf.dispatch_brief(self.s, ids[2], "desk")  # the ended agent's item waits for the desk; it holds no lane

    def test_expired_waiver_blocks_dispatch(self):
        cfg = json.loads(self.s.config_path.read_text())
        cfg["models"]["worker"]["waiver_expires"] = "2000-01-01"
        self.s.config_path.write_text(json.dumps(cfg))
        from dureh_core.site import Site
        s = Site(self.site_root)
        w = wf.wi_new(s, "desk", "r", "research", "researcher", "site", ["summary"])
        wf.wi_approve(s, "desk", w["id"], "ok")
        with self.assertRaisesRegex(DurehError, "waiver expired"):
            wf.dispatch_brief(s, w["id"], "desk")


class ScratchIsolation(SiteCase):
    def test_pipeline_runner_writes_only_untracked_scratch_in_git_project(self):
        self.install()
        proj = self.site_root / "projects" / "p1"
        proj.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(proj)], check=True)
        from dureh_core.org import Org
        from dureh_core.paths import write_decision
        from helpers import HARNESS
        # the fixture package does not enable pipeline-runner; check the rule on a full org
        s = self.site()
        s._org = Org.load(HARNESS)
        ok, _ = write_decision(s, "pipeline-runner", proj / "checkpoints" / "WI-0002" / "job.sh")
        self.assertTrue(ok)
        ok, _ = write_decision(s, "pipeline-runner", proj / "code" / "job.sh")
        self.assertFalse(ok)
        ok, _ = write_decision(s, "interpreter", proj / "checkpoints" / "x")
        self.assertFalse(ok, "worktree roles do not write the main tree, even scratch")


class BashGuard(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()

    def bash(self, command, role="desk", **extra):
        data = dict(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": command, **extra},
                    cwd=str(self.site_root))
        if role != "desk":
            data.update(agent_type=role, agent_id="a1")
        return hook(self.s, **data)

    def test_denials(self):
        cases = [
            "sbatch job.sh", "srun hostname", "nohup python x.py", "python x.py &", "git push origin main",
            "apptainer exec img.sif ls", "fslmaths a b", "cd projects && antsRegistration -d 3", "rm -rf projects",
            "echo x > references/old-org/docs/old.md", "echo x >> state/records/event/a.json",
            "cp a.txt startup/CORE-VALUES.md", "timeout 5 sbatch x", "DUREH_ACTOR=desk bin/dureh status",
        ]
        for c in cases:
            with self.subTest(c=c):
                self.assertTrue(denied(self.bash(c)), c)
        self.assertTrue(denied(self.bash("sleep 100", run_in_background=True)))
        audited = [e for e in Ledger(self.s).events() if e["type"] == "guard.denied"]
        self.assertEqual(len(audited), len(cases) + 1, "every denial is audited")

    def test_worker_write_targets(self):
        self.assertTrue(denied(self.bash("echo x > notes/a.md", role="py-writer")))
        self.assertTrue(denied(self.bash("touch /home/elsewhere/file", role="py-writer")))
        self.assertFalse(denied(self.bash(f"echo x > {os.environ['TMPDIR']}/scratch.txt", role="py-writer")))
        self.assertFalse(denied(self.bash("python3 -m unittest -q", role="py-writer")))
        self.assertIsNone(self.bash("echo x > notes/a.md"), "desk owns notes/")

    def test_dureh_identity_rewrite(self):
        out = self.bash(f"{self.site_root}/bin/dureh status", role="py-writer")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertTrue(out["hookSpecificOutput"]["updatedInput"]["command"].startswith("DUREH_ACTOR=py-writer DUREH_AGENT_ID=a1 "))
        self.assertTrue(denied(self.bash(f"{self.site_root}/bin/dureh wi stage --wi WI-0001 --summary x --actor desk", role="py-writer")))
        self.assertTrue(denied(self.bash(f"{self.site_root}/bin/dureh status; rm x", role="py-writer")))

    def test_foreign_agent_and_network(self):
        out = hook(self.s, hook_event_name="PreToolUse", tool_name="Write", agent_type="general-purpose", agent_id="z",
                   tool_input={"file_path": str(self.site_root / "notes/x.md")})
        self.assertTrue(denied(out))
        out = hook(self.s, hook_event_name="PreToolUse", tool_name="WebFetch", agent_type="py-writer", agent_id="z",
                   tool_input={"url": "https://example.org"})
        self.assertTrue(denied(out))

    def test_desk_memory_cap_and_audit(self):
        cfg = json.loads(self.s.config_path.read_text())
        cfg["limits"] = {"memory_cap_bytes": 200}
        self.s.config_path.write_text(json.dumps(cfg))
        from dureh_core.site import Site
        s = Site(self.site_root)
        big = hook(s, hook_event_name="PreToolUse", tool_name="Write",
                   tool_input={"file_path": str(self.site_root / "state/memory/notes.md"), "content": "x" * 500})
        self.assertTrue(denied(big))
        path = self.site_root / "state/memory/handoff.md"
        self.assertIsNone(hook(s, hook_event_name="PreToolUse", tool_name="Write",
                               tool_input={"file_path": str(path), "content": "short"}))
        path.write_text("short")
        hook(s, hook_event_name="PostToolUse", tool_name="Write", tool_input={"file_path": str(path), "content": "short"})
        self.assertTrue(any(e["type"] == "memory.changed" for e in Ledger(s).events()))

    def test_session_hooks(self):
        out = hook(self.s, hook_event_name="SessionStart", source="startup", session_id="t1")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Tester", ctx)
        self.assertIn("Gate 1", ctx)
        self.assertIn("state saved", ctx, "the session's own start record is not an unsaved change")
        hook(self.s, hook_event_name="SessionEnd", reason="exit", session_id="t1")
        phases = [r["payload"]["phase"] for r in Ledger(self.s).load("session")]
        self.assertEqual(phases, ["start", "end"])
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.site_root / "state", capture_output=True, text=True)
        self.assertEqual(status.stdout.strip(), "", "session end saves state")


class Paths(SiteCase):
    def test_traversal_and_symlink_rejected_by_artifact_write(self):
        self.install()
        from helpers import HARNESS
        import sys
        src = self.tmp / "in.txt"
        src.write_text("hello")
        run = lambda p: subprocess.run([sys.executable, str(HARNESS / "dureh.py"), "--site", str(self.site_root), "artifact",
                                        "write", "--role", "desk", "--path", p, "--input", str(src)], capture_output=True, text=True)
        self.assertEqual(run("notes/ok.txt").returncode, 0)
        self.assertNotEqual(run("../escape.txt").returncode, 0)
        self.assertNotEqual(run("state/records/event/x.json").returncode, 0)
        (self.site_root / "notes" / "link").symlink_to(self.tmp)
        r = run("notes/link/evil.txt")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.tmp / "evil.txt").exists())
        self.assertNotEqual(run("notes/ok.txt").returncode, 0, "create-only")


if __name__ == "__main__":
    unittest.main()
