"""Release 64, sections B and C: `member run|exec|status|collect` with the codex verifier, against a fake codex (no
model, no quota) and a fake sbatch that runs nothing. The verdict goes through the `verify --wi` recording path."""
import json
import os
import subprocess
import unittest
from pathlib import Path

from member_helpers import JOB_ID, MemberCase

from dureh_core import members, ops
from dureh_core.members import codex
from dureh_core.util import DurehError

FAKE_CODEX = """
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
mode = open(os.path.join(here, "codex.mode")).read().strip() if os.path.exists(os.path.join(here, "codex.mode")) else "pass"
if sys.argv[1:2] == ["--version"]:
    print("WARNING: failed to clean up stale arg0 temp dirs")
    print("codex-cli 9.9.9"); sys.exit(0)
with open(os.path.join(here, "codex.argv"), "w") as fh:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": sorted(os.environ)}, fh)
if mode == "hang":
    time.sleep(30)
if mode == "quota":
    print(json.dumps({"type": "error", "message": "You've hit your usage limit. Try again later."}))
    sys.exit(1)
ev = [{"type": "thread.started", "thread_id": "t1"},
      {"type": "item.completed", "item": {"type": "command_execution", "command": "cat diff.patch", "exit_code": 0,
                                           "status": "completed"}}]
verdict = {"pass": "VERDICT: PASS", "fail": "VERDICT: FAIL", "noverdict": "looks fine"}[mode]
ev.append({"type": "item.completed", "item": {"type": "agent_message",
           "text": "criterion g() returns 2: met (src/demo.py:2)\\n" + verdict}})
ev.append({"type": "turn.completed", "usage": {"input_tokens": 1200, "cached_input_tokens": 300, "output_tokens": 80}})
for e in ev:
    print(json.dumps(e))
"""


class CodexUnits(unittest.TestCase):
    CFG = {"codex": {**members.DEFAULTS["codex"], "model": "m-test", "reasoning_effort": "high"}}

    def test_argv_uses_the_named_profile_never_the_generic_sandbox_flag(self):
        argv = codex.exec_argv("/x/codex", Path("/r/evidence"), self.CFG, ["/r/evidence", "/x"])
        self.assertEqual(argv[:8], ["/x/codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                                    "--skip-git-repo-check", "--json", "-C"])
        self.assertEqual(argv[8], "/r/evidence")
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("-s", argv)
        joined = " ".join(argv)
        self.assertIn('default_permissions="dureh_member"', joined)
        self.assertIn('permissions.dureh_member.filesystem={":minimal"="read","/r/evidence"="read","/x"="read"}', joined)
        self.assertIn("permissions.dureh_member.network={enabled=false}", joined)
        self.assertNotIn('"write"', joined)
        for c in ("project_doc_max_bytes=0", 'web_search="disabled"', "allow_login_shell=false",
                  'model_reasoning_effort="high"'):
            self.assertIn(c, argv)
        self.assertEqual(argv[-2:], ["-m", "m-test"])

    def test_parse_events_verdict_and_failures(self):
        lines = [json.dumps(x) for x in (
            {"type": "thread.started", "thread_id": "t"},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "a\nVERDICT: FAIL"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
            {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}},
            {"type": "turn.failed", "error": {"message": "boom"}})] + ["not json"]
        p = codex.parse_events(lines)
        self.assertEqual(p["usage"], {"input_tokens": 15, "output_tokens": 3})
        self.assertEqual(p["errors"], ["boom"])
        self.assertEqual(len(p["tool_log"]), 1)
        self.assertEqual(codex.verdict(p["output"]), "FAIL")
        self.assertEqual(codex.verdict("x\nverdict: pass\n"), "PASS")
        self.assertIsNone(codex.verdict("no verdict here"))
        self.assertIn("login", codex.failure_reason(1, "Error: not logged in", []))
        self.assertIn("quota", codex.failure_reason(1, "", ["429 Too Many Requests"]))
        self.assertIn("exited 3", codex.failure_reason(3, "segfault", []))
        self.assertIsNone(codex.failure_reason(0, "", []))
        u = codex.usage_row(p, "default", "medium")
        self.assertEqual((u["coverage"], u["input_tokens"], u["output_tokens"], u["model_requested"]),
                         ("runtime", 15, 3, "default"))


def res_head(wt):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True).stdout.strip()


