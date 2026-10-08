"""Release 45: `dureh status` lists the site user's scheduler jobs that no harness record owns ("External jobs"),
and `day close` warns about them without refusing. Fake squeue only (never the real queue); the job ids below
are made up."""
import json
import os

from helpers import SiteCase

from dureh_core import ops
from dureh_core.ops import render_status, status
from dureh_core.records import Ledger


class ExternalJobs(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        self.squeue("")
        self.config(external_jobs=True)
        self.led = Ledger(self.s)

    def config(self, **slurm):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["slurm"] = {**cfg["slurm"], **slurm}
        cfg["env"] = {"path_prepend": [str(self.fake)]}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()

    def squeue(self, lines, rc=0):
        (self.fake / "squeue").write_text(f"#!/bin/sh\necho \"$@\" > {self.fake}/squeue.args\n"
                                          f"printf '%s' '{lines}'\nexit {rc}\n")
        (self.fake / "squeue").chmod(0o755)

    def run_record(self, job_id, **extra):
        self.led.append("run", "slurm", "desk", None, {"run_type": "slurm", "status": "submitted", "job_id": job_id,
                                                        **extra})

    def test_lists_only_jobs_no_record_owns(self):
        self.run_record("900001")
        self.run_record("900002", jobs=[{"job_id": "900003"}])
        self.led.event("session.started", "desk", None, {"job_id": "900004"})
        self.squeue("900001|t-WI-0001|RUNNING|1:00\n900003_[0-3]|t-WI-0002|PENDING|0:00\n900004|x|RUNNING|2:00:00\n"
                    "900005|cc-other-site|RUNNING|3:00:00\n900006|manual-test|RUNNING|0:12:01\n"
                    "900007_2|framework-step|RUNNING|0:05\n")
        ext = ops.external_jobs(self.s)
        self.assertEqual([j["job_id"] for j in ext["jobs"]], ["900006", "900007_2"])
        self.assertIn(f"-u {os.environ.get('USER') or ''}".strip(), (self.fake / "squeue.args").read_text())
        text = render_status(status(self.s))
        self.assertIn("External jobs (2, no harness record): 900006 manual-test RUNNING 0:12:01;"
                      " 900007_2 framework-step RUNNING 0:05", text)

    def test_own_session_job_and_ignore_globs(self):
        os.environ["SLURM_JOB_ID"] = "900010"  # the session itself (a made-up id; SiteCase restores the env)
        self.config(external_jobs=True, external_ignore=["keep-*"])
        self.squeue("900010|whatever|RUNNING|1:00\n900011|keep-me|RUNNING|1:00\n900012|cc-x|RUNNING|1:00\n")
        self.assertEqual([j["job_id"] for j in ops.external_jobs(self.s)["jobs"]], ["900012"])

    def test_limit_and_count(self):
        self.squeue("".join(f"9001{i:02d}|j{i}|PENDING|0:00\n" for i in range(15)))
        ext = ops.external_jobs(self.s)
        self.assertEqual((ext["count"], len(ext["jobs"])), (15, ops.EXTERNAL_LIMIT))
        self.assertIn("... and 5 more", ops.render_external(ext))

    def test_never_fails_without_squeue(self):
        self.squeue("", rc=1)
        self.assertIsNone(ops.external_jobs(self.s))
        (self.fake / "squeue").unlink()
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["env"] = {"vars": {"PATH": str(self.fake)}}  # no squeue at all (not even a login node's real one)
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        st = status(self.site())
        self.assertNotIn("External jobs", render_status(st))
        self.config(external_jobs=False)
        self.squeue("900020|x|RUNNING|1:00\n")
        self.assertIsNone(ops.external_jobs(self.s))

    def test_day_close_warns_and_does_not_refuse(self):
        from dureh_core import workday

        self.squeue("900030|manual-job|RUNNING|4:00:00\n")
        plan = self.led.event("day.planned", "desk", None, {"goal": "g", "plan": "p"})
        d = {"phase": "off-duty", "report": "x", "plan": plan["id"], "start": "2026-10-08T09:00:00+00:00"}
        orig = (workday._require_on, workday.day, workday.device_on)
        workday._require_on, workday.day, workday.device_on = (lambda s: None), (lambda s, *a: d), (lambda *a: False)
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["session"] = {"release_on_close": False}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        try:
            rec = workday.close(self.site(), "desk", "done")
        finally:
            workday._require_on, workday.day, workday.device_on = orig
        self.assertIn("External jobs (1, no harness record): 900030 manual-job RUNNING 4:00:00", rec["warning"])
        self.assertEqual(rec["payload"]["external_jobs"]["count"], 1)
