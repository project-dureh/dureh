"""Goals (release 98, dureh-team#161): the Director's goal, kept verbatim, with completion criteria that name their
deliverables; work items link to a goal and Gate 1 refuses a goal whose criteria no work item covers; a goal closes
only with evidence for every deliverable and the Director's acceptance (fail-closed: the desk cannot close it).

Records. No new record kind: a goal is a projection of events, like a work item's status.
- `goal.created`   {goal, title, text (verbatim), criteria [{id, text, deliverables}], source, by, memo}
- `goal.evidence`  {goal, criterion, deliverable, kind (path|figure|link), value, fingerprint, note}
- `goal.evidence-withdrawn` {goal, evidence, memo}
- `goal.closed`    {goal, decision, digest, by}; refs: the created event and the `goal-acceptance` decision
- `goal.dropped`   {goal, decision, by, memo}; refs: the created event and the `goal-drop` decision
The Director's acceptance is a decision record (`goal-acceptance`, `goal-drop`) bound to the goal and to the digest
of its evidence, as `push --approve` binds a push. `goal.*` events are reserved: `ledger add event` refuses them.

A work item names its goal in `goal_id` (`wi new --goal-id G-0001`); an acceptance line covers a criterion when it
carries the criterion's tag, `[C1]`. An item created for no goal says why (`wi new --no-goal "<reason>"`, recorded
as `no_goal`). Items recorded before release 98 have neither: they are legacy, approved as before with a warning.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .records import Ledger, approver, wi_label
from .site import Site
from .util import DurehError, fail, is_within, sha256_file

GOAL_ID = re.compile(r"^G-(\d{4,})$")
CRIT_TAG = re.compile(r"\[(C\d+)\]")
CREATED, EVIDENCE, WITHDRAWN, CLOSED, DROPPED = ("goal.created", "goal.evidence", "goal.evidence-withdrawn",
                                                 "goal.closed", "goal.dropped")
GOAL_EVENTS = (CREATED, EVIDENCE, WITHDRAWN, CLOSED, DROPPED)
ACCEPTANCE, DROP = "goal-acceptance", "goal-drop"  # decision record types
RESERVED_DECISIONS = (ACCEPTANCE, DROP)
SETTERS = ("Director", "DUREH")  # who set the goal: the Director's own words, or relayed by DUREH
KINDS = ("path", "figure", "link")
FIGURE_SUFFIXES = (".png", ".jpg", ".jpeg", ".svg", ".pdf", ".gif", ".webp", ".tif", ".tiff")
DIR_FILES_MAX = 20000  # a directory of evidence is bound by a manifest of at most this many files
TITLE_MAX = 60
SOURCE_RE = re.compile(r"^(https://\S+|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+|#\d+)$")
FINISHED = ("promoted", "stopped", "reverted")
NOT_COUNTED = ("stopped", "reverted")  # an item in these states covers nothing
STATUS_PREFIX = "Goal:"
NONE_OPEN = "Goal: none open"
NEXT_GOAL_LINE = ("0. Propose the next goal to the Director (no open goal; once the Director decides on the board,"
                  " record it with `dureh goal new --source <board item>`).")


# ------------------------------------------------------------------ parsing
def parse_criterion(spec: str, index: int) -> dict:
    """`<what must hold> => <deliverable>[; <deliverable>...]` -> {id: C<index>, text, deliverables}."""
    text, sep, rest = (spec or "").partition("=>")
    text = " ".join(text.split())
    deliverables = [" ".join(d.split()) for d in rest.split(";")] if sep else []
    deliverables = [d for d in deliverables if d]
    if not text or not deliverables:
        fail(f"--criterion takes \"<what must hold> => <deliverable>[; <deliverable>]\", for example \"each option"
             f" is compared => comparison document; summary figure\" (got {spec!r}): every criterion names its"
             " deliverables")
    if len({d.lower() for d in deliverables}) != len(deliverables):
        fail(f"--criterion {spec!r} names a deliverable twice")
    return {"id": f"C{index}", "text": text, "deliverables": deliverables}


def criterion_tags(lines: list[str] | None) -> list[str]:
    """Criterion ids tagged in acceptance lines (`[C1] ...`), in order of first use."""
    out: list[str] = []
    for line in lines or []:
        for tag in CRIT_TAG.findall(str(line)):
            if tag not in out:
                out.append(tag)
    return out


def _desk(site: Site, actor: str, what: str) -> None:
    if site.org.role(actor).kind != "desk":
        fail(f"only the desk {what}")


def director_terminal() -> bool:
    """The Director's own terminal: no harness identity and no agent session (as `site workday`)."""
    return not (os.environ.get("DUREH_ACTOR") or os.environ.get("CLAUDECODE") or os.environ.get("DUREH_AGENT_ID"))


