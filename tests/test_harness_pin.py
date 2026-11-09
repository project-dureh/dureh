"""Release 97 (2026-11-09: after `adapter sync` to a new release, a site's bin/dureh still execed the previous
release while config.json harness.root and deployment.json named the new one). Only the pinned harness regenerates
the site's files (`adapter sync` and `install` are the switch); verify --self and status report a bin/dureh,
config.json or deployment.json that disagree, with the one-line fix."""
import contextlib
import io
import json
import os
from pathlib import Path

from helpers import SiteCase
from test_permits import PermitBase

from dureh_core import cli, permits
from dureh_core.org import HARNESS_ROOT
from dureh_core.ops import render_status, status
from dureh_core.runtimes import claude_code
from dureh_core.selftest import bin_exec_root, harness_checks, run_self
from dureh_core.site import Site
from dureh_core.util import DurehError


def other_release(tmp: Path) -> Path:
    rel = tmp / "releases" / "dureh-r2099.01.01-1"
    (rel / "dureh_core").mkdir(parents=True)
    (rel / "dureh").write_text("#!/bin/sh\nexit 0\n")
    (rel / "dureh").chmod(0o755)
    return rel


def pin(site_root: Path, root: Path) -> None:
    path = site_root / "config.json"
    cfg = json.loads(path.read_text())
    cfg["harness"]["root"] = str(root)
    path.write_text(json.dumps(cfg, indent=2))


class GenerateFromThePin(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.bin = self.site_root / "bin" / "dureh"

    def test_generation_from_another_harness_follows_the_pin(self):
        """The measured case: a process of the previous release regenerates after the site moved on."""
        rel = other_release(self.tmp)
        pin(self.site_root, rel)  # config.json names the new release; this process (HARNESS_ROOT) is the old one
        self.assertNotEqual(rel, HARNESS_ROOT)
        claude_code.generate(Site(self.site_root))
        s = Site(self.site_root)
        self.assertEqual(bin_exec_root(s), rel, "bin/dureh follows config.json harness.root")
        settings = json.loads((self.site_root / ".claude" / "settings.json").read_text())
        self.assertEqual(settings["env"]["DUREH_ROOT"], str(rel))
        self.assertFalse(any(p.startswith("bin/dureh execs") for p in harness_checks(s)))

    def test_the_switch_names_the_running_harness(self):
        pin(self.site_root, other_release(self.tmp))
        claude_code.generate(Site(self.site_root), switch=True)  # adapter sync: it re-pins config below
        self.assertEqual(bin_exec_root(Site(self.site_root)), HARNESS_ROOT)

    def test_a_pin_without_a_harness_falls_back_to_the_running_tree(self):
        pin(self.site_root, self.tmp / "removed-release")
        claude_code.generate(Site(self.site_root))
        self.assertEqual(bin_exec_root(Site(self.site_root)), HARNESS_ROOT)

    def test_a_regenerating_command_from_another_harness_is_refused(self):
        """`site permit apply` and the module commands render this release's templates: refused up front."""
        pin(self.site_root, other_release(self.tmp))
        with self.assertRaisesRegex(DurehError, "only the pinned harness regenerates"):
            claude_code.require_pinned(Site(self.site_root))

    def test_adapter_sync_switches_back_to_the_running_harness(self):
        pin(self.site_root, other_release(self.tmp))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--site", str(self.site_root), "adapter", "sync"]), 0)
        s = Site(self.site_root)
        self.assertEqual(s.config["harness"]["root"], str(HARNESS_ROOT))
        self.assertEqual(bin_exec_root(s), HARNESS_ROOT)
        self.assertEqual(harness_checks(s), [])


class PermitFromThePin(PermitBase):
    def test_permit_apply_from_another_harness_writes_nothing(self):
        """The measured path: `site permit apply` (it writes config.json, then regenerates) run by the old release."""
        self.conf(modules=[{"id": "demo", "path": "modules/demo"}])
        pin(self.site_root, other_release(self.tmp))
        self.desk()
        before = (self.site_root / "config.json").read_text(), (self.site_root / "bin" / "dureh").read_text()
        with self.assertRaisesRegex(DurehError, "only the pinned harness regenerates"):
            permits.set_key(self.site(), "desk", "modules.demo.push", False, "push-off")
        self.assertEqual(((self.site_root / "config.json").read_text(), (self.site_root / "bin" / "dureh").read_text()),
                         before)


class PinChecks(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.bin = self.site_root / "bin" / "dureh"

    def s(self):
        return Site(self.site_root)

    def test_a_consistent_site_passes(self):
        self.assertEqual(harness_checks(self.s()), [])
        self.assertEqual(status(self.s())["harness_problems"], [])

    def test_bin_dureh_on_another_release_fails_verify_and_status(self):
        old = other_release(self.tmp)
        self.bin.write_text(self.bin.read_text().replace(str(HARNESS_ROOT / "dureh"), str(old / "dureh")))
        self.assertEqual(bin_exec_root(self.s()), old)
        fix = f"`{HARNESS_ROOT}/dureh --site {self.site_root} adapter sync`"
        r = run_self(self.s(), unit=False, record=False)
        self.assertEqual(r["result"], "fail")
        hit = [p for p in r["problems"] if p.startswith("bin/dureh execs")]
        self.assertEqual(len(hit), 1, r["problems"])
        self.assertIn(fix, hit[0])
        st = status(self.s())
        self.assertTrue(any(p.startswith("bin/dureh execs") for p in st["harness_problems"]))
        self.assertIn("HARNESS FAIL: bin/dureh execs", render_status(st))

    def test_a_deployment_pin_of_another_revision_fails(self):
        path = self.site_root / "deployment.json"
        dep = json.loads(path.read_text())
        dep["harness"]["rev"] = "0" * 40
        path.write_text(json.dumps(dep))
        problems = harness_checks(self.s())
        self.assertTrue(any(p.startswith(f"deployment.json pins harness {'0' * 12}") for p in problems), problems)

    def test_status_does_not_compare_with_the_reading_tree(self):
        """DUREH reads a company's status from its own tree: only the site's own three pins are compared there."""
        rel = other_release(self.tmp)
        pin(self.site_root, rel)
        self.bin.write_text(self.bin.read_text().replace(str(HARNESS_ROOT / "dureh"), str(rel / "dureh")))
        dep_path = self.site_root / "deployment.json"
        dep = json.loads(dep_path.read_text())
        dep["harness"]["rev"] = "not-a-git-repo"
        dep_path.write_text(json.dumps(dep))
        self.assertEqual(status(self.s())["harness_problems"], [])
        self.assertTrue(any("names harness" in p for p in harness_checks(self.s())), "verify --self still compares")
