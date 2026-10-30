"""Release 86 (dureh-team#91 item 5): the company's decision digest, in the format of the DUREH team's dashboard
(dureh-team#31): every open item with Status Needs approval, three Korean lines each, read from the issue body, and
every Waiting for Director item with its one action line. The parser is the team's (upstream/tools/dashboard.py);
the team tool reads it from here once this release lands, so the two dashboards read one format.

  결정할 것               the bold "결정할 것:" line, else the first line under "## 결정할 것", else the title
  데스크 추천              the option line marked "(추천)", else a "추천:" / "Recommend:" line, else (a review item)
                          the Approve 1 line
  안 하면 / 답이 없으면    the "30분 안에 답이 없으면:" / "답이 없으면:" / "Fallback:" line; for a `fail-closed` item
                          "없음 (fail-closed), 답을 기다립니다"
  대표님이 하실 일         (Waiting for Director) the bold line under "## 대표님이 하실 일", else its first line

A line not found prints <채워 주세요>; a line still holding a template placeholder (`<...>`) is not found. An item
without a board Priority carries "Priority 없음" in its heading (release 87, dureh-team#108 item 5)."""
from __future__ import annotations

import re

NEEDS, WAITING = "Needs approval", "Waiting for Director"
MISSING = "<채워 주세요>"
NO_PRIORITY = "Priority 없음"  # an item without the board's Priority is marked, not hidden
FAIL_CLOSED = "없음 (fail-closed), 답을 기다립니다"
LINE_MAX = 240
MARK = "<!-- dureh-digest -->"
DESK_MARK = "<!-- dureh-desk -->"  # github.DESK_MARK: watches and the team heartbeat skip desk-written text

_BOLD = re.compile(r"\*\*|__")
_PLACEHOLDER = re.compile(r"<[^<>\s!/][^<>]*>")
DECIDE = re.compile(r"^결정할\s*것\s*[:：]\s*(.+)$")
DECIDE_HEAD = re.compile(r"^#{1,6}\s*결정할\s*것\s*$")
HEADING = re.compile(r"^#{1,6}\s")
OPTION_LINE = re.compile(r"^(?:Approve\s*[1-3]|Option\s*[1-9]|Approved|Rejected?|Discuss|Later)\b.*[:：]", re.I)
DEFAULT_WAIT = re.compile(r"^기다립니다\.?$")
RECOMMEND_OPT = re.compile(r"^(.*?)\s*\(추천\)\s*[:：]?\s*(.*)$")
RECOMMEND = re.compile(r"^(?:두레\s*|데스크\s*)?(?:추천|Recommend(?:ation|ed)?)\s*[:：]\s*(.+)$", re.I)
RECOMMEND_INLINE = re.compile(r"추천은\s+(.+?)\s*입니다")
ACCEPT = re.compile(r"^(Approve\s*1|Approved)\s*[:：]\s*(.+)$")
FALLBACK = re.compile(r"^(?:(?:30\s*분\s*안에\s*)?답이\s*없으면|안\s*하면(?:\s*/\s*답이\s*없으면)?)\s*[:：]\s*(.+)$"
                      r"|^Fallback\s*[:：]\s*(.+)$", re.I)
FALLBACK_INLINE = re.compile(r"답이\s*없으면\s+(.+)$")
STEP_HEAD = re.compile(r"^\s*#{1,6}\s*대표님이\s*하실\s*일\s*$")
_BOLD_TEXT = re.compile(r"\*\*(.+?)\*\*")


def _clean(line: str) -> str:
    """A body line without list marks, numbering, bold marks and hidden comments."""
    line = re.sub(r"<!--.*?-->", "", line)
    line = re.sub(r"^\s*(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|>\s*)", "", line)
    return _BOLD.sub("", line).strip()


def _value(text: str | None) -> str | None:
    text = (text or "").strip()
    return None if not text or _PLACEHOLDER.search(text) else text


def _lines(body: str) -> list[str]:
    out, fence = [], False
    for raw in (body or "").splitlines():
        if raw.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            line = _clean(raw)
            if line:
                out.append(line)
    return out


def _decide_head(lines: list[str]) -> str | None:
    """The question under "## 결정할 것": its first line that is not an option line (None when unfilled)."""
    for k, line in enumerate(lines):
        if DECIDE_HEAD.match(line):
            for nxt in lines[k + 1:]:
                if HEADING.match(nxt):
                    return None
                if not OPTION_LINE.match(nxt):
                    return _value(nxt)
            return None
    return None


