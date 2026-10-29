"""Upstream requests: local, offline records that route harness problems (in `dureh_core`,
hooks, `org/roles.yaml`, schemas, or `wi`/`dispatch` behavior) back to the harness developers.

See `notes/upstream/README.md` for the protocol this module implements. This module never
pushes, fetches, or otherwise contacts a remote; it only reads and writes files under the
site's `notes/upstream/` directory and appends `upstream.status` events to the ledger.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .org import HARNESS_ROOT
from .records import Ledger
from .site import Site
from .util import DurehError, dumps, fail, git_revision, utc_now, write_create_only, write_replace

UR_ID = re.compile(r"^UR-(\d{8})-(\d{2,})$")
TYPES = ("bug", "change", "decision", "friction")
SEVERITIES = ("blocking", "degrading", "minor")
STATUSES = ("open", "sent", "accepted", "deferred", "declined", "landed")
# upstream may land a fix without a separate accepted reply (sent -> landed); `deferred` is an accepted
# request the Director postponed: it may go back to accepted, or be declined. Release 85: an `open` request upstream
# already answered with a `landed` message may close directly (open -> landed; the export marked it sent only by mail).
TRANSITIONS = {
    "open": {"sent", "landed"},
    "sent": {"accepted", "declined", "landed"},
    "accepted": {"landed", "deferred"},
    "deferred": {"accepted", "declined"},
    "declined": set(),
    "landed": set(),
}
FINDING_ID_RE = re.compile(r"\bfnd-[A-Za-z0-9._-]+\b")
WI_ID_RE = re.compile(r"\bWI-\d{4,}\b")


def _require_desk(site: Site, actor: str) -> None:
    if site.org.role(actor).kind != "desk":
        fail(f"only the desk manages upstream requests (actor {actor!r} is not the desk)")


def notes_dir(site: Site) -> Path:
    return site.root / "notes" / "upstream"


def _ur_path(dir_: Path, ur_id: str) -> Path:
    if not UR_ID.match(ur_id):
        fail(f"not an upstream request id: {ur_id!r} (expected UR-YYYYMMDD-NN)")
    path = dir_ / f"{ur_id}.md"
    if not path.is_file():
        fail(f"no upstream request file: {path}")
    return path


def _field(text: str, name: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        prefix = f"- {name}:"
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return ""


def _status_word(text: str) -> str:
    field = _field(text, "Status")
    if not field:
        fail("upstream request file has no '- Status:' line")
    word = re.split(r"[\s,(]", field, maxsplit=1)[0].strip()
    if word not in STATUSES:
        fail(f"upstream request file has an unrecognized Status: {field!r}")
    return word


def _next_id(dir_: Path, date_str: str) -> str:
    numbers = []
    if dir_.is_dir():
        for path in dir_.glob(f"UR-{date_str}-*.md"):
            m = UR_ID.match(path.stem)
            if m and m.group(1) == date_str:
                numbers.append(int(m.group(2)))
    return f"UR-{date_str}-{max(numbers, default=0) + 1:02d}"


# ------------------------------------------------------------- severity from facts (D2, release 53)
FACTS = ("blocks_job", "loses_data", "workaround", "affects_all")
_FACT_WORDS = {"yes": True, "true": True, "1": True, "no": False, "false": False, "0": False}


def derive_severity(facts: dict[str, bool]) -> str:
    """The severity the four facts imply (proposal table, D2): blocking if the problem blocks a running job or
    loses data, or affects every WI with no workaround; degrading if there is no workaround or it affects every
    WI; else minor. All four facts must be present."""
    missing = [f for f in FACTS if f not in facts]
    if missing:
        fail(f"cannot derive severity: missing facts {', '.join(missing)}")
    if facts["blocks_job"] or facts["loses_data"] or (facts["affects_all"] and not facts["workaround"]):
        return "blocking"
    if not facts["workaround"] or facts["affects_all"]:
        return "degrading"
    return "minor"


def parse_facts(line: str) -> dict[str, bool]:
    """`blocks_job=no, loses_data=no, workaround=yes, affects_all=no` (the text after `- Facts:`) -> dict of
    bools. Unknown names and non-yes/no values fail; `?` marks a fact not yet known and is left out."""
    out: dict[str, bool] = {}
    for part in filter(None, (p.strip() for p in line.split(","))):
        name, sep, value = part.partition("=")
        name, value = name.strip(), value.strip().lower()
        if not sep or name not in FACTS:
            fail(f"Facts line: expected one of {', '.join(FACTS)}=yes|no, got {part!r}")
        if value == "?":
            continue
        if value not in _FACT_WORDS:
            fail(f"Facts line: {name} must be yes or no, got {value!r}")
        out[name] = _FACT_WORDS[value]
    return out


def facts_line(facts: dict[str, bool]) -> str:
    return ", ".join(f"{f}={'yes' if facts[f] else 'no'}" if f in facts else f"{f}=?" for f in FACTS)


def _severity_word(text: str) -> str:
    field = _field(text, "Severity")
    return re.split(r"[\s,(]", field, maxsplit=1)[0].strip() if field else ""


def check_facts(text: str, name: str = "upstream request") -> None:
    """Refuse a UR whose `- Facts:` line is missing or whose derived severity disagrees with its Severity word,
    unless the Severity line carries `(override: ...)`. Used before a UR leaves the site (send, export)."""
    line = _field(text, "Facts")
    if not line:
        fail(f"{name}: no '- Facts:' line; add `- Facts: {facts_line({})}` under `- Severity:` with yes|no for"
             " each (release 53)")
    facts = parse_facts(line)
    field = _field(text, "Severity")
    word = _severity_word(text)
    if word not in SEVERITIES:
        fail(f"{name}: Severity must start with one of {', '.join(SEVERITIES)}: {field!r}")
    if "(override:" in field:
        return
    missing = [f for f in FACTS if f not in facts]
    if missing:
        fail(f"{name}: Facts line leaves {', '.join(missing)} unknown; answer them yes|no, or override the Severity"
             " with `(override: <why>)`")
    derived = derive_severity(facts)
    if derived != word:
        fail(f"{name}: Severity {word} disagrees with its facts (derived {derived}); change the Severity word or "
             f"write `- Severity: {word} (override: <why>; derived {derived})`")


def draft(site: Site, actor: str, finding: str, type_: str, severity: str | None = None,
          facts: dict[str, bool] | None = None, why: str | None = None) -> dict:
    """Write notes/upstream/UR-<YYYYMMDD>-<NN>.md from a finding. Status starts 'open'.

    Severity is derived from the four facts (`derive_severity`); `severity` is an override that needs `why`.
    Without an override all four facts are required."""
    _require_desk(site, actor)
    if type_ not in TYPES:
        fail(f"--type must be one of {', '.join(TYPES)}: {type_!r}")
    facts = dict(facts or {})
    unknown = [f for f in facts if f not in FACTS]
    if unknown:
        fail(f"--fact takes {', '.join(FACTS)}; unknown: {', '.join(unknown)}")
    missing = [f for f in FACTS if f not in facts]
    if severity is not None and severity not in SEVERITIES:
        fail(f"--severity must be one of {', '.join(SEVERITIES)}: {severity!r}")
    if severity is not None and not (why or "").strip():
        fail("--severity overrides the derived severity and needs --why TEXT")
    if severity is None and why:
        fail("--why goes with --severity")
    derived = derive_severity(facts) if not missing else None
    if severity is None:
        if missing:
            fail("missing facts: " + ", ".join(f"--fact {f}=yes|no" for f in missing)
                 + " (or override with --severity SEV --why TEXT)")
        severity = derived
        severity_field = severity
    else:
        why = why.strip()
        severity_field = (f"{severity} (override: {why}; derived {derived})" if derived
                          else f"{severity} (override: {why}; facts incomplete)")
    led = Ledger(site)
    rec = led.get("finding", finding)
    payload = rec["payload"]
    summary = payload["summary"]
    title = summary if len(summary) <= 72 else summary[:69].rstrip() + "..."

    dir_ = notes_dir(site)
    dir_.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    ur_id = _next_id(dir_, date_str)

    site_id = site.config.get("site", {}).get("id", site.site_id)
    package_id = site.config.get("startup", {}).get("id", "unknown-package")
    harness_rev = git_revision(HARNESS_ROOT)

    content = (
        f"# {ur_id}: {title}\n\n"
        f"- Type: {type_}\n"
        f"- Severity: {severity_field}\n"
        f"- Facts: {facts_line(facts)}\n"
        f"- Site: {site_id}, package {package_id}, harness rev {harness_rev}\n"
        f"- Finding: {finding}\n"
        f"- Status: open\n\n"
        f"## Evidence ({payload['evidence_level']})\n"
        f"{summary}\n\n"
        f"## Request\n<expected behavior or the exact diff>\n\n"
        f"## Options and recommendation\n<at least two if a decision is needed>\n\n"
        f"## Impact at this site\n"
        f"- Blocked WIs: <WIs that wait on this request>\n"
        f"- Continuing WIs: <WIs that proceed meanwhile>\n"
        f"- Workaround in use: <none, or what briefs must say until it lands>\n"
    )
    path = dir_ / f"{ur_id}.md"
    write_create_only(path, content.encode("utf-8"), 0o644)
    payload = {"ur": ur_id, "finding": finding, "type": type_, "severity": severity, "facts": facts}
    if why:
        payload["override_why"] = why
    led.event("upstream.drafted", actor, None, payload, [finding])
    return {"ur": ur_id, "path": str(path), "type": type_, "severity": severity, "facts": facts, "status": "open",
            "finding": finding, **({"override_why": why} if why else {})}


def list_requests(site: Site) -> list[dict]:
    """UR id, type, severity, and Status for every upstream request at this site."""
    dir_ = notes_dir(site)
    out = []
    if not dir_.is_dir():
        return out
    for path in sorted(dir_.glob("UR-*.md")):
        if not UR_ID.match(path.stem):
            continue
        text = path.read_text(encoding="utf-8")
        out.append({"id": path.stem, "type": _field(text, "Type"), "severity": _field(text, "Severity"),
                    "status": _field(text, "Status")})
    return out


CLOSED = ("landed", "declined", "withdrawn")
SEVERITY_ORDER = {s: i for i, s in enumerate(SEVERITIES)}


def _title(text: str, ur: str) -> str:
    first = text.splitlines()[0] if text else ""
    return first.split(":", 1)[1].strip() if first.startswith(f"# {ur}:") else ""


def _request_line(text: str) -> str:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Request":
            for nxt in lines[i + 1:]:
                if nxt.startswith("## "):
                    return ""
                if nxt.strip():
                    line = nxt.strip()
                    return line if len(line) <= 200 else line[:197] + "..."
    return ""


def _open_request_files(site: Site) -> list[Path]:
    out = []
    for path in sorted(notes_dir(site).glob("UR-*.md")) if notes_dir(site).is_dir() else []:
        if not UR_ID.match(path.stem):
            continue
        st = _field(path.read_text(encoding="utf-8"), "Status")
        if (st.split()[0].strip(",;:").lower() if st else "open") == "open":
            out.append(path)
    return out


def message(site: Site) -> str:
    """Paste-ready text the Director forwards upstream. Reads the UR files only; writes nothing. Every bare WI id
    in it carries its short title (UR-20261008-02)."""
    from .records import titled_text

    return titled_text(_message(site), Ledger(site).labels())


def _message(site: Site) -> str:
    dep = site.root / "deployment.json"
    rev = ""
    if dep.is_file():
        import json

        rev = (json.loads(dep.read_text(encoding="utf-8")).get("harness") or {}).get("rev") or ""
    rev = (rev or git_revision(HARNESS_ROOT))[:7]
    new, waiting, deferred = [], [], []
    for path in sorted(notes_dir(site).glob("UR-*.md")) if notes_dir(site).is_dir() else []:
        if not UR_ID.match(path.stem):
            continue
        text = path.read_text(encoding="utf-8")
        st = _field(text, "Status")
        word = st.split()[0].strip(",;:").lower() if st else "open"
        if word in CLOSED:
            continue
        item = {"id": path.stem, "title": _title(text, path.stem), "type": _field(text, "Type"),
                "severity": _field(text, "Severity").split()[0] if _field(text, "Severity") else "",
                "status": st, "request": _request_line(text)}
        (new if word == "open" else deferred if word == "deferred" else waiting).append(item)
    if not new and not waiting and not deferred:
        return f"No open upstream requests at {site.mailbox_name} (harness rev {rev}).\n\n" + "\n".join(_activity(site))
    new.sort(key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["id"]))
    lines = [f"Upstream requests from {site.mailbox_name} (site {site.site_id}, harness rev {rev}).",
             f"Details, evidence and exact diffs: {notes_dir(site)}/<UR>.md", ""]
    n = 0
    if new:
        lines.append("NEW")
        for r in new:
            n += 1
            lines.append(f"{n}. {r['id']} ({r['type']}, {r['severity']}): {r['title']}")
            if r["request"]:
                lines.append(f"   Request: {r['request']}")
        lines.append("")
    if waiting:
        lines.append("AWAITING REPLY OR LANDING")
        for r in waiting:
            n += 1
            lines.append(f"{n}. {r['id']} ({r['severity']}): {r['title']}; status: {r['status']}")
        lines.append("")
    if deferred:
        lines.append("DEFERRED BY THE DIRECTOR (no reply awaited)")
        for r in deferred:
            n += 1
            lines.append(f"{n}. {r['id']} ({r['severity']}): {r['title']}; status: {r['status']}")
        lines.append("")
    lines += _activity(site)
    lines.append("Please reply per UR (accepted, declined or revised), with the order you propose, and give the harness rev"
                 " when a change lands. Merges and syncs happen only in a merge window the Director opens.")
    return "\n".join(lines) + "\n"


def _live_agents(st: dict) -> list[str]:
    orphaned = {o["wi"] for o in st["orphaned"]} | {b["wi"] for b in st.get("bundles_to_close") or []}
    return [d.split(":")[0] for d in st["by_status"].get("dispatched", []) if d.split(" ")[0] not in orphaned]


def _activity(site: Site) -> list[str]:
    from .ops import status

    st = status(site)
    running = [f"{o['wi']} ({o['role']}, agent {o['agent']})" for o in st["orphaned"]]
    live = _live_agents(st)
    # an orphan whose agent ended does not block a merge; one whose spawn has no start yet may be starting
    ready = not live and not any(o["agent"] == "not started" for o in st["orphaned"])
    lines = ["SITE ACTIVITY", f"- active lanes {st['active_lanes']}; agents running: {', '.join(live) or 'none'}"]
    if running:
        lines.append(f"- orphaned: {', '.join(running)}")
    closable = [b["wi"] for b in st.get("bundles_to_close") or []]
    if closable:
        lines.append(f"- bundles to close (manager not needed): {', '.join(closable)}")
    lines += [f"- merge window: {'ready (no running agents)' if ready else 'not ready; the desk reports when idle'}", ""]
    return lines


def status(site: Site, actor: str, ur: str, to: str, rev: str | None = None) -> dict:
    """Append event `upstream.status` and update the Status line. Refuses invalid transitions."""
    _require_desk(site, actor)
    if to not in STATUSES:
        fail(f"--to must be one of {', '.join(STATUSES)}: {to!r}")
    dir_ = notes_dir(site)
    path = _ur_path(dir_, ur)
    text = path.read_text(encoding="utf-8")
    current = _status_word(text)
    if to not in TRANSITIONS.get(current, set()):
        fail(f"invalid transition for {ur}: {current} -> {to}")
    if to == "landed" and not rev:
        fail("--rev is required when --to landed")

    new_field = f"{to} {rev}" if to == "landed" else to
    new_field = f"{new_field} ({utc_now()[:10]})"
    lines = text.splitlines(keepends=True)
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith("- Status:"):
            newline = "\n" if line.endswith("\n") else ""
            lines[i] = f"- Status: {new_field}{newline}"
            replaced = True
            break
    if not replaced:
        fail(f"upstream request file has no '- Status:' line: {path}")
    write_replace(path, "".join(lines), 0o644)

    led = Ledger(site)
    led.event("upstream.status", actor, None, {"ur": ur, "from": current, "to": to, "rev": rev}, [ur])
    return {"ur": ur, "status": new_field, "from": current, "to": to}


def export(site: Site, actor: str, ur: str, out: str | None = None) -> dict:
    """Write a bundle dir with the UR file plus referenced finding and WI summaries.

    No project data, no secrets: only the UR markdown file and metadata already held in the
    ledger (findings and work-item summaries) are copied. Never pushes or contacts a remote.
    """
    _require_desk(site, actor)
    dir_ = notes_dir(site)
    path = _ur_path(dir_, ur)
    text = path.read_text(encoding="utf-8")
    check_facts(text, ur)

    bundle = Path(out).expanduser() if out else dir_ / "export" / ur
    if bundle.exists():
        fail(f"export bundle already exists: {bundle}")
    (bundle / "findings").mkdir(parents=True)
    (bundle / "work-items").mkdir(parents=True)
    write_create_only(bundle / f"{ur}.md", text.encode("utf-8"), 0o644)

    led = Ledger(site)
    findings = []
    for fid in sorted(set(FINDING_ID_RE.findall(text))):
        try:
            rec = led.get("finding", fid)
        except DurehError:
            continue
        write_create_only(bundle / "findings" / f"{fid}.json", dumps(rec).encode("utf-8"), 0o644)
        findings.append(fid)
    work_items = []
    for wid in sorted(set(WI_ID_RE.findall(text))):
        try:
            rec = led.get("work-item", wid)
        except DurehError:
            continue
        st = led.wi_status(wid)
        p = rec["payload"]
        summary = {"id": wid, "title": p["title"], "goal": p["goal"], "role": p["role"],
                   "target": p["target"], "status": st["status"]}
        write_create_only(bundle / "work-items" / f"{wid}.json", dumps(summary).encode("utf-8"), 0o644)
        work_items.append(wid)

    manifest = {"ur": ur, "created": utc_now(), "harness_rev": git_revision(HARNESS_ROOT),
                "findings": findings, "work_items": work_items}
    write_create_only(bundle / "manifest.json", dumps(manifest).encode("utf-8"), 0o644)
    return {"ur": ur, "bundle": str(bundle), "findings": findings, "work_items": work_items}


# ------------------------------------------------------------- mailbox (D6)
SEND_KINDS = ("request", "reply", "idle", "notice", "landed")
TEMPLATE_PLACEHOLDERS = ("<expected behavior or the exact diff>", "<at least two if a decision is needed>",
                         "<WIs that wait on this request>", "<WIs that proceed meanwhile>",
                         "<none, or what briefs must say until it lands>")
CLOSED_WORDS = CLOSED


def exchange_dir(site: Site) -> Path:
    raw = (site.config.get("upstream") or {}).get("exchange")
    if not raw:
        fail("config.json upstream.exchange is not set; the Director sets it to the DUREH-Exchange directory")
    root = Path(site.expand(raw))
    if not root.is_dir():
        fail(f"the upstream exchange {root} does not exist (config.json upstream.exchange)")
    return root / site.mailbox_name


def _header(text: str) -> dict:
    lines = text.splitlines()
    out = {}
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def _body(text: str) -> str:
    parts = text.split("\n---\n", 1)
    return parts[1].strip() if text.startswith("---\n") and len(parts) == 2 else text.strip()


def _messages(box: Path) -> list[Path]:
    return sorted(box.glob("*.md")) if box.is_dir() else []


def _open_requests(site: Site) -> list[dict]:
    out = []
    for r in list_requests(site):
        word = r["status"].split()[0].strip(",;:").lower() if r["status"] else "open"
        if word not in CLOSED_WORDS:
            out.append(r)
    return out


def send(site: Site, actor: str, kind: str = "request", body_file: str | None = None) -> dict:
    """Write one message into <exchange>/<site>/to-upstream/ and keep a FORWARD copy in notes/upstream/."""
    _require_desk(site, actor)
    if kind not in SEND_KINDS:
        fail(f"--kind must be one of {', '.join(SEND_KINDS)}")
    box = exchange_dir(site) / "to-upstream"
    if kind == "idle":
        from .ops import status

        live = _live_agents(status(site))
        if live:
            fail(f"agents are running ({', '.join(live)}); report idle only when none runs")
        body = "idle\n\n" + "\n".join(_activity(site))
    elif body_file:
        body = Path(body_file).read_text(encoding="utf-8")
    elif kind == "request":
        open_files = _open_request_files(site)
        unfilled = [r for r in open_files
                    if any(ph in r.read_text(encoding="utf-8") for ph in TEMPLATE_PLACEHOLDERS)]
        if unfilled:
            fail("fill the template sections before sending (placeholders left in "
                 + ", ".join(p.name for p in unfilled) + ")")
        for r in open_files:
            check_facts(r.read_text(encoding="utf-8"), r.stem)
        body = message(site)
    else:
        fail(f"--kind {kind} needs --body-file")
    refs = [r["id"] for r in _open_requests(site)]
    now = datetime.now(timezone.utc)
    import secrets

    mid = secrets.token_hex(3)
    text = ("---\n" f"id: {mid}\nsite: {site.mailbox_name}\nfrom: site\nkind: {kind}\nrefs: [{', '.join(refs)}]\n"
            f"created: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n---\n" + body.rstrip() + "\n")
    box.mkdir(parents=True, exist_ok=True)
    target = box / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{kind}-{mid}.md"
    write_create_only(target, text.encode("utf-8"), 0o644)
    copy = notes_dir(site) / f"FORWARD-{now.strftime('%Y%m%d-%H%M%S')}-{kind}.md"
    notes_dir(site).mkdir(parents=True, exist_ok=True)
    write_create_only(copy, text.encode("utf-8"), 0o644)
    Ledger(site).event("upstream.sent", actor, None, {"file": target.name, "kind": kind, "refs": refs})
    return {"file": str(target), "copy": str(copy), "kind": kind, "refs": refs}


def notify(site: Site, body: str) -> dict:
    """A harness-originated notice to upstream (for example a new contribution branch)."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(body)
    try:
        return send(site, "desk", "notice", fh.name)
    finally:
        Path(fh.name).unlink(missing_ok=True)


