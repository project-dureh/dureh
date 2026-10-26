"""Release 64, section D: the local runtime's tool loop against a local HTTP fake of the office-hours server (no real
model server), its stall handling, and the office-hours job (plan output, fake sbatch: nothing is submitted)."""
import json
import os
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from member_helpers import JOB_ID, MemberCase
from helpers import commit_work

from dureh_core import members, office_hours, sessions
from dureh_core.members import local
from dureh_core.util import DurehError


class FakeServer:
    """A scripted `/api/chat` (one script per call: a list of steps), `/api/tags` and `/api/ps`."""

    def __init__(self, models=("coder-test",)):
        self.models = list(models)
        self.loaded = list(models)
        self.scripts: list = []
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):
                names = fake.models if self.path == "/api/tags" else fake.loaded
                body = json.dumps({"models": [{"name": f"{n}:latest", "model": f"{n}:latest"} for n in names]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(req)
                steps = fake.scripts.pop(0) if fake.scripts else [("done", "")]
                if steps == "drop":
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                for kind, value in steps:
                    if kind == "sleep":
                        time.sleep(value)
                        continue
                    if kind == "text":
                        chunk = {"message": {"role": "assistant", "content": value}, "done": False}
                    elif kind == "tools":
                        chunk = {"message": {"role": "assistant", "content": "", "tool_calls": value}, "done": False}
                    elif kind == "final":  # a final chunk with the given counters (release 65)
                        chunk = {"message": {"role": "assistant", "content": value.get("content", "")}, "done": True,
                                 "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 20,
                                 **{k: v for k, v in value.items() if k != "content"}}
                    else:
                        chunk = {"message": {"role": "assistant", "content": value}, "done": True, "done_reason": "stop",
                                 "prompt_eval_count": 100, "eval_count": 20, "total_duration": 2_000_000_000,
                                 "eval_duration": 1_000_000_000}
                    try:
                        self.wfile.write((json.dumps(chunk) + "\n").encode())
                        self.wfile.flush()
                    except OSError:
                        return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def call(name, **args):
    return {"function": {"name": name, "arguments": args}}


class LocalMember(MemberCase):
    def setUp(self):
        super().setUp()
        self.srv = FakeServer()
        self.addCleanup(self.srv.close)
        self.wi = self.item(checks=["python3 -c 'import sys; sys.path.insert(0, \"src\"); import demo; assert demo.g() == 2'"])
        self.brief = self.wf.dispatch_brief(self.s, self.wi, "pm-modules")
        self.wt = Path(self.brief["worktree"])

    def serve(self, models=None, sha=None):
        oh = office_hours.logs_dir(self.s)
        oh.mkdir(parents=True, exist_ok=True)
        digest = sha or __import__("hashlib").sha256((self.s.startup / "profiles" / "coder.Modelfile").read_bytes()).hexdigest()
        (oh / "server.json").write_text(json.dumps({"kind": "ollama", "host": "127.0.0.1", "port": self.srv.port,
                                                     "models": models or ["coder-test"], "job": "8800",
                                                     "profiles": {"coder-test": {"sha256": digest}}}))

    def submit(self):
        return members.run(self.s, "pm-modules", self.wi)

    def exec_(self, run_id):
        os.environ["SLURM_JOB_ID"] = JOB_ID
        try:
            return members.execute(self.s, run_id)
        finally:
            os.environ.pop("SLURM_JOB_ID", None)

    def test_no_server_is_not_run_before_anything_is_submitted(self):
        with self.assertRaisesRegex(DurehError, "NOT_RUN: no office-hours server is running"):
            self.submit()
        self.assertFalse((self.fake / "sbatch.args").exists())
        self.serve(models=["other"])
        with self.assertRaisesRegex(DurehError, "does not serve coder-test"):
            self.submit()

    def test_no_declared_output_is_not_run(self):
        wi = self.item()
        # an item recorded before release 65 refused it at creation: the preflight still refuses at `member run`
        self.led.event("wi.amended", "desk", wi, {"fields": {"outputs": ["README.md"]}, "before": {}, "memo": "old",
                                                  "by": "Director"})
        self.serve()
        self.wf.dispatch_brief(self.s, wi, "pm-modules")
        with self.assertRaisesRegex(DurehError, "declares no output inside the role's write scopes"):
            members.run(self.s, "pm-modules", wi)

    def test_tool_loop_writes_the_allowlisted_file_and_runs_the_checks(self):
        self.serve()
        new = "def f():\n    return 1\n\ndef g():\n    return 2\n"
        self.srv.scripts = [
            [("tools", [call("list_files", path="."), call("read_file", path="src/demo.py")]), ("done", "")],
            [("tools", [call("read_file", path="../../config.json"), call("write_file", path="tests/x.py", content="x"),
                        call("write_file", path="src/demo.py", content=new), call("run_tests")]), ("done", "")],
            [("text", "Changed src/demo.py; "), ("done", "checks pass.")]]
        r = self.submit()
        script = Path((self.fake / "sbatch.args").read_text().split()[-1]).read_text()
        self.assertIn(f"member exec --run {r['run']}", script)
        res = self.exec_(r["run"])
        self.assertEqual(res["outcome"], "completed", res)
        self.assertEqual(res["writes"], {"src/demo.py": 1})
        self.assertEqual(res["tests"][0]["exit"], 0)
        self.assertEqual(res["output"], "Changed src/demo.py; checks pass.")
        self.assertEqual((res["usage"]["input_tokens"], res["usage"]["output_tokens"], res["usage"]["calls"]), (300, 60, 3))
        self.assertEqual(res["usage"]["gpu_seconds"], 6.0)
        self.assertEqual(res["profile"]["name"], "coder-test")
        log = json.loads((members.run_dir(self.s, self.wi, r["run"]) / "dispatch" / "tools.json").read_text())["log"]
        self.assertEqual([(e["tool"], e["result"]) for e in log],
                         [("list_files", "allowed"), ("read_file", "allowed"), ("read_file", "denied"),
                          ("write_file", "denied"), ("write_file", "allowed"), ("run_tests", "allowed")])
        # the whole conversation and the final files stay in the run directory for training examples
        rdir = members.run_dir(self.s, self.wi, r["run"]) / "dispatch"
        tr = json.loads((rdir / "transcript-1.json").read_text())
        self.assertEqual([m["role"] for m in tr["messages"]][:2], ["user", "assistant"])
        self.assertIn(self.brief["header"], tr["messages"][0]["content"])
        self.assertTrue(any(m["role"] == "tool" for m in tr["messages"]))
        self.assertEqual(tr["messages"][-1], {"role": "assistant", "content": "Changed src/demo.py; checks pass."})
        self.assertEqual((tr["model"], tr["status"]), ("coder-test", "ok"))
        self.assertEqual(json.loads((rdir / "final-files.json").read_text()), {"src/demo.py": new})
        first = self.srv.requests[0]
        self.assertEqual(first["model"], "coder-test")
        self.assertFalse(first["think"])
        self.assertEqual(first["options"]["temperature"], 0)
        self.assertEqual(first["options"]["num_ctx"], 8192)
        self.assertEqual(sorted(t["function"]["name"] for t in first["tools"]),
                         ["list_files", "read_file", "run_tests", "write_file"])
        self.assertIn("You may write only these files: src/demo.py", first["messages"][0]["content"])
        self.assertIn(self.brief["header"], first["messages"][0]["content"])
        # collect: the change stays for review, then --stage commits it and stages the item as the member
        out = members.collect(self.s, "pm-modules", self.wi)
        self.assertEqual(out["changed"], ["src/demo.py"])
        self.assertEqual(self.led.wi_status(self.wi)["status"], "dispatched")
        st = members.collect(self.s, "pm-modules", self.wi, stage=True, summary="g() added")
        self.assertEqual(self.led.wi_status(self.wi)["status"], "staged")
        staged = self.events(self.wi, "wi.staged")[-1]
        self.assertEqual((staged["actor"], staged["payload"]["reviewed_by"], staged["payload"]["head"]),
                         ("py-local", "pm-modules", st["head"]))
        log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=self.wt, capture_output=True, text=True).stdout
        self.assertIn(f"member run {r['run']}", log)

    def test_write_cap_and_scope_refusals(self):
        root = self.wt
        tools = local.Tools(self.s, root, ["src/demo.py"], 2, [], {}, 5)
        (root / "outside-link").symlink_to(self.site_root)
        self.assertIn("ERROR", tools.call("read_file", {"path": "/etc/hostname"}))
        self.assertIn("ERROR", tools.call("read_file", {"path": "outside-link/config.json"}))
        self.assertIn("ERROR", tools.call("read_file", {"path": ".git/HEAD"}))
        self.assertIn("ERROR", tools.call("list_files", {"path": ".."}))
        self.assertIn("ERROR: no tool", tools.call("shell", {"cmd": "ls"}))
        self.assertIn("wrote", tools.call("write_file", {"path": "src/demo.py", "content": "a"}))
        self.assertIn("wrote", tools.call("write_file", {"path": "./src/../src/demo.py", "content": "b"}))
        self.assertIn("write limit", tools.call("write_file", {"path": "src/demo.py", "content": "c"}))
        self.assertEqual((root / "src" / "demo.py").read_text(), "b")
        self.assertIn("ERROR", tools.call("write_file", {"path": "src/other.py", "content": "x"}))
        self.assertIn("the item declares no checks", tools.call("run_tests", {}))

    def test_wi_new_and_approve_refuse_a_local_item_without_outputs(self):
        """Release 65 (UR-20261015-03): the reason `member run` gives, at `wi new` and at `wi approve`."""
        for outputs in (None, ["README.md"], ["src/*.py"]):
            with self.assertRaisesRegex(DurehError, "py-local runs on the local runtime and the item declares no output"
                                                    " inside the role's write scopes.*--output <path>"):
                self.wf.wi_new(self.s, "desk", "t", "g", "py-local", "modules/demo", ["ok"], parent=self.bundle,
                               outputs=outputs)
        # a Claude worker is not affected
        self.wf.wi_new(self.s, "desk", "t", "g", "py-writer", "modules/demo", ["ok"], parent=self.bundle)
        wi = self.wf.wi_new(self.s, "desk", "t", "g", "py-local", "modules/demo", ["ok"], parent=self.bundle,
                            outputs=["src/demo.py"])["id"]
        self.led.event("wi.amended", "desk", wi, {"fields": {"outputs": []}, "before": {}, "memo": "old",
                                                  "by": "Director"})
        with self.assertRaisesRegex(DurehError, f"{wi}: py-local runs on the local runtime.*wi amend --wi {wi}"):
            self.wf.wi_approve(self.s, "desk", wi, "ok")
        self.assertEqual(self.events(wi, "wi.approved"), [])
        self.wf.wi_amend(self.s, "desk", wi, "add the output", {"outputs": ["src/demo.py"]})
        self.wf.wi_approve(self.s, "desk", wi, "ok")
        self.assertEqual(self.led.wi_status(wi)["status"], "approved")

    def test_malformed_calls_are_named_before_the_allowlist(self):
        """Release 65 (UR-20261015-01): a cut-off or malformed call is answered with what is missing, never with the
        allowlist refusal, and counted as `malformed`."""
        tools = local.Tools(self.s, self.wt, ["src/demo.py"], 2, [], {}, 5)
        out = tools.call("write_file", {"content": "def g():\n    return 2\n"})
        self.assertIn("write_file call incomplete: missing path", out)
        self.assertIn("probably cut by the token limit", out)
        self.assertIn("write smaller pieces", out)
        self.assertNotIn("allowlist", out)
        self.assertIn("missing content", tools.call("write_file", {"path": "src/demo.py"}))
        self.assertIn("missing path", tools.call("write_file", {"path": "", "content": "x"}))
        self.assertIn("not valid JSON", tools.call("write_file", '{"path": "src/demo.py", "content": "def g'))
        self.assertIn("missing path", tools.call("read_file", {}))
        self.assertIn("path must be a string", tools.call("read_file", {"path": 3}))
        cut = tools.call("write_file", {"content": "x"}, cut=True)
        self.assertIn("the output was cut by the token limit", cut)
        # a JSON string of complete arguments is accepted; a call without a path lists the work root (as before)
        self.assertIn("wrote", tools.call("write_file", json.dumps({"path": "src/demo.py", "content": "a"})))
        self.assertNotIn("ERROR", tools.call("list_files", {}))
        # a well-formed call off the allowlist is still a refusal
        self.assertIn("not on your write allowlist", tools.call("write_file", {"path": "src/other.py", "content": "x"}))
        self.assertEqual(tools.counts(), {"malformed": 7, "allowed": 2, "denied": 1})
        self.assertFalse((self.wt / "src" / "other.py").exists())

    def test_context_and_output_limits_are_recorded_and_told(self):
        """Release 65 (UR-20261015-02): the largest single request, the effective num_ctx/num_predict, context_full
        and output_cut notes; the model hears that its output was cut in the next tool result."""
        self.serve()
        self.config(members={"codex": {"sandbox_read": [], "wait_seconds": 30},
                             "local": {"quiet_seconds": 0.5, "wall_seconds": 20, "grace_windows": 1,
                                       "options": {"num_predict": 1400}}})
        self.srv.scripts = [
            [("tools", [call("write_file", content="def g(")]),
             ("final", {"prompt_eval_count": 3000, "done_reason": "length"})],
            [("tools", [call("read_file", path="src/demo.py")]), ("final", {"prompt_eval_count": 7900})],
            [("text", "partial summ"), ("final", {"prompt_eval_count": 8000, "done_reason": "length"})],
            [("final", {"content": "done.", "prompt_eval_count": 8100})]]
        r = self.submit()
        res = self.exec_(r["run"])
        self.assertEqual(res["outcome"], "completed", res)
        self.assertEqual(res["notes"], ["context_full", "output_cut"])
        ctx = res["context"]
        self.assertEqual((ctx["num_ctx"], ctx["num_predict"], ctx["max_prompt_eval_count"]), (8192, 1400, 8100))
        self.assertEqual(ctx["context_full_turns"], [{"attempt": 1, "turn": 2}, {"attempt": 1, "turn": 3},
                                                     {"attempt": 1, "turn": 4}])
        self.assertEqual(ctx["output_cut_turns"], [{"attempt": 1, "turn": 1}, {"attempt": 1, "turn": 3}])
        u = res["usage"]
        self.assertEqual((u["input_tokens"], u["max_prompt_eval_count"], u["num_ctx"], u["num_predict"]),
                         (27000, 8100, 8192, 1400))
        self.assertEqual(res["tool_results"], {"malformed": 1, "allowed": 1})
        self.assertEqual(self.srv.requests[0]["options"]["num_predict"], 1400, "config options over the profile")
        self.assertEqual(self.srv.requests[0]["options"]["num_ctx"], 8192)
        tool_msg = self.srv.requests[1]["messages"][-1]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertIn("it was cut", tool_msg["content"])
        self.assertIn("write_file call incomplete: missing path; the output was cut by the token limit",
                      tool_msg["content"])
        self.assertNotIn("it was cut", self.srv.requests[2]["messages"][-1]["content"])
        nudge = self.srv.requests[3]["messages"][-1]  # a cut answer without a tool call: told once, then on
        self.assertEqual(nudge["role"], "user")
        self.assertIn("it was cut", nudge["content"])
        tools_json = json.loads((members.run_dir(self.s, self.wi, r["run"]) / "dispatch" / "tools.json").read_text())
        self.assertEqual(tools_json["counts"], {"malformed": 1, "allowed": 1})
        self.assertEqual(tools_json["log"][0]["result"], "malformed")
        out = members.collect(self.s, "pm-modules", self.wi)
        self.assertEqual(out["notes"], ["context_full", "output_cut"])
        self.assertIn("notes context_full, output_cut", members.render_status(members.status(self.s, self.wi)))

    def test_stall_without_liveness_stops_and_is_not_run(self):
        self.serve()
        self.srv.loaded = []  # the model is not loaded: a quiet stream earns no grace
        self.srv.scripts = [[("sleep", 3), ("done", "late")]]
        r = self.submit()
        res = self.exec_(r["run"])
        self.assertEqual(res["outcome"], "not_run")
        self.assertIn("stalled: no output for 0.5 s and the model is not loaded", res["reason"])
        out = members.collect(self.s, "pm-modules", self.wi)
        self.assertIn("--fallback", out["next"])

    def test_grace_window_while_alive_then_stop(self):
        tools = local.Tools(self.s, self.wt, ["src/demo.py"], 2, [], {}, 5)
        self.srv.scripts = [[("sleep", 0.8), ("done", "ok")]]
        lim = {**members.DEFAULTS["local"], "quiet_seconds": 0.5, "wall_seconds": 10, "grace_windows": 1}
        r = local.converse(self.srv.base, "coder-test", "hi", tools, lim, {}, False)
        self.assertEqual((r["status"], r["output"]), ("ok", "ok"), "one grace window while the model is loaded")
        self.srv.scripts = [[("sleep", 2), ("done", "too late")]]
        r = local.converse(self.srv.base, "coder-test", "hi", tools, lim, {}, False)
        self.assertEqual(r["status"], "stalled")
        self.assertIn("after 1 grace windows", r["reason"])

    def test_repetition_and_wall_clock(self):
        tools = local.Tools(self.s, self.wt, ["src/demo.py"], 2, [], {}, 5)
        self.srv.scripts = [[("text", "ab" * 10)] * 40 + [("done", "")]]
        lim = {**members.DEFAULTS["local"], "quiet_seconds": 1, "wall_seconds": 10}
        r = local.converse(self.srv.base, "coder-test", "hi", tools, lim, {}, False)
        self.assertEqual(r["status"], "stalled")
        self.assertIn("repetition", r["reason"])
        self.assertTrue(local.repetition("xyz" * 300))
        self.assertFalse(local.repetition("".join(chr(65 + i % 26) + str(i) for i in range(400))))
        self.srv.scripts = [[("text", "a"), ("sleep", 0.4), ("text", "b"), ("sleep", 0.4), ("text", "c"),
                             ("sleep", 0.4), ("done", "")]]
        r = local.converse(self.srv.base, "coder-test", "hi", tools, {**lim, "wall_seconds": 0.7}, {}, False)
        self.assertEqual(r["status"], "stalled")
        self.assertIn("wall clock", r["reason"])

    def test_one_retry_on_a_harness_error(self):
        self.serve()
        self.srv.scripts = ["drop", [("done", "done after a retry")]]
        r = self.submit()
        res = self.exec_(r["run"])
        self.assertEqual(res["outcome"], "completed", res)
        self.assertEqual([a["status"] for a in res["attempts"]], ["harness_error", "ok"])
        self.srv.scripts = ["drop", "drop"]
        members.collect(self.s, "pm-modules", self.wi, discard=True, reason="retry test")
        self.wf.dispatch_brief(self.s, self.wi, "pm-modules")
        r = self.submit()
        res = self.exec_(r["run"])
        self.assertEqual(res["outcome"], "not_run")
        self.assertEqual(len(res["attempts"]), 2)

    def test_changed_profile_is_not_run(self):
        self.serve(sha="0" * 64)
        r = self.submit()
        res = self.exec_(r["run"])
        self.assertEqual(res["outcome"], "not_run")
        self.assertIn("changed since office hours started", res["reason"])

    def test_discard_restores_the_files(self):
        self.serve()
        self.srv.scripts = [[("tools", [call("write_file", path="src/demo.py", content="broken")]), ("done", "")],
                            [("done", "wrote it")]]
        r = self.submit()
        self.exec_(r["run"])
        with self.assertRaisesRegex(DurehError, "--discard needs --reason"):
            members.collect(self.s, "pm-modules", self.wi, discard=True)
        out = members.collect(self.s, "pm-modules", self.wi, discard=True, reason="wrong approach")
        self.assertEqual(out["discarded"], ["src/demo.py"])
        self.assertEqual((self.wt / "src" / "demo.py").read_text(), "def f():\n    return 1\n")
        self.assertEqual(self.led.wi_status(self.wi)["status"], "dispatched")
        with self.assertRaisesRegex(DurehError, "was discarded"):
            members.collect(self.s, "pm-modules", self.wi, stage=True, summary="x")
        b = self.wf.dispatch_brief(self.s, self.wi, "pm-modules")  # superseding is allowed: the member finished
        self.assertTrue(b["brief"])

    def noop_run(self):
        """A member run that writes nothing (the model answers at once), collected."""
        self.srv.scripts = [[("done", "nothing to change: the failure is not in my files")]]
        r = self.submit()
        self.exec_(r["run"])
        return r

    def test_stage_unchanged_restages_the_same_head_for_re_verification(self):
        """Release 66 (UR-20261015-08): a local rework that correctly changed nothing re-stages the same head."""
        self.serve()
        self.noop_run()
        commit_work(self.s, self.wi)
        first = self.wf.wi_stage(self.s, "py-local", self.wi, "first staging")  # the head staged before
        self.led.event("rework.requested", "checker", self.wi, {"run": "run-x", "reason": "codex FAIL"})
        self.wf.dispatch_brief(self.s, self.wi, "pm-modules")
        r = self.noop_run()
        self.assertEqual(self.led.wi_status(self.wi)["status"], "dispatched")
        with self.assertRaisesRegex(DurehError, "--unchanged goes with --stage"):
            members.collect(self.s, "pm-modules", self.wi, unchanged=True, reason="x")
        with self.assertRaisesRegex(DurehError, "needs --reason"):
            members.collect(self.s, "pm-modules", self.wi, stage=True, unchanged=True)
        with self.assertRaisesRegex(DurehError, "the member changed nothing"):
            members.collect(self.s, "pm-modules", self.wi, stage=True, summary="x")
        (self.wt / "src" / "demo.py").write_text("dirty\n")
        with self.assertRaisesRegex(DurehError, "the worktree has changes \\(src/demo.py\\)"):
            members.collect(self.s, "pm-modules", self.wi, stage=True, unchanged=True, reason="nothing to fix")
        subprocess.run(["git", "checkout", "--", "src/demo.py"], cwd=self.wt, check=True)
        out = members.collect(self.s, "pm-modules", self.wi, stage=True, unchanged=True,
                              reason="the FAIL was an evidence gap; the code is right")
        self.assertEqual(self.led.wi_status(self.wi)["status"], "staged")
        st = self.events(self.wi, "wi.staged")[-1]
        self.assertEqual((st["actor"], st["payload"]["head"], st["payload"]["unchanged"], st["payload"]["restaged_from"],
                          st["payload"]["member_run"], st["payload"]["reviewed_by"]),
                         ("py-local", first["payload"]["head"], True, first["id"], r["run"], "pm-modules"))
        self.assertIn("evidence gap", st["payload"]["reason"])
        self.assertEqual(out["head"], first["payload"]["head"])
        log = subprocess.run(["git", "log", "--format=%s"], cwd=self.wt, capture_output=True, text=True).stdout
        self.assertNotIn("member run", log, "nothing was committed")

    def test_stage_unchanged_refuses_a_head_never_staged(self):
        self.serve()
        self.noop_run()
        with self.assertRaisesRegex(DurehError, "was never staged before"):
            members.collect(self.s, "pm-modules", self.wi, stage=True, unchanged=True, reason="no-op")
        self.assertEqual(self.led.wi_status(self.wi)["status"], "dispatched")

    def test_a_pending_member_run_holds_the_item(self):
        self.serve()
        r = self.submit()
        from dureh_core import ops

        self.assertEqual(ops.agent_state(self.led, self.wi), "starting")
        with self.assertRaisesRegex(DurehError, f"member run {r['run']}"):
            self.wf.dispatch_brief(self.s, self.wi, "pm-modules")
        holders = ops.lane_holders(self.s, self.led, probe=False)
        self.assertTrue(any(f"job {JOB_ID}" in " ".join(h["by"]) for h in holders), holders)


class OfficeHours(MemberCase):
    def test_plan_builds_the_gpu_job_script(self):
        b = office_hours.plan(self.s)
        t = b["text"]
        for line in ("#SBATCH --partition=gpu", "#SBATCH --gres=gpu:1", "#SBATCH --mem=32G", "#SBATCH --cpus-per-task=8",
                     f"#SBATCH --job-name={self.s.session_name}-office-hours", "BIN=srv", "PORT=11999",
                     'export OLLAMA_HOST="$HOST:$PORT"', '"$BIN" serve', 'trap cleanup EXIT',
                     '"$BIN" show base-model >/dev/null 2>&1 || "$BIN" pull base-model',
                     f'"$BIN" create coder-test -f {self.s.startup / "profiles" / "coder.Modelfile"}', 'mv "$INFO.tmp" "$INFO"',
                     'wait "$SERVER"'):
            self.assertIn(line, t)
        self.assertIn(f"export OLLAMA_MODELS={self.site_root / 'cache' / 'models'}", t)
        self.assertIn('rm -f "$INFO"', t)
        self.assertRegex(t, r"#SBATCH --time=\d\d:\d\d:\d\d")
        bash = subprocess.run(["bash", "-n"], input=t, capture_output=True, text=True)
        self.assertEqual(bash.returncode, 0, bash.stderr)

    def test_wall_time_follows_the_workday_and_caps(self):
        hours, notes = office_hours.wall_hours(self.s)
        self.assertEqual(hours, 8)  # no running day: the working day (release 74: no overtime allowance)
        self.config(office_hours={**self.s.config["office_hours"], "time": "04:00:00"})
        hours, notes = office_hours.wall_hours(self.s)
        self.assertEqual(hours, 4)
        self.assertIn("capped by office_hours.time 04:00:00", notes)
        self.config(cluster={"scheduler": "slurm", "partitions": {"gpu": {"time_limit": "2:00:00"}}})
        self.assertEqual(office_hours.wall_hours(self.s)[0], 2)

    def test_wall_time_ends_with_the_day(self):
        """Release 74 (#8, #33): office hours never outlive the day: the wall time ends at today's session clock end
        (workday mode off here), and past the end they do not start."""
        from dureh_core import workday

        os.environ["DUREH_FAKE_NOW"] = "2026-10-19T11:35:00-04:00"
        self.addCleanup(os.environ.pop, "DUREH_FAKE_NOW", None)
        self.led.event("session.started", "desk", None, {"job_id": "777", "purpose": "desk",
                                                         "submitted_at": "2026-10-19T07:39:00-04:00"})
        hours, notes = office_hours.wall_hours(self.s)
        self.assertAlmostEqual(hours, 4 + 4 / 60, places=3)  # to 15:39 local
        self.assertIn("ends with the day at 2026-10-19T15:39:00-04:00 (2026-10-19T19:39:00Z)", notes)
        self.assertIn("#SBATCH --time=04:04:00", office_hours.plan(self.s)["text"])
        saved = {k: os.environ.pop(k) for k in ("DUREH_ACTOR", "CLAUDECODE") if k in os.environ}
        try:  # the Director's switch, from outside an agent session
            workday.set_device(self.s, "hours", False, "밤샘")  # no clock: the overtime allowance again
            self.assertGreater(office_hours.wall_hours(self.s)[0], 8)
            workday.set_device(self.s, "hours", True, "x")
        finally:
            os.environ.update(saved)
        os.environ["DUREH_FAKE_NOW"] = "2026-10-19T15:40:00-04:00"
        with self.assertRaisesRegex(DurehError, "office hours end with the day"):
            office_hours.wall_hours(self.s)
        self.config(office_hours={**self.s.config["office_hours"], "enabled": True})
        self.assertIn("office hours end with the day", office_hours.start_quietly(self.s, "desk")["error"])

    def test_start_status_stop_with_a_fake_scheduler(self):
        rec = office_hours.start(self.s, "desk")
        self.assertEqual(rec["payload"]["job_id"], JOB_ID)
        self.assertEqual(rec["type"], "office-hours.started")
        args = (self.fake / "sbatch.args").read_text().split()
        self.assertTrue(args[-1].endswith(".sbatch"))
        (self.fake / "squeue.out").write_text("RUNNING|7:00:00\n")
        with self.assertRaisesRegex(DurehError, "office hours already run"):
            office_hours.start(self.s, "desk")
        with self.assertRaisesRegex(DurehError, "only the desk"):
            office_hours.start(self.s, "pm-modules")
        st = office_hours.status(self.s)
        self.assertTrue(st["job"]["queued"])
        self.assertIsNone(st["server"])
        self.assertIn("server: none", office_hours.render_status(st))
        ev = office_hours.stop(self.s, "desk", "test")
        self.assertEqual((self.fake / "scancel.args").read_text().split(), [JOB_ID])
        self.assertEqual(ev["refs"], [rec["id"]])
        self.assertIsNone(office_hours.stop(self.s, "desk", "again"))
        with self.assertRaisesRegex(DurehError, "no office-hours job to stop"):
            office_hours.stop(self.s, "desk", "again", must_run=True)

    def test_session_start_starts_it_and_release_stops_it(self):
        self.config(office_hours={**self.s.config["office_hours"], "enabled": True},
                    session={"slurm": {"partition": "general"}})
        rec = sessions.start(self.s, "desk", apply_landed=False)
        self.assertEqual(rec["office_hours"]["job_id"], JOB_ID)
        self.assertEqual(len((self.fake / "sbatch.args").read_text().splitlines()), 2, "session job + office hours")
        (self.fake / "squeue.out").write_text("RUNNING|7:00:00\n")
        out = sessions.release(self.s, "desk")
        self.assertTrue(out["office_hours"]["event"])
        stops = self.events(None, office_hours.STOPPED)
        self.assertEqual(stops[-1]["payload"]["reason"], "session released")

    def test_config_errors(self):
        self.config(office_hours={"profiles": [{"name": "a", "file": "profiles/missing"}, {"name": "a", "file": "x"},
                                               {"file": "y"}],
                                  "server": {"kind": "other", "port": 0}, "bogus": 1},
                    members={"codex": {"wait_seconds": -1, "nope": 1}, "other": {}})
        from dureh_core import ops

        errs = "\n".join(ops.config_errors(self.s))
        for words in ("office_hours.bogus: unknown key", "office_hours.server.kind must be one of ollama",
                      "server.port must be a port number", "profiles/missing is not in the installed package",
                      "profiles[1].name must be a distinct model name", "profiles[2] must be {name",
                      "members.other: unknown key", "members.codex.nope: unknown key",
                      "members.codex.wait_seconds must be a non-negative number"):
            self.assertIn(words, errs)


if __name__ == "__main__":
    unittest.main()
