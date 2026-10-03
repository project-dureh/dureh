"""Phase 1 acceptance criteria 1-4 and 6: install, events, validation, create-only records."""
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import HARNESS, SiteCase, copy_writable

from dureh_core.util import DurehError


def cli(*args, env=None):
    return subprocess.run([sys.executable, str(HARNESS / "dureh.py"), *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


class Install(SiteCase):
    def test_install_creates_fresh_site(self):
        r = self.install()
        root = self.site_root
        self.assertEqual(r["package"], "minimal-test")
        self.assertTrue((root / "startup" / "startup.yaml").is_file())
        for kind in ("work-item", "run", "decision", "session", "finding", "event"):
            self.assertTrue((root / "state" / "records" / kind).is_dir())
        dep = json.loads((root / "deployment.json").read_text())
        self.assertEqual(stat.S_IMODE((root / "deployment.json").stat().st_mode), 0o600)
        self.assertEqual(dep["package"]["id"], "minimal-test")
        self.assertIsNotNone(dep["state"]["rev"])
        self.assertEqual(json.loads((root / "state" / "persona" / "persona.json").read_text())["name"], "Tester")
        self.assertTrue((root / "references" / "old-org").is_symlink())
        self.assertTrue((root / ".claude" / "agents" / "py-writer.md").is_file())
        self.assertFalse((root / ".claude" / "agents" / "pm-projects.md").exists(), "roles not enabled by the package")
        self.assertEqual(r["work_items"], ["WI-0001"])
        self.assertEqual(r["selftest"]["result"], "pass", r["selftest"])
        log = subprocess.run(["git", "log", "--oneline"], cwd=root / "state", capture_output=True, text=True).stdout
        self.assertIn("initialize site state", log)

    def test_missing_manifest_and_nonempty_destination_fail_without_change(self):
        (self.package / "startup.yaml").unlink()
        with self.assertRaises(DurehError):
            self.install()
        self.assertFalse(self.site_root.exists())
        self.package = self.tmp / "package2"
        import shutil
        from helpers import FIXTURE_PACKAGE
        copy_writable(FIXTURE_PACKAGE, self.package)
        self.site_root.mkdir()
        (self.site_root / "keep.txt").write_text("x")
        with self.assertRaises(DurehError):
            self.install()
        self.assertEqual(sorted(p.name for p in self.site_root.iterdir()), ["keep.txt"])

    def test_package_with_state_or_symlink_is_rejected(self):
        (self.package / "state").mkdir()
        with self.assertRaisesRegex(DurehError, "site state"):
            self.install()
        (self.package / "state").rmdir()
        (self.package / "link").symlink_to(self.ref)
        with self.assertRaisesRegex(DurehError, "symlink"):
            self.install()
        self.assertFalse(self.site_root.exists())

    def test_missing_required_persona_answer_fails(self):
        with self.assertRaisesRegex(DurehError, "persona question 'name'"):
            self.install(persona={})


class Events(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()

    def test_event_roundtrip_via_cli(self):
        s = str(self.site_root)
        out = cli("--site", s, "event", "add", "--kind", "note.added", "--wi", "WI-0001", "--actor", "desk",
                  "--payload", '{"title": "Review"}')
        self.assertEqual(out.returncode, 0, out.stderr)
        stored = json.loads(out.stdout)
        q = cli("--site", s, "event", "query", "--wi", "WI-0001")
        rows = json.loads(q.stdout)
        self.assertIn(stored, rows)
        on_disk = json.loads((self.site_root / "state/records/event" / f"{stored['id']}.json").read_text())
        self.assertEqual(on_disk, stored)

    def test_missing_input_and_malformed_payload_fail(self):
        s = str(self.site_root)
        out = cli("--site", s, "event", "add", "--kind", "x.y", "--wi", "WI-0001", "--actor", "desk")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("--payload", out.stderr)
        out = cli("--site", s, "event", "add", "--kind", "x.y", "--wi", "WI-0001", "--actor", "desk", "--payload", "{bad")
        self.assertEqual(out.returncode, 2)
        self.assertIn("valid JSON", out.stderr)
        out = cli("--site", s, "event", "add", "--kind", "x.y", "--wi", "WI-0001", "--actor", "py-writer", "--payload", "{}")
        self.assertEqual(out.returncode, 2)
        self.assertIn("not authorized", out.stderr)
        out = cli("--site", s, "event", "add", "--kind", "x.y", "--wi", "WI-9999", "--actor", "desk", "--payload", "{}")
        self.assertIn("unknown work item", out.stderr)

    def test_env_identity_overrides_and_conflicts(self):
        s = str(self.site_root)
        out = cli("--site", s, "event", "add", "--kind", "x.y", "--wi", "WI-0001", "--actor", "desk", "--payload", "{}",
                  env={"DUREH_ACTOR": "py-writer"})
        self.assertIn("conflicts", out.stderr)

    def test_create_only_and_malformed_record_fails_visibly(self):
        from dureh_core.records import Ledger
        from dureh_core.util import write_create_only

        led = Ledger(self.site())
        ev = led.event("note.added", "desk", "WI-0001", {})
        path = self.site_root / "state/records/event" / f"{ev['id']}.json"
        before = path.read_bytes()
        with self.assertRaises(DurehError):
            write_create_only(path, b"{}")
        self.assertEqual(path.read_bytes(), before)
        (self.site_root / "state/records/event/ev-bad.json").write_text("{not json")
        out = cli("--site", str(self.site_root), "event", "query")
        self.assertEqual(out.returncode, 2)
        self.assertIn("ev-bad.json", out.stderr)

    def test_schema_rejects_bad_records(self):
        from dureh_core.records import Ledger

        led = Ledger(self.site())
        with self.assertRaises(DurehError):
            led.append("finding", "finding", "desk", None, {"summary": "", "evidence_level": "measured"})
        with self.assertRaises(DurehError):
            led.append("finding", "finding", "desk", None, {"summary": "x", "evidence_level": "vibes"})

    def test_index_is_node_local_and_queryable(self):
        from dureh_core.records import Ledger

        led = Ledger(self.site())
        info = led.build_index()
        self.assertFalse(Path(info["path"]).is_relative_to(self.site_root))
        rows = led.sql("SELECT id, status FROM work_items")
        self.assertEqual(rows, [{"id": "WI-0001", "status": "proposed"}])
        led.event("note.added", "desk", "WI-0001", {})
        self.assertFalse(led.index_fresh())
        self.assertEqual(len(led.sql("SELECT * FROM records WHERE type='note.added'")), 1)
        with self.assertRaises(DurehError):
            led.sql("DELETE FROM records")


if __name__ == "__main__":
    unittest.main()