def _received(site: Site) -> set[str]:
    return {e["payload"].get("file") for e in Ledger(site).load("event") if e["type"] == "upstream.received"}


def inbox(site: Site, actor: str, record: bool = False) -> list[dict]:
    """New messages in <exchange>/<site>/from-upstream/; with record, mark them received and keep approvals."""
    box = exchange_dir(site) / "from-upstream"
    seen = _received(site)
    new = []
    for p in _messages(box):
        if p.name in seen:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        h = _header(text)
        new.append({"file": p.name, "kind": h.get("kind"), "refs": h.get("refs"), "body": _body(text)})
    if record and new:
        _require_desk(site, actor)
        led = Ledger(site)
        for m in new:
            led.event("upstream.received", actor, None, {"file": m["file"], "kind": m["kind"], "refs": m["refs"]})
            if m["kind"] == "approval":
                led.append("decision", "upstream-approval", actor, None, {
                    "subject": f"Director approval relayed by upstream ({m['refs']})", "disposition": "approve",
                    "by": "Director", "memo": m["body"], "binding": {"via": "upstream", "file": m["file"]}})
    return new


def _watch_line(m: dict) -> str:
    text = m.get("text") or ""
    mid = _header(text).get("id") or Path(m["file"]).stem
    first = next((ln.strip() for ln in m["body"].splitlines() if ln.strip()), "")
    if len(first) > 160:
        first = first[:157] + "..."
    return f"upstream {m.get('kind') or 'message'} {mid}: {first}"


