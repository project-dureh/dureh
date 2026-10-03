"""D1: a site records the harness checkout it runs; adapter sync --harness switches to another one."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import SiteCase

from dureh_core import cli
from dureh_core.org import HARNESS_ROOT
from dureh_core.records import Ledger
from dureh_core.selftest import run_self
from dureh_core.site import Site

HARNESS = Path(__file__).resolve().parent.parent


class HarnessRoot(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()

    def run_cli(self, *argv):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = cli.main(["--site", str(self.site_root), *argv])
        return code, out.getvalue(), err.getvalue()

    def point_config_elsewhere(self):
        cfg = json.loads(self.s.config_path.read_text())
        cfg["harness"]["root"] = str(self.tmp / "elsewhere")
        self.s.config_path.write_text(json.dumps(cfg, indent=2))
        self.s = Site(self.site_root)

    def test_install_names_the_running_harness(self):
        self.assertEqual(self.s.config["harness"]["root"], str(HARNESS_ROOT))
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn(f"harness {' '.join(out.split('harness ', 1)[1].split()[:1])}", out)
        self.assertIn(f"at {HARNESS_ROOT}", out)

    def test_verify_self_fails_on_a_mismatch_and_sync_repairs_it(self):
        self.point_config_elsewhere()
        r = run_self(self.s, unit=False)
        self.assertEqual(r["result"], "fail")
        self.assertTrue(any("names harness" in p for p in r["problems"]), r["problems"])
        code, out, err = self.run_cli("adapter", "sync")
        self.assertEqual(code, 0, err)
        self.assertIn("(was ", out)
        cfg = json.loads(self.s.config_path.read_text())
        self.assertEqual(cfg["harness"]["root"], str(HARNESS_ROOT))
        self.assertEqual(list(cfg)[0], "schema_version", "key order is preserved")
        ev = [e for e in Ledger(Site(self.site_root)).load("event") if e["type"] == "deployment.updated"]
        self.assertEqual(ev[-1]["payload"]["previous"]["harness_root"], str(self.tmp / "elsewhere"))
        self.assertEqual(ev[-1]["payload"]["current"]["harness_root"], str(HARNESS_ROOT))
        self.assertEqual(run_self(Site(self.site_root), unit=False)["result"], "pass")

    def test_sync_harness_execs_that_checkouts_dureh(self):
        other = self.tmp / "release"
        (other / "dureh_core").mkdir(parents=True)
        log = self.tmp / "argv.log"
        (other / "dureh").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {log}\n")
        (other / "dureh").chmod(0o755)
        proc = subprocess.run([sys.executable, str(HARNESS / "dureh.py"), "--site", str(self.site_root),
                               "adapter", "sync", "--harness", str(other)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(log.read_text().split("\n")[:4], ["--site", str(self.site_root), "adapter", "sync"])

    def test_sync_harness_rejects_a_non_checkout(self):
        code, _, err = self.run_cli("adapter", "sync", "--harness", str(self.tmp))
        self.assertEqual(code, 2)
        self.assertIn("not a harness checkout", err)

    def test_sync_harness_with_the_running_checkout_is_a_plain_sync(self):
        code, out, err = self.run_cli("adapter", "sync", "--harness", str(HARNESS_ROOT))
        self.assertEqual(code, 0, err)
        self.assertIn("deployment pins", out)


if __name__ == "__main__":
    unittest.main()
