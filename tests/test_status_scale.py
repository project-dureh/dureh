"""Reads at scale (2026-10-05): `--observe status` at a site with 3355 events spent minutes in NFS reads because
every per-item lookup re-read all event files. Each record file is read at most once per process now, and a
file changed in place is read again."""
import json
import os
import time

from helpers import SiteCase

from dureh_core import ops, records
from dureh_core import workflow as wf
from dureh_core.records import Ledger

N_WI = 40
N_EVENTS = 1500


class StatusScale(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        led = Ledger(self.s)
        self.make_module()
        ids = []
        for i in range(N_WI):
            w = wf.wi_new(self.s, "desk", f"W{i}", "g", "pm-modules", "modules/demo", ["ok"])["id"]
            wf.wi_approve(self.s, "desk", w, "ok")
            ids.append(w)
        led.event("wi.dispatched", "desk", ids[0], {})
        template = led.event("note", "desk", None, {"n": 0})
        d = self.s.record_dir("event")
        for i in range(N_EVENTS):  # plain files, as many days of a busy site leave them
            rec = dict(template, id=f"ev-20260101T{i // 3600:02d}{i // 60 % 60:02d}{i % 60:02d}000000Z-{i:08x}",
                       payload={"n": i})
            (d / f"{rec['id']}.json").write_text(json.dumps(rec))
        self.files = sum(len(list(self.s.record_dir(k).glob("*.json"))) for k in ("event", "work-item"))
        self.reads = 0
        real = records.read_json

        def counting(path):
            self.reads += 1
            return real(path)

        records.read_json = counting
        self.addCleanup(setattr, records, "read_json", real)

    def test_status_reads_each_record_once_and_a_second_status_reads_none(self):
        t = time.monotonic()
        st = ops.status(self.s)
        first = time.monotonic() - t
        self.assertEqual(st["work_items"], N_WI + 1)
        self.assertLessEqual(self.reads, self.files + 50, "each record file read at most once (plus small kinds)")
        self.assertLess(first, 30)
        self.reads = 0
        t = time.monotonic()
        ops.status(self.s)
        self.assertEqual(self.reads, 0, "unchanged records are not read again")
        self.assertLess(time.monotonic() - t, first + 1)

    def test_a_record_changed_in_place_is_read_again(self):
        led = Ledger(self.s)
        led.load("event")
        d = self.s.record_dir("event")
        path = sorted(d.glob("ev-20260101T*.json"))[0]
        rec = json.loads(path.read_text())
        rec["payload"] = {"n": "changed in place"}
        path.write_text(json.dumps(rec))
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 10**9))
        self.reads = 0
        got = [r for r in led.load("event") if r["id"] == rec["id"]]
        self.assertEqual(self.reads, 1)
        self.assertEqual(got[0]["payload"], {"n": "changed in place"})
        got[0]["payload"]["n"] = "caller's copy"
        self.assertEqual([r for r in led.load("event") if r["id"] == rec["id"]][0]["payload"]["n"], "changed in place")
        path.unlink()
        self.assertNotIn(rec["id"], {r["id"] for r in led.load("event")})

    def test_observe_index_is_kept_between_calls(self):
        os.environ["DUREH_OBSERVE"] = "1"
        led = Ledger(self.s)
        led.sql("SELECT count(*) AS n FROM records")
        path = self.s.index_path()
        self.assertTrue(path.is_file())
        self.assertFalse(str(path).startswith(str(self.site_root)))
        built = path.stat().st_mtime_ns
        self.assertTrue(Ledger(self.site()).index_fresh(), "a second observe call finds the index fresh")
        Ledger(self.site()).sql("SELECT count(*) AS n FROM records")
        self.assertEqual(path.stat().st_mtime_ns, built, "not rebuilt")