def watch_inbox(site: Site, actor: str, interval: float = 30, emit=print, sleep=None,
                rounds: int | None = None) -> int:
    """`upstream inbox --watch`: poll <exchange>/<mailbox>/from-upstream/ and emit one line per message not yet
    received (the `upstream.received` records `inbox --record` writes): the unread ones once at start, then each new
    one, every message at most once per process. Records nothing; `inbox --record` stays the explicit read. Runs
    until stopped (rounds: tests only). Returns the number of lines emitted."""
    import time

    sleep = sleep or time.sleep
    box = exchange_dir(site) / "from-upstream"  # refuses without upstream.exchange
    shown: set[str] = set()
    n = done = 0
    while True:
        seen = _received(site)
        for p in _messages(box):
            if p.name in seen or p.name in shown:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            emit(_watch_line({"file": p.name, "kind": _header(text).get("kind"), "body": _body(text), "text": text}))
            shown.add(p.name)
            n += 1
        done += 1
        if rounds is not None and done >= rounds:
            return n
        sleep(max(float(interval), 5.0))


LANDED_CHECKOUT = re.compile(r"adapter sync --harness\s+([^\s`'\"]+)")


def is_checkout(path: Path) -> bool:
    """A harness checkout: an executable `dureh` next to a `dureh_core/` package (the same test as adapter sync)."""
    import os

    return (path / "dureh_core").is_dir() and os.access(path / "dureh", os.X_OK)


