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
        wi = self.item(outputs=["README.md"])  # outside the role's write scopes
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
        self.assertGreater(hours, 8)  # no running day: the working day plus the overtime allowance
        self.config(office_hours={**self.s.config["office_hours"], "time": "04:00:00"})
        hours, notes = office_hours.wall_hours(self.s)
        self.assertEqual(hours, 4)
        self.assertIn("capped by office_hours.time 04:00:00", notes)
        self.config(cluster={"scheduler": "slurm", "partitions": {"gpu": {"time_limit": "2:00:00"}}})
        self.assertEqual(office_hours.wall_hours(self.s)[0], 2)

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
