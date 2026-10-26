"""Append-only JSON records, a rebuildable node-local SQLite index, and the WI
status projection. Records are authoritative; the index never is."""
from __future__ import annotations

import contextlib
import fcntl
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
    "wi.reopened": "approved",  # release 45: `wi amend --reopen` of a verified, unpromoted item
}
TRANSITIONS = {
    None: {"proposed"},
    "proposed": {"approved", "stopped"},
    "approved": {"dispatched", "staged", "stopped"},  # -> staged only for a checker item (STAGED_DIRECT)
    "dispatched": {"dispatched", "staged", "stopped", "rework"},  # re-dispatch resumes a finished agent's item
    "staged": {"staged", "verified", "rework", "stopped"},  # release 81: re-staged at a new head (wi_stage)
    "rework": {"dispatched", "staged", "stopped"},
    "verified": {"promoted", "rework", "stopped", "approved"},  # -> approved only by wi.reopened
    "promoted": {"reverted"},
    "stopped": {"approved"},
    "reverted": set(),
}
ONLY_BY = {("verified", "approved"): "wi.reopened"}  # transitions only one event type may make
# release 81 (UR-20261023-02): staged without a dispatch only for an item owned by a role with the verify
# capability that has no worktree (a read-only sweep with no worker; Ledger.checker_item)
STAGED_DIRECT = {"approved", "rework"}
ACTIVE = {"dispatched", "staged", "rework", "verified"}
TITLE_CHARS = 40  # UR-20261008-02: a WI named to the Director carries a short title, at most this long
WI_MENTION = re.compile(r"(?<![\w-])(WI-\d{4,})(?![\w-])(?!\s*\()")


def short_title(payload: dict) -> str:
    """The short title of a work item: its `label` (`wi new --label`), else its title cut to TITLE_CHARS."""
    text = " ".join(str(payload.get("label") or payload.get("title") or "").split())
    return text if len(text) <= TITLE_CHARS else text[:TITLE_CHARS - 3].rstrip() + "..."


def wi_label(wi: str, payload: dict | None) -> str:
    """`WI-0138 (framework design doc)`: how a WI id is shown to the Director (UR-20261008-02)."""
    short = short_title(payload or {})
    return f"{wi} ({short})" if short else wi


def titled_text(text: str, labels: dict[str, str]) -> str:
    """Director-facing text with each bare WI id followed by its short title. Ids inside `backquotes` (commands to
    copy), ids already followed by a parenthesis, and unknown ids are left as they are."""
    parts = text.split("`")
    for i in range(0, len(parts), 2):
        parts[i] = WI_MENTION.sub(lambda m: labels.get(m.group(1), m.group(1)), parts[i])
    return "`".join(parts)


# Typed decision block (release 52, proposals/typed-decisions-and-model-routing.md A1): one question, a fixed
# list of options, the choice, its confidence and what was done with it. Facts are booleans only; prose stays
# in `memo`. The block is optional on every dec-* record; records without it keep their shape.
CONFIDENCE = ("high", "medium", "low")
ACTIONS = ("acted", "escalated", "provisional")
_FACT_WORDS = {"yes": True, "true": True, "1": True, "no": False, "false": False, "0": False}


def parse_fact(text: str) -> tuple[str, bool]:
    """`name=yes|no` (also true/false, 1/0) -> (name, bool)."""
    name, sep, value = text.partition("=")
    if not sep or not name.strip() or value.strip().lower() not in _FACT_WORDS:
        fail(f"--fact takes name=yes|no, got {text!r}")
    return name.strip(), _FACT_WORDS[value.strip().lower()]


def typed_block(question: str, options: list[str], chosen: str, confidence: str = "medium", action: str = "acted",
                facts: dict[str, bool] | None = None, by: str = "", actor: str = "") -> dict:
    """Build and check a typed decision block. Rules beyond the schema: the choice is one of the options,
    `low` confidence is never `acted`, and an `escalated` choice names in `by` someone other than the deciding
    desk (the Director)."""
    options = [o.strip() for o in options or []]
    if not (question or "").strip():
        fail("a typed decision needs --question")
    if len(options) < 2 or len(set(options)) != len(options) or not all(options):
        fail("a typed decision needs two or more distinct, nonempty --option values")
    if (chosen or "").strip() not in options:
        fail(f"--chosen {chosen!r} is not one of the options {options}")
    if confidence not in CONFIDENCE:
        fail(f"--confidence must be one of {', '.join(CONFIDENCE)}")
    if action not in ACTIONS:
        fail(f"--action must be one of {', '.join(ACTIONS)}")
    if confidence == "low" and action == "acted":
        fail("confidence low requires --action escalated or provisional")
    if action == "escalated" and (not by.strip() or by.strip().lower() in {"desk", (actor or "").lower()}):
        fail("--action escalated names the Director (or whoever decides) in --by, not the desk itself")
    facts = dict(facts or {})
    for name, value in facts.items():
        if not isinstance(value, bool) or not str(name).strip():
            fail(f"facts are booleans only: {name}={value!r}")
    block = {"question": question.strip(), "options": options, "chosen": chosen.strip(), "confidence": confidence,
             "action": action}
    if facts:
        block["facts"] = facts
    return block


