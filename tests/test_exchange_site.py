"""D6, site side: `dureh upstream send|inbox` through the configured mailbox, and collaboration mode in status."""
import contextlib
import io
import json
import os
import unittest
from pathlib import Path

from helpers import SiteCase

from dureh_core import cli
from dureh_core import upstream as up
from dureh_core.ops import render_status, status
from dureh_core.records import Ledger
from dureh_core.site import Site
from dureh_core.util import DurehError

UR = "# {id}: {title}\n\n- Type: bug\n- Severity: blocking\n- Facts: blocks_job=yes, loses_data=no, workaround=no, affects_all=no\n- Site: s\n- Status: {status}\n\n## Request\nfix it\n\n" \
     "## Impact at this site\n- Blocked WIs: {blocked}\n- Continuing WIs: none\n"


class SiteMailbox(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.x = self.tmp / "exchange"
        self.x.mkdir()  # the Director creates the exchange root
        os.environ["DUREH_TEST_EXCHANGE"] = str(self.x)
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["upstream"] = {"exchange": "${DUREH_TEST_EXCHANGE}"}
        (self.site_root / "config.json").write_text(json.dumps(cfg, indent=2))
        self.s = Site(self.site_root)
        self.led = Ledger(self.s)
        self.wi = self.led.work_items()[0]["id"]
        self.ur("UR-20261002-01", "open", self.wi)
        self.box = self.x / self.site_root.name

    def ur(self, ur_id, st, blocked="none"):
        d = up.notes_dir(self.s)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{ur_id}.md").write_text(UR.format(id=ur_id, title="thing", status=st, blocked=blocked))

    def from_upstream(self, kind, body, name=None):
        d = self.box / "from-upstream"
        d.mkdir(parents=True, exist_ok=True)
        name = name or f"20260101T00000{len(list(d.iterdir()))}Z-{kind}-ab{len(list(d.iterdir()))}.md"
        (d / name).write_text(f"---\nid: x\nsite: {self.site_root.name}\nfrom: upstream\nkind: {kind}\nrefs: [UR-20261002-01]\n"
                              f"created: t\n---\n{body}\n")
        return name

    def test_send_refuses_template_placeholders(self):
        d = up.notes_dir(self.s)
        (d / "UR-20261002-02.md").write_text(UR.format(id="UR-20261002-02", title="t", status="open", blocked="none")
                                             .replace("fix it", "<expected behavior or the exact diff>"))
        with self.assertRaises(DurehError) as cm:
            up.send(self.s, "desk", "request")
        self.assertIn("UR-20261002-02.md", str(cm.exception))

    def test_send_reply_kind(self):
        body = self.tmp / "reply.md"
        body.write_text("answers")
        r = up.send(self.s, "desk", "reply", str(body))
        self.assertEqual(r["kind"], "reply")

    def test_send_writes_the_message_and_a_forward_copy(self):
        r = up.send(self.s, "desk", "request")
        sent = list((self.box / "to-upstream").glob("*-request-*.md"))
        self.assertEqual(len(sent), 1)
        text = sent[0].read_text()
        self.assertIn("from: site\nkind: request\nrefs: [UR-20261002-01]", text)
        self.assertIn("UR-20261002-01 (bug, blocking)", text)
        self.assertTrue(any(p.name.startswith("FORWARD-") for p in up.notes_dir(self.s).iterdir()))
        self.assertTrue(any(e["type"] == "upstream.sent" for e in self.led.load("event")))
        self.assertEqual(r["file"], str(sent[0]))

    def test_send_needs_the_exchange_configured(self):
        cfg = json.loads((self.site_root / "config.json").read_text())
        del cfg["upstream"]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        with self.assertRaisesRegex(DurehError, "upstream.exchange"):
            up.send(Site(self.site_root), "desk", "request")

    def test_idle_is_refused_while_an_agent_runs(self):
        self.led.event("wi.approved", "desk", self.wi, {})
        d = self.led.event("wi.dispatched", "desk", self.wi, {"role": "pm-modules", "brief": "0" * 16, "by": "desk"})
        self.led.event("agent.started", "desk", self.wi, {"agent_id": "a1", "brief": "0" * 16, "bound_by": "brief"}, [d["id"]])
        with self.assertRaisesRegex(DurehError, "agents are running"):
            up.send(self.s, "desk", "idle")
        self.led.event("agent.finished", "desk", self.wi, {"agent_id": "a1"})
        up.send(self.s, "desk", "idle")
        self.assertIn("SITE ACTIVITY", next((self.box / "to-upstream").glob("*-idle-*.md")).read_text())

    def test_inbox_records_once_and_approvals_become_decisions(self):
        self.from_upstream("reply", "UR-20261002-01: accepted, landed abc1234")
        self.from_upstream("approval", "Director: approve the merge window for release 2 now.")
        new = up.inbox(self.s, "desk", record=False)
        self.assertEqual([m["kind"] for m in new], ["reply", "approval"])
        up.inbox(self.s, "desk", record=True)
        self.assertEqual(up.inbox(self.s, "desk", record=False), [])
        dec = [d for d in self.led.load("decision") if (d["payload"].get("binding") or {}).get("via") == "upstream"]
        self.assertEqual(len(dec), 1)
        self.assertEqual(dec[0]["payload"]["by"], "Director")
        self.assertEqual(dec[0]["payload"]["memo"], "Director: approve the merge window for release 2 now.")

    def test_status_shows_collaboration_and_merge_window(self):
        st = status(self.s)["upstream"]
        self.assertEqual(st["mode"], "collaborating")
        self.assertEqual(st["open"], ["UR-20261002-01"])
        self.assertEqual(st["blocked_wis"], [self.wi])
        self.assertEqual(st["merge_window"], "closed")
        self.from_upstream("merge-window", "open: release 2")
        self.assertEqual(status(self.s)["upstream"]["merge_window"], "requested")
        up.send(self.s, "desk", "idle")
        self.assertEqual(status(self.s)["upstream"]["merge_window"], "open (site idle)")
        text = render_status(status(self.s))
        self.assertIn("Upstream: collaborating", text)
        self.assertIn("1 new message from upstream", text)
        self.from_upstream("merge-window", "closed", name="20990101T000000Z-merge-window-zz.md")
        self.assertEqual(status(self.s)["upstream"]["merge_window"], "closed")

    def test_independent_when_nothing_is_open(self):
        self.ur("UR-20261002-01", "landed abc1234")
        st = status(self.s)["upstream"]
        self.assertEqual((st["mode"], st["open"]), ("independent", []))
        self.assertIn("SITE ACTIVITY", up.message(self.s))

    def test_observe_allows_listing_only(self):
        self.from_upstream("reply", "x")
        run = lambda *argv: cli.main(["--site", str(self.site_root), "--observe", *argv])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run("upstream", "inbox"), 0)
            self.assertEqual(run("upstream", "inbox", "--record"), 2)
            self.assertEqual(run("upstream", "send"), 2)


