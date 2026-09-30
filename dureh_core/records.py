"""Append-only JSON records, a rebuildable node-local SQLite index, and the WI
status projection. Records are authoritative; the index never is."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

from . import SCHEMA_VERSION
from .site import RECORD_KINDS, Site
from .util import dumps, fail, read_json, utc_compact, utc_now, write_create_only
from .validate import check_record

PREFIX = {"event": "ev", "run": "run", "decision": "dec", "session": "ses", "finding": "fnd"}
WI_ID = re.compile(r"^WI-(\d{4,})$")

# WI lifecycle. Status is a projection of events; records are never rewritten.
EVENT_TO_STATUS = {
    "wi.created": "proposed",
    "wi.approved": "approved",
    "wi.dispatched": "dispatched",
    "wi.staged": "staged",
    "rework.requested": "rework",
    "wi.verified": "verified",
    "wi.promoted": "promoted",
    "wi.stopped": "stopped",
    "wi.reverted": "reverted",
}
TRANSITIONS = {
    None: {"proposed"},
    "proposed": {"approved", "stopped"},
    "approved": {"dispatched", "stopped"},
    "dispatched": {"staged", "stopped", "rework"},
    "staged": {"verified", "rework", "stopped"},
    "rework": {"dispatched", "stopped"},
    "verified": {"promoted", "rework", "stopped"},
    "promoted": {"reverted"},
    "stopped": {"approved"},
    "reverted": set(),
}
ACTIVE = {"dispatched", "staged", "rework", "verified"}


class Ledger:
    def __init__(self, site: Site):
        self.site = site

    # --- writing -----------------------------------------------------------
    def _new_id(self, kind: str) -> str:
        if kind == "work-item":
            fail("work-item ids are allocated by new_work_item")
        return f"{PREFIX[kind]}-{utc_compact()}-{secrets.token_hex(4)}"

    def append(self, kind: str, type_: str, actor: str, wi: str | None, payload: dict,
               refs: list[str] | None = None, record_id: str | None = None) -> dict:
        if not isinstance(payload, dict):
            fail("payload must be a JSON object")
        if not actor or not type_:
            fail("record type and actor must be nonempty")
        org = self.site.org
        org.role(actor)  # unknown actor fails
        if kind == "event" and not org.may_append(actor, type_):
            fail(f"role {actor!r} is not authorized to append event type {type_!r}")
        if kind in ("work-item", "decision") and org.role(actor).kind != "desk":
            fail(f"only the desk may create {kind} records")
        if wi is not None and not WI_ID.match(wi):
            fail(f"invalid work-item id: {wi!r}")
        if wi is not None and kind != "work-item" and not (self.site.record_dir("work-item") / f"{wi}.json").is_file():
            fail(f"unknown work item {wi}; create it with 'wi new' first")
        if kind == "event" and type_ in EVENT_TO_STATUS:
            if wi is None:
                fail(f"event {type_} requires --wi")
            current = self.wi_status(wi)["status"]
            target = EVENT_TO_STATUS[type_]
            if target not in TRANSITIONS.get(current, set()):
                fail(f"invalid transition for {wi}: {current} -> {target} (event {type_})")
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": record_id or self._new_id(kind),
            "kind": kind,
            "type": type_,
            "ts": utc_now(),
            "actor": actor,
            "wi": wi,
            "refs": list(refs or []),
            "payload": payload,
        }
        check_record(record)
        directory = self.site.record_dir(kind)
        if not directory.is_dir():
            fail(f"record store is missing; initialize the site first: {directory}")
        write_create_only(directory / f"{record['id']}.json", dumps(record).encode("utf-8"), 0o644)
        return record

    def event(self, type_: str, actor: str, wi: str | None = None, payload: dict | None = None,
              refs: list[str] | None = None) -> dict:
        return self.append("event", type_, actor, wi, payload or {}, refs)

    def new_work_item(self, actor: str, payload: dict, refs: list[str] | None = None) -> dict:
        directory = self.site.record_dir("work-item")
        numbers = [int(m.group(1)) for p in directory.glob("WI-*.json") if (m := WI_ID.match(p.stem))]
        number = max(numbers, default=0) + 1
        for _ in range(20):
            wi = f"WI-{number:04d}"
            if not (directory / f"{wi}.json").exists():
                break
            number += 1
        record = self.append("work-item", "work-item", actor, wi, payload, refs, record_id=wi)
        self.event("wi.created", actor, wi, {"title": payload["title"], "role": payload["role"]}, [wi])
        return record

    # --- reading -----------------------------------------------------------
    def load(self, kind: str) -> list[dict]:
        directory = self.site.record_dir(kind)
        if not directory.is_dir():
            fail(f"record store is missing: {directory}")
        rows = []
        for path in sorted(directory.glob("*.json")):
            rec = read_json(path)
            if rec.get("id") != path.stem:
                fail(f"record id does not match its file name: {path}")
            check_record(rec)
            rows.append(rec)
        rows.sort(key=lambda r: (r["ts"], r["id"]))
        return rows

    def get(self, kind: str, record_id: str) -> dict:
        path = self.site.record_dir(kind) / f"{record_id}.json"
        if not path.is_file():
            fail(f"no {kind} record {record_id}")
        rec = read_json(path)
        check_record(rec)
        return rec

    def events(self, wi: str | None = None) -> list[dict]:
        rows = self.load("event")
        voided = {ref for r in rows if r["type"] == "record.voided" for ref in r["refs"]}
        return [r for r in rows if r["id"] not in voided and (wi is None or r["wi"] == wi)]

    @staticmethod
    def _project(events: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for ev in events:
            target = EVENT_TO_STATUS.get(ev["type"])
            if target is None or ev["wi"] is None:
                continue
            st = out.setdefault(ev["wi"], {"wi": ev["wi"], "status": None, "rework": 0, "updated": None})
            st["status"], st["updated"] = target, ev["ts"]
            if target == "rework":
                st["rework"] += 1
        return out

    def wi_status(self, wi: str) -> dict:
        return self._project(self.events(wi)).get(wi, {"wi": wi, "status": None, "rework": 0, "updated": None})

    def work_items(self) -> list[dict]:
        out = []
        projected = self._project(self.events())
        for rec in self.load("work-item"):
            st = projected.get(rec["id"], {"status": None, "rework": 0, "updated": None})
            p = rec["payload"]
            out.append({"id": rec["id"], "title": p["title"], "role": p["role"], "target": p["target"],
                        "lane": p["lane"], "bundle": p.get("bundle"), "status": st["status"],
                        "rework": st["rework"], "updated": st["updated"] or rec["ts"]})
        return out

    # --- index -------------------------------------------------------------
    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for kind in RECORD_KINDS:
            d = self.site.record_dir(kind)
            if d.is_dir():
                for p in sorted(d.glob("*.json")):
                    st = p.stat()
                    h.update(f"{kind}/{p.name}:{st.st_size}:{st.st_mtime_ns}\n".encode())
        return h.hexdigest()

    def build_index(self) -> dict:
        path = self.site.index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
        if tmp.exists():
            tmp.unlink()
        con = sqlite3.connect(tmp)
        try:
            con.executescript(
                """
                CREATE TABLE records(id TEXT PRIMARY KEY, kind TEXT, type TEXT, ts TEXT, actor TEXT, wi TEXT,
                                     refs TEXT, payload TEXT);
                CREATE TABLE work_items(id TEXT PRIMARY KEY, title TEXT, role TEXT, target TEXT, lane TEXT,
                                        bundle TEXT, status TEXT, rework INTEGER, updated TEXT);
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE INDEX records_wi ON records(wi);
                CREATE INDEX records_kind_type ON records(kind, type);
                """
            )
            count = 0
            for kind in RECORD_KINDS:
                for r in self.load(kind):
                    con.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?)",
                                (r["id"], r["kind"], r["type"], r["ts"], r["actor"], r["wi"],
                                 json.dumps(r["refs"]), json.dumps(r["payload"], ensure_ascii=False)))
                    count += 1
            for w in self.work_items():
                con.execute("INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)",
                            (w["id"], w["title"], w["role"], w["target"], w["lane"], w["bundle"],
                             w["status"], w["rework"], w["updated"]))
            fp = self.fingerprint()
            con.executemany("INSERT INTO meta VALUES (?,?)", [("fingerprint", fp), ("built_at", utc_now()),
                                                              ("site", str(self.site.root)), ("records", str(count))])
            con.commit()
        finally:
            con.close()
        os.replace(tmp, path)
        return {"path": str(path), "records": count, "seconds": round(time.monotonic() - start, 3)}

    def index_fresh(self) -> bool:
        path = self.site.index_path()
        if not path.is_file():
            return False
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            row = con.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
            con.close()
        except sqlite3.Error:
            return False
        return bool(row) and row[0] == self.fingerprint()

    def sql(self, statement: str) -> list[dict]:
        if not self.index_fresh():
            self.build_index()
        con = sqlite3.connect(f"file:{self.site.index_path()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in con.execute(statement).fetchall()]
        except sqlite3.Error as exc:
            fail(f"query failed: {exc}")
        finally:
            con.close()
        return rows