RELEASE_TAG = re.compile(r"^r(\d{4})\.(\d{2})\.(\d{2})-(\d+)$")
LANDED_SKIPPED = "harness.switch-skipped"  # release 70: a landed checkout not newer than the site's harness


def _head(path: Path) -> str | None:
    import subprocess

    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _release_key(tag: str | None) -> tuple | None:
    m = RELEASE_TAG.match(tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def release_order(current: Path, candidate: Path) -> dict:
    """Whether `candidate` is a strictly newer harness than `current` (release 70; on 2026-10-16 a site was switched
    from r2026.10.15-3 back to r2026.10.13-2 because the newest landed message named it). Newer means: a different
    revision; when both are release tags (rYYYY.MM.DD-N), a later tag; and, when git can tell, the candidate's
    history contains the current revision. When ancestry cannot be decided (the current revision is not in the
    candidate's repository) the tag order alone decides; with neither, it is not newer. Returns {newer, reason,
    from, to} where from/to are the tag, else the short revision."""
    import subprocess

    from .util import git_tag

    cur_rev, cand_rev = _head(current), _head(candidate)
    cur_tag, cand_tag = git_tag(current), git_tag(candidate)
    name = lambda tag, rev: tag or (rev[:7] if rev else "?")  # noqa: E731
    out = {"from": name(cur_tag, cur_rev), "to": name(cand_tag, cand_rev), "from_rev": cur_rev, "to_rev": cand_rev}

    def no(why: str) -> dict:
        return {**out, "newer": False, "reason": f"{out['to']} is not newer than {out['from']} ({why})"}

    if cand_rev is None:
        return no("not a git checkout")
    if cand_rev == cur_rev:
        return no("same revision")
    ck, kk = _release_key(cur_tag), _release_key(cand_tag)
    if ck and kk and kk <= ck:
        return no("release tag order")
    ancestor = None
    if cur_rev:
        try:
            proc = subprocess.run(["git", "merge-base", "--is-ancestor", cur_rev, cand_rev], cwd=candidate,
                                  capture_output=True, text=True, timeout=60)
            ancestor = {0: True, 1: False}.get(proc.returncode)
        except (OSError, subprocess.SubprocessError):
            ancestor = None
    if ancestor is False:
        return no(f"its history does not contain {cur_rev[:7]}")
    if ancestor is None and not (ck and kk):
        return no("neither commit ancestry nor release tags tell")
    return {**out, "newer": True, "reason": f"{out['to']} is newer than {out['from']}"
            + (" (contains it)" if ancestor else " (release tag order)")}


def skipped_landed(site: Site) -> set[str]:
    """Ids of landed messages a `session start` already skipped as not newer (release 70): never applied later."""
    return {e["payload"].get("landed") for e in Ledger(site).events() if e["type"] == LANDED_SKIPPED}


def pending_landed_checkout(site: Site) -> dict | None:
    """The release checkout the newest `landed` message in <exchange>/<mailbox>/from-upstream/ names
    (`adapter sync --harness <checkout>` in its body) when the site does not run it yet: `{"id", "file",
    "checkout", "created"}`, else None. None when no exchange is configured or reachable, when no landed message
    names a checkout, when the named path is not a checkout, and when it is already the site's pinned harness
    (config.json `harness.root`, what `bin/dureh` execs). Only the newest landed message that names a checkout
    counts: an older landed message never pulls the site back to an older release. Release 70: a landed message
    that `session start` skipped as not newer (`harness.switch-skipped`) is consumed: it is never pending again.
    Whether the checkout is newer is `release_order`'s question, asked by the caller. Read-only."""
    raw = (site.config.get("upstream") or {}).get("exchange")
    if not raw:
        return None
    box = Path(site.expand(raw)) / site.mailbox_name / "from-upstream"
    for p in reversed(_messages(box)):  # names start with the creation time: newest last
        text = p.read_text(encoding="utf-8", errors="replace")
        h = _header(text)
        if (h.get("kind") or "").strip() != "landed":
            continue
        m = LANDED_CHECKOUT.search(_body(text))
        if not m:
            continue
        checkout = Path(site.expand(m.group(1)))
        if not checkout.is_dir() or not is_checkout(checkout):
            return None
        if checkout.resolve() == site.pinned_harness_root.resolve():
            return None
        if (h.get("id") or p.stem) in skipped_landed(site):
            return None
        return {"id": h.get("id") or p.stem, "file": p.name, "checkout": checkout, "created": h.get("created")}
    return None


def collaboration(site: Site) -> dict:
    """independent | collaborating, open requests, blocked WIs, merge window, unread upstream messages."""
    opened = _open_requests(site)
    blocked: list[str] = []
    for r in opened:
        text = (notes_dir(site) / f"{r['id']}.md").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith("- Blocked WIs:"):
                blocked += [w for w in WI_ID_RE.findall(line) if w not in blocked]
    out = {"mode": "collaborating" if opened else "independent", "open": [r["id"] for r in opened],
           "blocked_wis": blocked, "merge_window": "closed", "new_from_upstream": 0, "exchange": None}
    raw = (site.config.get("upstream") or {}).get("exchange")
    if not raw:
        return out
    base = Path(site.expand(raw)) / site.mailbox_name
    out["exchange"] = str(base)
    incoming = _messages(base / "from-upstream")
    seen = _received(site)
    out["new_from_upstream"] = sum(1 for p in incoming if p.name not in seen)
    windows = [p for p in incoming if "-merge-window-" in p.name]
    if windows:
        last = windows[-1]
        if _body(last.read_text(encoding="utf-8")).lower().startswith("open"):
            idle_after = [p for p in _messages(base / "to-upstream") if "-idle-" in p.name and p.name[:16] >= last.name[:16]]
            out["merge_window"] = "open (site idle)" if idle_after else "requested"
    out["landed_open"] = _landed_open(incoming, out["open"])
    if out["merge_window"] != "closed" or out["new_from_upstream"]:
        out["mode"] = "collaborating"
    return out


_LANDED_REV = re.compile(r"\bharness\s+([0-9a-f]{7,40})\b")


def _landed_open(incoming: list[Path], open_ids: list[str]) -> list[dict]:
    """Release 85 (dureh-team#91): open requests that a `landed` message from upstream names in its refs, with the
    harness revision the message gives, so `status` can say "close it" (10-29: three landed requests stayed open at
    one site for days and one was exported again as NEW)."""
    found: dict[str, dict] = {}
    for p in incoming:
        text = p.read_text(encoding="utf-8", errors="replace")
        h = _header(text)
        if (h.get("kind") or "").strip() != "landed":
            continue
        refs = [r.strip() for r in (h.get("refs") or "").strip("[]").split(",") if r.strip()]
        m = _LANDED_REV.search(_body(text))
        for ur in refs:
            if ur in open_ids:
                found[ur] = {"ur": ur, "rev": m.group(1) if m else None, "message": h.get("id") or p.stem}
    return [found[k] for k in open_ids if k in found]
