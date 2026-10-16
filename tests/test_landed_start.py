"""Release 38: `site session start` applies a pending landed release checkout (the newest `landed` mailbox message)
before it submits the session job (Director, 2026-10-06: on 10-06 the desk booted on release 33 while 34 had landed)."""
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path

from helpers import SiteCase

from dureh_core import sessions
from dureh_core import upstream as up
from dureh_core.cli import main
from dureh_core.org import HARNESS_ROOT
from dureh_core.records import Ledger
from dureh_core.site import Site
from dureh_core.util import DurehError

SWITCH = """#!/bin/sh
printf '%s\\n' "$@" > {log}
python3 - <<'PY'
import json
p = {cfg!r}
c = json.load(open(p))
c.setdefault("harness", {{}})["root"] = {root!r}
json.dump(c, open(p, "w"), indent=2)
PY
echo "generated 9 files for claude-code"
echo "deployment pins: harness deadbeef package cafe"
echo "harness: deadbee (release r2026.10.05-8) at {root}"
exit {code}
"""


class LandedStart(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.x = self.tmp / "exchange"
        self.x.mkdir()
        os.environ["DUREH_TEST_EXCHANGE"] = str(self.x)
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        (self.fake / "sbatch").write_text(f"#!/bin/sh\necho \"$@\" >> {self.fake}/sbatch-calls\necho 'Submitted batch job 12345'\n")
        (self.fake / "squeue").write_text("#!/bin/sh\n")
        (self.fake / "claude").write_text("#!/bin/sh\nexit 0\n")
        for f in ("sbatch", "squeue", "claude"):
            (self.fake / f).chmod(0o755)
        self.config(upstream={"exchange": "${DUREH_TEST_EXCHANGE}"}, env={"path_prepend": [str(self.fake)]})
        self.box = self.x / self.site_root.name / "from-upstream"
        self.box.mkdir(parents=True)

    def config(self, **keys):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg.update(keys)
        (self.site_root / "config.json").write_text(json.dumps(cfg, indent=2))
        self.s = Site(self.site_root)
        return cfg

    def checkout(self, name="release-38", code=0, base=None, tag=None, commit=True, at="HEAD") -> Path:
        """A fake release checkout: `dureh_core/` and a `dureh` that logs its argv and re-pins config.json harness.root
        the way the real adapter sync does (then exits `code`). Release 70: a git repository sharing the objects of
        `base` (default the running harness), one empty commit on top of its `at` (`commit`), optionally tagged."""
        root = self.tmp / name
        base = base or HARNESS_ROOT
        git = lambda *a, cwd=root: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],  # noqa: E731
                                                   cwd=cwd, check=True, capture_output=True, text=True)
        if not (root / ".git").exists():
            git("clone", "-q", "--shared", "--no-checkout", str(base), str(root), cwd=self.tmp)
            git("checkout", "-q", "--detach", git("rev-parse", at, cwd=base).stdout.strip())
            if commit:
                git("commit", "-q", "--allow-empty", "-m", f"landed {name}")
            if tag:
                git("tag", "-f", tag)
        (root / "dureh_core").mkdir(parents=True, exist_ok=True)
        (root / "dureh").write_text(SWITCH.format(log=root / "argv.log", cfg=str(self.site_root / "config.json"),
                                                  root=str(root), code=code))
        (root / "dureh").chmod(0o755)
        return root

    def landed(self, checkout, mid="a94fde", stamp="20261005T212348Z", kind="landed", extra=""):
        name = f"{stamp}-{kind}-{mid}.md"
        (self.box / name).write_text(
            f"---\nid: {mid}\nsite: {self.site_root.name}\nfrom: upstream\nkind: {kind}\nrefs: [UR-20261005-02]\n"
            f"created: 2026-10-05T21:23:48Z\n---\nRelease 34 landed (r2026.10.05-8, cb7e040; checkout 503 OK).{extra}\n"
            f"Switch: 1) `bin/dureh adapter sync --harness {checkout}` 2) `bin/dureh verify --self` 3) notice.\n")
        return name

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(["--site", str(self.site_root), *args])
        return rc, out.getvalue() + err.getvalue()

    def argv_log(self, checkout: Path):
        log = checkout / "argv.log"
        return log.read_text().split("\n")[:4] if log.exists() else None

    def events(self, type_):
        return [e for e in Ledger(Site(self.site_root)).events() if e["type"] == type_]

    # ------------------------------------------------------- pending_landed_checkout
    def test_pending_picks_the_newest_landed_message(self):
        older, newer = self.checkout("release-37"), self.checkout("release-38")
        self.landed(older, mid="111111", stamp="20261004T100000Z")
        self.landed(newer, mid="222222", stamp="20261005T212348Z")
        self.landed(self.tmp / "ignored", mid="333333", stamp="20261006T000000Z", kind="notice")  # not a landed message
        p = up.pending_landed_checkout(self.s)
        self.assertEqual((p["id"], p["checkout"], p["file"], p["created"]),
                         ("222222", newer, "20261005T212348Z-landed-222222.md", "2026-10-05T21:23:48Z"))

    def test_pending_none_when_the_newest_landed_is_the_current_harness(self):
        # the running tree is a checkout (dureh + dureh_core/) and the site is pinned to it by install
        self.landed(self.checkout("release-37"), mid="111111", stamp="20261004T100000Z")  # older: never a downgrade
        self.landed(HARNESS_ROOT, mid="222222", stamp="20261005T212348Z")
        self.assertIsNone(up.pending_landed_checkout(self.s))

    def test_pending_none_when_the_path_is_not_a_checkout(self):
        self.landed(self.tmp / "missing", mid="111111")
        self.assertIsNone(up.pending_landed_checkout(self.s))
        bare = self.tmp / "bare"
        bare.mkdir()
        self.landed(bare, mid="222222", stamp="20261006T000000Z")
        self.assertIsNone(up.pending_landed_checkout(self.s), "a directory without dureh + dureh_core/ is not a checkout")

    def test_pending_none_without_exchange_or_landed_message(self):
        self.assertIsNone(up.pending_landed_checkout(self.s), "empty mailbox")
        self.landed(self.checkout(), extra="\nno switch line here", mid="444444")
        (self.box / "20261005T212348Z-landed-444444.md").write_text(
            f"---\nid: 444444\nsite: s\nfrom: upstream\nkind: landed\nrefs: []\ncreated: t\n---\nRelease 34 landed; nothing to switch.\n")
        self.assertIsNone(up.pending_landed_checkout(self.s), "a landed message without a checkout path")
        cfg = json.loads((self.site_root / "config.json").read_text())
        del cfg["upstream"]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.landed(self.checkout())
        self.assertIsNone(up.pending_landed_checkout(Site(self.site_root)), "no exchange configured")

    # ------------------------------------------------------- session start
    def test_start_applies_the_landed_release_before_submitting(self):
        co = self.checkout()
        self.landed(co)
        rc, out = self.cli("site", "session", "start")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.argv_log(co), ["--site", str(self.site_root), "adapter", "sync"])
        sw = self.events(sessions.SWITCHED)
        self.assertEqual(len(sw), 1)
        p = sw[0]["payload"]
        self.assertEqual((p["from"], p["to"], p["via"], p["landed"], p["landed_file"]),
                         (str(HARNESS_ROOT), str(co), "session start", "a94fde", "20261005T212348Z-landed-a94fde.md"))
        self.assertEqual(p["output_tail"][-1], f"harness: deadbee (release r2026.10.05-8) at {co}")
        self.assertEqual(p["command"], [str(co / "dureh"), "--site", str(self.site_root), "adapter", "sync"])
        started = self.events("session.started")
        self.assertEqual(len(started), 1)
        hs = started[0]["payload"]["harness_switch"]
        self.assertEqual((hs["from"], hs["to"], hs["landed"], hs["record"]), (str(HARNESS_ROOT), str(co), "a94fde", sw[0]["id"]))
        # the job was built after the switch: it exports the new checkout, not the one pinned before
        script = Path(started[0]["payload"]["script"]).read_text()
        self.assertIn(f"\nexport DUREH_ROOT={co}\n", script)
        self.assertIn(f"# harness: {co} ", script)
        calls = (self.fake / "sbatch-calls").read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn(f"switched harness {hs['from_label']} -> {hs['to_label']} (landed a94fde); recorded {sw[0]['id']} (harness.switched). "
                      "Submitted session job 12345", out)
        # a second look sees nothing pending: the site is on the landed checkout now
        self.assertIsNone(up.pending_landed_checkout(Site(self.site_root)))

    def test_start_stops_when_the_switch_fails(self):
        co = self.checkout(code=3)
        self.landed(co)
        with self.assertRaisesRegex(DurehError, r"(?s)could not be applied: .*adapter sync exited 3:\n.*deployment pins.*\nharness: .*"
                                                r"\nnothing was submitted; fix the switch \(or start with --no-apply-landed\)"):
            sessions.start(self.s, "desk")
        self.assertEqual(self.argv_log(co), ["--site", str(self.site_root), "adapter", "sync"])
        self.assertFalse((self.fake / "sbatch-calls").exists(), "nothing submitted")
        self.assertEqual(self.events("session.started"), [])
        self.assertEqual(self.events(sessions.SWITCHED), [])

    def test_no_apply_landed_skips_the_switch(self):
        co = self.checkout()
        self.landed(co)
        rec = sessions.start(self.s, "desk", apply_landed=False)
        self.assertIsNone(self.argv_log(co))
        self.assertNotIn("harness_switch", rec["payload"])
        self.assertIn(f"\nexport DUREH_ROOT={HARNESS_ROOT}\n", Path(rec["payload"]["script"]).read_text())
        self.assertEqual(self.events(sessions.SWITCHED), [])
        # the CLI flag, on a fresh day record (overtime purpose: not a second desk start)
        rc, out = self.cli("site", "session", "start", "--no-apply-landed", "--purpose", "overtime")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self.argv_log(co))
        self.assertNotIn("switched harness", out)
        self.assertIn("Submitted session job 12345", out)

    def test_start_without_a_pending_release_does_not_switch(self):
        co = self.checkout("release-37")
        self.landed(co, mid="111111", stamp="20261004T100000Z")
        self.landed(HARNESS_ROOT, mid="222222", stamp="20261005T212348Z")  # the newest names the current harness
        rc, out = self.cli("site", "session", "start")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self.argv_log(co), "an older landed message never pulls the site back")
        self.assertNotIn("switched harness", out)
        self.assertEqual(self.events(sessions.SWITCHED), [])

    def test_refusals_come_before_the_switch(self):
        co = self.checkout()
        self.landed(co)
        (self.fake / "squeue").write_text("#!/bin/sh\necho 'RUNNING|7:00:00'\n")
        sessions.start(self.s, "desk", apply_landed=False)
        with self.assertRaisesRegex(DurehError, "still in squeue"):
            sessions.start(self.s, "desk")
        self.assertIsNone(self.argv_log(co), "a refused start does not touch the harness")
        self.assertEqual(self.events(sessions.SWITCHED), [])

    def test_plan_mentions_the_pending_release(self):
        co = self.checkout()
        rc, out = self.cli("site", "session", "plan")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("will switch harness", out)
        self.landed(co)
        rc, out = self.cli("site", "session", "plan")
        self.assertEqual(rc, 0, out)
        from dureh_core.util import harness_label

        self.assertIn(f"will switch harness to {harness_label(co)} at {co} (landed a94fde) before submitting", out)
        self.assertIn("#!/bin/bash\n", out)
        self.assertIsNone(self.argv_log(co), "plan switches nothing")
        self.assertNotIn("will switch harness", self.cli("site", "session", "plan", "--no-apply-landed")[1])

    # ------------------------------------------------------- release order (release 70)
    def pin(self, checkout: Path):
        """Pin the site to `checkout` as its current harness (what adapter sync records in config.json)."""
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg.setdefault("harness", {})["root"] = str(checkout)
        (self.site_root / "config.json").write_text(json.dumps(cfg, indent=2))
        self.s = Site(self.site_root)

    def test_an_older_landed_release_is_skipped_recorded_and_not_applied_again(self):
        current = self.checkout("r-current", tag="r2026.10.15-3")
        older = self.checkout("r-older", tag="r2026.10.13-2", commit=False)  # the parent of the current release
        self.pin(current)
        self.landed(older, mid="b01a72")
        rc, out = self.cli("site", "session", "plan")
        self.assertIn("will skip landed b01a72: r2026.10.13-2 is not newer than r2026.10.15-3", out)
        rc, out = self.cli("site", "session", "start")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self.argv_log(older), "never a downgrade")
        self.assertIn("skipped landed b01a72: r2026.10.13-2 is not newer than r2026.10.15-3 (release tag order); recorded",
                      out)
        self.assertEqual(self.events(sessions.SWITCHED), [])
        sk = self.events(up.LANDED_SKIPPED)
        self.assertEqual(len(sk), 1)
        self.assertEqual((sk[0]["payload"]["landed"], sk[0]["payload"]["to"], sk[0]["payload"]["from_release"]),
                         ("b01a72", str(older), "r2026.10.15-3"))
        started = self.events("session.started")[-1]["payload"]
        self.assertEqual(started["harness_switch_skipped"]["record"], sk[0]["id"])
        self.assertIn(f"\nexport DUREH_ROOT={current}\n", Path(started["script"]).read_text())
        self.assertIsNone(up.pending_landed_checkout(Site(self.site_root)), "the skipped message is consumed")
        rc, out = self.cli("site", "session", "start", "--purpose", "overtime")
        self.assertNotIn("skipped landed", out, "reported once, not applied later")
        self.assertEqual(len(self.events(up.LANDED_SKIPPED)), 1)

    def test_a_newer_landed_release_is_applied(self):
        current = self.checkout("r-current", tag="r2026.10.13-2")
        newer = self.checkout("r-newer", base=current, tag="r2026.10.15-3")
        self.pin(current)
        self.landed(newer)
        order = up.release_order(current, newer)
        self.assertTrue(order["newer"], order)
        self.assertEqual(order["reason"], "r2026.10.15-3 is newer than r2026.10.13-2 (contains it)")
        rc, out = self.cli("site", "session", "start")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.argv_log(newer), ["--site", str(self.site_root), "adapter", "sync"])
        self.assertEqual(len(self.events(sessions.SWITCHED)), 1)
        self.assertEqual(self.events(up.LANDED_SKIPPED), [])

    def test_the_same_release_at_another_path_is_skipped(self):
        current = self.checkout("r-current", tag="r2026.10.15-3")
        same = self.checkout("r-same", base=current, commit=False, tag="r2026.10.15-3")
        self.pin(current)
        self.landed(same)
        order = up.release_order(current, same)
        self.assertEqual((order["newer"], order["reason"]),
                         (False, "r2026.10.15-3 is not newer than r2026.10.15-3 (same revision)"))
        rec = sessions.start(self.s, "desk")
        self.assertEqual(rec["payload"]["harness_switch_skipped"]["landed"], "a94fde")
        self.assertIsNone(self.argv_log(same))

    def test_ancestry_decides_without_tags_and_overrides_a_later_tag(self):
        current = self.checkout("r-current")  # untagged: ancestry alone decides
        child = self.checkout("r-child", base=current)
        sibling = self.checkout("r-sibling", base=current, at="HEAD~1")  # a sibling commit: does not contain current
        self.assertTrue(up.release_order(current, child)["newer"])
        order = up.release_order(current, sibling)
        self.assertFalse(order["newer"])
        self.assertIn("its history does not contain", order["reason"])
        self.assertFalse(up.release_order(child, current)["newer"], "the parent is older")
        # a later tag on a branch that does not contain the current release is still not newer
        tagged_cur = self.checkout("t-current", tag="r2026.10.13-2")
        stray = self.checkout("t-stray", base=tagged_cur, at="HEAD~1", tag="r2026.10.16-1")
        self.assertIn("does not contain", up.release_order(tagged_cur, stray)["reason"])
        # not a git checkout: never applied
        plain = self.tmp / "plain"
        (plain / "dureh_core").mkdir(parents=True)
        self.assertEqual(up.release_order(current, plain)["newer"], False)
        # --no-apply-landed still switches and skips nothing
        self.pin(current)
        self.landed(sibling)
        rec = sessions.start(self.s, "desk", apply_landed=False)
        self.assertNotIn("harness_switch_skipped", rec["payload"])
        self.assertEqual(self.events(up.LANDED_SKIPPED), [])