class CodexMember(MemberCase):
    def setUp(self):
        super().setUp()
        self.py_tool("codex", FAKE_CODEX)
        self.wi = self.item(role="py-writer", checks=["test -f src/demo.py"])
        b = self.wf.dispatch_brief(self.s, self.wi, "pm-modules")
        self.wt = Path(b["worktree"])
        self.led.event("wi.dispatched", "pm-modules", self.wi, {"role": "py-writer", "brief": "1" * 16, "by": "pm-modules"})
        self.commit("def f():\n    return 1\n\ndef g():\n    return 2\n")
        self.wf.wi_stage(self.s, "py-writer", self.wi, "done")
        os.environ.pop("DUREH_ACTOR", None)

    def commit(self, text):
        (self.wt / "src" / "demo.py").write_text(text)
        subprocess.run(["git", "commit", "-qam", "change"], cwd=self.wt, check=True)

    def mode(self, m):
        (self.fake / "codex.mode").write_text(m)

    def checker_brief(self):
        return self.wf.dispatch_brief(self.s, self.wi, "pm-modules", "checker")

    def submit(self):
        self.checker_brief()
        return members.run(self.s, "pm-modules", self.wi)

    def exec_(self, run_id):
        os.environ["SLURM_JOB_ID"] = JOB_ID
        try:
            return members.execute(self.s, run_id)
        finally:
            os.environ.pop("SLURM_JOB_ID", None)

    # ------------------------------------------------------------- run
    def test_run_without_a_canary_is_not_run_and_keeps_the_brief(self):
        b = self.checker_brief()
        with self.assertRaisesRegex(DurehError, "is NOT_RUN: no passing codex canary"):
            members.run(self.s, "pm-modules", self.wi)
        nr = self.events(self.wi, "member.not-run")
        self.assertEqual(len(nr), 1)
        self.assertEqual(nr[0]["payload"]["brief"], b["header"].split("brief=")[1])
        self.assertFalse((self.fake / "sbatch.args").exists(), "nothing submitted")
        r = members.run(self.s, "pm-modules", self.wi, fallback=True, reason="canary pending")  # the brief is unused
        self.assertTrue(r["fallback"])

    def test_run_submits_one_job_and_records_the_dispatch(self):
        self.pass_canary()
        r = self.submit()
        self.assertEqual(r["job_id"], JOB_ID)
        args = (self.fake / "sbatch.args").read_text().split()
        self.assertEqual(args[0], "--parsable")
        script = Path(args[-1]).read_text()
        self.assertIn(f"#SBATCH --job-name={self.s.session_name}-member-{self.wi}", script)
        self.assertIn(f"member exec --run {r['run']}", script)
        self.assertIn("unset DUREH_ACTOR DUREH_AGENT_ID", script)
        self.assertIn("#SBATCH --cpus-per-task=2", script)
        d = self.events(self.wi, "checker.dispatched")[-1]
        self.assertEqual((d["actor"], d["payload"]["runtime"], d["payload"]["member_run"], d["payload"]["job_id"]),
                         ("pm-modules", "codex", r["run"], JOB_ID))
        run = next(x for x in self.led.load("run") if x["id"] == r["run"])
        self.assertEqual((run["payload"]["run_type"], run["payload"]["status"]), ("member", "submitted"))
        self.assertIn(run, ops._tracked_runs(self.led))
        with self.assertRaisesRegex(DurehError, "already used"):
            members.run(self.s, "pm-modules", self.wi)

    # ------------------------------------------------------------- exec + collect
    def test_pass_verdict_is_recorded_as_the_verification_bound_to_the_head(self):
        self.pass_canary()
        r = self.submit()
        res = self.exec_(r["run"])
        self.assertEqual((res["outcome"], res["verdict"]), ("completed", "PASS"), res)
        call = json.loads((self.fake / "codex.argv").read_text())
        self.assertNotIn("--sandbox", call["argv"])
        self.assertTrue(call["cwd"].endswith("/evidence"))
        self.assertNotIn("DUREH_SITE", call["env"], "a minimal environment")
        ev = Path(call["cwd"])
        for f in ("brief.md", "acceptance.md", "diff.patch", "checks.json", "changed.txt"):
            self.assertTrue((ev / f).is_file(), f)
        self.assertIn("+def g():", (ev / "diff.patch").read_text())
        self.assertEqual(json.loads((ev / "checks.json").read_text())[0]["exit"], 0)
        started = self.events(self.wi, "agent.started")[-1]
        self.assertEqual((started["actor"], started["payload"]["bound_by"], started["refs"]),
                         ("checker", "member-run", [self.events(self.wi, "checker.dispatched")[-1]["id"]]))
        self.assertEqual(self.events(self.wi, "agent.finished")[-1]["payload"]["outcome"], "completed")
        usage = [x for x in self.led.load("run") if x["payload"].get("member_run") == r["run"]][0]["payload"]
        self.assertEqual((usage["runtime"], usage["usage"]["input_tokens"], usage["usage"]["output_tokens"]),
                         ("codex", 1200, 80))
        status = json.loads((Path(res["run"] and members.run_dir(self.s, self.wi, r["run"])) / "dispatch"
                             / "run-status.json").read_text())
        self.assertEqual((status["state"], status["outcome"]), ("done", "completed"))
        st = members.status(self.s, self.wi)
        self.assertEqual(st["runs"][-1]["verdict"], "PASS")
        out = members.collect(self.s, "pm-modules", self.wi)
        self.assertEqual((out["verdict"], out["result"]), ("PASS", "pass"))
        self.assertEqual(self.led.wi_status(self.wi)["status"], "verified")
        rec = self.events(self.wi, "verification.recorded")[-1]
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.wt, capture_output=True, text=True).stdout.strip()
        self.assertEqual((rec["actor"], rec["payload"]["head"]), ("checker", head))
        vrun = next(x for x in self.led.load("run") if x["id"] == out["verification"])["payload"]
        self.assertEqual(vrun["checks"][-1]["verdict"], "PASS")
        self.assertIn("review PASS", vrun["note"])
        self.assertEqual(vrun["member_run"], r["run"])
        again = members.collect(self.s, "pm-modules", self.wi)
        self.assertIn("already collected", again["note"])

    def test_evidence_lists_tracked_changes_only_captured_before_the_checks(self):
        """Release 65 (UR-20261015-05): build output the declared checks create (ignored or untracked) is never in
        changed.txt or the diff; the reviewer is told that scope is judged on changed.txt and that a process claim
        the diff cannot show is `not checked`."""
        (self.wt / "src" / ".gitignore").write_text("*.egg-info/\n")
        subprocess.run(["git", "add", "src/.gitignore"], cwd=self.wt, check=True)
        subprocess.run(["git", "commit", "-qm", "ignore build output"], cwd=self.wt, check=True)
        checks = ["mkdir -p src/demo.egg-info && echo x > src/demo.egg-info/PKG-INFO && echo y > build-note.txt"
                  " && test -f src/demo.py"]
        self.led.event("wi.amended", "desk", self.wi, {"fields": {"checks": checks}, "before": {}, "memo": "test",
                                                       "by": "Director"})
        self.assertEqual(self.led.get("work-item", self.wi)["payload"]["checks"], checks)
        self.pass_canary()
        r = self.submit()
        res = self.exec_(r["run"])
        self.assertEqual(res["outcome"], "completed", res)
        ev = Path(json.loads((self.fake / "codex.argv").read_text())["cwd"])
        self.assertTrue((self.wt / "src" / "demo.egg-info" / "PKG-INFO").is_file(), "the check did build it")
        changed = (ev / "changed.txt").read_text().split()
        self.assertEqual(changed, ["src/.gitignore", "src/demo.py"])
        self.assertEqual(res["changed"], changed)
        self.assertNotIn("PKG-INFO", (ev / "diff.patch").read_text())
        self.assertNotIn("build-note", (ev / "diff.patch").read_text())
        self.assertFalse((ev / "files").exists())
        prompt = json.loads((self.fake / "codex.argv").read_text())["argv"][-1]
        self.assertIn("not checked", prompt)
        self.assertIn("changed.txt alone", prompt)
        self.assertNotIn("build-note.txt", res["owned_paths"], "the ownership list is taken before the checks")
        out = members.collect(self.s, "pm-modules", self.wi)
        vrun = next(x for x in self.led.load("run") if x["id"] == out["verification"])["payload"]
        self.assertNotIn("build-note.txt", vrun["changed"])
        self.assertEqual(vrun["violations"], [])

    def held_fail(self):
        self.pass_canary()
        self.mode("fail")
        r = self.submit()
        self.exec_(r["run"])
        return r, members.collect(self.s, "pm-modules", self.wi)

    def verify_runs(self):
        return [x for x in self.led.load("run") if x["wi"] == self.wi and x["payload"].get("run_type") == "verify"]

    def test_fail_verdict_is_held_until_the_dispatcher_chooses_rework(self):
        """Release 66 (UR-20261015-07): a collected codex FAIL is recorded (one verify run, bound to the head) and
        held: the item stays staged; `member collect --rework` turns the held verification into the rework."""
        r, out = self.held_fail()
        self.assertEqual(out["result"], "fail")
        self.assertTrue(out["held"])
        self.assertIn("--rework", out["next"])
        self.assertIn("--fallback", out["next"])
        self.assertEqual(self.led.wi_status(self.wi)["status"], "staged")
        self.assertEqual(self.events(self.wi, "rework.requested"), [])
        held = self.events(self.wi, "member.held")
        self.assertEqual(len(held), 1)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.wt, capture_output=True, text=True).stdout.strip()
        self.assertEqual((held[0]["actor"], held[0]["payload"]["head"], held[0]["payload"]["member_run"]),
                         ("checker", head, r["run"]))
        runs = self.verify_runs()
        self.assertEqual([x["payload"]["status"] for x in runs], ["fail"])
        self.assertEqual(held[0]["payload"]["run"], runs[0]["id"])
        st = members.status(self.s, self.wi)
        self.assertEqual((st["held"]["event"], st["held"]["choice"]), (held[0]["id"], None))
        self.assertIn("verdict FAIL, held", members.render_status(st))
        # a new codex run of the checker is not one of the two choices
        self.checker_brief()
        with self.assertRaisesRegex(DurehError, "is held"):
            members.run(self.s, "pm-modules", self.wi)
        self.assertEqual(self.events(self.wi, "member.not-run"), [])
        with self.assertRaisesRegex(DurehError, "only it or the desk"):
            members.collect(self.s, "py-writer", self.wi, rework=True)
        with self.assertRaisesRegex(DurehError, "--rework stands alone"):
            members.collect(self.s, "pm-modules", self.wi, rework=True, stage=True)
        done = members.collect(self.s, "pm-modules", self.wi, rework=True, reason="a real gap in g()")
        self.assertEqual(self.led.wi_status(self.wi)["status"], "rework")
        rw = self.events(self.wi, "rework.requested")
        self.assertEqual(len(rw), 1)
        self.assertEqual((rw[0]["actor"], rw[0]["payload"]["run"], rw[0]["payload"]["held"], rw[0]["payload"]["choice"]),
                         ("pm-modules", runs[0]["id"], held[0]["id"], done["choice"]))
        ch = self.events(self.wi, "member.choice")[-1]
        self.assertEqual((ch["payload"]["choice"], ch["payload"]["held"]), ("rework", held[0]["id"]))
        self.assertEqual(len(self.verify_runs()), 1, "the one FAIL stays the only verification")
        b = self.wf.dispatch_brief(self.s, self.wi, "pm-modules")
        self.assertIn(f"Last verification (run {runs[0]['id']}", b["brief"])
        with self.assertRaisesRegex(DurehError, "no held checker verdict"):
            members.collect(self.s, "pm-modules", self.wi, rework=True)

    def test_held_fail_allows_the_claude_checker_fallback_on_the_same_head(self):
        r, out = self.held_fail()
        held = self.events(self.wi, "member.held")[-1]
        b = self.checker_brief()
        fb = members.run(self.s, "pm-modules", self.wi, fallback=True, reason="codex lacked evidence")
        self.assertEqual(fb["held"], held["id"])
        ch = self.events(self.wi, "member.choice")[-1]
        self.assertEqual((ch["payload"]["choice"], ch["payload"]["fallback"], ch["payload"]["head"]),
                         ("fallback", fb["event"], held["payload"]["head"]))
        self.assertEqual(self.events(self.wi, "member.fallback")[-1]["payload"]["brief"], b["header"].split("brief=")[1])
        self.assertEqual(self.led.wi_status(self.wi)["status"], "staged")
        self.assertEqual(members.status(self.s, self.wi)["held"]["choice"], "fallback")
        # the Claude checker verifies the same staged head (here: the declared checks pass, so it is verified)
        res = self.wf.verify_wi(self.s, "checker", self.wi, local=True)
        self.assertEqual(res["result"], "pass")
        self.assertEqual(self.led.wi_status(self.wi)["status"], "verified")
        runs = self.verify_runs()
        self.assertEqual([(x["payload"]["status"], x["payload"]["head"]) for x in runs],
                         [("fail", held["payload"]["head"]), ("pass", held["payload"]["head"])])
        self.assertEqual(members.hold_state(self.led, self.wi), (None, None))

    def test_rework_is_still_possible_after_a_fallback_that_did_not_verify(self):
        self.held_fail()
        self.checker_brief()
        members.run(self.s, "pm-modules", self.wi, fallback=True, reason="try the Claude checker")
        members.collect(self.s, "desk", self.wi, rework=True)
        self.assertEqual(self.led.wi_status(self.wi)["status"], "rework")
        self.assertEqual([e["payload"]["choice"] for e in self.events(self.wi, "member.choice")], ["fallback", "rework"])

    def run_cli(self, *argv, actor="pm-modules"):
        import contextlib
        import io

        from dureh_core.cli import main

        out = io.StringIO()
        os.environ["DUREH_ACTOR"] = actor
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = main(["--site", str(self.site_root), *argv])
        finally:
            os.environ.pop("DUREH_ACTOR", None)
        return rc, out.getvalue()

    def test_member_held_and_choice_are_reserved_and_rework_is_a_cli_choice(self):
        self.held_fail()
        for t in ("member.held", "member.choice"):
            rc, out = self.run_cli("ledger", "add", "event", "--type", t, "--wi", self.wi, actor="desk")
            self.assertEqual(rc, 2, out)
        rc, out = self.run_cli("member", "status", "--wi", self.wi)
        self.assertIn("verdict FAIL, held", out)
        rc, out = self.run_cli("member", "collect", "--wi", self.wi, "--rework", "--reason", "fix g()")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.led.wi_status(self.wi)["status"], "rework")

    def pin_requirements(self, text="# Requirements\n- g() must not import anything new\n"):
        req = self.site_root / "req-g.md"
        req.write_text(text)
        pin = self.wf.requirements_pin(self.s, "req-g.md")
        self.led.event("wi.amended", "desk", self.wi, {"fields": {"requirements": pin}, "before": {}, "memo": "test",
                                                       "by": "Director"})
        return req, pin

    def evidence(self):
        return Path(json.loads((self.fake / "codex.argv").read_text())["cwd"])

    def test_evidence_has_the_requirements_the_brief_and_a_snapshot_of_the_target(self):
        """Release 66 (UR-20261015-06): the pinned requirements file (sha256 checked), the checker's brief and a
        read-only `git archive` snapshot of the target at the reviewed head, unchanged files included."""
        req, pin = self.pin_requirements()
        self.pass_canary()
        b = self.checker_brief()
        r = members.run(self.s, "pm-modules", self.wi)
        res = self.exec_(r["run"])
        self.assertEqual(res["outcome"], "completed", res)
        ev = self.evidence()
        self.assertEqual((ev / "requirements" / "req-g.md").read_text(), req.read_text())
        self.assertIn(b["header"], (ev / "brief.md").read_text())
        man = json.loads((ev / "manifest.json").read_text())
        self.assertEqual((man["requirements"]["included"], man["requirements"]["sha256"]), (True, pin["sha256"]))
        self.assertEqual(man["brief"]["digest"], b["header"].split("brief=")[1])
        self.assertTrue(man["tree"]["included"])
        self.assertEqual(man["head"], res["head"])
        self.assertEqual((ev / "tree" / "src" / "demo.py").read_text(), (self.wt / "src" / "demo.py").read_text())
        self.assertTrue((ev / "tree" / "tests" / "test_demo.py").is_file(), "an unchanged file is in the snapshot")
        self.assertEqual((ev / "changed.txt").read_text().split(), ["src/demo.py"], "changed.txt stays base..head")
        self.assertFalse(os.access(ev / "tree" / "src" / "demo.py", os.W_OK) and os.geteuid() != 0)
        acc = (ev / "acceptance.md").read_text()
        self.assertIn("requirements/req-g.md", acc)
        self.assertIn("tree/: 2 files", acc)
        prompt = json.loads((self.fake / "codex.argv").read_text())["argv"][-1]
        for word in ("requirements/", "tree/", "manifest.json"):
            self.assertIn(word, prompt)
        self.assertEqual((res["evidence"]["requirements"], res["evidence"]["tree"]), (True, True))

    def test_snapshot_cap_and_no_read_paths_are_listed_not_included(self):
        from unittest import mock

        from dureh_core.site import Site

        self.config(members={**self.s.config["members"], "codex": {"sandbox_read": [], "wait_seconds": 30,
                                                                   "tree_max_bytes": 10}})
        self.pass_canary()
        r = self.submit()
        self.exec_(r["run"])
        man = json.loads((self.evidence() / "manifest.json").read_text())
        self.assertFalse(man["tree"]["included"])
        self.assertIn("over the 10-byte cap", man["tree"]["reason"])
        self.assertIsNone(man["requirements"])
        self.assertFalse((self.evidence() / "tree").exists())
        self.assertIn("tree/: not included", (self.evidence() / "acceptance.md").read_text())
        members.collect(self.s, "pm-modules", self.wi)
        self.config(members={**self.s.config["members"], "codex": {"sandbox_read": [], "wait_seconds": 30}})
        rules = [{"path": "modules/*/tests/**", "level": "no-read", "why": "test data"}]
        with mock.patch.object(Site, "protected", lambda site: rules):
            ctx_dir = self.tmp / "ev-noread"
            out = codex._tree(self.s, self.wt, "modules/demo", res_head(self.wt), ctx_dir, 1 << 20)
        self.assertTrue(out["included"])
        self.assertEqual(out["left_out"], [{"path": "tests/test_demo.py", "why": "no-read (protected by the package)"}])
        self.assertTrue((ctx_dir / "tree" / "src" / "demo.py").is_file())
        self.assertFalse((ctx_dir / "tree" / "tests").exists())

    def test_requirements_changed_after_the_brief_is_not_run(self):
        req, _ = self.pin_requirements()
        self.pass_canary()
        r = self.submit()
        req.write_text("other requirements\n")
        res = self.exec_(r["run"])
        self.assertEqual(res["outcome"], "not_run")
        self.assertIn("requirements file", res["reason"])

    def test_tree_max_bytes_config_is_checked(self):
        self.assertIn("config members.codex.tree_max_bytes must be a non-negative number",
                      members.config_errors(self.s, {"members": {"codex": {"tree_max_bytes": "big"}}}))
        self.assertEqual(members.settings(self.s)["codex"]["tree_max_bytes"], 20 * 1024 * 1024)

    def test_a_moved_head_supersedes_the_review(self):
        self.pass_canary()
        r = self.submit()
        self.exec_(r["run"])
        self.commit("def f():\n    return 1\n\ndef g():\n    return 3\n")
        out = members.collect(self.s, "pm-modules", self.wi)
        self.assertTrue(out["superseded"])
        self.assertEqual(self.events(self.wi, "verification.recorded"), [])
        self.assertEqual(self.led.wi_status(self.wi)["status"], "staged")

    def test_hang_quota_and_no_verdict_are_not_run(self):
        self.pass_canary()
        for mode, words in (("hang", "hung past its wait limit"), ("quota", "quota or rate limit"),
                            ("noverdict", "no `VERDICT")):
            self.mode(mode)
            if mode == "hang":
                self.config(members={**self.s.config["members"], "codex": {"sandbox_read": [], "wait_seconds": 1}})
            r = self.submit()
            res = self.exec_(r["run"])
            self.assertEqual(res["outcome"], "not_run", mode)
            self.assertIn(words, res["reason"])
            out = members.collect(self.s, "pm-modules", self.wi)
            self.assertIn("NOT_RUN", out["next"])
            self.assertIn("--fallback", out["next"])
            self.assertEqual(self.led.wi_status(self.wi)["status"], "staged")
            if mode == "hang":
                self.config(members={**self.s.config["members"], "codex": {"sandbox_read": [], "wait_seconds": 30}})

    def test_exec_only_inside_its_own_job(self):
        self.pass_canary()
        r = self.submit()
        os.environ["DUREH_ACTOR"] = "checker"
        with self.assertRaisesRegex(DurehError, "never from an agent"):
            members.execute(self.s, r["run"])
        os.environ.pop("DUREH_ACTOR")
        os.environ["SLURM_JOB_ID"] = "999"
        with self.assertRaisesRegex(DurehError, "only inside its own job"):
            members.execute(self.s, r["run"])
        os.environ.pop("SLURM_JOB_ID")
        self.exec_(r["run"])
        with self.assertRaisesRegex(DurehError, "already done"):
            self.exec_(r["run"])

    def test_collect_while_running_and_a_job_that_died(self):
        self.pass_canary()
        r = self.submit()
        with self.assertRaisesRegex(DurehError, "still RUNNING"):
            members.collect(self.s, "pm-modules", self.wi)
        self.assertEqual(ops.agent_state(self.led, self.wi), "not started")  # a checker dispatch: no agent state
        (self.fake / "sacct.out").write_text("NODE_FAIL|0:0|00:00:02\n")
        out = members.collect(self.s, "pm-modules", self.wi)
        self.assertEqual(out["outcome"], "not_run")
        self.assertIn("NODE_FAIL", out["reason"])
        fin = self.events(self.wi, "agent.finished")[-1]
        self.assertEqual((fin["actor"], fin["payload"]["run"]), ("checker", r["run"]))
        with self.assertRaisesRegex(DurehError, "only it or the desk"):
            members.collect(self.s, "py-writer", self.wi)

    def test_watch_reports_the_member_job_with_collect_next(self):
        self.pass_canary()
        r = self.submit()
        (self.fake / "sacct.out").write_text(f"{JOB_ID}|COMPLETED|0:0|00:00:09\n")  # the batched form: JobID first
        lines = ops.watch_round(self.s, "desk", set())
        self.assertEqual(len(lines), 1)
        self.assertIn(f"run={r['run']}", lines[0])
        self.assertIn(f"member collect --wi {self.wi}", lines[0])

    def test_dispatch_stop_cancels_a_live_member_job(self):
        self.pass_canary()
        r = self.submit()
        self.wf.dispatch_stop(self.s, "desk", self.wi, "wrong checks")
        self.assertEqual((self.fake / "scancel.args").read_text().split(), [JOB_ID])
        stop = self.events(self.wi, "wi.stopped")[-1]
        self.assertEqual(stop["payload"]["member_runs_cancelled"][0]["run"], r["run"])
        self.assertEqual(members.live_runs(self.led, self.wi), [])

    def test_canary_records_pass_and_fail(self):
        def sandbox(leak):
            def runner(argv, **kw):
                cmd = argv[argv.index("--") + 1:]
                self.assertEqual(argv[1:3], ["sandbox", "-P"])
                self.assertIn("permissions.dureh_member.network={enabled=false}", argv)
                if cmd == ["/bin/cat", "allowed.txt"]:
                    return subprocess.CompletedProcess(argv, 0, codex.CANARY_ALLOW + "\n", "")
                if leak and cmd[0] == "/bin/cat":
                    return subprocess.CompletedProcess(argv, 0, codex.CANARY_DENY + "\n", "")
                return subprocess.CompletedProcess(argv, 1, "", "Read-only file system")
            return runner

        cfg = members.settings(self.s)
        ok = codex.canary(self.s, "desk", cfg, runner=sandbox(False))
        self.assertEqual(ok["result"], "pass", ok["rows"])
        self.assertEqual(ok["version"], "codex-cli 9.9.9")
        self.assertTrue(codex.passed_canary(self.led, ok["host"], "codex-cli 9.9.9"))
        bad = codex.canary(self.s, "desk", cfg, runner=sandbox(True))
        self.assertEqual(bad["result"], "fail")
        self.assertIsNone(codex.passed_canary(self.led), "the newest canary failed")
        rows = {r["case"]: r["ok"] for r in bad["rows"]}
        self.assertFalse(rows["read outside (relative)"])
        self.assertTrue(rows["no file written inside"])

    def test_exec_runs_the_canary_on_a_new_host_and_refuses_when_it_fails(self):
        self.pass_canary(version="codex-cli 0.0.1")  # another codex version: this host has no canary for 9.9.9
        r = self.submit()
        res = self.exec_(r["run"])  # the fake codex has no sandbox: `codex sandbox` "succeeds" at everything
        self.assertEqual(res["outcome"], "not_run")
        self.assertIn("the codex canary failed", res["reason"])
        self.assertEqual(self.events(None, "member.canary")[-1]["actor"], "checker")


if __name__ == "__main__":
    unittest.main()
