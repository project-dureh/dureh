"""Release 45 (6e): a work item may run its declared checks in the verify process (`--checks-runner local`) when
every check matches config `checks.local_allow`, so a trivial grep check is not a scheduler job plus two checker
spawns. Without the key nothing changes. Fake sbatch/sacct only."""
import os

from helpers import SiteCase

from test_verify_slurm import VerifyChecksJob

from dureh_core import workflow as wf
from dureh_core.cli import main
from dureh_core.records import Ledger
from dureh_core.util import DurehError

ALLOW = ["grep -q *", "test -f *"]


class LocalChecks(SiteCase):
    tool, config, with_sbatch = VerifyChecksJob.tool, VerifyChecksJob.config, VerifyChecksJob.with_sbatch
    item = VerifyChecksJob.item

    def setUp(self):
        super().setUp()
        self.install()
        self.make_module()
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        # sbatch: logs its arguments, runs the job script at once in --chdir, prints a job id (as test_verify_slurm)
        self.tool("sbatch", f"echo \"$@\" >> {self.fake}/sbatch.args\n"
                            "dir=.; for a in \"$@\"; do case \"$a\" in --chdir=*) dir=${a#--chdir=};; esac; done\n"
                            "(cd \"$dir\" && bash \"${@: -1}\" > /dev/null 2>&1)\necho 5100\n")
        self.tool("sacct", "echo 'COMPLETED|0:0|00:00:02'\n")
        self.s = self.site()
        self.led = Ledger(self.s)
        self.bundle = wf.wi_new(self.s, "desk", "Bundle", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", self.bundle, "ok")
        self.led.event("wi.dispatched", "desk", self.bundle, {"role": "pm-modules", "brief": "0" * 16, "by": "desk"})
        self.wi = self.item(checks=["python3 -m unittest discover -s tests -q"])
        self.with_sbatch(runner="slurm", interval=1, local_allow=ALLOW)

    def local_item(self, checks=("test -f src/demo.py", "grep -q 'def f' src/demo.py")):
        wi = wf.wi_new(self.s, "desk", "Doc check", "g", "py-writer", "modules/demo", ["ok"], parent=self.bundle,
                       checks=list(checks), checks_runner_choice="local")["id"]
        wf.wi_approve(self.s, "desk", wi, "ok")
        wf.dispatch_brief(self.s, wi, "pm-modules")
        self.led.event("wi.dispatched", "desk", wi, {"role": "py-writer", "brief": "1" * 16, "by": "desk"})
        wf.wi_stage(self.s, "py-writer", wi, "done")
        return wi

    def test_local_item_runs_in_process_and_records_its_source(self):
        wi = self.local_item()
        r = wf.verify_wi(self.s, "checker", wi)
        self.assertEqual((r["result"], r["runner"], r["runner_source"]), ("pass", "local", "wi"))
        self.assertFalse((self.fake / "sbatch.args").exists(), "no scheduler job")
        run = self.led.get("run", r["run"])
        self.assertEqual(run["payload"]["runner_source"], "wi")

    def test_submit_verifies_at_once_and_the_brief_says_foreground(self):
        wi = self.local_item()
        brief = wf.dispatch_brief(self.s, wi, "pm-modules", as_role="checker")["brief"]
        self.assertIn(f"verify --wi {wi}` in the foreground", brief)
        self.assertNotIn("--submit", brief)
        r = wf.verify_submit(self.s, "checker", wi)
        self.assertEqual((r["submitted"], r["result"], r["runner"]), (False, "pass", "local"))

    def test_other_items_keep_the_site_runner(self):
        r = wf.verify_wi(self.s, "checker", self.wi)
        self.assertEqual((r["runner"], r["runner_source"]), ("slurm", "site"))
        self.assertTrue((self.fake / "sbatch.args").exists())

    def test_choice_refused_unless_every_check_is_allowed(self):
        for checks in (["python3 -m pytest"], ["grep -q x a.md; rm -rf b"], ["grep -q x a.md | head"],
                       ["test -f a && touch b"], ["grep -q x $(ls)"]):
            with self.assertRaises(DurehError, msg=checks):
                wf.wi_new(self.s, "desk", "t", "g", "py-writer", "modules/demo", ["ok"], parent=self.bundle,
                          checks=checks, checks_runner_choice="local")

    def test_choice_refused_without_local_allow(self):
        self.with_sbatch(runner="slurm")
        with self.assertRaises(DurehError) as cm:
            wf.wi_new(self.s, "desk", "t", "g", "py-writer", "modules/demo", ["ok"], parent=self.bundle,
                      checks=["grep -q x a.md"], checks_runner_choice="local")
        self.assertIn("checks.local_allow", str(cm.exception))

    def test_config_change_falls_back_to_the_site_runner(self):
        wi = self.local_item()
        self.with_sbatch(runner="slurm", interval=1)  # local_allow removed after the choice
        r = wf.verify_wi(self.s, "checker", wi)
        self.assertEqual((r["runner"], r["runner_source"]), ("slurm", "site"))
        self.assertIn("no longer match", r["runner_note"])

    def test_amend_runner_on_a_staged_item_and_recheck_amended_checks(self):
        wi = self.wi  # staged, site runner, checks not allowed locally
        with self.assertRaises(DurehError):
            wf.wi_amend(self.s, "desk", wi, "ok", {"checks_runner": "local"})
        other = self.item(checks=["test -f src/demo.py"])
        wf.wi_amend(self.s, "desk", other, "Director: run it locally", {"checks_runner": "local"})
        self.assertEqual(wf.verify_wi(self.s, "checker", other)["runner_source"], "wi")
        local = self.local_item()
        wf.dispatch_stop(self.s, "desk", local, "change checks")
        with self.assertRaises(DurehError):  # amended checks of a local item are checked again
            wf.wi_amend(self.s, "desk", local, "ok", {"checks": ["python3 -m pytest"]})
        wf.wi_amend(self.s, "desk", local, "ok", {"checks": ["python3 -m pytest"], "checks_runner": "site"})

    def test_cli(self):
        os.environ["DUREH_SITE"] = str(self.site_root)
        self.assertEqual(main(["wi", "new", "--title", "t", "--goal", "g", "--role", "doc-writer", "--target",
                               "modules/demo", "--acceptance", "ok", "--check", "test -f README.md",
                               "--checks-runner", "local"]), 0)


del VerifyChecksJob  # its tests run in their own module