# ------------------------------------------------------------------ projection
def goals(led: Ledger, events: list[dict] | None = None) -> dict[str, dict]:
    """Every goal of the site, in creation order: its created payload plus `state` (open|closed|dropped),
    `created` (event id), `ts`, `evidence` (current entries, withdrawn ones left out) and the closing record."""
    events = led.events() if events is None else events
    out: dict[str, dict] = {}
    for e in events:
        if not e["type"].startswith("goal."):
            continue
        gid = (e.get("payload") or {}).get("goal")
        if e["type"] == CREATED:
            out[gid] = {**e["payload"], "state": "open", "created": e["id"], "ts": e["ts"], "evidence": [],
                        "withdrawn": []}
            continue
        g = out.get(gid)
        if g is None:
            continue
        if e["type"] == EVIDENCE:
            g["evidence"].append({"id": e["id"], "ts": e["ts"], **e["payload"]})
        elif e["type"] == WITHDRAWN:
            g["withdrawn"].append(e["payload"].get("evidence"))
            g["evidence"] = [x for x in g["evidence"] if x["id"] != e["payload"].get("evidence")]
        elif e["type"] == CLOSED:
            g.update(state="closed", closed={"event": e["id"], "ts": e["ts"], "decision": e["payload"].get("decision"),
                                             "digest": e["payload"].get("digest"), "by": e["payload"].get("by")})
        elif e["type"] == DROPPED:
            g.update(state="dropped", dropped={"event": e["id"], "ts": e["ts"], "decision": e["payload"].get("decision"),
                                               "by": e["payload"].get("by"), "memo": e["payload"].get("memo")})
    return out


def get_goal(led: Ledger, gid: str, events: list[dict] | None = None) -> dict:
    if not GOAL_ID.match(gid or ""):
        fail(f"invalid goal id {gid!r}: goal ids look like G-0001")
    g = goals(led, events).get(gid)
    if g is None:
        fail(f"unknown goal {gid}; record it with `dureh goal new` first (`dureh goal list` shows the site's goals)")
    return g


def open_goals(led: Ledger, events: list[dict] | None = None) -> list[dict]:
    return [g for g in goals(led, events).values() if g["state"] == "open"]


def open_goal(led: Ledger, gid: str, what: str, events: list[dict] | None = None) -> dict:
    g = get_goal(led, gid, events)
    if g["state"] != "open":
        fail(f"goal {gid} is {g['state']}; {what} needs an open goal")
    return g


# ------------------------------------------------------------------ goal new
def new(site: Site, actor: str, text: str, criteria: list[str], source: str, by: str | None,
        title: str | None = None, memo: str = "") -> dict:
    """Record the Director's goal verbatim (`text` is stored exactly as given), with its criteria and the board item
    it comes from (`source`: an https URL, `owner/repo#N` or `#N`; never fetched)."""
    _desk(site, actor, "records goals")
    who = approver(by, SETTERS, "`goal new` (who set the goal)")
    if not (text or "").strip():
        fail("--text must carry the Director's goal, verbatim")
    source = (source or "").strip()
    if not SOURCE_RE.match(source):
        fail(f"--source must name the board item the goal comes from (an https URL, owner/repo#N or #N): {source!r}")
    if not criteria:
        fail("a goal needs at least one --criterion \"<what must hold> => <deliverable>\"")
    crits = [parse_criterion(c, i) for i, c in enumerate(criteria, 1)]
    head = " ".join((title if title is not None else text.strip().splitlines()[0]).split())
    if title is not None and not 1 <= len(head) <= TITLE_MAX:
        fail(f"--title must be 1-{TITLE_MAX} characters: {title!r}")
    if len(head) > TITLE_MAX:
        head = head[:TITLE_MAX - 3].rstrip() + "..."
    led = Ledger(site)
    with led.lock():
        known = goals(led)
        same = [g for g in known.values() if g["state"] == "open" and g.get("source") == source]
        if same:
            fail(f"goal {same[0]['goal']} from {source} is still open; close or drop it before recording another"
                 " goal from the same board item")
        numbers = [int(m.group(1)) for g in known if (m := GOAL_ID.match(g or ""))]
        gid = f"G-{max(numbers, default=0) + 1:04d}"
        return led.event(CREATED, actor, None, {"goal": gid, "title": head, "text": text, "criteria": crits,
                                                "source": source, "by": who, "memo": memo or ""})


