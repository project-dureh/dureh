"""Reads at scale (2026-10-05): `--observe status` at a site with 3355 events spent minutes in NFS reads because
every per-item lookup re-read all event files. Each record file is read at most once per process now, and a
file changed in place is read again by a new process. Release 96 (2026-11-09): each record file is also stat'ed at
most once per process (a status loads the events six times; on NFS each pass cost a stat per file)."""
import collections
import json
import os
import time
import unittest.mock
from pathlib import Path

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

    def test_a_record_changed_in_place_is_read_again_by_a_new_process(self):
        led = Ledger(self.s)
        led.load("event")
        d = self.s.record_dir("event")
        path = sorted(d.glob("ev-20260101T*.json"))[0]
        rec = json.loads(path.read_text())
        rec["payload"] = {"n": "changed in place"}
        path.write_text(json.dumps(rec))
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 10**9))
        self.reads = 0
        led.load("event")
        self.assertEqual(self.reads, 0, "a file this process has checked is trusted (records are create-only)")
        records.forget_cache()  # a new process: the pack entry no longer matches the file's size and mtime
        got = [r for r in led.load("event") if r["id"] == rec["id"]]
        self.assertEqual(self.reads, 1)
        self.assertEqual(got[0]["payload"], {"n": "changed in place"})
        got[0]["payload"]["n"] = "caller's copy"
        self.assertEqual([r for r in led.load("event") if r["id"] == rec["id"]][0]["payload"]["n"], "changed in place")
        path.unlink()
        self.assertNotIn(rec["id"], {r["id"] for r in led.load("event")})

    def _count_stats(self):
        """Patch os.scandir so every DirEntry.stat() of a record file is counted in self.stats[kind]."""
        real = os.scandir
        self.stats = collections.Counter()
        test = self

        class Entry:
            def __init__(self, e):
                self._e, self.name, self.path = e, e.name, e.path

            def stat(self):
                test.stats[Path(self.path).parent.name] += 1
                return self._e.stat()

        class Scan:
            def __init__(self, path):
                self._it = real(path)

            def __enter__(self):
                return (Entry(e) for e in self._it)

            def __exit__(self, *exc):
                self._it.close()

        def scandir(path="."):  # record directories only: rmtree and the rest use the real one
            return Scan(path) if isinstance(path, (str, Path)) and Path(path).parent.name == "records" else real(path)

        patcher = unittest.mock.patch.object(records.os, "scandir", scandir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_record_file_is_stated_once_per_process(self):
        """Release 96: a status loads the events six times; on NFS each pass stat'ed every file again."""
        records.forget_cache()
        self._count_stats()
        ops.status(self.s)
        events = len(list(self.s.record_dir("event").glob("*.json")))
        self.assertEqual(self.stats["event"], events, "each event file stat'ed exactly once in one status")
        self.assertEqual(self.stats["work-item"], N_WI + 1)
        self.stats.clear()
        Ledger(self.s).load("event")
        Ledger(self.s).fingerprint()
        ops.status(self.s)
        self.assertEqual(sum(self.stats.values()), 0, "later loads and the fingerprint stat no record file again")
        new = Ledger(self.s).event("note", "desk", None, {"n": "new"})
        self.assertIn(new["id"], {r["id"] for r in Ledger(self.s).load("event")})
        self.assertEqual(dict(self.stats), {"event": 1}, "only the new file is stat'ed")

    def test_cached_loads_give_the_same_records_as_the_files(self):
        led = Ledger(self.s)
        for kind in ("event", "work-item", "decision"):
            on_disk = sorted((json.loads(p.read_text()) for p in self.s.record_dir(kind).glob("*.json")),
                             key=lambda r: (r["ts"], r["id"]))
            self.assertEqual(led.load(kind), on_disk)
            self.assertEqual(led.load(kind), on_disk, "a repeated load (one cached array) gives the same")
            got = led.load(kind)
            got[0]["payload"]["x"] = "caller's change"
            got.pop()
            self.assertEqual(led.load(kind), on_disk, "what a caller changes never reaches the cache")
        fp = led.fingerprint()
        wi = led.get("work-item", "WI-0001")
        records.forget_cache()
        self.assertEqual(led.fingerprint(), fp, "the fingerprint from cached stats is the one a new process computes")
        self.assertEqual(led.get("work-item", "WI-0001"), wi)
        led.load("work-item")
        self.reads = 0
        self.assertEqual(led.get("work-item", "WI-0001"), wi)
        self.assertEqual(self.reads, 0, "get serves a record this process has read")

    def test_a_new_process_reads_only_files_written_since_the_pack(self):
        Ledger(self.s).load("event")
        pack = Ledger(self.s)._pack_path("event")
        self.assertTrue(pack.is_file())
        self.assertFalse(str(pack).startswith(str(self.site_root)), "the pack lives outside the site")
        records._RECORD_CACHE.clear()  # as a new process sees it
        self.reads = 0
        before = Ledger(self.s).load("event")
        self.assertEqual(self.reads, 0, "every record comes from the pack")
        Ledger(self.s).event("note", "desk", None, {"n": "new"})
        records._RECORD_CACHE.clear()
        self.reads = 0
        after = Ledger(self.s).load("event")
        self.assertEqual(self.reads, 1, "only the new record file is opened")
        self.assertEqual(len(after), len(before) + 1)

    def test_a_damaged_or_foreign_pack_is_ignored(self):
        Ledger(self.s).load("event")
        pack = Ledger(self.s)._pack_path("event")
        for text in ("not json\n", '{"v": 1, "dir": "/elsewhere"}\n', pack.read_text()[:200]):
            pack.write_text(text)
            records._RECORD_CACHE.clear()
            n = len(Ledger(self.s).load("event"))
            self.assertEqual(n, len(list(self.s.record_dir("event").glob("*.json"))))

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
