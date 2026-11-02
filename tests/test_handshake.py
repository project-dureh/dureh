"""`dureh day handshake`: the desk's start-of-day check (Director, 2026-11-02). Read-only; one line per check;
exit 2 on a FAIL, 1 on a warning, else 0."""
import contextlib
import io
import json
import os
import unittest
import unittest.mock
from datetime import date, timedelta

from dureh_core import handshake as hs
from dureh_core import sessions
from dureh_core.cli import main
from dureh_core.records import Ledger
from tests.helpers import ExampleSiteCase


def _job(state="RUNNING", left="7:30:00"):
    return unittest.mock.patch.object(sessions, "_squeue", return_value={"queued": True, "state": state,
                                                                          "time_left": left})


class Handshake(ExampleSiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        patches = [unittest.mock.patch.object(sessions, "unrecorded_jobs", return_value=[]),
                   unittest.mock.patch.object(sessions, "_mem_use", return_value=None),
                   unittest.mock.patch.object(sessions, "_ended", return_value=None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.led = Ledger(self.s)

    def set_config(self, **keys):
        cfg = json.loads(self.s.config_path.read_text())
        cfg.update(keys)
        self.s.config_path.write_text(json.dumps(cfg, indent=2))
        self.s = self.site()

    def qualify_all(self, **override):
        models = dict(self.s.config.get("models") or {})
        for t in self.s.model_tiers():  # release 90: a run id must name a passing qualification record
            rec = self.led.append("run", "qualification", "desk", None, {
                "run_type": "qualification", "status": "pass", "tier": t, "model": models[t]["model"], "score": "4/4",
                "expires": (date.today() + timedelta(days=90)).isoformat()})
            models[t] = {**models[t], "qualification": rec["id"]}
        models.update(override)
        self.set_config(models=models)

    def start_session(self):
        self.led.event("session.started", "desk", None, {"job_id": "4242", "wall_time": "10:00:00", "purpose": "desk"})

    def levels(self, res, name):
        return [c["level"] for c in res["checks"] if c["check"] == name]

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--site", str(self.site_root), *argv])
        return code, out.getvalue()

    def test_clean_site_is_ok(self):
        self.qualify_all()
        self.start_session()
        with _job():
            res = hs.handshake(self.s)
        bad = [c for c in res["checks"] if c["level"] in (hs.WARN, hs.FAIL)]
        self.assertEqual(bad, [])
        self.assertEqual(res["exit"], 0)
        names = [c["check"] for c in res["checks"]]
        for n in ("waivers", "harness", "session", "day", "asks", "blocked WIs", "mailbox", "config"):
            self.assertIn(n, names)
        self.assertTrue(any(n.startswith("dispatch ") for n in names))
        self.assertEqual(self.levels(res, "harness"), [hs.SKIP])  # no exchange: the newest release is not known
        self.assertEqual(self.levels(res, "asks"), [hs.SKIP])  # github off

    def test_expired_waiver_fails_and_soon_warns(self):
        today = date.today()
        tiers = self.s.model_tiers()
        self.assertGreaterEqual(len(tiers), 2)
        gone = {"model": "m", "qualification": "waived-bootstrap", "waiver_expires": (today - timedelta(days=1)).isoformat()}
        soon = {"model": "m", "qualification": "waived-bootstrap", "waiver_expires": (today + timedelta(days=3)).isoformat()}
        self.qualify_all(**{tiers[0]: gone, tiers[1]: soon})
        self.start_session()
        with _job():
            res = hs.handshake(self.s, today=today)
        self.assertEqual(res["exit"], 2)
        self.assertEqual(self.levels(res, f"dispatch {tiers[0]}"), [hs.FAIL])
        self.assertEqual(self.levels(res, f"dispatch {tiers[1]}"), [hs.OK])
        self.assertEqual(sorted(self.levels(res, "waivers")), [hs.FAIL, hs.WARN])
        text = hs.render(res)
        self.assertIn(f"FAIL dispatch {tiers[0]}: m: bootstrap waiver expired", text)
        self.assertTrue(text.splitlines()[-1].startswith("handshake FAIL: 2 FAIL, 1 warn"), text)

    def test_missing_tier_mapping_fails(self):
        self.qualify_all()
        models = dict(self.s.config["models"])
        role_tier = next(r.tier for r in self.s.org.roles.values() if r.kind != "desk")
        models.pop(role_tier)
        self.set_config(models=models)
        self.start_session()
        with _job():
            res = hs.handshake(self.s)
        self.assertEqual(self.levels(res, f"dispatch {role_tier}"), [hs.FAIL])

    def test_session_warnings(self):
        self.qualify_all()
        res = hs.handshake(self.s)
        self.assertEqual(self.levels(res, "session"), [hs.WARN])  # no session recorded
        self.start_session()
        with _job(left="45:10"):
            res = hs.handshake(self.s)
        self.assertEqual(self.levels(res, "session"), [hs.WARN])  # under an hour left
        self.assertEqual(res["exit"], 1)
        with unittest.mock.patch.object(sessions, "_squeue", return_value={"queued": None, "error": "no squeue"}):
            res = hs.handshake(self.s)
        self.assertEqual(self.levels(res, "session"), [hs.SKIP])  # unknown is never ok
        self.assertEqual(hs._seconds("1-02:00:00"), 93600)
        self.assertIsNone(hs._seconds("UNLIMITED"))

    def test_earlier_open_day_warns(self):
        self.qualify_all()
        self.start_session()
        self.led.event("day.planned", "desk", None, {"date": "2026-01-05", "goal": "g", "plan": "p"})
        with _job():
            res = hs.handshake(self.s, today=date(2026, 1, 6))
        self.assertEqual(self.levels(res, "day"), [hs.WARN])

    def test_mailbox_and_landed_release(self):
        self.qualify_all()
        self.start_session()
        ex = self.tmp / "exchange"
        box = ex / self.s.mailbox_name / "from-upstream"
        box.mkdir(parents=True)
        (box / "20261102T0800-reply-a.md").write_text("---\nid: m1\nkind: reply\n---\nhello\n")
        self.set_config(upstream={"exchange": str(ex)})
        with _job():
            res = hs.handshake(self.s)
        self.assertEqual(self.levels(res, "mailbox"), [hs.WARN])
        self.assertIn("1 unread message from upstream", hs.render(res))
        self.assertEqual(self.levels(res, "harness"), [hs.OK])

    def test_open_asks_from_records_and_cached_view(self):
        self.qualify_all()
        self.start_session()
        tok = self.tmp / "token"
        tok.write_text("x")
        os.chmod(tok, 0o600)
        self.set_config(github={"enabled": True, "org": "example-org", "project": 1, "token_file": str(tok)})
        self.assertEqual(hs._director(self.s, {}), [hs._line("asks", hs.OK, "no open board ask or Director step recorded")])
        for n, iid in ((1, "I1"), (2, "I2"), (3, "I3")):
            self.led.event("github.ask.created", "desk", None, {"url": f"https://github.com/example-org/office/issues/{n}",
                                                                 "repo": "example-org/office", "number": n, "item_id": iid})
        self.led.event("github.item.closed", "desk", None, {"url": "https://github.com/example-org/office/issues/3"})
        lines = hs._director(self.s, {})
        self.assertEqual([x["level"] for x in lines], [hs.WARN])
        self.assertIn("2 open (office#1, office#2); Decisions not checked", lines[0]["text"])
        from dureh_core import github

        github._save_state(self.s, {"board": "example-org/1", "decisions": {"I1": "Approve 1", "I2": None}})
        lines = hs._director(self.s, {"day": {"director_actions": ["link the dataset"]}})
        self.assertEqual([x["level"] for x in lines], [hs.OK, hs.OK, hs.WARN])
        self.assertIn("1 wait for the Director: office#2", lines[1]["text"])
        self.assertIn("office#1 -> Approve 1", lines[2]["text"])

    def test_cli_exit_json_and_observe(self):
        self.qualify_all()
        self.start_session()
        with _job():
            code, out = self.cli("--observe", "day", "handshake")
            self.assertEqual(code, 0, out)
            self.assertIn("handshake ok:", out)
            before = sorted(p.name for p in (self.site_root / "state" / "records" / "event").iterdir())
            code, out = self.cli("--observe", "day", "handshake", "--json")
            after = sorted(p.name for p in (self.site_root / "state" / "records" / "event").iterdir())
        self.assertEqual(before, after)  # records nothing
        self.assertEqual(json.loads(out)["exit"], 0)
        with _job(left="10:00"):
            code, _ = self.cli("day", "handshake")
        self.assertEqual(code, 1)

    def test_desk_text_names_the_handshake(self):
        text = (self.site_root / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("day handshake", text)
        self.assertIn("FAIL", text)
        from dureh_core.runtimes import claude_code as cc

        self.assertIn("in your reply", cc._handshake_rule(self.s, "bin/dureh"))
        self.set_config(upstream={"exchange": str(self.tmp)})
        rule = cc._handshake_rule(self.s, "bin/dureh")
        self.assertIn("upstream send --kind notice", rule)
        self.assertIn("does not wait for the plan", rule)


if __name__ == "__main__":
    unittest.main()