# ------------------------------------------------------------------ evidence
def _inside(site: Site, raw: str) -> Path:
    p = Path(raw).expanduser()
    p = (p if p.is_absolute() else site.root / p)
    real = p.resolve()
    if not is_within(real, site.root.resolve()):
        fail(f"evidence must lie inside the site ({site.root}): {raw}")
    return real


def fingerprint(site: Site, path: Path) -> dict:
    """What binds a path: a file's sha256 and size; a directory's manifest (sorted lines of relative path, size and
    sha256 of every file under it, at most DIR_FILES_MAX files)."""
    from . import protect

    why = protect.read_refusal(site, path)
    if why:
        fail(f"refused: {why}")
    if path.is_file():
        return {"type": "file", "sha256": sha256_file(path), "bytes": path.stat().st_size}
    if not path.is_dir():
        fail(f"evidence path does not exist: {path}")
    files = sorted(f for f in path.rglob("*") if f.is_file())
    if len(files) > DIR_FILES_MAX:
        fail(f"{path} holds more than {DIR_FILES_MAX} files; name the report or its figure directory instead")
    h, total = hashlib.sha256(), 0
    for f in files:
        why = protect.read_refusal(site, f)
        if why:
            fail(f"refused: {why}")
        size = f.stat().st_size
        total += size
        h.update(f"{f.relative_to(path).as_posix()}\t{size}\t{sha256_file(f)}\n".encode())
    if not files:
        fail(f"{path} is an empty directory; evidence is a deliverable that exists")
    return {"type": "dir", "sha256": h.hexdigest(), "bytes": total, "files": len(files)}


def _deliverable(crit: dict, name: str | None) -> str:
    dl = crit["deliverables"]
    if name is None or not name.strip():
        if len(dl) == 1:
            return dl[0]
        fail(f"criterion {crit['id']} names {len(dl)} deliverables; say which with --deliverable"
             f" ({' | '.join(dl)}, or its number)")
    name = " ".join(name.split())
    if name.isdigit() and 1 <= int(name) <= len(dl):
        return dl[int(name) - 1]
    match = [d for d in dl if d.lower() == name.lower()]
    if not match:
        fail(f"criterion {crit['id']} has no deliverable {name!r}; its deliverables: {' | '.join(dl)}")
    return match[0]


def add_evidence(site: Site, actor: str, gid: str, criterion: str, kind: str, value: str,
                 deliverable: str | None = None, note: str = "") -> dict:
    """Record one piece of evidence for a deliverable: a `path` (a file or directory inside the site), a `figure`
    (an image or PDF file inside the site) or a `link` (an https URL, for example the board report item). Paths are
    bound by their fingerprint; a later change makes the evidence `changed` until it is recorded again."""
    _desk(site, actor, "records goal evidence")
    led = Ledger(site)
    g = open_goal(led, gid, "evidence")
    crit = next((c for c in g["criteria"] if c["id"] == criterion), None)
    if crit is None:
        fail(f"goal {gid} has no criterion {criterion!r}; its criteria: {', '.join(c['id'] for c in g['criteria'])}")
    dl = _deliverable(crit, deliverable)
    if kind not in KINDS:
        fail(f"evidence kind must be one of {', '.join(KINDS)}")
    value = (value or "").strip()
    payload = {"goal": gid, "criterion": criterion, "deliverable": dl, "kind": kind, "note": note or ""}
    if kind == "link":
        if not re.match(r"^https://\S+$", value):
            fail(f"--link must be an https URL (for example the board item carrying the report): {value!r}")
        payload.update(value=value, fingerprint=None)
    else:
        p = _inside(site, value)
        if kind == "figure" and (not p.is_file() or p.suffix.lower() not in FIGURE_SUFFIXES):
            fail(f"--figure must be an image or PDF file ({', '.join(FIGURE_SUFFIXES)}): {value}")
        payload.update(value=p.relative_to(site.root.resolve()).as_posix(), fingerprint=fingerprint(site, p))
    return led.event(EVIDENCE, actor, None, payload, [g["created"]])


