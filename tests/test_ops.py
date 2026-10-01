"""Operational tools: path-refs, purge, push, slurm, usage, exams, adapter drift, YAML subset."""
import json
import os
import shutil
import stat
import subprocess
import unittest
from pathlib import Path

from helpers import HARNESS, SiteCase

from dureh_core import ops
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError


class Ops(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()

    def test_path_refs(self):
        (self.site_root / "notes" / "a.md").write_text("see modules/demo/src/demo.py for details\n")
        r = ops.path_refs(self.s, "modules/demo/src/demo.py", roots=[self.site_root])
        self.assertTrue(any("notes/a.md:1" in h for h in r["hits"]))

    def test_purge_removes_links_not_targets(self):
        self.make_module()
        b = wf.wi_new(self.s, "desk", "b", "g", "pm-modules", "modules/demo", ["a"])["id"]
        w = wf.wi_new(self.s, "desk", "t", "g", "py-writer", "modules/demo", ["a"], parent=b)["id"]
        wf.wi_approve(self.s, "desk", w, "ok")
        # the bundle is never dispatched here; create the worktree directly
        wf._create_worktree(self.s, w, "modules/demo")
        precious = self.tmp / "precious"
        precious.mkdir()
        (precious / "keep.txt").write_text("keep")
        with self.assertRaisesRegex(DurehError, "purge only"):
            wf.purge(self.s, "desk", w)
        wf.dispatch_stop(self.s, "desk", w, "test")
        link = self.site_root / "worktrees" / w / "linked"
        link.symlink_to(precious)
        plan = wf.purge(self.s, "desk", w, dry_run=True)
        self.assertTrue(plan["planned"] and not plan["removed"])
        wf.purge(self.s, "desk", w)
        self.assertTrue((precious / "keep.txt").is_file(), "symlink target survives purge")
        self.assertFalse((self.site_root / "worktrees" / w).exists())
        with self.assertRaisesRegex(DurehError, "only the desk"):
            wf.purge(self.s, "py-writer", w)

    def test_push_requires_allowlist_and_bound_approval(self):
        repo = self.make_module()
        bare = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
        with self.assertRaisesRegex(DurehError, "allowed_remotes"):
            ops.push(self.s, "desk", str(repo), "origin", "main", "dec-x")
        cfg = json.loads(self.s.config_path.read_text())
        cfg["push"] = {"allowed_remotes": [str(bare)]}
        self.s.config_path.write_text(json.dumps(cfg))
        from dureh_core.site import Site
        s = Site(self.site_root)
        dec = ops.push_approve(s, "desk", str(repo), "origin", "main", "Director approved in test")
        (repo / "src" / "demo.py").write_text("def f():\n    return 3\n")
        subprocess.run(["git", "commit", "-qam", "later change"], cwd=repo, check=True)
        with self.assertRaisesRegex(DurehError, "does not approve"):
            ops.push(s, "desk", str(repo), "origin", "main", dec["id"])
        dec2 = ops.push_approve(s, "desk", str(repo), "origin", "main", "approved again")
        with self.assertRaisesRegex(DurehError, "only the desk"):
            ops.push(s, "py-writer", str(repo), "origin", "main", dec2["id"])
        ev = ops.push(s, "desk", str(repo), "origin", "main", dec2["id"])
        remote_head = subprocess.run(["git", "rev-parse", "main"], cwd=bare, capture_output=True, text=True).stdout.strip()
        self.assertEqual(remote_head, ev["payload"]["revision"])

    def test_slurm_submit_records_run_with_fake_scheduler(self):
        fake = self.tmp / "fakebin"
        fake.mkdir()
        (fake / "sbatch").write_text("#!/bin/sh\necho \"$@\" > \"$(dirname \"$0\")/args\"\necho 4242\n")
        (fake / "sbatch").chmod(0o755)
        cfg = json.loads(self.s.config_path.read_text())
        cfg["env"] = {"path_prepend": [str(fake)]}
        self.s.config_path.write_text(json.dumps(cfg))
        from dureh_core.site import Site
        s = Site(self.site_root)
        w = wf.wi_new(s, "desk", "run", "g", "researcher", "site", ["a"])["id"]
        script = self.tmp / "job.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        with self.assertRaisesRegex(DurehError, "only pipeline-runner"):
            ops.slurm_submit(s, "py-writer", w, str(script))
        with self.assertRaisesRegex(DurehError, "not dispatched"):
            ops.slurm_submit(s, "desk", w, str(script))
        wf.wi_approve(s, "desk", w, "ok")
        Ledger(s).event("wi.dispatched", "desk", w, {"role": "researcher"})
        with self.assertRaisesRegex(DurehError, "missing"):
            ops.slurm_submit(s, "desk", w, str(self.tmp / "nope.sh"))
        run = ops.slurm_submit(s, "desk", w, str(script))
        self.assertEqual(run["payload"]["job_id"], "4242")
        self.assertIn("--account=acct-x", (fake / "args").read_text())
        dry = ops.slurm_submit(s, "desk", w, str(script), dry_run=True)
        self.assertEqual(dry["payload"]["status"], "not-run")

    def test_usage_reports_undefined_without_promotions(self):
        t = self.tmp / "t.jsonl"
        t.write_text(json.dumps({"message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5}}}) + "\n" +
                     json.dumps({"message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5}}}) + "\n")
        u = ops.transcript_usage(t)
        self.assertEqual((u["input_tokens"], u["output_tokens"], u["messages"]), (10, 5, 1))
        Ledger(self.s).append("run", "agent", "desk", "WI-0001", {"run_type": "agent", "status": "completed", "usage": u})
        r = ops.usage(self.s)
        self.assertEqual(r["tokens_total"], 15)
        self.assertTrue(str(r["tokens_per_promoted_wi"]).startswith("undefined"))

    def test_exam_scoring(self):
        from dureh_core.exams import tasks

        task = [t for t in tasks("py-writer") if t.name == "pyw-001-missing-input"][0]
        ws = self.tmp / "ws"
        shutil.copytree(task / "fixture", ws)
        from dureh_core.exams import score
        r = score(self.s, "desk", "py-writer", str(ws), None)
        self.assertEqual(r["result"], "fail", "unfixed fixture must fail the oracle")
        (ws / "src/demo/__init__.py").write_text(
            "import json\nimport os\n\n\ndef load_manifest(path):\n    if not os.path.exists(path):\n"
            "        raise FileNotFoundError(f'manifest is missing: {path}')\n"
            "    with open(path, encoding='utf-8') as stream:\n        return json.load(stream)\n")
        (ws / "tests/test_missing.py").write_text(
            "import os, sys, tempfile, unittest\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
            "import demo\n\nclass M(unittest.TestCase):\n    def test_missing(self):\n"
            "        with self.assertRaises(FileNotFoundError):\n            demo.load_manifest(os.path.join(tempfile.mkdtemp(), 'x.json'))\n")
        r = score(self.s, "desk", "py-writer", str(ws), None)
        self.assertEqual(r["result"], "pass", r)
        (ws / "data").mkdir()
        (ws / "data" / "x.nii").write_text("forbidden")
        r = score(self.s, "desk", "py-writer", str(ws), None)
        self.assertTrue(r["hard_failure"])

    def test_adapter_drift_and_tool_coverage(self):
        from dureh_core.runtimes.claude_code import drift, generate
        from dureh_core.selftest import tool_coverage

        self.assertEqual(drift(self.s), [])
        self.assertEqual(tool_coverage(self.s), [])
        agent = self.site_root / ".claude" / "agents" / "py-writer.md"
        agent.write_text(agent.read_text() + "\nextra hand edit\n")
        self.assertTrue(any("edited by hand" in d for d in drift(self.s)))
        generate(self.s)
        self.assertEqual(drift(self.s), [])
        settings = json.loads((self.site_root / ".claude" / "settings.json").read_text())
        self.assertIn("PreToolUse", settings["hooks"])
        self.assertFalse(settings["autoMemoryEnabled"])
        front = agent.read_text().split("---")[1]
        self.assertIn("model: claude-sonnet-5", front)
        self.assertIn("name: py-writer", front)


class SiteLinks(SiteCase):
    def test_link_module_pins_revision(self):
        self.install()
        s = self.site()
        src = self.tmp / "upstream"
        (src / "src").mkdir(parents=True)
        (src / "src" / "m.py").write_text("x = 1\n")
        subprocess.run(["git", "init", "-q", "-b", "dev", str(src)], check=True)
        subprocess.run(["git", "add", "-A"], cwd=src, check=True)
        subprocess.run(["git", "commit", "-qm", "one"], cwd=src, check=True)
        first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True).stdout.strip()
        (src / "src" / "m.py").write_text("x = 2\n")
        subprocess.run(["git", "commit", "-qam", "two"], cwd=src, check=True)
        from dureh_core.sitelinks import link_module
        e = link_module(s, "desk", "demo", str(src), first)
        self.assertEqual(e["rev"], first)
        self.assertEqual((self.site_root / "modules/demo/src/m.py").read_text(), "x = 1\n")
        with self.assertRaisesRegex(DurehError, "only the desk"):
            link_module(s, "py-writer", "demo2", str(src), first)
        with self.assertRaisesRegex(DurehError, "already linked"):
            link_module(s, "desk", "demo", str(src), first)
        cfg = json.loads(s.config_path.read_text())
        self.assertEqual(cfg["modules"][0]["id"], "demo")


class YamlSubset(unittest.TestCase):
    def test_matches_pyyaml_on_harness_files(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed; subset parser checked by the other tests only")
        from dureh_core import yamlsub

        files = list((HARNESS / "org").rglob("*.yaml")) + list((HARNESS / "tests" / "fixtures").rglob("*.yaml"))
        self.assertTrue(files)
        for f in files:
            with self.subTest(f=f.name):
                self.assertEqual(yamlsub.load_file(f), yaml.safe_load(f.read_text()))

    def test_rejects_unsupported(self):
        from dureh_core import yamlsub

        for bad in ("a: {b: 1}", "a:\n\tb: 1", "a: 1\na: 2", "- a\nb: 1"):
            with self.subTest(bad=bad):
                with self.assertRaises(DurehError):
                    yamlsub.loads(bad)


if __name__ == "__main__":
    unittest.main()