# Release 78 (improvement plan v0.4 bundle 1): the approver of an approval record is always named, never assumed.
# Before, `wi approve` and the gate approvals (`push --approve`, `purge --approve`, `day approve`) wrote
# by=Director when the desk left `--by` out. Old records keep their `by` and load as before.
APPROVER_ROLES = ("Director", "DUREH", "desk")


def approver(by: str | None, allowed: tuple[str, ...] = APPROVER_ROLES, what: str = "this approval") -> str:
    """The named approver of `what`, checked against `allowed`; refused when missing (no default)."""
    name = (by or "").strip()
    if not name:
        fail(f"--by is required for {what}: name who approved ({' | '.join(allowed)}); the approver is never assumed "
             "(release 78: it used to default to the Director, so a desk's own approval was recorded as the Director's)")
    if name not in allowed:
        fail(f"--by must be one of {', '.join(allowed)} for {what} (not {name!r})")
    return name


# Release 78 (improvement plan v0.4 bundle 1, fail-closed kinds): a fail-closed ask never proceeds without an explicit
# answer from its approver: no 30-minute fallback, no timeout approval, no provisional choice by the desk.
FAIL_CLOSED_LABEL = "fail-closed"
FAIL_CLOSED_KINDS = ("a push other than the standing fast-forward (new branch, new remote)", "a dataset or project link",
                     "a config change that changes running behaviour", "a GPU or Slurm request above the caps",
                     "a harness merge or release", "training data or a fine-tuning run",
                     "making anything public or changing organization settings", "a structural change",
                     "closing the day early", "writing or deleting data outside a WI worktree (dataset save, cache or "
                     "image deletion)", "anything about credentials")
FAIL_CLOSED_RULE = ("Fail-closed asks (release 78): " + "; ".join(FAIL_CLOSED_KINDS) + ". For these there is no 30-minute"
                    " fallback and no timeout approval: wait for an explicit answer (the Decision column, the Director's"
                    " words in the session, or a relayed approval); with no answer the action waits to the next working day."
                    " Independent work continues meanwhile. Mark such a board ask fail-closed (`--fail-closed` on the ask, label"
                    " `fail-closed`; its body names no fallback option) and record a decision on it with `day decide"
                    " --fail-closed` (or `--ask <url>`): `--action provisional` is refused, and an `acted` choice needs the"
                    " approver in `--by`.")
_FALLBACK_CUE = re.compile(r"fall\s*-?\s*back|폴백|대체\s*선택|30\s*-?\s*min|30\s*분|무응답|무답|no answer", re.I)
_OPTION_NAME = re.compile(r"\bOption\s*[1-9A-C](?![0-9A-Za-z])|선택지\s*[1-9A-C]|옵션\s*[1-9A-C]|(?<![\d.])[1-9]\s*번(?!째)", re.I)
_NO_FALLBACK = re.compile(r"\bnone\b|\bno fallback\b|\bnot? apply\b|없음|없다|없습니다|적용하지 않|기다린다|기다립니다|wait",
                          re.I)


def fallback_lines(text: str) -> list[str]:
    """Lines of `text` that name a fallback option (a fallback or 30-minute cue together with an option name), unless
    the line says there is none ("Fallback: none, this waits for the Director")."""
    return [ln.strip() for ln in (text or "").splitlines()
            if _FALLBACK_CUE.search(ln) and _OPTION_NAME.search(ln) and not _NO_FALLBACK.search(ln)]


def fail_closed_check(action: str, by: str, actor: str, what: str = "a fail-closed ask") -> None:
    """Refuse a decision on a fail-closed ask that the desk takes itself: `provisional` (the 30-minute fallback path)
    always, and `acted` unless `by` names the approver who answered (not the desk)."""
    if action == "provisional":
        fail(f"refused: {what} has no 30-minute fallback and no provisional choice (release 78, fail-closed); wait "
             "for the approver's explicit answer and keep independent work going (record the wait with "
             "`--action escalated --by Director`)")
    if action == "acted" and (not (by or "").strip() or by.strip().lower() in {"desk", (actor or "").lower()}):
        fail(f"refused: {what} is decided only by its approver's explicit answer (release 78, fail-closed): record "
             "it with `--by Director` (or `--by DUREH` for a layer-2 kind) once that answer exists")