def withdraw_evidence(site: Site, actor: str, gid: str, evidence: str, memo: str) -> dict:
    _desk(site, actor, "withdraws goal evidence")
    if not (memo or "").strip():
        fail("--memo must say why the evidence is withdrawn")
    led = Ledger(site)
    g = open_goal(led, gid, "withdrawing evidence")
    if evidence not in {x["id"] for x in g["evidence"]}:
        fail(f"{evidence} is not current evidence of goal {gid}")
    return led.event(WITHDRAWN, actor, None, {"goal": gid, "evidence": evidence, "memo": memo}, [evidence])


def evidence_state(site: Site, ev: dict) -> str:
    """ok | missing | changed | refused (a path now protected no-read)."""
    if ev["kind"] == "link":
        return "ok"
    p = site.root / ev["value"]
    if not p.exists():
        return "missing"
    try:
        now = fingerprint(site, p.resolve())
    except DurehError as exc:
        return "refused" if "protected" in str(exc) else "missing"
    return "ok" if now.get("sha256") == (ev.get("fingerprint") or {}).get("sha256") else "changed"


# ------------------------------------------------------------------ coverage (Gate 1)
def linked_items(led: Ledger, gid: str, events: list[dict] | None = None,
                 extra: dict[str, dict] | None = None, payloads: dict[str, dict] | None = None) -> list[dict]:
    """Work items that name goal `gid`: {wi, label, status, tags, payload}, stopped and reverted ones included."""
    events = led.events() if events is None else events
    payloads = dict(led.work_item_payloads(events) if payloads is None else payloads)
    payloads.update(extra or {})
    status = Ledger._project(events)
    out = []
    for wi, p in payloads.items():
        if p.get("goal_id") != gid:
            continue
        out.append({"wi": wi, "label": wi_label(wi, p), "status": (status.get(wi) or {}).get("status") or "proposed",
                    "tags": criterion_tags(p.get("acceptance")), "payload": p})
    return out


def coverage(led: Ledger, g: dict, events: list[dict] | None = None, extra: dict[str, dict] | None = None,
             items: list[dict] | None = None) -> dict:
    """{criterion id: [items covering it]}: items of the goal, not stopped or reverted, whose acceptance tags it. An
    item in `extra` (the one at Gate 1, with its draft payload) counts whatever its status (a stopped item being
    re-approved)."""
    items = linked_items(led, g["goal"], events, extra) if items is None else items
    items = [i for i in items if i["status"] not in NOT_COUNTED or i["wi"] in (extra or {})]
    return {c["id"]: [i for i in items if c["id"] in i["tags"]] for c in g["criteria"]}


def coverage_lines(g: dict, cov: dict, this: str | None = None) -> list[str]:
    lines = [f"Goal {g['goal']} ({g['title']}) coverage, criterion -> work items:"]
    for c in g["criteria"]:
        items = cov.get(c["id"]) or []
        who = ", ".join(f"{i['label']} [{i['status']}]" + (" (this item)" if i["wi"] == this else "") for i in items)
        lines.append(f"  {c['id']} {c['text']} (deliverables: {'; '.join(c['deliverables'])}) -> {who or 'NOT COVERED'}")
    return lines


def check_tags(g: dict, acceptance: list[str], what: str) -> None:
    known = {c["id"] for c in g["criteria"]}
    unknown = [t for t in criterion_tags(acceptance) if t not in known]
    if unknown:
        fail(f"{what}: acceptance tags {', '.join(unknown)}, which goal {g['goal']} does not have (its criteria:"
             f" {', '.join(sorted(known))})")


