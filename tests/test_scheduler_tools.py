"""Director 2026-10-09, compute route B: a company's own framework schedules its own compute. A package declares
`scheduler_tools:` ({name, commands, exclude, job_match, description}). Its commands keep working for every agent
(no capability gate: the framework submits its own jobs), `exclude` globs are refused for everyone, direct
sbatch/srun and container runtimes stay denied. The tools' jobs are the package's: `status` lists them as `Package jobs` (no run records),
`slurm.external_match` defaults to the union of `job_match`, and `day close` warns while any runs."""
import contextlib
import io
import json

from dureh_core import ops
from dureh_core.ops import render_status, status
from dureh_core.org import HARNESS_ROOT, Org
from dureh_core.package import check
from dureh_core.records import Ledger
from dureh_core.util import DurehError
from tests.helpers import EXAMPLE_PACKAGE, ExampleSiteCase, denied, hook
from dureh_core import yamlsub

TOOLS = """scheduler_tools:
  - name: exflow
    description: the example framework's workflow runner
    commands: ["exflow workflow *", "exflow status *", "uv run * exflow workflow *"]
    exclude: ["exflow run-inner *"]
    job_match: ["exflow-*"]
"""


class SchedulerTools(ExampleSiteCase):
    def setUp(self):
        super().setUp()
        m = self.package / "startup.yaml"
        m.write_text(m.read_text() + TOOLS)
        self.install()
        self.s = self.site()

    def bash(self, command, role):
        data = dict(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": command},
                    cwd=str(self.site_root))
        if role != "desk":
            data.update(agent_type=role, agent_id="a1")
        return hook(self.s, **data)

    def test_declared_commands_work_for_every_agent(self):
        settings = json.loads((self.site_root / ".claude" / "settings.json").read_text())
        from fnmatch import fnmatchcase

        for cmd in ("exflow workflow run --steps a", "exflow status --all", "uv run --frozen exflow workflow run x",
                    "cd projects/p1 && exflow workflow run x", "EXFLOW_PROFILE=slurm exflow workflow run x",
                    "timeout 60 exflow status x"):
            for role in ("runner", "builder", "checker", "scout", "coordinator", "desk"):
                self.assertFalse(denied(self.bash(cmd, role)), (role, cmd))
            for rule in settings["permissions"]["deny"]:
                self.assertFalse(fnmatchcase(cmd, rule[len("Bash("):-1]), (cmd, rule))
        self.assertFalse([e for e in Ledger(self.s).events() if e["type"] == "guard.denied"])
        self.assertNotIn("scheduler_tools", self.s.org.role("runner").capabilities)

    def test_exclude_wins_and_direct_scheduler_commands_stay_denied(self):
        for role in ("runner", "builder", "desk"):
            out = self.bash("exflow run-inner --task 3", role)
            self.assertTrue(denied(out), role)
            self.assertIn("internal", out["hookSpecificOutput"]["permissionDecisionReason"])
            for cmd in ("sbatch job.sh", "srun -n1 x", "apptainer run img.sif exflow workflow run"):
                self.assertTrue(denied(self.bash(cmd, role)), (role, cmd))
        self.assertFalse(denied(self.bash("exflow --version", "builder")), "a command the tool does not declare is plain")

    def test_texts_name_the_tools(self):
        md = (self.site_root / "CLAUDE.md").read_text()
        self.assertIn("Agents never call scheduler commands (`sbatch`, `srun`, ...) or container runtimes directly.", md)
        self.assertIn("or the package's scheduler tools: exflow (`exflow workflow *`, `exflow status *`,"
                      " `uv run * exflow workflow *`", md)
        self.assertIn("Never run `exflow run-inner *`", md)
        self.assertNotIn("Compute runs only as scheduler jobs", md)
        for role in ("runner", "builder"):
            text = (self.site_root / ".claude" / "agents" / f"{role}.md").read_text()
            self.assertIn("the package's scheduler tools (exflow: `exflow workflow *`", text)

    def fake_squeue(self, lines):
        fake = self.tmp / "fakebin"
        fake.mkdir(exist_ok=True)
        (fake / "squeue").write_text(f"#!/bin/sh\nprintf '%s' '{lines}'\n")
        (fake / "squeue").chmod(0o755)
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["env"] = {"path_prepend": [str(fake)]}
        cfg["slurm"]["external_jobs"] = True
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()

    def test_package_jobs_are_listed_without_records(self):
        self.fake_squeue("700001|exflow-step-a|RUNNING|0:10\n700002|manual|RUNNING|0:01\n700003|cc-x|RUNNING|1:00\n")
        runs = len(Ledger(self.s).load("run"))
        ext = ops.external_jobs(self.s)
        self.assertEqual([j["job_id"] for j in ext["tool_jobs"]], ["700001"])
        self.assertEqual(ext["count"], 0, "external_match defaults to the tools' job_match")
        text = render_status(status(self.s))
        self.assertIn("Package jobs (1, scheduler tools): 700001 exflow-step-a RUNNING 0:10 [exflow]", text)
        self.assertEqual(len(Ledger(self.s).load("run")), runs, "the harness writes no run record for them")
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["slurm"]["external_match"] = ["manual"]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        ext = ops.external_jobs(self.site())
        self.assertEqual(([j["job_id"] for j in ext["jobs"]], ext["tool_count"]), (["700002"], 1))

    def test_day_close_warns_while_package_jobs_run(self):
        from dureh_core import workday

        self.fake_squeue("700010|exflow-step-b|RUNNING|2:00:00\n")
        led = Ledger(self.s)
        plan = led.event("day.planned", "desk", None, {"goal": "g", "plan": "p"})
        d = {"phase": "off-duty", "report": "x", "plan": plan["id"], "start": "2026-10-09T09:00:00+00:00"}
        orig = (workday._require_on, workday.day, workday.device_on)
        workday._require_on, workday.day, workday.device_on = (lambda s: None), (lambda s, *a: d), (lambda *a: False)
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["session"] = {"release_on_close": False}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        try:
            rec = workday.close(self.site(), "desk", "done")
        finally:
            workday._require_on, workday.day, workday.device_on = orig
        self.assertIn("Package jobs (1, scheduler tools): 700010 exflow-step-b", rec["warning"])