def typed_line(typed: dict | None) -> str:
    """One line for show/list output: `chosen (of a | b | c), confidence medium, acted; facts x=yes, y=no`."""
    if not typed:
        return ""
    line = (f"{typed['chosen']} (of {' | '.join(typed['options'])}), confidence {typed['confidence']}, "
            f"{typed['action']}")
    if typed.get("facts"):
        line += "; facts " + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in typed["facts"].items())
    return line


_RECORD_CACHE: dict[str, dict[str, tuple[tuple[int, int], str]]] = {}  # dir -> file name -> (size, mtime), json


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
        if self.site.observe:
            fail(f"observe mode: refusing to append a {kind} record ({type_})")
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
            if target not in TRANSITIONS.get(current, set()) or ONLY_BY.get((current, target), type_) != type_:
                fail(f"invalid transition for {wi}: {current} -> {target} (event {type_})")
            if target == "staged" and current in STAGED_DIRECT and not self.checker_item(wi):
                fail(f"invalid transition for {wi}: {current} -> staged is only for an item owned by a role with the"
                     " verify capability and without a worktree (a read-only sweep); dispatch it first")
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

    @contextlib.contextmanager
    def lock(self):
        """Exclusive across processes, for read-decide-append sequences run by concurrent hooks."""
        self.site.worktrees.mkdir(exist_ok=True)
        with open(self.site.worktrees / ".ledger.lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

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
        """All records of a kind, oldest first. A file read and checked once is kept in a per-process cache keyed
        by its size and mtime (records are create-only), so the many loads of one command (status reads the
        events a dozen times) cost a directory listing and a stat each, not a read of every file: measured
        2026-10-05, `--observe status` at a site with 3355 events spent minutes in NFS reads."""
        directory = self.site.record_dir(kind)
        if not directory.is_dir():
            fail(f"record store is missing: {directory}")
        cache = _RECORD_CACHE.setdefault(str(directory), {})
        rows, seen = [], set()
        with os.scandir(directory) as it:
            entries = sorted((e for e in it if e.name.endswith(".json")), key=lambda e: e.name)
        for entry in entries:
            path = Path(entry.path)
            try:
                st = entry.stat()
                key = (st.st_size, st.st_mtime_ns)
            except OSError:
                key = None
            hit = cache.get(entry.name)
            if key is not None and hit is not None and hit[0] == key:
                text = hit[1]
            else:
                rec = read_json(path)
                if rec.get("id") != path.stem:
                    fail(f"record id does not match its file name: {path}")
                check_record(rec)
                text = json.dumps(rec)
                if key is not None:
                    cache[entry.name] = (key, text)
            seen.add(entry.name)
            rows.append(json.loads(text))  # a fresh copy: callers may change what they get
        for gone in set(cache) - seen:
            del cache[gone]
        rows.sort(key=lambda r: (r["ts"], r["id"]))
        return rows

    def work_item_payloads(self, events: list[dict] | None = None) -> dict[str, dict]:
        """Every work item's payload with its `wi.amended` fields applied (as `get` does), from one read of the
        events: for loops over all items, where `get` per item would read the events once per item."""
        events = self.events() if events is None else events
        amended: dict[str, list[dict]] = {}
        for e in events:
            if e["type"] == "wi.amended" and e["wi"]:
                amended.setdefault(e["wi"], []).append(e)
        out = {}
        for rec in self.load("work-item"):
            payload = rec["payload"]
            for e in amended.get(rec["id"], []):
                payload.update(e["payload"].get("fields") or {})
            out[rec["id"]] = payload
        return out

    def labels(self, events: list[dict] | None = None) -> dict[str, str]:
        """{WI id: `WI-xxxx (short title)`} for every work item (UR-20261008-02)."""
        return {wi: wi_label(wi, p) for wi, p in self.work_item_payloads(events).items()}

    def get(self, kind: str, record_id: str) -> dict:
        path = self.site.record_dir(kind) / f"{record_id}.json"
        if not path.is_file():
            fail(f"no {kind} record {record_id}")
        rec = read_json(path)
        check_record(rec)
        if kind == "work-item":
            amended = [e for e in self.events(record_id) if e["type"] == "wi.amended"]
            for e in amended:  # records stay create-only; amendments apply on read, in order
                rec["payload"].update(e["payload"].get("fields") or {})
            if amended:
                rec["amended_by"] = [e["id"] for e in amended]
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

    def checker_item(self, wi: str) -> bool:
        """The item is owned by a role with the verify capability and has no worktree (release 81)."""
        role = self.get("work-item", wi)["payload"].get("role")
        return self.site.org.has(role, "verify") and not (self.site.worktrees / wi).is_dir()

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