def link_for_new(site: Site, led: Ledger, goal_id: str | None, no_goal: str | None, acceptance: list[str],
                 inherited: str | None) -> dict:
    """The goal fields of a new work item: {goal_id} or {no_goal}, or {} for a library caller that names neither
    (the CLI requires one; release 98). `inherited` is the goal of the item's --parent or --fix-for."""
    if goal_id and no_goal is not None:
        fail("give --goal-id or --no-goal, not both")
    if no_goal is not None:
        if not no_goal.strip():
            fail("--no-goal needs the reason this item serves no goal (for example an urgent repair)")
        return {"no_goal": " ".join(no_goal.split())}
    if not goal_id and inherited:  # inherited only while that goal is open
        known = goals(led).get(inherited)
        goal_id = inherited if known and known["state"] == "open" else None
    if not goal_id:
        return {}
    g = open_goal(led, goal_id, "a new work item")
    check_tags(g, acceptance, "wi new")
    return {"goal_id": goal_id}


def gate1(site: Site, led: Ledger, wi: str, payload: dict) -> dict:
    """The goal part of Gate 1: for an item of a goal, the coverage table, refused while a criterion of the goal is
    covered by no item (this one or another of the goal that is not stopped or reverted). Returns {goal, coverage,
    lines} for a goal item, {no_goal} or {legacy: True} otherwise."""
    gid = payload.get("goal_id")
    if not gid:
        if payload.get("no_goal"):
            return {"no_goal": payload["no_goal"], "lines": [f"Goal: none ({wi} was created with --no-goal:"
                                                             f" {payload['no_goal']})"]}
        return {"legacy": True, "lines": [f"Warning: {wi} names no goal (recorded before release 98, or without the"
                                          f" CLI); link it with `dureh wi amend --wi {wi} --goal-id <G> ...` or approve"
                                          " it as legacy work"]}
    events = led.events()
    g = open_goal(led, gid, f"Gate 1 of {wi}", events)
    check_tags(g, payload.get("acceptance") or [], f"Gate 1 of {wi}")
    cov = coverage(led, g, events, extra={wi: payload})
    lines = coverage_lines(g, cov, wi)
    missing = [c for c, items in cov.items() if not items]
    if missing:
        fail("\n".join([f"refused: Gate 1 of {wi}: criteria {', '.join(missing)} of goal {gid} are covered by no work"
                        " item. Tag the acceptance line that delivers each one (`[C2] ...`, `wi amend --acceptance`),"
                        " or create the work item that does (`wi new --goal-id"
                        f" {gid} --acceptance \"[C2] ...\"`) before approving. Nothing was recorded.", *lines]))
    if wi not in {i["wi"] for items in cov.values() for i in items}:
        lines.append(f"Warning: {wi} tags no criterion of goal {gid}")
    return {"goal": gid, "coverage": {c: [i["wi"] for i in items] for c, items in cov.items()}, "lines": lines}