class MailboxWatch(SiteMailbox):
    """Release 44: `upstream inbox --watch` prints each unread message once and records nothing."""

    def snapshot(self):
        rec = self.site_root / "state" / "records"
        return sorted(str(p) for p in rec.rglob("*") if p.is_file())

    def test_unread_then_new_each_once_and_nothing_recorded(self):
        old = self.from_upstream("reply", "\n  first answer line  \nmore")
        up.inbox(self.s, "desk", record=True)  # received before the watch: never shown
        self.from_upstream("merge-window", "open for release 44")
        self.from_upstream("landed", "x" * 200)
        before = self.snapshot()
        lines, rounds = [], []

        def sleep(_):
            rounds.append(1)
            if len(rounds) == 1:
                self.from_upstream("approval", "go ahead")

        n = up.watch_inbox(self.s, "desk", 30, emit=lines.append, sleep=sleep, rounds=3)
        self.assertEqual(n, 3)
        self.assertEqual(len(lines), 3, lines)
        self.assertEqual(lines[0], "upstream merge-window x: open for release 44")
        self.assertTrue(lines[1].startswith("upstream landed x: xxx") and lines[1].endswith("...") and len(lines[1]) < 200)
        self.assertEqual(lines[2], "upstream approval x: go ahead")
        self.assertFalse(any(old in line for line in lines))
        self.assertEqual(self.snapshot(), before, "the watch records nothing")
        self.assertEqual(len(up.inbox(self.s, "desk")), 3, "still unread for inbox --record")

    def test_cli_watch_is_allowed_under_observe_and_refuses_record(self):
        self.from_upstream("reply", "hello")
        out, err = io.StringIO(), io.StringIO()
        orig = up.watch_inbox
        up.watch_inbox = lambda site, actor, interval, emit: orig(site, actor, interval, emit=emit, rounds=1)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["--site", str(self.site_root), "--observe", "upstream", "inbox", "--watch"])
                bad = cli.main(["--site", str(self.site_root), "upstream", "inbox", "--watch", "--record"])
        finally:
            up.watch_inbox = orig
            os.environ.pop("DUREH_OBSERVE", None)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(out.getvalue(), "upstream reply x: hello\n")
        self.assertEqual(bad, 2)

    def test_refused_without_the_exchange(self):
        cfg = json.loads((self.site_root / "config.json").read_text())
        del cfg["upstream"]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        with self.assertRaisesRegex(DurehError, "upstream.exchange is not set"):
            up.watch_inbox(Site(self.site_root), "desk", rounds=1)

    def test_generated_desk_text_only_with_the_exchange(self):
        from dureh_core.runtimes import claude_code

        md = claude_code._claude_md(self.s)
        self.assertIn("upstream inbox --watch` running as a Monitor", md)
        self.assertIn("upstream send --kind idle", md)
        self.assertIn("adapter sync --harness <checkout>", md)
        cfg = json.loads((self.site_root / "config.json").read_text())
        del cfg["upstream"]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.assertNotIn("--watch` running as a Monitor (item 10)", claude_code._claude_md(Site(self.site_root)))
        self.assertNotIn("inbox --watch", claude_code._claude_md(Site(self.site_root)))

    def test_watch_off_switch_drops_the_monitor_text(self):
        """Release 45 (6a): `upstream.watch: false` keeps the mailbox handling but drops the standing Monitor."""
        from dureh_core.runtimes import claude_code

        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["upstream"]["watch"] = False
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        md = claude_code._claude_md(Site(self.site_root))
        self.assertNotIn("inbox --watch", md)
        self.assertNotIn("as a Monitor (item 10)", md)
        self.assertIn("no standing watch: config `upstream.watch: false`", md)
        self.assertIn("upstream inbox --record`, then by kind", md)
        self.assertIn("upstream send --kind idle", md)
        up.watch_inbox(Site(self.site_root), "desk", rounds=1, emit=lambda line: None)  # the command still works
        cfg["upstream"]["watch"] = True
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.assertIn("upstream inbox --watch` running as a Monitor", claude_code._claude_md(Site(self.site_root)))


if __name__ == "__main__":
    unittest.main()
