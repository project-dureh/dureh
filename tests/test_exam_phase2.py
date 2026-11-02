"""Model qualification exams, phase 2 (proposal model-qualification-exams sections 2-5): exam briefs through the spawn
and tool hooks (bound to the exam run, exact model, writes only in the task workspace, no dureh, no commits, usage per
task), one rerun of a failing deterministic task, the harness/package example-task fallback and `not examinable`, and
the `newer models` handshake line. Neutral example package; agents are simulated through the hook payloads."""
import json
import unittest
from pathlib import Path

from dureh_core import handshake as hs
from dureh_core import qualification as q
from dureh_core.records import Ledger
from dureh_core.util import DurehError
from tests.helpers import denied, hook
from tests.test_qualification import HAIKU, SONNET, QualCase


class ExamCase(QualCase):
    def setUp(self):
        super().setUp()
        self.session = self.tmp / "projects" / "sess-1.jsonl"
        self.session.parent.mkdir(parents=True)
        self.session.write_text("")

    def spawn(self, brief_text, model="claude-sonnet-5", target="builder"):
        ti = {"subagent_type": target, "prompt": brief_text, "description": "exam"}
        if model is not None:
            ti["model"] = model
        return hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root), tool_input=ti)

    def transcript(self, agent_id, text, tokens=0):
        p = self.session.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"type": "user", "message": {"role": "user", "content": text}})]
        if tokens:
            lines.append(json.dumps({"type": "assistant", "message": {"id": f"m-{agent_id}", "usage": {
                "input_tokens": tokens, "output_tokens": tokens}}}))
        p.write_text("\n".join(lines) + "\n")
        return p

    def agent(self, agent_id, tool, role="builder", **ti):
        return hook(self.s, hook_event_name="PreToolUse", tool_name=tool, agent_type=role, agent_id=agent_id,
                    transcript_path=str(self.session), cwd=str(self.site_root), tool_input=ti)

    def stop(self, agent_id, role="builder"):
        hook(self.s, hook_event_name="SubagentStop", agent_type=role, agent_id=agent_id,
             transcript_path=str(self.session),
             agent_transcript_path=str(self.session.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"))

    def good(self, ws):
        ws = Path(ws)
        (ws / "src" / "g7.py").write_text("def g7():\n    return 7\n")
        (ws / "tests" / "test_g7.py").write_text(
            "import sys, os, unittest\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
            "import g7\n\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertEqual(g7.g7(), 7)\n")

    def bad(self, ws):
        (Path(ws) / "tests" / "test_g7.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n"
                                                       "    def test(self):\n        self.fail('no')\n")

    def events(self, type_):
        return [e for e in Ledger(self.s).events() if e["type"] == type_]


class SpawnHook(ExamCase):
    def setUp(self):
        super().setUp()
        for n in (1, 2, 3):
            self.promoted(n, rework=n == 1)
        q.build(self.s, "desk", "worker", 3)
        self.run_ = q.run(self.s, "desk", "worker", SONNET)
        self.t0 = self.run_["tasks"][0]
        self.text = Path(self.t0["brief"]).read_text()

    def test_exam_brief_spawns_once_with_the_exact_model(self):
        self.assertTrue(denied(self.spawn(self.text, model=None)), "the model is required")
        self.assertTrue(denied(self.spawn(self.text, model="sonnet")), "a family alias is not the exact model")
        self.assertTrue(denied(self.spawn(self.text, model="claude-haiku-4-5-20251001")))
        self.assertTrue(denied(self.spawn(self.text + "\nAlso push.\n")), "an edited brief is refused")
        self.assertTrue(denied(self.spawn(self.text, target="scout")), "the task's role only")
        self.assertIsNone(self.spawn(self.text))
        sp = self.events("exam.spawned")
        self.assertEqual(len(sp), 1)
        self.assertEqual((sp[0]["payload"]["task"], sp[0]["payload"]["attempt"], sp[0]["payload"]["model"]),
                         (self.t0["task"], 1, SONNET))
        self.assertTrue(denied(self.spawn(self.text)), "a task is spawned once")
        attempt2 = self.text.replace(f"model={SONNET}", f"model={SONNET} attempt=2", 1)
        self.assertTrue(denied(self.spawn(attempt2)), "a rerun exists only after exam score sends one")

    def test_desk_may_spawn_exam_role_it_does_not_dispatch(self):
        # builder is the coordinator's worker, not the desk's: an exam brief is the desk's own work
        self.assertIsNone(self.spawn(self.text))

    def test_bound_agent_writes_only_in_its_workspace(self):
        self.assertIsNone(self.spawn(self.text))
        self.transcript("ex-1", self.text)
        ws = Path(self.t0["workspace"])
        self.assertIsNone(self.agent("ex-1", "Write", file_path=str(ws / "src" / "g7.py"), content="x"))
        b = self.events("exam.agent.started")
        self.assertEqual((b[0]["payload"]["exam_run"], b[0]["payload"]["task"]), (self.run_["run"], self.t0["task"]))
        self.assertIsNone(self.agent("ex-1", "Write", file_path=self.t0["verdict"], content="{}"))
        for path in (self.mod / "src" / "demo.py", ws / ".git", self.s.state / "x.json",
                     Path(self.run_["tasks"][1]["workspace"]) / "src" / "g7.py"):
            self.assertTrue(denied(self.agent("ex-1", "Write", file_path=str(path), content="x")), path)
        self.assertTrue(denied(self.agent("ex-1", "Bash", command=f"{self.site_root}/bin/dureh status")))
        self.assertTrue(denied(self.agent("ex-1", "Bash", command=f"git -C {ws} commit -am x")))
        self.assertTrue(denied(self.agent("ex-1", "Bash", command=f"git -C {ws} push origin HEAD")))
        self.assertTrue(denied(self.agent("ex-1", "Bash", command=f"touch {self.mod}/x")))
        self.assertTrue(denied(self.agent("ex-1", "Agent", subagent_type="builder", prompt=self.text)))
        self.assertFalse(denied(self.agent("ex-1", "Bash", command=f"git -C {ws} diff")))
        self.assertFalse(denied(self.agent("ex-1", "Bash", command=f"touch {ws}/src/x.py")))
        self.assertFalse(any(e["type"] == "agent.started" and e["wi"] for e in Ledger(self.s).events()),
                         "an exam agent is never bound to a work item")

    def test_unrecorded_exam_agent_is_refused(self):
        self.transcript("ex-x", self.text)  # its spawn never went through the hook
        out = self.agent("ex-x", "Write", file_path=str(Path(self.t0["workspace"]) / "a.py"), content="x")
        self.assertTrue(denied(out))
        self.assertIn("no unbound recorded spawn", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_usage_is_recorded_per_task_and_scored(self):
        for i, t in enumerate(self.run_["tasks"]):
            text = Path(t["brief"]).read_text()
            self.assertIsNone(self.spawn(text))
            self.transcript(f"ex-{i}", text, tokens=1000 * (i + 1))
            self.good(t["workspace"])
            self.stop(f"ex-{i}")
        runs = [r for r in Ledger(self.s).load("run") if r["payload"].get("run_type") == "agent"]
        self.assertEqual({r["payload"]["exam_task"] for r in runs}, {t["task"] for t in self.run_["tasks"]})
        self.assertTrue(all(r["wi"] is None and r["payload"]["exam_run"] == self.run_["run"] for r in runs))
        res = q.score(self.s, "desk", self.run_["run"], local=True)
        self.assertEqual(res["status"], "pass", res)
        self.assertIsInstance(res["units"], float)
        self.assertIsInstance(res["units_per_task"], float)
        per = [x["units"] for x in res["tasks"]]
        self.assertTrue(all(isinstance(u, float) for u in per))
        self.assertLess(per[0], per[2])

    def test_units_unmeasured_names_the_reason(self):
        for t in self.run_["tasks"]:
            self.good(t["workspace"])
        res = q.score(self.s, "desk", self.run_["run"], local=True)
        self.assertEqual(res["units"], "unmeasured")
        self.assertIn("no agent run", res["units_why"])


class Rerun(ExamCase):
    def setUp(self):
        super().setUp()
        for n in (1, 2, 3):
            self.promoted(n, rework=n == 1)
        q.build(self.s, "desk", "worker", 3)
        self.run_ = q.run(self.s, "desk", "worker", SONNET)

    def test_failing_task_is_rerun_once_then_scored_with_both_attempts(self):
        t = self.run_["tasks"]
        self.good(t[0]["workspace"])
        self.good(t[1]["workspace"])
        self.bad(t[2]["workspace"])
        first = q.score(self.s, "desk", self.run_["run"], local=True)
        self.assertEqual(first["status"], "rerun")
        self.assertEqual(first["first_score"], "2/3")
        self.assertEqual([x["task"] for x in first["tasks"]], [t[2]["task"]])
        ws = Path(t[2]["workspace"])
        self.assertFalse((ws / "tests" / "test_g7.py").exists(), "the rerun starts from the frozen commit")
        text = Path(first["tasks"][0]["brief"]).read_text()
        self.assertIn("attempt=2", text.splitlines()[0])
        self.assertIsNone(self.spawn(text))
        self.assertTrue(denied(self.spawn(Path(t[0]["brief"]).read_text().replace(
            f"model={SONNET}", f"model={SONNET} attempt=2", 1))), "only the failing task is rerun")
        self.good(ws)
        res = q.score(self.s, "desk", self.run_["run"], local=True)
        self.assertEqual(res["status"], "pass", res)
        self.assertEqual(res["reruns"], [t[2]["task"]])
        row = res["tasks"][2]
        self.assertTrue(row["rerun"])
        self.assertEqual((row["result"], row["first"]["result"]), ("pass", "fail"))
        self.assertFalse(res["tasks"][0]["rerun"])

    def test_hard_failure_is_not_rerun_and_no_rerun_scores_first_attempts(self):
        t = self.run_["tasks"]
        for x in t:
            self.good(x["workspace"])
        (Path(t[0]["workspace"]) / "NOTES.md").write_text("outside the write scopes\n")
        self.bad(t[1]["workspace"])
        first = q.score(self.s, "desk", self.run_["run"], local=True)
        self.assertEqual(first["status"], "rerun")
        self.assertEqual([x["task"] for x in first["tasks"]], [t[1]["task"]], "a hard failure is final")
        res = q.score(self.s, "desk", self.run_["run"], local=True, rerun=False)
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["score"], "1/3")
        self.assertIn("rerun not scored", res["tasks"][1]["why"])


EVAL_TASK = """id: {id}
role: builder
split: example
prompt: |
  Write the integer {n} as JSON {{"n": {n}}} to answer.json. Do not edit input.txt.
required_outputs:
  - answer.json
forbidden_writes:
  - input.txt
checks:
  - python3 -c "import json; raise SystemExit(0 if json.load(open('answer.json')) == {{'n':{n}}} else 1)"
"""


class FixtureFallback(ExamCase):
    def install_with_evals(self, n_tasks):
        import shutil

        shutil.rmtree(self.site_root)
        m = self.package / "startup.yaml"
        m.write_text(m.read_text() + "lessons:\n  evals: evals\n")
        for i in range(1, n_tasks + 1):
            d = self.package / "evals" / "builder" / f"ex-00{i}"
            (d / "fixture").mkdir(parents=True)
            (d / "fixture" / "input.txt").write_text("x\n")
            (d / "task.yaml").write_text(EVAL_TASK.format(id=f"ex-00{i}", n=i))
        self.install()
        self.s = self.site()

    def test_too_few_items_fall_back_to_package_example_tasks(self):
        self.install_with_evals(3)
        self.assertTrue(q.examinable(self.s, "worker")[0])
        out = q.build(self.s, "desk", "worker", 3)
        self.assertEqual(out["source"], "fixture")
        run = q.run(self.s, "desk", "worker", HAIKU)
        self.assertTrue(all(t["kind"] == "fixture" for t in run["tasks"]))
        for i, t in enumerate(run["tasks"], 1):
            ws = Path(t["workspace"])
            self.assertTrue((ws / "input.txt").is_file())
            (ws / "answer.json").write_text(json.dumps({"n": i}))
        res = q.score(self.s, "desk", run["run"], local=True)
        self.assertEqual(res["status"], "pass", res)
        self.assertEqual(res["exam_set"]["source"], "fixture")
        self.assertIn("fixture set", res["config_entry"]["note"])
        self.assertFalse(any(Path(t["workspace"]).exists() for t in run["tasks"]))

    def test_forbidden_write_in_example_task_is_hard(self):
        self.install_with_evals(3)
        q.build(self.s, "desk", "worker", 3)
        run = q.run(self.s, "desk", "worker", HAIKU)
        for i, t in enumerate(run["tasks"], 1):
            (Path(t["workspace"]) / "answer.json").write_text(json.dumps({"n": i}))
        (Path(run["tasks"][0]["workspace"]) / "input.txt").write_text("changed\n")
        res = q.score(self.s, "desk", run["run"], local=True)
        self.assertEqual(res["status"], "fail")
        self.assertTrue(res["tasks"][0]["hard_failure"])

    def test_not_examinable_with_too_few_example_tasks(self):
        self.install_with_evals(2)
        ok, why = q.examinable(self.s, "worker")
        self.assertFalse(ok)
        self.assertIn("2 harness/package example task(s)", why)
        with self.assertRaisesRegex(DurehError, "tier worker is not examinable"):
            q.build(self.s, "desk", "worker", 3)
        row = q.plan(self.s, "worker")["tiers"]["worker"]
        self.assertFalse(row["examinable"])
        self.assertTrue(all(c["state"] == "not examinable" and not c["needs_run"] for c in row["candidates"]))
        self.assertIn("not examinable", q.render_plan(q.plan(self.s, "worker")))


class NewerModel(ExamCase):
    def test_model_version(self):
        self.assertEqual(q.model_version("anthropic:claude-opus-5-5"), ("anthropic", "opus", (5, 5)))
        self.assertEqual(q.model_version("anthropic:claude-haiku-4-5-20251001"), ("anthropic", "haiku", (4, 5)))
        self.assertEqual(q.model_version("claude-sonnet-5"), ("", "sonnet", (5,)))
        self.assertIsNone(q.model_version("anthropic:local"))

    def test_handshake_names_a_newer_version_of_the_tier_family(self):
        line = lambda: [c for c in hs.handshake(self.s)["checks"] if c["check"] == "newer models"]  # noqa: E731
        self.assertEqual([c["level"] for c in line()], [hs.SKIP])
        self.set_models(candidates=[HAIKU, SONNET, "anthropic:claude-opus-5-5"])
        self.assertEqual([c["level"] for c in line()], [hs.OK])
        self.set_models(candidates=[HAIKU, "anthropic:claude-sonnet-5-5", "anthropic:claude-sonnet-4-9",
                                    "anthropic:claude-opus-5-5"])
        rows = line()
        self.assertEqual([c["level"] for c in rows], [hs.WARN])
        self.assertIn("newer model available for tier worker: anthropic:claude-sonnet-5-5"
                      f" (incumbent {SONNET})", rows[0]["text"])
        self.assertEqual([r["tier"] for r in q.newer_models(self.s)], ["worker"])


if __name__ == "__main__":
    unittest.main()
