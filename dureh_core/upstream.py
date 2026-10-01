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
STATUSES = ("open", "sent", "accepted", "declined", "landed")
TRANSITIONS = {
    "open": {"sent"},
    "sent": {"accepted", "declined"},
    "accepted": {"landed"},
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


def draft(site: Site, actor: str, finding: str, type_: str, severity: str) -> dict:
    """Write notes/upstream/UR-<YYYYMMDD>-<NN>.md from a finding. Status starts 'open'."""
    _require_desk(site, actor)
    if type_ not in TYPES:
        fail(f"--type must be one of {', '.join(TYPES)}: {type_!r}")
    if severity not in SEVERITIES:
        fail(f"--severity must be one of {', '.join(SEVERITIES)}: {severity!r}")
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
        f"- Severity: {severity}\n"
        f"- Site: {site_id}, package {package_id}, harness rev {harness_rev}\n"
        f"- Finding: {finding}\n"
        f"- Status: open\n\n"
        f"## Evidence ({payload['evidence_level']})\n"
        f"{summary}\n\n"
        f"## Request\n<expected behavior or the exact diff>\n\n"
        f"## Options and recommendation\n<at least two if a decision is needed>\n\n"
        f"## Impact at this site\n<which WIs wait, what proceeds meanwhile>\n"
    )
    path = dir_ / f"{ur_id}.md"
    write_create_only(path, content.encode("utf-8"), 0o644)
    led.event("upstream.drafted", actor, None, {"ur": ur_id, "finding": finding, "type": type_,
                                                 "severity": severity}, [finding])
    return {"ur": ur_id, "path": str(path), "type": type_, "severity": severity, "status": "open",
            "finding": finding}


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


def message(site: Site) -> str:
    """Paste-ready text the Director forwards upstream. Reads the UR files only; writes nothing."""
    dep = site.root / "deployment.json"
    rev = ""
    if dep.is_file():
        import json

        rev = (json.loads(dep.read_text(encoding="utf-8")).get("harness") or {}).get("rev") or ""
    rev = (rev or git_revision(HARNESS_ROOT))[:7]
    new, waiting = [], []
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
        (new if word == "open" else waiting).append(item)
    if not new and not waiting:
        return f"No open upstream requests at {site.root.name} (harness rev {rev}).\n"
    new.sort(key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["id"]))
    lines = [f"Upstream requests from {site.root.name} (site {site.site_id}, harness rev {rev}).",
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
    lines.append("Please reply per UR (accepted, declined or revised) and give the harness rev when a change lands.")
    return "\n".join(lines) + "\n"


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
