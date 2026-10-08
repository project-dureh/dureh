"""Site folder-name independence, step 4 (proposals/site-rename-independence.md rows 7 and 12): after the site
directory is moved or renamed, `adapter sync` refreshes config `site.root` and records one `site.moved`; paths that
records name under the old root resolve against the live root on read (`Site.relocate`), records are never
rewritten; the generated files name the new location only; status, path-refs and `worktree repair --all` work."""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import SiteCase, hook

from dureh_core import cli, ops
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.site import Site


class RelocateRoot(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.s = self.site()
        self.old = self.site_root
        b = wf.wi_new(self.s, "desk", "B", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        self.wi = wf.wi_new(self.s, "desk", "W", "g", "py-writer", "modules/demo", ["ok"], parent=b)["id"]
        for x in (b, self.wi):
            wf.wi_approve(self.s, "desk", x, "ok")
        hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", cwd=str(self.site_root),
             tool_input={"subagent_type": "pm-modules", "prompt": wf.dispatch_brief(self.s, b, "desk")["brief"]})
        hook(self.s, hook_event_name="SubagentStart", agent_type="pm-modules", agent_id="a-pm")
        # a real worktree; its dispatch.prepared event names it under the old root (a worktree.created-like record)
        self.wt = Path(wf.dispatch_brief(self.s, self.wi, "pm-modules")["worktree"])
        self.assertEqual(self.wt, self.old / "worktrees" / self.wi)
        led = Ledger(self.s)
        # a slurm run with a log dir, script and logs under the old root (the log files exist)
        logs = self.old / "logs" / "slurm" / self.wi
        logs.mkdir(parents=True)
        (logs / f"dureh-{self.wi}-4600.out").write_text("out\n")
        (logs / f"dureh-{self.wi}-4600.err").write_text("err\n")
        script = self.old / "modules" / "demo" / "job.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        self.run_rec = led.append("run", "slurm", "desk", self.wi, {
            "run_type": "slurm", "status": "submitted", "job_id": "4600", "command": ["sbatch", f"--job-name=dureh-{self.wi}"],
            "script": str(script), "logs": str(logs), "chdir": str(self.old / "modules" / "demo"),
            "log_out": f"{logs}/%x-%j.out", "log_err": f"{logs}/%x-%j.err"})
        # a failed verify run with a check log path and a job log under the old root
        self.verify_rec = led.append("run", "verify", "desk", self.wi, {
            "run_type": "verify", "status": "fail", "result": "fail", "head": "a" * 40, "note": "0/1 declared checks passed",
            "checks": [{"command": "pytest -q", "exit": 1, "seconds": 2.0, "tail": "boom\n",
                        "log": f"{logs}/verify-x.check1.out"}],
            "violations": [], "missing_outputs": [],
            "job": {"job_id": "4601", "state": "FAILED", "log_out": f"{logs}/verify-x.out", "log_err": f"{logs}/verify-x.err"}})
        self.assertEqual(self.s.stored_root, str(self.old))
        self.assertEqual(self.s.previous_roots(), [])
        self.assertEqual(self.s.relocate(self.wt), self.wt)

    def rename(self):
        new = self.tmp / "renamed-org"
        os.rename(self.site_root, new)
        self.site_root = new
        self.s = Site(new)
        return new

    def cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--site", str(self.site_root), *argv])
        return code, out.getvalue()

    def moved_events(self):
        return [e for e in Ledger(self.s).events() if e["type"] == "site.moved"]

    def test_adapter_sync_refreshes_the_root_and_records_the_move_once(self):
        new = self.rename()
        # before the sync: the stale config site.root is already a known previous root
        self.assertEqual(self.s.stored_root, str(self.old))
        self.assertEqual(self.s.previous_roots(), [self.old])
        self.assertEqual(self.s.relocate(self.old / "worktrees" / self.wi), new / "worktrees" / self.wi)
        code, text = self.cli("adapter", "sync")
        self.assertEqual(code, 0, text)
        self.assertIn(f"site root: {new} (config site.root said {self.old}", text)
        self.assertIn("site.moved recorded", text)
        cfg = json.loads((new / "config.json").read_text())
        self.assertEqual(cfg["site"]["root"], str(new))
        self.assertEqual(cfg["site"]["id"], self.s.site_id)  # the other site keys and the rest of the config stay
        self.assertEqual(cfg["startup"]["id"], self.s.config["startup"]["id"])
        self.assertEqual(sorted(cfg), sorted(self.s.config))
        self.s = Site(new)
        self.assertEqual(self.s.stored_root, str(new))
        moved = self.moved_events()
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["payload"], {"from": str(self.old), "to": str(new)})
        self.assertEqual(moved[0]["actor"], "desk")
        # a second sync changes nothing and records nothing
        code, text = self.cli("adapter", "sync")
        self.assertEqual(code, 0, text)
        self.assertNotIn("site root:", text)
        self.assertEqual(len(self.moved_events()), 1)
        self.assertEqual(json.loads((new / "config.json").read_text())["site"]["root"], str(new))
        # the previous root now comes from the event, not the config
        self.s = Site(new)
        self.assertEqual(self.s.previous_roots(), [self.old])

    def test_relocate_maps_old_root_paths_and_leaves_others_alone(self):
        new = self.rename()
        self.cli("adapter", "sync")
        s = Site(new)
        self.assertEqual(s.relocate(self.old / "worktrees" / self.wi), new / "worktrees" / self.wi)
        self.assertEqual(s.relocate(str(self.old / "logs" / "slurm" / self.wi / "x.out")), new / "logs" / "slurm" / self.wi / "x.out")
        self.assertEqual(s.relocate(self.old), new)
        self.assertEqual(s.relocate(new / "modules" / "demo"), new / "modules" / "demo")  # live: unchanged
        self.assertEqual(s.relocate("/elsewhere/site/worktrees/WI-0001"), Path("/elsewhere/site/worktrees/WI-0001"))
        self.assertEqual(s.relocate(f"{self.old}-other/x"), Path(f"{self.old}-other/x"))  # a prefix, not a parent
        self.assertEqual(s.relocate("modules/demo"), Path("modules/demo"))  # relative: unchanged
        self.assertEqual(s.relocate(self.tmp / "reference-repo" / "docs"), self.tmp / "reference-repo" / "docs")
        # a second move: both earlier roots map to the newest location
        newer = self.tmp / "moved-again"
        os.rename(new, newer)
        self.site_root = newer
        self.cli("adapter", "sync")
        s = self.s = Site(newer)
        self.assertEqual(s.previous_roots(), [new, self.old])
        self.assertEqual(len(self.moved_events()), 2)
        self.assertEqual(s.relocate(self.old / "worktrees" / self.wi), newer / "worktrees" / self.wi)
        self.assertEqual(s.relocate(new / "worktrees" / self.wi), newer / "worktrees" / self.wi)
        # config site.previous_roots is honoured as well
        cfg = json.loads(s.config_path.read_text())
        cfg["site"]["previous_roots"] = ["/some/earlier/place"]
        s.config_path.write_text(json.dumps(cfg))
        s = Site(newer)
        self.assertIn(Path("/some/earlier/place"), s.previous_roots())
        self.assertEqual(s.relocate("/some/earlier/place/logs/a"), newer / "logs" / "a")

    def test_readers_resolve_recorded_paths_against_the_live_root(self):
        new = self.rename()
        self.cli("adapter", "sync")
        s = Site(new)
        led = Ledger(s)
        # rework brief: the "Last verification" block names the logs where they are now
        block = wf.last_verification_block(led, self.wi)
        self.assertIn(f"log {new}/logs/slurm/{self.wi}/verify-x.check1.out", block)
        self.assertIn(f"logs: {new}/logs/slurm/{self.wi}/verify-x.out, {new}/logs/slurm/{self.wi}/verify-x.err", block)
        self.assertNotIn(str(self.old), block)
        # slurm status/wait log hint
        hint = ops._log_hint(s, led.get("run", self.run_rec["id"]))
        self.assertEqual(hint, {"log_out": f"{new}/logs/slurm/{self.wi}/dureh-{self.wi}-4600.out",
                                "log_err": f"{new}/logs/slurm/{self.wi}/dureh-{self.wi}-4600.err"})
        # purge finds the job logs by their recorded (old-root) names at the new location
        wf.dispatch_stop(s, "desk", self.wi, "moved")
        plan = wf.purge(s, "desk", self.wi, dry_run=True)
        self.assertEqual(sorted(plan["planned_logs"]), sorted([f"{new}/logs/slurm/{self.wi}/dureh-{self.wi}-4600.out",
                                                               f"{new}/logs/slurm/{self.wi}/dureh-{self.wi}-4600.err"]))
        self.assertIn(str(new / "worktrees" / self.wi), plan["planned"])
        # the records themselves are untouched
        on_disk = json.loads((new / "state" / "records" / "run" / f"{self.run_rec['id']}.json").read_text())
        self.assertEqual(on_disk["payload"]["script"], str(self.old / "modules" / "demo" / "job.sh"))
        self.assertEqual(on_disk["payload"]["log_out"], f"{self.old}/logs/slurm/{self.wi}/%x-%j.out")

    def test_status_path_refs_and_repair_dry_run_work_on_the_moved_site(self):
        new = self.rename()
        code, text = self.cli("status")  # even before the sync
        self.assertEqual(code, 0, text)
        self.assertIn(f"DUREH site {new}", text)
        code, text = self.cli("adapter", "sync")
        self.assertEqual(code, 0, text)
        code, text = self.cli("status")
        self.assertEqual(code, 0, text)
        self.assertIn(f"DUREH site {new}", text)
        # path-refs: a path given under the old root is searched as the new one and as the old one
        r = ops.path_refs(Site(new), str(self.old / "modules" / "demo"), roots=[new])
        self.assertIn(str(new / "modules" / "demo"), r["needles"])
        self.assertIn(str(self.old / "modules" / "demo"), r["needles"])
        self.assertTrue(any(f"{self.run_rec['id']}.json" in h for h in r["hits"]), r["hits"])  # the record's old path
        code, text = self.cli("path-refs", str(new / "modules" / "demo"))
        self.assertEqual(code, 0, text)
        self.assertIn(f"{self.old}/modules/demo", text)
        # worktree repair --all --dry-run lists the worktree under the new path
        d = wf.worktree_repair_all(Site(new), "desk", dry_run=True)
        rows = {row["name"]: row for row in d["items"] if row["kind"] == "worktree"}
        self.assertEqual(rows[self.wi]["path"], str(new / "worktrees" / self.wi))
        self.assertEqual(rows[self.wi]["state"], "repaired")
        code, text = self.cli("worktree", "repair", "--all", "--dry-run")
        self.assertEqual(code, 0, text)
        self.assertIn(f"{self.wi}", text)
        self.assertIn("would repair", text)

    def test_generated_files_name_the_new_location_only(self):
        new = self.rename()
        self.assertIn(str(self.old), (new / "CLAUDE.md").read_text())  # stale until the sync
        code, text = self.cli("adapter", "sync")
        self.assertEqual(code, 0, text)
        generated = [new / ".claude" / "settings.json", new / "CLAUDE.md",
                     *sorted((new / ".claude" / "agents").glob("*.md"))]
        self.assertTrue(len(generated) > 2)
        for f in generated:
            body = f.read_text()
            self.assertNotIn(str(self.old), body, f)
            self.assertIn(str(new), body, f)
        settings = json.loads((new / ".claude" / "settings.json").read_text())
        self.assertEqual(settings["env"]["DUREH_SITE"], str(new))
        self.assertIn(f"Bash({new}/bin/dureh *)", settings["permissions"]["allow"])
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn(f"${{CLAUDE_PROJECT_DIR:-{new}}}/bin/dureh", cmd)
        # bin/dureh execs the pinned release with its own location as --site
        launcher = (new / "bin" / "dureh").read_text()
        self.assertIn(f'exec {Site(new).pinned_harness_root / "dureh"} --site "$SITE"', launcher)
        self.assertNotIn(str(self.old), launcher)
        self.assertNotIn(str(new), launcher)


if __name__ == "__main__":
    unittest.main()