# ------------------------------------------------------------------ check (read-only)
def digest(evidence: list[dict]) -> str:
    rows = sorted([e["id"], e["criterion"], e["deliverable"], e["kind"], e["value"],
                   (e.get("fingerprint") or {}).get("sha256")] for e in evidence)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def check(site: Site, gid: str) -> dict:
    """Criteria against evidence, read-only (allowed with --observe): per deliverable its evidence and their state,
    per criterion the work items covering it; `ready` when every deliverable has evidence in state ok and no linked
    item is still open; `problems` says what is missing."""
    led = Ledger(site)
    events = led.events()
    g = get_goal(led, gid, events)
    items = linked_items(led, gid, events)
    cov = coverage(led, g, events, items=items)
    problems, ok_evidence, crits = [], [], []
    for c in g["criteria"]:
        dls = []
        for d in c["deliverables"]:
            evs = [{**e, "state": evidence_state(site, e)} for e in g["evidence"]
                   if e["criterion"] == c["id"] and e["deliverable"] == d]
            good = [e for e in evs if e["state"] == "ok"]
            ok_evidence += good
            if not good:
                problems.append(f"{c['id']} deliverable '{d}': " + (
                    "no evidence recorded" if not evs else
                    "evidence " + ", ".join(f"{e['value']} {e['state']}" for e in evs) + " (record it again)"))
            dls.append({"name": d, "ok": bool(good), "evidence": [
                {k: e.get(k) for k in ("id", "kind", "value", "state", "note", "ts")} for e in evs]})
        crits.append({"id": c["id"], "text": c["text"], "deliverables": dls,
                      "work_items": [{"wi": i["wi"], "label": i["label"], "status": i["status"]}
                                     for i in cov.get(c["id"]) or []]})
    running = [i for i in items if i["status"] not in FINISHED]
    for i in running:
        problems.append(f"{i['label']} is still {i['status']}")
    out = {"goal": gid, "title": g["title"], "state": g["state"], "source": g["source"], "text": g["text"],
           "by": g.get("by"), "created": g["ts"], "criteria": crits, "problems": problems,
           "ready": g["state"] == "open" and not problems, "digest": digest(ok_evidence),
           "last_evidence": max((e["ts"] for e in g["evidence"]), default=None)}
    for k in ("closed", "dropped"):
        if g.get(k):
            out[k] = g[k]
    return out


def render_check(r: dict) -> str:
    lines = [f"Goal {r['goal']} [{r['state']}]: {r['title']} (from {r['source']}, set by {r.get('by')})",
             "Director's words:", *[f"  {x}" for x in r["text"].splitlines()]]
    for c in r["criteria"]:
        wis = ", ".join(f"{w['label']} [{w['status']}]" for w in c["work_items"]) or "none"
        lines.append(f"{c['id']} {c['text']} (work items: {wis})")
        for d in c["deliverables"]:
            ev = "; ".join(f"{e['kind']} {e['value']} {e['state']}" for e in d["evidence"]) or "none"
            lines.append(f"  [{'x' if d['ok'] else ' '}] {d['name']}: {ev}")
    if r.get("closed"):
        lines.append(f"Closed {r['closed']['ts'][:16]} by {r['closed']['by']} (acceptance {r['closed']['decision']})")
    elif r.get("dropped"):
        lines.append(f"Dropped {r['dropped']['ts'][:16]} by {r['dropped']['by']}: {r['dropped'].get('memo') or ''}")
    elif r["ready"]:
        lines.append(f"Check: READY for the Director's acceptance (evidence digest {r['digest'][:12]}); close with"
                     f" `dureh goal close --goal {r['goal']} --approval <board item URL>` after the Director's Decision")
    else:
        lines.append(f"Check: NOT READY ({len(r['problems'])} problem{'s' if len(r['problems']) != 1 else ''})")
        lines += [f"  - {p}" for p in r["problems"]]
    return "\n".join(lines)