class Validation(ExampleSiteCase):
    def m(self, tool):
        m = yamlsub.load_file(EXAMPLE_PACKAGE / "startup.yaml")
        m["scheduler_tools"] = [tool]
        return m

    def test_refusals(self):
        good = {"name": "exflow", "commands": ["exflow workflow *"], "job_match": ["exflow-*"]}
        self.assertEqual(check(self.m(good)), [])
        for bad, msg in (({**good, "commands": ["sbatch *"]}, "may not start with 'sbatch'"),
                         ({**good, "commands": ["apptainer run * exflow *"]}, "may not start with 'apptainer'"),
                         ({**good, "commands": ["/usr/bin/srun *"]}, "may not start with"),
                         ({**good, "commands": ["* exflow"]}, "must start with a command word"),
                         ({**good, "commands": []}, "non-empty list"),
                         ({**good, "commands": "exflow *"}, "non-empty list"),
                         ({**good, "job_match": []}, "job_match must be a non-empty list"),
                         ({**good, "exclude": "x"}, "exclude must be a list"),
                         ({**good, "colour": 1}, "must be {name")):
            with self.assertRaisesRegex(DurehError, msg):
                check(self.m(bad))
        m = self.m(good)
        m["roles"]["runner"]["capabilities"] = ["scheduler_tools"]
        with self.assertRaisesRegex(DurehError, "unknown capability 'scheduler_tools'"):
            Org.load(HARNESS_ROOT, manifest=m)

    def test_no_tools_keeps_todays_text(self):
        self.install()
        md = (self.site_root / "CLAUDE.md").read_text()
        self.assertIn("Compute runs only as scheduler jobs through", md)
        self.assertNotIn("Package jobs", md)
