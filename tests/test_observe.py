"""`--observe`: upstream reads a company without changing it."""
import contextlib
import io
import os
import unittest

from helpers import SiteCase

from dureh_core import cli
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError


def snapshot(root):
    out = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


class Observe(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        self.wi = Ledger(self.s).work_items()[0]["id"]

    def run_cli(self, *argv):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = cli.main(["--site", str(self.site_root), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_read_commands_change_nothing_in_the_site(self):
        before = snapshot(self.site_root)
        for argv in (["status"], ["wi", "list"], ["wi", "show", "--wi", self.wi], ["usage"], ["upstream", "list"],
                     ["path-refs", str(self.site_root / "notes")], ["ledger", "query", "SELECT count(*) AS n FROM records"],
                     ["verify", "--self", "--skip-unit"]):
            code, _, err = self.run_cli("--observe", *argv)
            self.assertEqual(code, 0, f"{argv}: {err}")
        self.assertEqual(snapshot(self.site_root), before)

    def test_verify_self_records_no_run_and_uses_a_private_index(self):
        runs = len(Ledger(self.s).load("run"))
        shared = self.s.index_path()
        shared.unlink(missing_ok=True)
        code, out, err = self.run_cli("--observe", "verify", "--self", "--skip-unit")
        self.assertEqual(code, 0, err)
        self.assertIn('"result": "pass"', out)
        self.assertEqual(len(Ledger(self.s).load("run")), runs)
        self.assertFalse(shared.exists(), "observe must not build the site's shared index")

    def test_state_changing_commands_are_refused(self):
        n = len(Ledger(self.s).work_items())
        for argv in (["wi", "new", "--title", "t", "--goal", "g", "--role", "pm-modules", "--target", "site",
                      "--acceptance", "a"],
                     ["wi", "approve", "--wi", self.wi, "--memo", "m"],
                     ["dispatch", "brief", "--wi", self.wi],
                     ["ledger", "add", "finding", "--summary", "s", "--evidence-level", "measured"],
                     ["ledger", "index"], ["adapter", "sync"], ["verify", "--wi", self.wi]):
            code, _, err = self.run_cli("--observe", *argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("observe mode", err, argv)
        self.assertEqual(len(Ledger(self.s).work_items()), n)

    def test_environment_variable_enables_observe(self):
        os.environ["DUREH_OBSERVE"] = "1"
        code, _, err = self.run_cli("dispatch", "brief", "--wi", self.wi)
        self.assertEqual(code, 2)
        self.assertIn("observe mode", err)

    def test_record_layer_refuses_appends_in_observe(self):
        os.environ["DUREH_OBSERVE"] = "1"
        with self.assertRaisesRegex(DurehError, "observe mode"):
            Ledger(self.s).event("wi.approved", "desk", self.wi, {})
        with self.assertRaisesRegex(DurehError, "observe mode"):
            wf.wi_new(self.s, "desk", "t", "g", "pm-modules", "site", ["a"])


if __name__ == "__main__":
    unittest.main()
