"""Release 32: scheduler stdout/stderr never land in the company root or a project tree. Every sbatch call carries
its own --output/--error inside `<slurm.log_dir>/<WI>/` (command-line options override #SBATCH directives); a
script's own name keeps only its basename there. Evidence: 8 `dureh-WI-0058-<job>.out/.err` files in the
company site root from 2026-10-03 (`#SBATCH --output=dureh-%x-%j.out`). Fake sbatch only."""
import contextlib
import io
import json

from helpers import SiteCase

from dureh_core import ops
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.runtimes import claude_code
from dureh_core.util import DurehError


class SlurmLogs(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        (self.fake / "sbatch").write_text(f"#!/bin/sh\necho \"$@\" >> {self.fake}/args\necho 4600\n")
        (self.fake / "sbatch").chmod(0o755)
        self.config()
        (self.site_root / "projects" / "lna").mkdir(parents=True)
        self.w = wf.wi_new(self.s, "desk", "run", "g", "researcher", "projects/lna", ["a"])["id"]
        wf.wi_approve(self.s, "desk", self.w, "ok")
        Ledger(self.s).event("wi.dispatched", "desk", self.w, {"role": "researcher"})
        self.logs = f"{self.site_root}/logs/slurm/{self.w}"

    def config(self, **slurm):
        cfg = json.loads(self.s.config_path.read_text() if hasattr(self, "s") else (self.site_root / "config.json").read_text())
        cfg["env"] = {"path_prepend": [str(self.fake)]}
        cfg["slurm"] = {**(cfg.get("slurm") or {}), **slurm}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()

    def script(self, *directives):
        p = self.tmp / "job.sh"
        p.write_text("#!/bin/sh\n" + "".join(f"#SBATCH {d}\n" for d in directives) + "echo hi\n")
        return str(p)

    def submit(self, script, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run = ops.slurm_submit(self.s, "desk", self.w, script, **kw)
        return run["payload"], err.getvalue()

    def last_args(self):
        return (self.fake / "args").read_text().splitlines()[-1].split()

    def test_default_names_in_the_log_dir(self):
        p, err = self.submit(self.script())
        a = self.last_args()
        self.assertIn(f"--output={self.logs}/%x-%j.out", a)
        self.assertIn(f"--error={self.logs}/%x-%j.err", a)
        self.assertEqual(err, "")

    def test_job_name_defaults_to_the_session_name(self):
        # release 44: `<session name>-<WI>`, the company part of the session job `cc-<name>`, not `dureh-<WI>`
        p, _ = self.submit(self.script())
        self.assertIn(f"--job-name={self.s.session_name}-{self.w}", self.last_args())
        self.assertEqual(p["job_name"], f"{self.s.session_name}-{self.w}")

    def test_config_job_prefix_overrides_the_session_name(self):
        self.config(job_prefix="lab-x")
        p, _ = self.submit(self.script())
        self.assertIn(f"--job-name=lab-x-{self.w}", self.last_args())
        self.assertEqual(p["job_name"], f"lab-x-{self.w}")
        self.config(job_prefix="bad name")
        with self.assertRaisesRegex(DurehError, "job_prefix"):
            self.submit(self.script())

    def test_the_scripts_own_job_name_is_kept(self):
        for directive in ("-J step-one", "--job-name=step-one"):
            p, _ = self.submit(self.script(directive))
            a = self.last_args()
            self.assertFalse([x for x in a if x.startswith("--job-name") or x == "-J"], a)
            self.assertEqual(p["job_name"], "step-one")
            self.assertIn(f"--output={self.logs}/%x-%j.out", a, "logs stay in the log dir whatever the name")
        p, _ = self.submit(self.script(), extra=["-J", "from-arg"])
        self.assertEqual(p["job_name"], "from-arg")
        self.assertEqual([x for x in self.last_args() if x.startswith("--job-name")], [])
        hint = ops._log_hint(self.s, {"wi": self.w, "payload": {**p, "job_id": "4600"}})
        self.assertEqual(hint["log_out"], f"{self.logs}/from-arg-4600.out")

    def test_relative_output_is_redirected_with_a_warning(self):
        p, err = self.submit(self.script("--output=dureh-%x-%j.out", "--error=dureh-%x-%j.err"))
        a = self.last_args()
        self.assertEqual(a[-3:-1], [f"--output={self.logs}/dureh-%x-%j.out", f"--error={self.logs}/dureh-%x-%j.err"],
                         "after the script's own arguments, so they win")
        self.assertEqual((p["log_out"], p["log_err"]), (f"{self.logs}/dureh-%x-%j.out", f"{self.logs}/dureh-%x-%j.err"))
        self.assertIn("is not in the site's log dir", err)
        self.assertIn("warnings", p)
        self.assertFalse(list(self.site_root.glob("dureh-*")))

    def test_absolute_outside_is_redirected_with_a_warning(self):
        away = self.tmp / "away"
        p, err = self.submit(self.script(f"-o {away}/x-%j.out"), extra=["-e", f"{self.site_root}/x-%j.err"])
        self.assertEqual((p["log_out"], p["log_err"]), (f"{self.logs}/x-%j.out", f"{self.logs}/x-%j.err"))
        self.assertIn(f"--output {away}/x-%j.out (#SBATCH directive)", err)
        self.assertIn(f"--error {self.site_root}/x-%j.err (sbatch argument)", err)

    def test_absolute_inside_the_log_dir_is_kept(self):
        inside = f"{self.site_root}/logs/slurm/shared/%x-%j.out"
        p, err = self.submit(self.script(f"--output={inside}"))
        self.assertEqual(p["log_out"], inside)
        self.assertEqual(p["log_err"], inside, "stderr follows --output when no --error is named")
        self.assertIn(f"--output={inside}", self.last_args())
        self.assertFalse(any(x.startswith("--error=") for x in self.last_args()))
        self.assertEqual(err, "")

    def test_array_names_keep_a_task_pattern(self):
        p, err = self.submit(self.script("--output=step-%j.out"), items=["sub-01", "sub-02"])
        self.assertEqual(p["log_out"], f"{self.logs}/step-%A_%a.out", "%j of a task is not resolvable: %A_%a")
        p, err = self.submit(self.script("--output=step.log"), items=["sub-01", "sub-02"])
        self.assertEqual(p["log_out"], f"{self.logs}/step_%a.log")
        self.assertIn("holds no %a", err)
        p, err = self.submit(self.script("--output=step-%A_%a.out"), items=["sub-01", "sub-02"])
        self.assertEqual(p["log_out"], f"{self.logs}/step-%A_%a.out")
        self.assertNotIn("holds no %a", err)
        p, _ = self.submit(self.script(), items=["sub-01"])
        self.assertEqual(p["log_err"], f"{self.logs}/%x-%A_%a.err")

    def test_config_log_dir(self):
        self.config(log_dir="scratch/joblogs")
        p, _ = self.submit(self.script("--output=a-%j.out"))
        d = f"{self.site_root}/scratch/joblogs/{self.w}"
        self.assertEqual((p["logs"], p["log_out"]), (d, f"{d}/a-%j.out"))
        self.assertTrue((self.site_root / "scratch" / "joblogs" / self.w).is_dir())
        inside = f"{self.site_root}/scratch/joblogs/x/%j.out"
        self.assertEqual(self.submit(self.script(f"--output={inside}"))[0]["log_out"], inside)
        self.assertIn("scratch/joblogs/<WI>/", claude_code._log_rule(self.s))
        for bad in ("/tmp", ".", "../elsewhere", str(self.site_root)):
            self.config(log_dir=bad)
            with self.assertRaisesRegex(DurehError, "slurm.log_dir"):
                self.submit(self.script())
        self.config(log_dir=f"{self.site_root}/logs/jobs")
        self.assertEqual(self.submit(self.script())[0]["logs"], f"{self.site_root}/logs/jobs/{self.w}")

    def test_generated_instructions_name_the_log_dir(self):
        rule = claude_code._log_rule(self.s)
        self.assertIn(f"Do not set --output/--error in job scripts; the harness writes scheduler logs to logs/slurm/<WI>/.", rule)
        self.assertIn(rule, claude_code._claude_md(self.s))

    def test_purge_removes_the_wi_logs_in_the_configured_dir(self):
        self.config(log_dir="scratch/joblogs")
        p, _ = self.submit(self.script("--output=run-%x-%j.out"), items=["sub-01", "sub-02"])
        d = self.site_root / "scratch" / "joblogs" / self.w
        mine = [d / f"run-{self.s.session_name}-{self.w}-4600_{i}.out" for i in (0, 1)]
        for f in mine:
            f.write_text("x")
        keep = self.site_root / "projects" / "lna" / "keep.out"
        keep.write_text("x")
        wf.dispatch_stop(self.s, "desk", self.w, "test")
        out = wf.purge(self.s, "desk", self.w)
        self.assertTrue(set(map(str, mine)) <= set(out["removed_logs"]), out)
        self.assertTrue(any(x.endswith(".items") for x in out["removed_logs"]), "the --each item list in the log dir")
        self.assertFalse(any(f.exists() for f in mine))
        self.assertTrue(keep.is_file())

    def test_old_records_still_found(self):
        """A release-31 record whose relative #SBATCH --output put the logs in the site root (the company-site case)."""
        script = self.site_root / "projects" / "lna" / "job.sh"
        script.write_text("#!/bin/sh\n#SBATCH --output=%x-%j.out\n#SBATCH --error=%x-%j.err\necho hi\n")
        Ledger(self.s).append("run", "slurm", "desk", self.w, {
            "run_type": "slurm", "status": "submitted", "job_id": "3700001", "script": str(script),
            "chdir": str(self.site_root), "command": ["sbatch", "--parsable", f"--job-name=dureh-{self.w}", str(script)],
            "log_out": f"{self.site_root}/%x-%j.out", "log_err": f"{self.site_root}/%x-%j.err"})
        old = [self.site_root / f"dureh-{self.w}-3700001.out", self.site_root / f"dureh-{self.w}-3700001.err"]
        for f in old:
            f.write_text("x")
        wf.dispatch_stop(self.s, "desk", self.w, "test")
        out = wf.purge(self.s, "desk", self.w)
        self.assertEqual(sorted(out["removed_logs"]), sorted(map(str, old)))

    def test_status_shows_the_log_paths(self):
        p, _ = self.submit(self.script())
        (self.fake / "sacct").write_text("#!/bin/sh\necho 'COMPLETED|0:0|00:00:01'\n")
        (self.fake / "sacct").chmod(0o755)
        run = [r for r in Ledger(self.s).load("run") if r["wi"] == self.w][-1]
        ev = ops.slurm_status(self.s, "desk", run["id"])
        name = f"{self.s.session_name}-{self.w}"
        self.assertEqual(ev["logs"], {"log_out": f"{self.logs}/{name}-4600.out", "log_err": f"{self.logs}/{name}-4600.err"})


if __name__ == "__main__":
    import unittest

    unittest.main()
