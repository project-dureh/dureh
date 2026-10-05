"""Cluster profile, session runtime seam, day job plan, and the submit check against the working day."""
import contextlib
import io
import json
import os
import shlex
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone

from helpers import SiteCase

from dureh_core import cluster, ops, sessions, workday
from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.org import HARNESS_ROOT
from dureh_core.records import Ledger
from dureh_core.util import DurehError

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=timezone(timedelta(hours=-4)))

SINFO = """general*|(null)|120|11-00:00:00
gpu|gpu:a100:4(S:0-1)|3|2-00:00:00
gpu|gpu:l40:8(S:0-1)|2|2-00:00:00
volta-gpu|gpu:tesla_v100-sxm2-16gb:4|5|6-00:00:00
"""
ASSOC = "rc_lab_pi|normal,gpu_access\nrc_lab_pi|normal\nother_acct|interact\n"


class ClusterSession(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        self.config(env={"path_prepend": [str(self.fake)]})

    def config(self, **keys):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg.update(keys)
        (self.site_root / "config.json").write_text(json.dumps(cfg, indent=2))
        self.s = self.site()
        return cfg

    def tool(self, name, body):
        (self.fake / name).write_text("#!/bin/sh\n" + body)
        (self.fake / name).chmod(0o755)

    def cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--site", str(self.site_root), *args])
        return rc, buf.getvalue()

    # ------------------------------------------------------------ cluster
    def test_detect_parses_sinfo_and_sacctmgr(self):
        self.tool("sinfo", f"cat <<'X'\n{SINFO}X\n")
        self.tool("sacctmgr", f"echo \"$@\" > {self.tmp}/acct-args\ncat <<'X'\n{ASSOC}X\n")
        os.environ["USER"] = "tester"
        p = cluster.detect(self.s)
        self.assertEqual(p["scheduler"], "slurm")
        self.assertEqual(p["partitions"]["general"], {"nodes": 120, "time_limit": "11-00:00:00", "default": True,
                                                      "gpus": []})
        gpu = p["partitions"]["gpu"]
        self.assertEqual(gpu["nodes"], 5)
        self.assertEqual(gpu["gpus"], [{"type": "a100", "per_node": 4, "nodes": 3},
                                       {"type": "l40", "per_node": 8, "nodes": 2}])
        self.assertEqual(p["partitions"]["volta-gpu"]["gpus"][0]["type"], "tesla_v100-sxm2-16gb")
        self.assertEqual(p["account"], "rc_lab_pi")
        self.assertEqual(p["accounts"], ["rc_lab_pi", "other_acct"])
        self.assertEqual(p["qos"], ["normal", "gpu_access", "interact"])
        self.assertIn("user=tester", (self.tmp / "acct-args").read_text())

    def test_detect_write_keeps_key_order_and_extra_keys(self):
        self.tool("sinfo", f"cat <<'X'\n{SINFO}X\n")
        self.tool("sacctmgr", "exit 1\n")
        self.config(cluster={"container_runtime": "apptainer"})
        before = list(json.loads((self.site_root / "config.json").read_text()))
        self.assertEqual(cluster.profile(self.s)["partitions"], {})
        rc, out = self.cli("site", "cluster", "detect", "--write")
        self.assertEqual(rc, 0)
        cfg = json.loads((self.site_root / "config.json").read_text())
        self.assertEqual(list(cfg), before)
        self.assertEqual(cfg["cluster"]["container_runtime"], "apptainer")
        self.assertIsNone(cfg["cluster"]["account"])
        self.assertIn("gpu", cfg["cluster"]["partitions"])
        rc, out = self.cli("site", "cluster", "show")
        self.assertEqual(json.loads(out)["partitions"]["general"]["nodes"], 120)

    def test_detect_without_sinfo_fails_and_defaults(self):
        self.config(env={"path_prepend": [str(self.fake)]})
        os.environ["PATH"] = str(self.fake)
        with self.assertRaisesRegex(DurehError, "cannot detect the cluster"):
            cluster.detect(self.s)
        self.assertEqual(cluster.profile(self.s), {"scheduler": "slurm", "partitions": {}})

    def test_parse_time(self):
        self.assertEqual(cluster.parse_time("90"), timedelta(minutes=90))
        self.assertEqual(cluster.parse_time("04:00:00"), timedelta(hours=4))
        self.assertEqual(cluster.parse_time("1-12"), timedelta(days=1, hours=12))
        self.assertEqual(cluster.parse_time("2-00:30"), timedelta(days=2, minutes=30))
        self.assertIsNone(cluster.parse_time("infinite"))
        self.assertEqual(cluster.hms(7.5), "07:30:00")

    # ------------------------------------------------------------ session runtime
    def test_runtime_default_argv_and_template(self):
        rt = sessions.get(self.s)
        self.assertEqual(rt.name, "claude-code")
        self.assertTrue(rt.talks_to_director)
        # measured 2026-10-04: the flag form registers from a batch job; the name defaults to the site directory
        self.assertEqual(rt.launch_command(self.s, "desk"),
                         ["claude", "--remote-control", "site", "--permission-mode", "auto"])
        # Director, 2026-10-04: the day's session gets no first prompt (the team sends the start message to the
        # booted remote-control session); an overtime session gets "<desk name>, 추가근무 출근" from the persona
        self.assertIsNone(rt.first_prompt(self.s, "desk"))
        self.assertEqual(rt.first_prompt(self.s, "overtime"), "Tester, 추가근무 출근")
        self.assertEqual(rt.launch_command(self.s, "overtime", rt.first_prompt(self.s, "overtime")),
                         ["claude", "--remote-control", "site", "--permission-mode", "auto", "Tester, 추가근무 출근"])
        self.config(session={"command": ["/opt/bin/claude", "--verbose"],
                             "options": {"model": "opus", "remote_control": "camri-{purpose}"},
                             "flags": {"model": "--model"}, "prompts": {"desk": "{name}, 출근 ({site})", "overtime": False}})
        rt = sessions.get(self.s)
        self.assertEqual(rt.first_prompt(self.s, "desk"), "Tester, 출근 (site)")
        self.assertIsNone(rt.first_prompt(self.s, "overtime"))
        self.assertEqual(rt.launch_command(self.s, "desk", rt.first_prompt(self.s, "desk")),
                         ["/opt/bin/claude", "--verbose", "--remote-control", "camri-desk", "--permission-mode", "auto",
                          "--model", "opus", "Tester, 출근 (site)"])
        self.assertTrue(any("not on PATH" in p for p in rt.check(self.s)))
        self.config(session={"options": {"effort": "high"}})
        with self.assertRaisesRegex(DurehError, "effort have no flag"):
            sessions.get(self.s).launch_command(self.s, "desk")

    def test_runtime_check_finds_command_on_path(self):
        self.tool("claude", "exit 0\n")
        self.assertEqual(sessions.get(self.s).check(self.s), [])

    def test_unknown_runtime_names_available(self):
        self.config(session={"runtime": "other-cli"})
        with self.assertRaisesRegex(DurehError, "unknown session runtime 'other-cli'.*available: claude-code"):
            sessions.get(self.s)

    # ------------------------------------------------------------ session plan
    def test_session_plan_prints_script(self):
        self.config(session={"slurm": {"cpus": 4, "mem": "16g"}},
                    cluster={"partitions": {"general": {"nodes": 1, "time_limit": "11-00:00:00", "default": True}}})
        rc, out = self.cli("site", "session", "plan")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("#!/bin/bash\n#SBATCH --job-name=cc-site\n"))
        self.assertIn("#SBATCH --time=24:00:00\n", out)  # 8 h day + 16 h overtime allowance, capped at 24 h
        self.assertIn("#SBATCH --account=acct-x\n", out)
        self.assertIn("#SBATCH --partition=general\n", out)
        self.assertIn("#SBATCH --cpus-per-task=4\n", out)
        self.assertIn("#SBATCH --mem=16g\n", out)
        self.assertNotIn("--gres", out)
        self.assertIn(f"#SBATCH --chdir={self.site_root}\n", out)
        self.assertIn(f"#SBATCH --output={self.site_root}/logs/session/%x-%j.out", out)
        self.assertIn(f"export DUREH_SITE={self.site_root}", out)
        # measured 2026-10-04 (labs/session-test): scrub TMUX, private tmux socket, flag form, hold while it lives
        self.assertIn("\nunset TMUX TMUX_PANE", out)
        self.assertIn("\nSOCK=dureh-", out)
        self.assertIn('tmux -L "$SOCK" new-session -d -s "$NAME" -x 200 -y 50 -c ', out)
        self.assertIn("CMD='claude --remote-control site --permission-mode auto'\n", out)
        self.assertIn("# first prompt: none\n", out)
        self.assertNotIn("claude remote-control", out)
        self.assertTrue(out.rstrip().endswith('while tmux -L "$SOCK" has-session -t "$NAME" 2>/dev/null; do sleep 20; done'))
        self.assertFalse((self.site_root / "logs").exists(), "a plan creates nothing")

    def test_session_plan_defaults_without_session_config(self):
        out = sessions.plan(self.s)  # the company config has only `slurm`
        self.assertIn("#SBATCH --cpus-per-task=2\n#SBATCH --mem=16G\n", out)
        self.assertIn("#SBATCH --account=acct-x\n#SBATCH --partition=general\n", out)
        self.assertIn("--remote-control site --permission-mode auto'", out)

    # ------------------------------------------------------------ first prompt (desk persona registry)
    def persona(self, data):
        p = self.site_root / "state" / "persona" / "persona.json"
        if data is None:
            p.unlink()
        else:
            p.write_text(json.dumps(data, ensure_ascii=False))

    def cmd_argv(self, script: str) -> list:
        """What the generated script's CMD line gives bash: the prompt must survive the quoting."""
        line = next(l for l in script.splitlines() if l.startswith("CMD="))
        got = subprocess.run(["bash", "-c", line + '; printf "%s" "$CMD"'], capture_output=True, text=True, check=True).stdout
        return shlex.split(got)

    def test_overtime_session_first_prompt_from_the_persona_name(self):
        self.persona({"name": "서지아", "recorded_at": "x"})
        out = sessions.plan(self.s, "overtime")
        self.assertIn("#SBATCH --job-name=cc-site-overtime\n", out)
        self.assertEqual(self.cmd_argv(out), ["claude", "--remote-control", "site", "--permission-mode", "auto", "서지아, 추가근무 출근"])
        self.assertIn("# first prompt: '서지아, 추가근무 출근'\n", out)
        self.assertEqual(self.cmd_argv(sessions.plan(self.s, "desk"))[-1], "auto", "the day's session: no prompt")
        self.persona({"name": "O'Brien \"Z\" $USER"})  # quoting survives shell metacharacters
        self.assertEqual(self.cmd_argv(sessions.plan(self.s, "overtime"))[-1], "O'Brien \"Z\" $USER, 추가근무 출근")
        rc, out = self.cli("site", "session", "plan", "--purpose", "overtime")
        self.assertEqual(rc, 0)
        self.assertEqual(self.cmd_argv(out)[-1], "O'Brien \"Z\" $USER, 추가근무 출근")

    def test_overtime_first_prompt_without_a_persona_name(self):
        for data in ({"lab": "CAMRI"}, {"name": "  "}, None):
            self.persona(data)
            self.assertEqual(self.cmd_argv(sessions.plan(self.s, "overtime"))[-1], "데스크, 추가근무 출근", data)
        (self.site_root / "state" / "persona" / "persona.json").write_text("{not json")
        self.assertEqual(self.cmd_argv(sessions.plan(self.s, "overtime"))[-1], "데스크, 추가근무 출근")

    def test_first_prompt_config_and_overrides(self):
        self.config(session={"prompts": {"overtime": False, "desk": "{name}, 출근"}})
        self.assertEqual(self.cmd_argv(sessions.plan(self.s, "overtime")), ["claude", "--remote-control", "site", "--permission-mode", "auto"])
        self.assertIn("# first prompt: none\n", sessions.plan(self.s, "overtime"))
        self.assertEqual(self.cmd_argv(sessions.plan(self.s, "desk"))[-1], "Tester, 출근")
        self.config(session={"prompts": {"overtime": ""}})  # empty: none too
        self.assertEqual(self.cmd_argv(sessions.plan(self.s, "overtime"))[-1], "auto")
        self.config(session={})
        rc, out = self.cli("site", "session", "plan", "--purpose", "overtime", "--no-prompt")
        self.assertEqual(rc, 0)
        self.assertEqual(self.cmd_argv(out)[-1], "auto")
        rc, out = self.cli("site", "session", "plan", "--prompt", "지아, 오늘은 보고서부터")
        self.assertEqual(rc, 0)
        self.assertEqual(self.cmd_argv(out)[-1], "지아, 오늘은 보고서부터")
        self.assertIn("# first prompt: '지아, 오늘은 보고서부터'\n", out)
        with self.assertRaisesRegex(DurehError, "unknown session purpose 'night'.*desk, overtime"):
            sessions.plan(self.s, "night")

    def test_session_plan_wall_time_from_overtime_allowance(self):
        self.config(workday={"overtime_hours": 2})
        self.assertIn("#SBATCH --time=10:00:00\n", sessions.plan(self.s))
        self.config(workday={"hours": 6, "overtime_hours": 0})
        self.assertIn("#SBATCH --time=06:00:00\n", sessions.plan(self.s))
        self.config(workday={"hours": 10, "overtime_hours": 20})
        self.assertIn("#SBATCH --time=24:00:00\n", sessions.plan(self.s))
        self.config(workday={"overtime_hours": 2},
                    cluster={"partitions": {"general": {"time_limit": "04:00:00"}}})
        self.assertIn("time limit 04:00:00 is shorter", sessions.plan(self.s))

    def test_session_script_exports_the_pinned_harness(self):
        # 2026-10-04: the first live script exported the upstream clone (the tree running `session start`) while
        # the site ran releases/dureh-r2026.10.03-14; DUREH_ROOT must be config.json harness.root
        pinned = self.tmp / "releases" / "dureh-r2026.10.03-14"
        pinned.mkdir(parents=True)
        self.config(harness={"root": str(pinned)})
        self.assertEqual(self.s.pinned_harness_root, pinned)
        self.assertEqual(self.s.harness_root, HARNESS_ROOT)  # the running tree is still what D1 compares against
        for running in (None, str(HARNESS_ROOT), str(pinned)):  # whatever DUREH_ROOT the caller's shell carries
            if running is None:
                os.environ.pop("DUREH_ROOT", None)
            else:
                os.environ["DUREH_ROOT"] = running
            out = sessions.plan(self.s)
            self.assertIn(f"\nexport DUREH_ROOT={pinned}\n", out, f"caller DUREH_ROOT={running}")
            self.assertIn(f"\nexport DUREH_SITE={self.site_root}\n", out)
            self.assertNotIn(f"export DUREH_ROOT={HARNESS_ROOT}\n", out)
            self.assertIn(f"# harness: {pinned} (config.json harness.root; exported as DUREH_ROOT)\n", out)
        rc, out = self.cli("site", "session", "plan")
        self.assertIn(f"\nexport DUREH_ROOT={pinned}\n", out)

    def test_session_script_exports_the_running_harness_when_none_is_pinned(self):
        cfg = json.loads((self.site_root / "config.json").read_text())
        self.assertEqual(cfg["harness"]["root"], str(HARNESS_ROOT))  # install pins the installing tree
        self.assertIn(f"\nexport DUREH_ROOT={HARNESS_ROOT}\n", sessions.plan(self.s))
        del cfg["harness"]
        (self.site_root / "config.json").write_text(json.dumps(cfg, indent=2))
        self.s = self.site()
        self.assertEqual(self.s.pinned_harness_root, HARNESS_ROOT)
        os.environ["DUREH_ROOT"] = str(HARNESS_ROOT)  # already in the caller's environment: still exported
        self.assertIn(f"\nexport DUREH_ROOT={HARNESS_ROOT}\n", sessions.plan(self.s))

    # ------------------------------------------------------------ submit check against the day
    def _on_duty(self):
        os.environ.pop("CLAUDECODE", None)
        os.environ["DUREH_USAGE_CACHE"] = str(self.tmp / "usage-cache.json")
        os.environ["DUREH_FAKE_NOW"] = T0.isoformat()
        (self.tmp / "transcripts").mkdir()
        self.config(workday={"transcripts": [str(self.tmp / "transcripts")]})
        workday.switch(self.s, True, "test")
        workday.plan(self.s, "desk", "goal", "plan")
        workday.approve(self.s, "desk", "ok")  # 09:00-17:00 -04:00
        os.environ["DUREH_FAKE_NOW"] = (T0 + timedelta(hours=1)).isoformat()

    def _job(self):
        w = wf.wi_new(self.s, "desk", "run", "g", "researcher", "site", ["a"])["id"]
        wf.wi_approve(self.s, "desk", w, "ok")
        Ledger(self.s).event("wi.dispatched", "desk", w, {"role": "researcher"})
        script = self.tmp / "job.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        return w, str(script)

    def _sbatch(self, start):
        self.tool("sbatch", f"echo \"$@\" >> {self.tmp}/sbatch-calls\n"
                            "case \"$*\" in *--test-only*) "
                            f"echo 'sbatch: Job 99 to start at {start} using 1 processors on nodes c1 in partition general' >&2;"
                            " exit 0;; esac\necho 4242\n")

    def test_submit_refused_when_job_ends_after_the_day(self):
        self._on_duty()
        w, script = self._job()
        self._sbatch("2026-10-03T14:00:00")
        n = len(Ledger(self.s).load("run"))
        with self.assertRaisesRegex(DurehError, "after today's end 2026-10-03T17:00:00-04:00"):
            ops.slurm_submit(self.s, "desk", w, script, extra=["--time=04:00:00"])
        self.assertEqual(len(Ledger(self.s).load("run")), n, "no run recorded")
        calls = (self.tmp / "sbatch-calls").read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn("--test-only", calls[0])
        self.assertFalse((self.site_root / "logs" / "slurm" / w).exists())

    def test_submit_allowed_when_job_fits_the_day(self):
        self._on_duty()
        w, script = self._job()
        self._sbatch("2026-10-03T14:00:00")
        self.config(slurm={"account": "acct-x", "partition": "general", "time": "02:30:00"})
        run = ops.slurm_submit(self.s, "desk", w, script)
        self.assertEqual(run["payload"]["job_id"], "4242")
        self.assertEqual(run["payload"]["start_check"]["expected_end"], "2026-10-03T16:30:00-04:00")
        self.assertEqual((run["payload"]["start_check"]["time"], run["payload"]["start_check"]["time_source"]),
                         ("02:30:00", "config"))
        run = ops.slurm_submit(self.s, "desk", w, script, extra=["-t", "1:00:00"])
        self.assertEqual(run["payload"]["start_check"]["expected_end"], "2026-10-03T15:00:00-04:00")
        self.assertEqual(run["payload"]["start_check"]["time_source"], "sbatch-arg")

    def test_submit_check_uses_the_scripts_time_over_config(self):
        # UR-20261003-09: the check estimated the end from config slurm.time and ignored the script's directive
        self._on_duty()
        w, script = self._job()
        self._sbatch("2026-10-03T14:00:00")
        self.config(slurm={"account": "acct-x", "partition": "general", "time": "01:00:00"})
        Path(script).write_text("#!/bin/sh\n#SBATCH --time=04:00:00\necho hi\n")
        with self.assertRaisesRegex(DurehError, r"--time 04:00:00 \(from the script\).*after today's end"):
            ops.slurm_submit(self.s, "desk", w, script)
        self.assertNotIn("--time=01:00:00", (self.tmp / "sbatch-calls").read_text(), "config time is only a fallback")
        Path(script).write_text("#!/bin/sh\n#SBATCH -t 02:00:00\necho hi\n")
        run = ops.slurm_submit(self.s, "desk", w, script)
        self.assertEqual(run["payload"]["start_check"]["expected_end"], "2026-10-03T16:00:00-04:00")
        self.assertEqual((run["payload"]["start_check"]["time"], run["payload"]["start_check"]["time_source"]),
                         ("02:00:00", "script"))
        self.assertNotIn("--time=", " ".join(run["payload"]["command"]))
        run = ops.slurm_submit(self.s, "desk", w, script, extra=["--time=00:30:00"])
        self.assertEqual(run["payload"]["start_check"]["time_source"], "sbatch-arg")
        self.assertEqual(run["payload"]["start_check"]["expected_end"], "2026-10-03T14:30:00-04:00")

    def test_submit_check_unavailable_or_skipped(self):
        self._on_duty()
        w, script = self._job()
        self.tool("sbatch", "case \"$*\" in *--test-only*) echo 'error: no' >&2; exit 1;; esac\necho 4242\n")
        run = ops.slurm_submit(self.s, "desk", w, script, extra=["--time=04:00:00"])
        self.assertEqual(run["payload"]["start_check"], "unavailable")
        self.assertEqual(run["payload"]["job_id"], "4242")
        run = ops.slurm_submit(self.s, "desk", w, script)  # no --time anywhere
        self.assertIn("skipped", run["payload"]["start_check"])

    def test_submit_check_for_an_array_uses_the_per_task_time(self):
        # release 26: one --test-only for the whole array; the expected end is its start plus one task's --time
        self._on_duty()
        w, script = self._job()
        Path(script).write_text("#!/bin/sh\n#SBATCH --time=02:00:00\necho $DUREH_ITER\n")
        self._sbatch("2026-10-03T14:00:00")
        run = ops.slurm_submit(self.s, "desk", w, script, each="sub-01,sub-02,sub-03,sub-04,sub-05")
        chk = run["payload"]["start_check"]
        self.assertEqual((chk["expected_end"], chk["time_source"], chk["per_task"]),
                         ("2026-10-03T16:00:00-04:00", "script", True))
        calls = (self.tmp / "sbatch-calls").read_text().splitlines()
        self.assertEqual([("--test-only" in c, "--array=0-4" in c) for c in calls], [(True, True), (False, True)])
        before = sorted((self.site_root / "logs" / "slurm" / w).glob("each-*"))
        with self.assertRaisesRegex(DurehError, r"this array's first task .*--time 04:00:00 \(from the sbatch-arg, "
                                                r"per task\).*after today's end"):
            ops.slurm_submit(self.s, "desk", w, script, extra=["--time=04:00:00"], each="sub-01,sub-02")
        self.assertEqual(list((self.site_root / "logs" / "slurm" / w).glob("each-*")), before, "refusal leaves nothing")

    def test_submit_not_checked_when_workday_off(self):
        w, script = self._job()
        self._sbatch("2026-10-03T23:00:00")
        run = ops.slurm_submit(self.s, "desk", w, script, extra=["--time=10:00:00"])
        self.assertNotIn("start_check", run["payload"])
        self.assertNotIn("--test-only", (self.tmp / "sbatch-calls").read_text())

    # ------------------------------------------------------------ job directory (UR-20261003-08)
    def test_submit_runs_in_the_wi_target_root(self):
        mod = self.make_module()
        w = wf.wi_new(self.s, "desk", "run", "g", "researcher", "modules/demo", ["a"])["id"]
        wf.wi_approve(self.s, "desk", w, "ok")
        Ledger(self.s).event("wi.dispatched", "desk", w, {"role": "researcher"})
        self.tool("sbatch", f"echo \"$@\" >> {self.tmp}/sbatch-calls\necho 4243\n")
        script = self.tmp / "job.sh"
        script.write_text("#!/bin/sh\n#SBATCH -o checkpoints/phase-d/%x-%j.out\necho hi\n")
        run = ops.slurm_submit(self.s, "desk", w, str(script))
        p = run["payload"]
        self.assertEqual(p["chdir"], str(mod))
        self.assertIn(f"--chdir={mod}", (self.tmp / "sbatch-calls").read_text())
        logs = f"{self.site_root}/logs/slurm/{w}"
        # release 32: the script's relative -o keeps only its name, inside the site's log dir
        self.assertEqual(p["log_out"], f"{logs}/%x-%j.out")
        self.assertEqual(p["log_err"], p["log_out"])  # stderr follows -o when no -e is given
        self.assertIn(f"--output={logs}/%x-%j.out", (self.tmp / "sbatch-calls").read_text())
        self.assertNotIn("--error=", (self.tmp / "sbatch-calls").read_text())
        own = self.tmp / "own.sh"
        own.write_text(f"#!/bin/sh\n#SBATCH --chdir={self.tmp}\n#SBATCH --output=o.out\n#SBATCH -e e.err\necho hi\n")
        p = ops.slurm_submit(self.s, "desk", w, str(own))["payload"]
        self.assertEqual(p["chdir"], str(self.tmp))
        self.assertNotIn("--chdir", (self.tmp / "sbatch-calls").read_text().splitlines()[-1].replace(str(own), ""))
        self.assertEqual((p["log_out"], p["log_err"]), (f"{logs}/o.out", f"{logs}/e.err"))

    def test_submit_site_target_runs_in_site_root_with_our_logs(self):
        w, script = self._job()
        self.tool("sbatch", f"echo \"$@\" >> {self.tmp}/sbatch-calls\necho 4244\n")
        p = ops.slurm_submit(self.s, "desk", w, script)["payload"]
        self.assertEqual(p["chdir"], str(self.site_root))
        self.assertEqual(p["log_out"], f"{self.site_root}/logs/slurm/{w}/%x-%j.out")
        p = ops.slurm_submit(self.s, "desk", w, script, extra=["-D", str(self.tmp)], dry_run=True)["payload"]
        self.assertEqual(p["chdir"], str(self.tmp))
        self.assertEqual(p["command"].count(f"--chdir={self.site_root}"), 0)