# ------------------------------------------------------------------ close / drop (fail-closed)
def same_item(ref: str, url: str) -> bool:
    """`ref` (an https URL, `owner/repo#N` or `#N`) names the board item at `url`."""
    ref, url = ref.strip().rstrip("/"), url.strip().rstrip("/")
    if ref.startswith("https://"):
        return ref == url
    m = re.match(r"^(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(\d+)$", ref)
    if not m:
        return False
    tail = re.search(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/(\d+)$", url)
    return bool(tail) and tail.group(2) == m.group(2) and (m.group(1) is None or tail.group(1) == m.group(1))


def _authority(site: Site, g: dict, approval: str | None, by: str | None, memo: str, what: str,
               after: str | None = None) -> dict:
    """Who decided `what` on goal `g`, fail-closed: a recorded board Decision (Approve 1-3, recorded by `github
    watch`, unchanged since, later than `after`, on the goal's source item or a board item given as its evidence),
    or the Director in his own terminal (`--by Director --memo`). The desk's own word never closes a goal."""
    from . import github

    if approval:
        led = Ledger(site)
        events = led.events()
        approval = approval.strip()
        if approval.startswith("https://"):
            decs = [e for e in events if e["type"] == github.DECIDED and e["payload"].get("url") == approval]
            if not decs:
                fail(f"refused: --approval {approval}: no recorded board Decision for that item (github.decision.changed,"
                     " recorded by the desk's `github watch`); nothing was recorded")
            dec = decs[-1]
        else:
            try:
                dec = led.get("event", approval)
            except DurehError:
                fail(f"refused: --approval {approval}: no such record; give the board item URL")
            if dec["type"] != github.DECIDED:
                fail(f"refused: --approval {approval} is a {dec['type']} record, not a board Decision")
        url = dec["payload"].get("url")
        later = [e for e in events if e["type"] == github.DECIDED and e["payload"].get("url") == url
                 and (e["ts"], e["id"]) > (dec["ts"], dec["id"])]
        if later:
            fail(f"refused: the Decision on {url} changed since {dec['id']} ({later[-1]['id']}); give the item URL")
        value = github.decision_value(dec["payload"].get("to"))
        if not github.is_approval(value):
            fail(f"refused: the Decision on {url} is {value or 'empty'}, not an approval (Approve 1-3); {what} waits")
        allowed = [g["source"]] + [e["value"] for e in g["evidence"] if e["kind"] == "link"]
        if not any(same_item(a, url or "") for a in allowed):
            fail(f"refused: the Decision on {url} is not on goal {g['goal']}'s board item ({g['source']}) nor on a board"
                 " item given as its evidence (`goal evidence --link <url>`)")
        if after and dec["ts"] < after:
            fail(f"refused: the Decision on {url} ({dec['ts'][:16]}) is older than the goal's latest evidence"
                 f" ({after[:16]}); the Director decides on the evidence as it is now: ask again on the board")
        return {"by": "Director", "approval": {"record": dec["id"], "url": url, "decision": value,
                                               "director_comments": dec["payload"].get("director_comments") or []}}
    who = (by or "").strip()
    if who != "Director" or not director_terminal():
        fail(f"refused (fail-closed): {what} needs the Director: a board Decision (`--approval <board item URL>` after"
             " the Director chose Approve on the goal's item, read by `github watch`), or the Director runs `--by"
             " Director --memo ...` in his own terminal. The desk, DUREH or an agent session cannot do it; nothing was"
             " recorded")
    if not (memo or "").strip():
        fail("--memo must carry the Director's words")
    return {"by": "Director", "approval": {"terminal": True}}


def close(site: Site, actor: str, gid: str, approval: str | None = None, by: str | None = None,
          memo: str = "") -> dict:
    """Close a goal: every deliverable has evidence in state ok, no linked item is open, and the Director accepted
    (see `_authority`). Records a `goal-acceptance` decision bound to the evidence digest, then `goal.closed`."""
    _desk(site, actor, "closes goals (with the Director's acceptance)")
    led = Ledger(site)
    with led.lock():
        r = check(site, gid)
        if r["state"] != "open":
            fail(f"goal {gid} is {r['state']}")
        if not r["ready"]:
            fail("\n".join([f"refused: goal {gid} is not ready to close; nothing was recorded:",
                            *[f"  - {p}" for p in r["problems"]]]))
        auth = _authority(site, get_goal(led, gid), approval, by, memo, f"closing goal {gid}", after=r["last_evidence"])
        dec = led.append("decision", ACCEPTANCE, actor, None, {
            "subject": f"Goal {gid} accepted: {r['title']}", "disposition": "approve", "by": auth["by"],
            "memo": memo or "", "binding": {"goal": gid, "digest": r["digest"],
                                            "criteria": [c["id"] for c in r["criteria"]], **auth["approval"]}})
        g = get_goal(led, gid)
        return led.event(CLOSED, actor, None, {"goal": gid, "decision": dec["id"], "digest": r["digest"],
                                               "by": auth["by"]}, [g["created"], dec["id"]])


def drop(site: Site, actor: str, gid: str, memo: str, approval: str | None = None, by: str | None = None) -> dict:
    """Drop an open goal (the Director withdrew it): the same authority as closing, no evidence needed."""
    _desk(site, actor, "drops goals (with the Director's decision)")
    if not (memo or "").strip():
        fail("--memo must say why the goal is dropped (the Director's words)")
    led = Ledger(site)
    g = open_goal(led, gid, "dropping")
    auth = _authority(site, g, approval, by, memo, f"dropping goal {gid}")
    dec = led.append("decision", DROP, actor, None, {"subject": f"Goal {gid} dropped: {g['title']}",
                                                     "disposition": "reject", "by": auth["by"], "memo": memo,
                                                     "binding": {"goal": gid, **auth["approval"]}})
    return led.event(DROPPED, actor, None, {"goal": gid, "decision": dec["id"], "by": auth["by"], "memo": memo},
                     [g["created"], dec["id"]])


# ------------------------------------------------------------------ status and day plan
def on_duty(site: Site, events: list[dict] | None = None) -> bool:
    """A day is open (planned, on duty, past its end or in overtime) or a session was started today."""
    from . import workday

    try:
        d = workday.day(site, events)
    except DurehError:
        return False
    return d.get("phase") not in (None, "off-duty") or bool(d.get("session"))


def status_block(site: Site, led: Ledger | None = None, events: list[dict] | None = None) -> dict:
    """For `dureh status`: open goals with their evidence count, whether the site is on duty, and open items that
    name no goal."""
    led = led or Ledger(site)
    events = led.events() if events is None else events
    gs = goals(led, events)
    status = Ledger._project(events)
    payloads = led.work_item_payloads(events)
    open_ = []
    for g in gs.values():
        if g["state"] != "open":
            continue
        need = sum(len(c["deliverables"]) for c in g["criteria"])
        have = len({(e["criterion"], e["deliverable"]) for e in g["evidence"]})
        linked = linked_items(led, g["goal"], events, payloads=payloads)
        items = [i for i in linked if i["status"] not in NOT_COUNTED]
        open_.append({"goal": g["goal"], "title": g["title"], "criteria": len(g["criteria"]), "deliverables": need,
                      "with_evidence": have, "work_items": len(items),
                      "uncovered": [c for c, v in coverage(led, g, events, items=linked).items() if not v]})
    unlinked = sorted(wi for wi, p in payloads.items() if not p.get("goal_id")
                      and (status.get(wi) or {}).get("status") in ("proposed", "approved", "dispatched", "staged",
                                                                   "rework", "verified"))
    return {"open": open_, "on_duty": on_duty(site, events), "closed": sum(g["state"] == "closed" for g in gs.values()),
            "unlinked_open_items": unlinked}


def status_lines(block: dict | None) -> list[str]:
    """Stable, greppable lines, each starting with `Goal:`. With no open goal: `Goal: none open` and, on duty,
    `Goal: none open (on duty): propose the next goal ...`."""
    if not block:
        return []
    lines = []
    for g in block.get("open") or []:
        line = (f"Goal: {g['goal']} open: {g['title']} (criteria {g['criteria']}, deliverables with evidence"
                f" {g['with_evidence']}/{g['deliverables']}, work items {g['work_items']})")
        if g["uncovered"]:
            line += f"; not covered by any work item: {', '.join(g['uncovered'])}"
        lines.append(line)
    if not lines:
        lines.append(f"{NONE_OPEN} (on duty): propose the next goal to the Director; the day plan starts with it"
                     if block.get("on_duty") else NONE_OPEN)
    n = len(block.get("unlinked_open_items") or [])
    if n:
        shown = ", ".join(block["unlinked_open_items"][:10]) + (", ..." if n > 10 else "")
        lines.append(f"Goal: {n} open work item{'s' if n != 1 else ''} name no goal (legacy or --no-goal): {shown}")
    return lines


def plan_prefix(site: Site, text: str) -> str | None:
    """The line a day plan starts with when the site has no open goal (release 98), or None (an open goal exists,
    or the plan already starts by proposing the next goal)."""
    led = Ledger(site)
    if open_goals(led):
        return None
    first = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
    if re.search(r"next goal", first, re.I):
        return None
    return NEXT_GOAL_LINE


def list_lines(led: Ledger, state: str | None = None) -> list[str]:
    out = []
    for g in goals(led).values():
        if state and g["state"] != state:
            continue
        out.append(f"{g['goal']}  {g['state']:8s} {len(g['criteria'])} criteria  {g['source']}  {g['title']}")
    return out