def three_lines(title: str, body: str, labels=()) -> dict:
    """{"decide", "recommend", "fallback"} for one item; None where the body has no such line."""
    labels = {str(x).lower() for x in labels or ()}
    lines = _lines(body)
    decide = next((_value(m.group(1)) for m in map(DECIDE.match, lines) if m), None) or _decide_head(lines)
    unfilled = any(_PLACEHOLDER.search(x) for x in lines)
    recommend = None
    for line in lines:
        m = RECOMMEND_OPT.match(line)
        if m:
            opt, text = m.group(1).strip(" :："), _value(m.group(2))
            if m.group(2).strip() and not text:  # an unfilled template line
                break
            recommend = f"{opt}: {text}" if opt and text else opt or text
            break
    if not recommend:
        recommend = next((_value(m.group(1)) for m in map(RECOMMEND.match, lines) if m), None)
    if not recommend:
        recommend = next((_value(m.group(1)) for m in map(RECOMMEND_INLINE.search, lines) if m), None)
    review = "needs-review" in labels or (title or "").startswith("Review:")
    if not recommend and review:
        recommend = next((f"Approve 1: {v}" for v in (_value(m.group(2)) for m in map(ACCEPT.match, lines) if m) if v),
                         None)
    if "fail-closed" in labels:
        fallback = FAIL_CLOSED
    else:
        fallback = next((_value(m.group(1) or m.group(2)) for m in map(FALLBACK.match, lines) if m), None)
        if not fallback:
            fallback = next((_value(m.group(1)) for m in map(FALLBACK_INLINE.search, lines) if m), None)
        if fallback and unfilled and DEFAULT_WAIT.match(fallback):  # the template's own default, not an answer
            fallback = None
    return {"decide": decide or _value(title), "recommend": recommend, "fallback": fallback}


def action_line(body: str) -> str | None:
    """The first line under "## 대표님이 하실 일" (its bold part when it has one); None when the body has no such
    section or the section is empty."""
    lines = (body or "").splitlines()
    for n, raw in enumerate(lines):
        if STEP_HEAD.match(raw):
            for nxt in lines[n + 1:]:
                if nxt.lstrip().startswith("#") or nxt.strip() == "---":
                    return None
                if nxt.strip():
                    m = _BOLD_TEXT.search(nxt)
                    return _value(_clean(m.group(1)) if m else _clean(nxt))
    return None


def _short(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= LINE_MAX else text[:LINE_MAX - 1].rstrip() + "…"


def _ref(it: dict) -> str:
    repo = (it.get("repo") or "").split("/")[-1]
    return f"{repo}#{it['number']}" if it.get("number") else "draft"


def _open(it: dict) -> bool:
    return (it.get("state") or "OPEN") == "OPEN"


def _prio(it: dict) -> str:
    return str(it.get("priority") or "P9")


def digest(items: list[dict]) -> dict:
    """{"decide": [...], "steps": [...]} from board items (github._parse_item rows read with their bodies): open
    Needs approval items with their three lines, open Waiting for Director items with their action line; Priority
    first, then the reference."""
    decide, steps = [], []
    for it in items:
        if not _open(it):
            continue
        row = {"ref": _ref(it), "url": it.get("url"), "title": it.get("title") or "",
               "priority": str(it.get("priority") or ""), "decision": it.get("decision")}
        if it.get("status") == NEEDS:
            decide.append({**row, **three_lines(row["title"], it.get("body") or "", it.get("labels") or ())})
        elif it.get("status") == WAITING:
            steps.append({**row, "action": action_line(it.get("body") or "") or _value(row["title"])})
    key = lambda d: (_prio(d), d["ref"])  # noqa: E731
    return {"decide": sorted(decide, key=key), "steps": sorted(steps, key=key)}


def _head(d: dict, n: int) -> str:
    link = f"[{d['ref']}]({d['url']})" if d.get("url") else d["ref"]
    pr = f" · {d['priority'][:2]}" if d.get("priority") else f" · {NO_PRIORITY}"  # release 87, dureh-team#108 item 5
    return f"#### {n}. {link}{pr}"


def text(dig: dict, org: str, stamp: str) -> str:
    """The digest body (Korean, as the Director reads #31): two sections and the time it was made."""
    out = [f"# {org} 대시보드", "", f"갱신: {stamp} · `dureh github digest` 가 만듭니다 (손으로 고치지 마세요)", "",
           "## 1. 정하실 것 (Needs approval)", ""]
    if not dig["decide"]:
        out += ["결정을 기다리는 항목이 없습니다.", ""]
    for n, d in enumerate(dig["decide"], 1):
        out += [_head(d, n), f"- **결정할 것:** {_short(d.get('decide')) or MISSING}",
                f"- **데스크 추천:** {_short(d.get('recommend')) or MISSING}",
                f"- **안 하면 / 답이 없으면:** {_short(d.get('fallback')) or MISSING}", ""]
    out += ["## 2. 대표님이 하실 일 (Waiting for Director)", ""]
    if not dig["steps"]:
        out += ["없습니다.", ""]
    for n, d in enumerate(dig["steps"], 1):
        out += [_head(d, n), f"- {_short(d.get('action')) or MISSING}", ""]
    out += [MARK, DESK_MARK]  # without the desk mark the issue reads as the Director's own (release 87, a company's office#141)
    return "\n".join(out) + "\n"
