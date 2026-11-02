"""Per-tier model qualification exams with efficiency selection (release 90 candidate; Director 2026-11-02,
proposal model-qualification-exams sections 2-7a): the strict gate, the fallback path, `exam build|run|score`,
`exam compare|plan`. Neutral example package; the candidate agents are simulated by writing into the exam workspaces."""
import contextlib
import io
import json
import subprocess
import unittest
from datetime import date, timedelta
from pathlib import Path

from dureh_core import handshake as hs
from dureh_core import qualification as q
from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.records import Ledger
from dureh_core.util import DurehError
from tests.helpers import ExampleSiteCase

HAIKU, SONNET, OPUS = q.default_candidates()
CHECK = "python3 -m unittest discover -s tests"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class QualCase(ExampleSiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        self.mod = self.make_module()
        self.led = Ledger(self.s)
        self.bundle = wf.wi_new(self.s, "desk", "Demo work", "Improve demo", "coordinator", "modules/demo",
                                ["items verified"])["id"]

    def set_models(self, **entries):
        cfg = json.loads(self.s.config_path.read_text())
        cfg["models"].update(entries)
        self.s.config_path.write_text(json.dumps(cfg, indent=2))
        self.s = self.site()
        self.led = Ledger(self.s)

    def qual_record(self, tier, model, status="pass", passed=4, total=4, units="unmeasured", expires=None,
                    exam_set="set-x", sha="abc"):
        p = {"run_type": "qualification", "status": status, "tier": tier, "model": model, "score": f"{passed}/{total}",
             "passed": passed, "total": total, "units_per_task": units, "exam_set": {"id": exam_set, "sha256": sha}}
        if status == "pass":
            p["expires"] = expires or (date.today() + timedelta(days=90)).isoformat()
        return self.led.append("run", "qualification", "desk", None, p)["id"]

    def promoted(self, n, rework=False):
        """A promoted builder WI whose recorded base is main's HEAD (its own change is not on main)."""
        base = git(self.mod, "rev-parse", "HEAD")
        w = wf.wi_new(self.s, "desk", f"Add g{n}", f"Add g{n}() returning {n}", "builder", "modules/demo",
                      [f"g{n}() == {n}"], checks=[CHECK], parent=self.bundle)["id"]
        wf.wi_approve(self.s, "desk", w, "ok", by="Director")
        self.led.event("dispatch.prepared", "desk", w, {"role": "builder", "brief": "0" * 16, "kind": "build",
                                                        "base": base, "checking": False, "by": "desk"})
        self.led.event("wi.dispatched", "desk", w, {"role": "builder", "brief": "0" * 16, "by": "desk"})
        self.led.event("wi.staged", "desk", w, {"summary": "s"})
        if rework:
            r = self.led.append("run", "verify", "checker", w, {"run_type": "verify", "status": "fail", "head": base,
                                                                "changed": ["src/demo.py"]})
            self.led.event("rework.requested", "desk", w, {"run": r["id"], "reason": "1/1 failed"})
            self.led.event("wi.dispatched", "desk", w, {"role": "builder", "brief": "1" * 16, "by": "desk"})
            self.led.event("wi.staged", "desk", w, {"summary": "s"})
        r = self.led.append("run", "verify", "checker", w, {"run_type": "verify", "status": "pass", "head": base,
                                                            "changed": ["src/demo.py"]})
        self.led.event("wi.verified", "desk", w, {"run": r["id"]})
        self.led.event("wi.promoted", "desk", w, {})
        return w


class Gate(QualCase):
    def test_typed_run_id_without_record_is_refused(self):
        ok, why = wf._qualified({"model": SONNET, "qualification": "run-x"}, self.s, "worker")
        self.assertFalse(ok)
        self.assertIn("names no run record", why)
        ok, why = wf._qualified({"model": SONNET, "qualification": "run-x"})
        self.assertFalse(ok, "without the site a run id is never taken on trust")

    def test_record_must_match_tier_model_status_and_date(self):
        good = self.qual_record("worker", SONNET)
        self.assertTrue(wf._qualified({"model": SONNET, "qualification": good}, self.s, "worker")[0])
        ok, why = wf._qualified({"model": OPUS, "qualification": good}, self.s, "worker")
        self.assertFalse(ok)
        self.assertIn("a model change needs a new exam", why)
        ok, why = wf._qualified({"model": SONNET, "qualification": good}, self.s, "pm")
        self.assertIn("is for tier worker", why)
        failed = self.qual_record("worker", SONNET, status="fail", passed=2)
        self.assertIn("did not pass", wf._qualified({"model": SONNET, "qualification": failed}, self.s, "worker")[1])
        old = self.qual_record("worker", SONNET, expires="2026-01-01")
        self.assertIn("expired on 2026-01-01", wf._qualified({"model": SONNET, "qualification": old}, self.s, "worker")[1])

    def test_waiver_path_unchanged(self):
        e = {"model": SONNET, "qualification": "waived-bootstrap", "waiver_expires": "2099-01-01"}
        self.assertTrue(wf._qualified(e)[0])

    def test_dispatch_refuses_without_fallback_and_falls_back_with_it(self):
        w = wf.wi_new(self.s, "desk", "Look up h", "A sourced summary of h", "scout", "site", ["summary"])["id"]
        wf.wi_approve(self.s, "desk", w, "ok", by="Director")
        self.set_models(worker={"model": SONNET, "qualification": "run-x"})
        with self.assertRaisesRegex(DurehError, "tier worker model .* cannot be dispatched: qualification run-x names no"):
            wf.dispatch_brief(self.s, w, "desk")
        cfg = json.loads(self.s.config_path.read_text())
        self.set_models(fallback_tiers=["recount", "worker", "pm"],
                        pm={**cfg["models"]["pm"], "waiver_expires": "2099-01-01"})
        r = wf.dispatch_brief(self.s, w, "desk")
        self.assertEqual(r["fallback_tier"]["to"], "pm")
        self.assertEqual(r["model"], cfg["models"]["pm"]["model"])
        prep = [e for e in Ledger(self.s).events(w) if e["type"] == "dispatch.prepared"][-1]["payload"]
        self.assertEqual(prep["fallback_tier"]["from"], "worker")
        res = hs.handshake(self.s)
        line = [c for c in res["checks"] if c["check"] == "dispatch worker"][0]
        self.assertEqual(line["level"], hs.WARN)
        self.assertIn("falls back to tier pm", line["text"])

    def test_config_errors_for_new_keys(self):
        from dureh_core.ops import config_errors

        self.set_models(fallback_tiers=["worker", "nope"], candidates="x")
        errs = " ".join(config_errors(self.s))
        self.assertIn("models.fallback_tiers: nope names no tier", errs)
        self.assertIn("models.candidates must be a nonempty list", errs)
        self.assertNotIn("candidates", self.s.model_tiers())

    def test_status_and_handshake_warn_before_expiry(self):
        soon = self.qual_record("worker", SONNET, expires=(date.today() + timedelta(days=3)).isoformat())
        cfg = json.loads(self.s.config_path.read_text())
        self.set_models(worker={**cfg["models"]["worker"], "qualification": soon})
        w = q.warnings(self.s)
        self.assertEqual(len(w), 1)
        self.assertIn("ends within 7 days: worker", w[0])
        res = hs.handshake(self.s)
        self.assertEqual([c["level"] for c in res["checks"] if c["check"] == "qualifications"], [hs.WARN])
        self.assertEqual([c["level"] for c in res["checks"] if c["check"] == "dispatch worker"], [hs.OK])


class BuildRunScore(QualCase):
    def setUp(self):
        super().setUp()
        self.wis = [self.promoted(1, rework=True), self.promoted(2), self.promoted(3)]

    def answer(self, run, good=True, outside=False):
        for t in run["tasks"]:
            ws = Path(t["workspace"])
            n = 7  # any new function with its own test
            (ws / "src" / f"g{n}.py").write_text(f"def g{n}():\n    return {n if good else 0}\n")
            (ws / "tests" / f"test_g{n}.py").write_text(
                f"import sys, os, unittest\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
                f"import g{n}\n\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertEqual(g{n}.g{n}(), {n})\n")
            if outside:
                (ws / "NOTES.md").write_text("not in the write scopes\n")

    def test_build_freezes_tasks_with_the_rework_one(self):
        out = q.build(self.s, "desk", "worker", 3)
        self.assertEqual(len(out["picked"]), 3)
        self.assertEqual(out["picked"][0]["wi"], self.wis[0], "the item that needed rework is always included")
        meta = q.load_set(self.s, "worker")
        self.assertTrue(all(t["split"] == "holdout" and t["expected"] == "pass" for t in meta["task_specs"]))
        self.assertTrue(str(q.exams_root(self.s, "worker")).startswith(str(self.s.state / "exams")))
        self.assertIn("exam.built", [e["type"] for e in Ledger(self.s).events()])
        with self.assertRaisesRegex(DurehError, "needs at least 3"):
            q.build(self.s, "desk", "pm", 3)
        with self.assertRaisesRegex(DurehError, "by the desk"):
            q.build(self.s, "builder", "worker", 3)

    def test_run_and_score_pass_then_gate_accepts(self):
        q.build(self.s, "desk", "worker", 3)
        run = q.run(self.s, "desk", "worker", SONNET)
        self.assertEqual(len(run["tasks"]), 3)
        text = Path(run["tasks"][0]["brief"]).read_text()
        self.assertTrue(text.startswith(f"DUREH-EXAM {run['run']} task="))
        self.assertIn(f"model={SONNET}", text.splitlines()[0])
        with self.assertRaisesRegex(DurehError, "one exam run per site"):
            q.run(self.s, "desk", "worker", HAIKU)
        self.answer(run)
        res = q.score(self.s, "desk", run["run"], local=True)
        self.assertEqual(res["status"], "pass", res)
        self.assertEqual(res["score"], "3/3")
        self.assertEqual(res["units"], "unmeasured")
        self.assertIn("class B", res["apply"])
        self.assertFalse(any(Path(t["workspace"]).exists() for t in run["tasks"]))
        entry = res["config_entry"]
        self.assertTrue(wf._qualified(entry, self.s, "worker")[0])
        with self.assertRaisesRegex(DurehError, "already scored"):
            q.score(self.s, "desk", run["run"], local=True)

    def test_write_outside_scopes_is_a_hard_failure(self):
        q.build(self.s, "desk", "worker", 3)
        run = q.run(self.s, "desk", "worker", HAIKU)
        self.answer(run, outside=True)
        res = q.score(self.s, "desk", run["run"], local=True)
        self.assertEqual(res["status"], "fail")
        self.assertTrue(all(t["hard_failure"] for t in res["tasks"]))
        self.assertNotIn("config_entry", res)

    def test_failing_checks_fail_the_pass_rule(self):
        q.build(self.s, "desk", "worker", 3)
        run = q.run(self.s, "desk", "worker", HAIKU)
        self.answer(run, good=False)
        res = q.score(self.s, "desk", run["run"], local=True, rerun=False)
        self.assertEqual(res["status"], "fail")
        self.assertIn("0/3 below the pass rule (3/3)", res["reasons"][0])


class CompareAndPlan(QualCase):
    def test_cheapest_passing_no_worse_than_incumbent_minus_one(self):
        cfg = json.loads(self.s.config_path.read_text())
        inc = cfg["models"]["worker"]["model"]
        self.assertEqual(inc, SONNET)
        self.qual_record("worker", SONNET, passed=4, units=2.0)
        self.qual_record("worker", HAIKU, passed=4, units=0.5)
        rec = q.compare(self.s, "worker")["tiers"]["worker"]["recommendation"]
        self.assertEqual((rec["action"], rec["model"]), ("switch", HAIKU))
        self.assertIn("units per task", rec["why"])

    def test_keep_incumbent_when_cheaper_fails_or_is_worse(self):
        self.qual_record("worker", SONNET, passed=5, total=5, units=2.0)
        self.qual_record("worker", HAIKU, status="fail", passed=3, total=5, units=0.4)
        rec = q.compare(self.s, "worker")["tiers"]["worker"]["recommendation"]
        self.assertEqual((rec["action"], rec["model"]), ("keep", SONNET))

    def test_unmeasured_units_rank_by_candidate_order(self):
        self.qual_record("worker", OPUS)
        self.qual_record("worker", HAIKU)
        rec = q.compare(self.s, "worker")["tiers"]["worker"]["recommendation"]
        self.assertEqual(rec["model"], HAIKU)
        self.assertIn("candidate order", rec["why"])

    def test_plan_lists_candidates_cheapest_first(self):
        (q.exams_root(self.s, "worker") / "set-x").mkdir(parents=True)
        (q.exams_root(self.s, "worker") / "set-x" / "set.json").write_text("{}")
        self.qual_record("worker", HAIKU)
        rows = q.plan(self.s, "worker")["tiers"]["worker"]["candidates"]
        self.assertEqual([r["model"] for r in rows], [HAIKU, SONNET, OPUS])
        self.assertTrue(rows[0]["state"].startswith("passed"))
        self.assertEqual(rows[1]["state"], "needs a run (incumbent reference)")
        self.assertEqual(rows[2]["state"], "not needed (a cheaper candidate passed)")
        pm = q.plan(self.s, "pm")["tiers"]["pm"]
        self.assertFalse(pm["examinable"])
        self.assertIn("not examinable", pm["next"])

    def test_cli_compare_and_plan_are_read_only(self):
        self.qual_record("worker", HAIKU)
        n = len(list((self.s.records / "run").glob("*.json")))
        for argv in (["exam", "compare", "--all"], ["exam", "plan", "--tier", "worker", "--json"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--site", str(self.site_root), "--observe", *argv])
            self.assertEqual(code, 0, out.getvalue())
        self.assertIn(HAIKU, out.getvalue())
        self.assertEqual(len(list((self.s.records / "run").glob("*.json"))), n)


if __name__ == "__main__":
    unittest.main()
