"""The company's GitHub board (release 69): one organization per company, a private repository `office` for issues
not tied to a project, and the organization Project (v2) board with the fields Status, Project, Layer, Decision and
Units. One project is one repository in the organization; one work item is one board item.

The Director decides only through the board's Decision column (approval layer 3); comments are opinions, never
instructions. Layers 1 (the company) and 2 (DUREH, after one delegation) are recorded on the board by the desk.

Organization, board, office repository and token file come only from site config `github` (docs/github.md); no flag
changes them. Every `gh` call goes through `gh()`, which reads the fine-grained token from its file (mode 600) at call
time and passes it to the subprocess as GH_TOKEN only: the token is never printed, logged, put on a command line or
recorded. Records carry ids, URLs and counts (`github.item.added`, `github.item.updated`, `github.ask.created`,
`github.comment.posted`, `github.decision.changed`, `github.issue.opened`), never text.

Release 71: `watch` also reports every newly opened issue in the organization's repositories (the office repository
always, every repository the token can list when it can) that the desk did not write; a `kind:request` issue (the
"일 요청 · Work request" template) is a work request.

Release 79 (v0.4 bundle 1 step 3): the watch asks small pages and scans the other repositories every
ORG_SCAN_INTERVAL; every watch query reports its GraphQL cost to cache/github/usage.jsonl (M1) and a rate-limit hit
adds an `exhausted` line (M2).

Decision values (Director 2026-10-27): Approve 1, Approve 2, Approve 3, Rejected, Discuss. The other names Approved /
Option N / Reject / Later are read as the current names until every board is migrated (`decision_value`): Approved and Option 1 ->
Approve 1, Option 2/3 -> Approve 2/3, Reject -> Rejected, Later -> Discuss. Discuss is a hold: the work does not
proceed and the desk answers the Director in the item's comments; it is neither an approval nor a rejection. Wherever
the desk reads a Decision (`watch`, `status`) it also reads the Director's comments on the item since the ask
(`director_comments`)."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from datetime import date
from pathlib import Path

from .records import Ledger, short_title
from .site import Site
from .util import DurehError, fail

KEYS = ("enabled", "org", "project", "office_repo", "token_file", "director_login", "app", "dashboard_issue")
DEFAULT_OFFICE = "office"
DESK_MARK = "<!-- dureh-desk -->"
DECISION_LABEL = "kind:decision"
REQUEST_LABEL = "kind:request"
# dureh-team#28 A (Director 2026-10-20): Needs approval = the Director approves, chooses or comments (decision asks,
# review requests); Waiting for Director = a step only the Director does
STATUSES = ("Backlog", "Today", "In progress", "Needs approval", "Waiting for Director", "Done")
DECISIONS = ("Approve 1", "Approve 2", "Approve 3", "Rejected", "Discuss")  # Director 2026-10-27, the board's names
APPROVE = DECISIONS[:3]
REJECT, DISCUSS = "Rejected", "Discuss"
DECISION_ALIASES = {"Approved": "Approve 1", "Option 1": "Approve 1", "Option 2": "Approve 2", "Option 3": "Approve 3",
                    "Reject": "Rejected", "Later": "Discuss"}  # other names, read until every board is migrated
_DECISION_CANON = {**{v.lower(): v for v in DECISIONS}, **{k.lower(): v for k, v in DECISION_ALIASES.items()}}
TEAM_MARK = "<!-- dureh -->"  # DUREH's own comments (it posts as the Director's account)
LAYERS = {1: "company", 2: "DUREH", 3: "Director"}
FIELD_STATUS, FIELD_PROJECT, FIELD_LAYER, FIELD_DECISION, FIELD_UNITS = "Status", "Project", "Layer", "Decision", "Units"
# #31: roadmap fields; a Priority of P1 matches an option named "P1" or "P1 ..."; dates are YYYY-MM-DD
FIELD_PRIORITY, FIELD_START, FIELD_TARGET = "Priority", "Start date", "Target date"
PRIORITIES = ("P0", "P1", "P2", "P3")
# Release 87 (Director, dureh-team#108 items 4 and 6): `item ask --decide` holds the question only (the choices go in
# --options), and a layer-3 option that is a step only the Director's hands do is filed with --class D instead. The
# cue list is kept small on purpose: the Director as the subject of a create/make/run verb, in Korean or English.
DECIDE_MAX = 150
_DECIDE_OPTION = re.compile(r"(?<![A-Za-z])(?:Approve\s*[1-3]|Option\s*\d)(?!\d)", re.I)  # "Approve 1로" too
DIRECTOR_HANDS = (re.compile(r"대표님(?:이|께서)\s*[^.\n]{0,40}?(?:생성|만들|만드|직접\s*실행)"),
                  re.compile(r"\b(?:the\s+)?Director\s+(?:creates?|makes?|runs?|sets\s+up|logs\s+in)\b", re.I))
SECRET_MARKS = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")
MAX_BODY = 60000
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
OFF_LINE = "github: off (config github.enabled is not true)"
NO_CONFIG = ("GitHub is not configured for this site: the Director/DUREH set `github` in config.json "
             "({enabled, org, project, office_repo?, token_file, director_login?}; see docs/github.md)")
ADDED, UPDATED, ASKED, COMMENTED, DECIDED = ("github.item.added", "github.item.updated", "github.ask.created",
                                            "github.comment.posted", "github.decision.changed")
OPENED = "github.issue.opened"
CLOSED, SWEPT = "github.item.closed", "github.sweep.done"
# #34: a Done item that still waits for the Director (an answer or a review) stays open for the sweep
HOLD_LABELS = ("waiting-director", "needs-review", "needs-approval")
# release 82 (Director 2026-10-26, dureh-team#74): at most this many open items of the company In progress at once;
# an item moved back to Today with `item set --on-hold` carries ON_HOLD_LABEL until it is In progress again
WIP_IN_PROGRESS = 2
ON_HOLD_LABEL = "on-hold"


def decision_value(value) -> str | None:
    """The Decision's current name for a board value of either set; None when empty; an unknown value unchanged."""
    if value is None or not str(value).strip():
        return None
    text = " ".join(str(value).split())
    return _DECISION_CANON.get(text.lower(), text)


def is_approval(value) -> bool:
    return decision_value(value) in APPROVE


def director_comments(it: dict, director: str | None) -> list[dict]:
    """The Director's comments on the item since the ask (Director 2026-10-27: a Decision is read with them): the
    comments after the last `ANSWER:` pass-on comment (else all of the item's last 30), by `director` (config
    github.director_login; any author when it is unset) and not marked as the desk's or DUREH's. Oldest first, as
    {"text": first non-empty line (at most 200 characters), "url"}."""
    cs = it.get("comments") or []
    start = 0
    for i, c in enumerate(cs):
        if (c.get("body") or "").lstrip().startswith("ANSWER:"):
            start = i + 1
    out = []
    for c in cs[start:]:
        body = c.get("body") or ""
        if DESK_MARK in body or TEAM_MARK in body or (director and c.get("author") != director):
            continue
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        out.append({"text": _one_line(first, 200), "url": c.get("url") or it.get("url")})
    return out


def replies_text(replies: list[dict]) -> str:
    """` | Director: <latest> <url> (+N earlier)`, or that there is none."""
    if not replies:
        return " | Director's comments: none"
    r = replies[-1]
    more = f" (+{len(replies) - 1} earlier)" if len(replies) > 1 else ""
    return f" | Director: {r['text']} {r['url'] or ''}".rstrip() + more


class GhError(DurehError):
    """A failed `gh` call (network, API, permission). `watch` skips the poll; every other command fails."""


# ------------------------------------------------------------------ config
def _raw(config: dict) -> dict | None:
    raw = config.get("github")
    return raw if isinstance(raw, dict) else None


def enabled(site: Site) -> bool:
    raw = _raw(site.config)
    return bool(raw) and raw.get("enabled") is True


def _mode_problem(path: str) -> str | None:
    """Why the token file cannot be used, or None (it must be a regular file no one but its owner can read)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return f"the token file {path} does not exist (the Director places the token there, mode 600)"
    except OSError as exc:
        return f"cannot stat the token file {path}: {exc.strerror}"
    if not stat.S_ISREG(st.st_mode):
        return f"the token file {path} is not a regular file"
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        return f"the token file {path} has mode {mode:o}; it must be 600 (`chmod 600 {path}`)"
    return None


def config_errors(site: Site, config: dict | None = None, startup=None) -> list[str]:
    """What is wrong with config `github` (release 69); `adapter sync` and `verify --self` fail on it, `status` warns.
    A block with `enabled` false is only checked for its shape."""
    raw = (site.config if config is None else config).get("github")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["config github must be an object (enabled, org, project, office_repo, token_file, director_login)"]
    out = [f"config github.{k}: unknown key (release 69: {', '.join(KEYS)})" for k in raw if k not in KEYS]
    on = raw.get("enabled", False)
    if not isinstance(on, bool):
        out.append(f"config github.enabled must be true or false, not {on!r} (default false)")
        on = False
    org, number, office = raw.get("org"), raw.get("project"), raw.get("office_repo", DEFAULT_OFFICE)
    token_file, director = raw.get("token_file"), raw.get("director_login")
    if org is not None and (not isinstance(org, str) or not _LOGIN.match(org)):
        out.append(f"config github.org must be a GitHub organization login such as example-org, not {org!r}")
    if number is not None and (isinstance(number, bool) or not isinstance(number, int) or number < 1):
        out.append(f"config github.project must be the organization Project number (an integer >= 1), not {number!r}")
    if not isinstance(office, str) or not _REPO.match(office):
        out.append(f"config github.office_repo must be a repository name (default \"office\"), not {office!r}")
    if token_file is not None and (not isinstance(token_file, str) or not token_file.strip()):
        out.append("config github.token_file must be a path (the fine-grained token's file, mode 600)")
    if director is not None and (not isinstance(director, str) or not _LOGIN.match(director)):
        out.append(f"config github.director_login must be a GitHub login, not {director!r}")
    dash = raw.get("dashboard_issue")
    if dash is not None and (isinstance(dash, bool) or not isinstance(dash, int) or dash < 1):
        out.append(f"config github.dashboard_issue must be an issue number of office_repo (release 86), not {dash!r}")
    from .github_app import app_errors

    out += app_errors(site, raw.get("app"))
    if on:
        missing = [k for k, v in (("org", org), ("project", number), ("token_file", token_file)) if v is None]
        if missing:
            out.append(f"config github.enabled is true but {', '.join('github.' + k for k in missing)} "
                       f"{'is' if len(missing) == 1 else 'are'} missing (docs/github.md)")
        if isinstance(token_file, str) and token_file.strip() and raw.get("app") is None:  # an App token comes later
            problem = _mode_problem(site.expand(token_file))
            if problem:
                out.append(f"config github.token_file: {problem}")
    return out


def config(site: Site) -> dict:
    raw = _raw(site.config)
    if site.config.get("github") is None:
        fail(NO_CONFIG)
    errs = config_errors(site)
    if errs:
        fail("; ".join(errs))
    if not raw.get("enabled"):
        fail(OFF_LINE)
    return {"org": raw["org"], "project": raw["project"], "office_repo": raw.get("office_repo", DEFAULT_OFFICE),
            "token_file": site.expand(raw["token_file"]), "director_login": raw.get("director_login"),
            "dashboard_issue": raw.get("dashboard_issue")}


def _token(cfg: dict) -> str:
    path = cfg["token_file"]
    problem = _mode_problem(path)
    if problem:
        fail(f"refused: {problem}")
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        fail(f"cannot read the GitHub token file {path}: {exc.strerror}")
    if not token:
        fail(f"the GitHub token file is empty: {path}")
    return token


# ------------------------------------------------------------------ the one runner
def _exec(argv: list[str], env: dict, input_text: str | None, timeout: float) -> subprocess.CompletedProcess:
    """Run gh; tests replace this function."""
    return subprocess.run(argv, env=env, input=input_text, capture_output=True, text=True, timeout=timeout)


def _redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def gh(cfg: dict, args: list[str], *, input_text: str | None = None, timeout: float = 60) -> str:
    """One `gh` call with GH_TOKEN from the token file in the subprocess environment only. Failures raise GhError
    with the token redacted; nothing is retried."""
    token = _token(cfg)
    if any(token in str(a) for a in args):
        fail("refused: the GitHub token would be on the command line")
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")}
    env.update(GH_TOKEN=token, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
    what = " ".join(str(a) for a in args[:2])
    try:
        proc = _exec(["gh", *[str(a) for a in args]], env, input_text, timeout)
    except FileNotFoundError:
        fail("gh (GitHub CLI) is not on PATH")
    except subprocess.TimeoutExpired:
        raise GhError(f"gh {what}: timed out after {timeout:g} s (not retried)") from None
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise GhError(f"gh {what}: exit {proc.returncode}: {_redact(lines[-1] if lines else '', token)[:300]} "
                      "(not retried)")
    return _redact(proc.stdout or "", token)


def graphql(cfg: dict, query: str, variables: dict, costs: list | None = None, name: str | None = None) -> dict:
    """One GraphQL call. When `costs` is a list and the query asks for RATE_LIMIT (release 79), the answer's
    `rateLimit {cost remaining resetAt}` is appended to it as {query: name, cost, remaining, reset_at}."""
    out = gh(cfg, ["api", "graphql", "--input", "-"], input_text=json.dumps({"query": query, "variables": variables}))
    try:
        data = json.loads(out)
    except ValueError:
        raise GhError("gh api graphql: the answer is not JSON (not retried)") from None
    if data.get("errors"):
        msgs = "; ".join(str(e.get("message", e))[:200] for e in data["errors"][:3])
        raise GhError(f"gh api graphql: {msgs}")
    data = data.get("data") or {}
    rl = data.get("rateLimit")
    if costs is not None and isinstance(rl, dict):
        costs.append({"query": name, "cost": rl.get("cost"), "remaining": rl.get("remaining"),
                      "reset_at": rl.get("resetAt")})
    return data


# ------------------------------------------------------------------ the board
_FIELD = "field { ... on ProjectV2FieldCommon { name } }"
BOARD_QUERY = """query($org: String!, $number: Int!, $after: String) {
  organization(login: $org) {
    projectV2(number: $number) {
      id title
      fields(first: 50) { nodes {
        ... on ProjectV2FieldCommon { id name dataType }
        ... on ProjectV2SingleSelectField { options { id name } } } }
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          fieldValues(first: 20) { nodes {
            ... on ProjectV2ItemFieldSingleSelectValue { name %(f)s }
            ... on ProjectV2ItemFieldNumberValue { number %(f)s }
            ... on ProjectV2ItemFieldTextValue { text %(f)s } } }
          content {
            __typename
            ... on Issue { number title url state repository { nameWithOwner }
              assignees(first: 5) { nodes { login } } labels(first: 20) { nodes { name } }
              comments(last: 30) { nodes { id url author { login } body } } }
            ... on PullRequest { number title url state repository { nameWithOwner }
              comments(last: 30) { nodes { id url author { login } body } } }
            ... on DraftIssue { title } } } } } } }""" % {"f": _FIELD}
MAX_PAGES = 20
# release 86 (dureh-team#91 item 5): the digest reads bodies too; the watch's query stays without them
DIGEST_QUERY = BOARD_QUERY.replace("... on Issue { number title url state", "... on Issue { number title url state body") \
    .replace("... on PullRequest { number title url state", "... on PullRequest { number title url state body")


def _parse_meta(proj: dict) -> dict:
    fields = {}
    for f in (proj.get("fields") or {}).get("nodes") or []:
        if f and f.get("name"):
            fields[f["name"]] = {"id": f.get("id"), "type": f.get("dataType"),
                                 "options": {o["name"]: o["id"] for o in f.get("options") or []}}
    return {"id": proj.get("id"), "title": proj.get("title"), "fields": fields}


def _parse_item(node: dict) -> dict:
    values = {}
    for v in (node.get("fieldValues") or {}).get("nodes") or []:
        name = ((v or {}).get("field") or {}).get("name")
        if name:
            values[name] = v.get("name", v.get("number", v.get("text")))
    c = node.get("content") or {}
    comments = [{"id": m.get("id"), "url": m.get("url"), "author": (m.get("author") or {}).get("login"),
                 "body": m.get("body") or ""} for m in (c.get("comments") or {}).get("nodes") or [] if m]
    return {"id": node.get("id"), "type": c.get("__typename"), "number": c.get("number"), "title": c.get("title"),
            "url": c.get("url"), "state": c.get("state"), "repo": (c.get("repository") or {}).get("nameWithOwner"),
            "assignees": [a.get("login") for a in (c.get("assignees") or {}).get("nodes") or [] if a],
            "labels": [n.get("name") for n in (c.get("labels") or {}).get("nodes") or [] if n],
            "status": values.get(FIELD_STATUS), "decision": decision_value(values.get(FIELD_DECISION)),
            "decision_raw": values.get(FIELD_DECISION),
            "project": values.get(FIELD_PROJECT), "layer": values.get(FIELD_LAYER), "units": values.get(FIELD_UNITS),
            "priority": values.get(FIELD_PRIORITY), "body": c.get("body"), "comments": comments}


def board(cfg: dict, query: str = BOARD_QUERY, costs: list | None = None) -> dict:
    """{meta, items}: the board's fields and every item (paged, at most MAX_PAGES x 100)."""
    items, after, meta = [], None, None
    for _ in range(MAX_PAGES):
        data = graphql(cfg, query, {"org": cfg["org"], "number": cfg["project"], "after": after}, costs, "board")
        proj = (data.get("organization") or {}).get("projectV2")
        if not proj:
            raise GhError(f"organization {cfg['org']} has no Project {cfg['project']} the token can read")
        meta = meta or _parse_meta(proj)
        page = proj.get("items") or {}
        items += [_parse_item(n) for n in page.get("nodes") or [] if n]
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
    return {"meta": meta, "items": items}


def status(site: Site) -> dict:
    cfg = config(site)
    b = board(cfg)
    groups: dict[str, list[dict]] = {s: [] for s in STATUSES}
    for it in b["items"]:
        row = {k: it[k] for k in ("number", "repo", "title", "url", "status", "decision", "project", "layer", "units",
                                  "assignees")}
        row["comments"] = len(it["comments"])
        row["director_comments"] = director_comments(it, cfg.get("director_login"))
        groups.setdefault(it["status"] or "(no status)", []).append(row)
    return {"org": cfg["org"], "project": cfg["project"], "title": b["meta"]["title"],
            "groups": {k: v for k, v in groups.items() if v}, "count": len(b["items"])}


DIGESTED = "github.digest.written"


def refresh_digest(site: Site, actor: str | None) -> str | None:
    """Release 87 (the Director 2026-10-30: a company dashboard still listed a finished step): after a desk write to
    the board, keep the dashboard issue current when config github.dashboard_issue is set. One line, or None when
    no dashboard is configured; a failure is reported in the line, never raised (the write itself already ran)."""
    try:
        if not (config(site).get("dashboard_issue")):
            return None
        res = digest(site, actor, write=True)
    except (GhError, DurehError) as exc:
        return f"digest: not refreshed: {exc}"
    return f"digest: {res['url']} {'unchanged' if res.get('unchanged') else 'refreshed'}"


def digest(site: Site, actor: str | None = None, write: bool = False) -> dict:
    """Release 86 (dureh-team#91 item 5): the company dashboard in the team's #31 format (board_digest): what waits
    for the Director's decision and his own steps on this board. Printed by default; with `write` (the desk) the body
    of issue config github.dashboard_issue in office_repo is replaced (only when it changed), records
    github.digest.written. A write without dashboard_issue is refused."""
    from . import board_digest

    cfg = config(site)
    if write:
        _require_desk(site, actor)
        if not cfg.get("dashboard_issue"):
            fail("config github.dashboard_issue is not set: the Director/DUREH name the office_repo issue that holds "
                 "the dashboard (docs/github.md); `github digest` without --write prints it")
    dig = board_digest.digest(board(cfg, DIGEST_QUERY)["items"])
    body = board_digest.text(dig, cfg["org"], time.strftime("%Y-%m-%d %H:%M %Z"))
    res = {"decide": len(dig["decide"]), "steps": len(dig["steps"]), "text": body, "written": False}
    if not write:
        return res
    repo = f"{cfg['org']}/{cfg['office_repo']}"
    target = f"https://github.com/{repo}/issues/{cfg['dashboard_issue']}"
    old = gh(cfg, ["issue", "view", target, "--json", "body", "--jq", ".body"])
    stale = lambda t: "\n".join(x for x in (t or "").splitlines() if not x.startswith("갱신: "))  # noqa: E731
    if stale(old.strip()) == stale(body.strip()):
        return {**res, "url": target, "unchanged": True}
    gh(cfg, ["issue", "edit", target, "--body-file", "-"], input_text=_clean(body, "the dashboard"))
    Ledger(site).event(DIGESTED, actor, None, {"url": target, "decide": res["decide"], "steps": res["steps"]})
    return {**res, "written": True, "url": target}


def _ref(it: dict) -> str:
    repo = (it.get("repo") or "").split("/")[-1]
    return f"{repo}#{it['number']}" if it.get("number") else "draft"


def status_lines(res: dict) -> list[str]:
    lines = [f"{res['org']} Project {res['project']} ({res['title'] or '?'}): {res['count']} item(s)"]
    for name, rows in res["groups"].items():
        lines.append(f"{name} ({len(rows)})")
        for r in rows:
            extra = "".join(f" [{k}: {r[k]}]" for k in ("decision", "project", "layer") if r.get(k) not in (None, ""))
            lines.append(f"  {_ref(r)} {r['title'] or ''}{extra}")
            said = r.get("director_comments") or []
            if r.get("decision") == DISCUSS:
                lines.append("    Discuss: hold; the work does not proceed; answer the Director in the item's comments")
            if said and (r.get("decision") or r.get("status") in ("Needs approval", "Waiting for Director")):
                more = f" (+{len(said) - 1} earlier)" if len(said) > 1 else ""
                lines.append(f"    Director: {said[-1]['text']} {said[-1]['url'] or ''}".rstrip() + more)
    return lines


# ------------------------------------------------------------------ watch
def state_path(site: Site) -> Path:
    """The watch's last-seen Decisions and comments: a runtime cache under the site (not a record, not memory)."""
    return site.root / "cache" / "github" / "watch.json"


def _load_state(site: Site, cfg: dict) -> dict | None:
    try:
        data = json.loads(state_path(site).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("board") != f"{cfg['org']}/{cfg['project']}":
        return None
    return data


def _save_state(site: Site, data: dict) -> None:
    path = state_path(site)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _one_line(text: str, limit: int = 300) -> str:
    text = " ".join(text.replace(DESK_MARK, "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _linked(site: Site) -> dict[str, dict]:
    """{item_id: {wi, url, layer, ask, fail_closed}} from the records of this site's board items. Release 81
    (finding cc81a566): an item made by `item ask` carries `ask: true`; its `wi` is the WI the ask is about, not
    that WI's board link (linked_item skips it for --wi). Release 87 (a company's finding): a `--class D` step
    (github.director-step.created) is linked the same way, so `item set/close --url` accept it."""
    out: dict[str, dict] = {}
    for e in Ledger(site).events():
        if e["type"] in (ADDED, ASKED, UPDATED, DIRECTOR_STEP) and e["payload"].get("item_id"):
            row = out.setdefault(e["payload"]["item_id"], {})
            if e["type"] in (ASKED, DIRECTOR_STEP):  # release 87: a class D step is set and closed by --url too
                row["ask"] = True
            if e["wi"]:
                row["wi"] = e["wi"]
            row["url"] = e["payload"].get("url") or row.get("url")
            layer = (e["payload"].get("fields") or {}).get(FIELD_LAYER) or e["payload"].get("layer")
            if layer is not None:
                row["layer"] = layer
            if e["payload"].get("fail_closed"):
                row["fail_closed"] = True
    return out


def ask_fail_closed(site: Site, url: str | None) -> bool:
    """Release 78: True when the board ask at `url` was recorded fail-closed (`github item ask --fail-closed`)."""
    target = (url or "").strip()
    return bool(target) and any(row.get("url") == target and row.get("fail_closed") for row in _linked(site).values())


# Release 79 (v0.4 bundle 1 step 3, G1-G3/M1): GitHub prices a GraphQL query by the page sizes it asks for
# (`first`/`last`), not by what comes back: 100 repositories x 50 issues x 20 labels cost about 51 points per poll
# for an organization of 2-3 repositories. The watch asks for small pages, pages repositories only when there are
# more, scans the organization every ORG_SCAN_INTERVAL seconds, and asks every query for its own cost.
RATE_LIMIT = "rateLimit { cost remaining resetAt }"
ISSUE_PAGE = 20
_ISSUE = ("issues(first: %d, states: OPEN, orderBy: {field: CREATED_AT, direction: DESC}) { nodes {"
          " id number title url createdAt body author { login } labels(first: 10) { nodes { name } } } }" % ISSUE_PAGE)
OFFICE_ISSUES_QUERY = ("query($org: String!, $repo: String!) { %s repository(owner: $org, name: $repo) {"
                       " nameWithOwner %s } }" % (RATE_LIMIT, _ISSUE))
ORG_ISSUES_QUERY = ("query($org: String!, $after: String) { %s organization(login: $org) { repositories(first: 10,"
                    " after: $after, orderBy: {field: PUSHED_AT, direction: DESC}) { pageInfo { hasNextPage endCursor }"
                    " nodes { nameWithOwner isArchived %s } } } }" % (RATE_LIMIT, _ISSUE))
MAX_REPO_PAGES = 10
ORG_SCAN_INTERVAL = 600.0  # G2: the other repositories every 10 minutes; the office repository every poll
# G3: the watch reads Decisions and comments only (no assignees, no labels) and asks for its cost
WATCH_BOARD_QUERY = (BOARD_QUERY.replace("query($org: String!, $number: Int!, $after: String) {",
                                         "query($org: String!, $number: Int!, $after: String) {\n  %s" % RATE_LIMIT)
                     .replace("\n              assignees(first: 5) { nodes { login } } labels(first: 20) { nodes { name } }",
                              ""))
MAX_SEEN_ISSUES = 5000


def _parse_issues(repo_node: dict) -> list[dict]:
    repo = repo_node.get("nameWithOwner")
    out = []
    for n in (repo_node.get("issues") or {}).get("nodes") or []:
        if not n or not n.get("id"):
            continue
        out.append({"id": n["id"], "repo": repo, "number": n.get("number"), "title": n.get("title") or "",
                    "url": n.get("url"), "created": n.get("createdAt") or "", "body": n.get("body") or "",
                    "author": (n.get("author") or {}).get("login"),
                    "labels": [lab.get("name") for lab in (n.get("labels") or {}).get("nodes") or [] if lab]})
    return out


def open_issues(cfg: dict, scan_org: bool = True, costs: list | None = None) -> tuple[dict[str, list[dict]], bool]:
    """({repo: [open issue]}, org_listed): the office repository's newest open issues (required; a failure raises
    GhError) and, with `scan_org` and when the token can list them, those of every other unarchived repository of the
    organization (10 repositories a page)."""
    data = graphql(cfg, OFFICE_ISSUES_QUERY, {"org": cfg["org"], "repo": cfg["office_repo"]}, costs, "office_issues")
    office = data.get("repository")
    if not office:
        raise GhError(f"repository {cfg['org']}/{cfg['office_repo']} is not readable with the token")
    repos = {office.get("nameWithOwner") or f"{cfg['org']}/{cfg['office_repo']}": _parse_issues(office)}
    if not scan_org:
        return repos, False
    after = None
    for _ in range(MAX_REPO_PAGES):
        try:
            org = graphql(cfg, ORG_ISSUES_QUERY, {"org": cfg["org"], "after": after}, costs,
                          "org_issues").get("organization") or {}
        except GhError as e:
            if rate_limited(e):
                raise
            return repos, False  # the token may not list the organization's repositories: the office repository only
        page = org.get("repositories") or {}
        for r in page.get("nodes") or []:
            if r and r.get("nameWithOwner") and not r.get("isArchived") and r["nameWithOwner"] not in repos:
                repos[r["nameWithOwner"]] = _parse_issues(r)
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
    return repos, True


def _utc_stamp(old: dict | None) -> str | None:
    """The last saved poll time as UTC `YYYY-MM-DDTHH:MM:SSZ` (older states saved local time without a zone)."""
    if not old:
        return None
    if old.get("polled_utc"):
        return old["polled_utc"]
    try:
        t = time.mktime(time.strptime(old.get("polled") or "", "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _org_scan_due(old: dict | None, now: float) -> bool:
    """G2: the organization's other repositories are scanned at the first poll and then every ORG_SCAN_INTERVAL s."""
    try:
        last = float((old or {}).get("org_scanned_at"))
    except (TypeError, ValueError):
        return True
    return not (0 <= now - last < ORG_SCAN_INTERVAL)


def _issue_lines(site: Site, actor: str, cfg: dict, old: dict | None,
                 costs: list | None = None) -> tuple[list[str], dict]:
    """Lines for newly opened issues not written by the desk, and the issue part of the state. A repository seen for
    the first time (or the first poll) takes its open issues as seen, except those opened after the last saved poll.
    Only the newest ISSUE_PAGE open issues of a repository come back: when that page is full the first time a repository
    is scanned, its oldest creation time is the repository's floor, and an older unseen issue that later slides into
    the page (newer ones closed) was open before the watch and is taken as seen, not reported."""
    now = time.time()
    scan = _org_scan_due(old, now)
    repos, _ = open_issues(cfg, scan, costs)
    org_at = now if scan else (old or {}).get("org_scanned_at")
    old = old or {}
    seen = set(old.get("issues") or [])
    scanned = set(old.get("issue_repos") or [])
    since = _utc_stamp(old) if old else None
    floors = dict(old.get("issue_floors") or {})
    lines, led = [], Ledger(site)
    for repo, issues in repos.items():
        fresh_repo = repo not in scanned
        floor = floors.get(repo)
        if floor is None:
            floors[repo] = min(i["created"] for i in issues) if len(issues) >= ISSUE_PAGE else ""
        for it in sorted(issues, key=lambda i: (i["created"], i["number"] or 0)):
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            if fresh_repo and not (since and it["created"] > since):
                continue
            if floor and it["created"] < floor:
                continue
            if DESK_MARK in it["body"]:
                continue
            request = REQUEST_LABEL in it["labels"]
            kind = "work request" if request else "issue"
            short = (repo or "").split("/")[-1]
            lines.append(f"{kind} {short}#{it['number']} by {it['author'] or '?'}: {_one_line(it['title'], 200)} | "
                         f"{it['url'] or ''}".rstrip())
            led.event(OPENED, actor, None, {"issue_id": it["id"], "repo": repo, "number": it["number"],
                                            "url": it["url"], "author": it["author"], "request": request,
                                            "created": it["created"]})
    keep = sorted(seen)[-MAX_SEEN_ISSUES:] if len(seen) > MAX_SEEN_ISSUES else sorted(seen)
    return lines, {"issues": keep, "issue_repos": sorted(scanned | set(repos)), "org_scanned_at": org_at,
                   "issue_floors": floors}


FOLLOWUP_AFTER = 20 * 60  # release 80 (project-dureh/dureh-team#63): seconds after a Decision before the reminder
FOLLOWUP_DECISIONS = (*APPROVE, DISCUSS)  # Discuss: the desk owes the Director an answer in the comments


def _clock() -> float:
    return time.time()


def _followups(items: list[dict], pending: dict, first: bool, changed: set) -> tuple[list[str], dict]:
    """Release 80 (dureh-team#63 part 3): a Decision the desk must act on (Approve 1-3, or Discuss: an answer in
    the comments) whose item is still open 20 minutes later with no new desk comment gets one line: report the
    follow-up, or the block and the Director's step, on that item (Discuss: answer the Director's comments). `pending`
    is {item id: {"at", "decision", "before": [comment ids]}}."""
    now, out, keep = _clock(), [], {}
    by_id = {it["id"]: it for it in items}
    for iid, p in pending.items():
        it = by_id.get(iid)
        if not it or it.get("state") not in (None, "OPEN") or it.get("decision") != decision_value(p.get("decision")):
            continue
        before = set(p.get("before") or [])
        if any(c.get("id") not in before and DESK_MARK in (c.get("body") or "") for c in it["comments"]):
            continue
        if now - float(p.get("at") or now) < FOLLOWUP_AFTER:
            keep[iid] = p
            continue
        dec = decision_value(p["decision"])
        todo = ("answer the Director's comments on the item (Discuss: a hold, the work does not proceed)"
                if dec == DISCUSS else "report the follow-up on the item, or the block with the Director's step "
                                       "(대표님 조치 필요)")
        out.append(f"followup: {_ref(it)} {it['title'] or ''} -> {dec} {int((now - p['at']) // 60)} min ago, "
                   f"still open and no desk comment since: {todo} {it['url'] or ''}".rstrip())
    if not first:
        for it in items:
            if it["id"] in changed and it.get("decision") in FOLLOWUP_DECISIONS and it.get("state") in (None, "OPEN"):
                keep[it["id"]] = {"at": now, "decision": it["decision"],
                                  "before": [c["id"] for c in it["comments"] if c.get("id")]}
    return out, keep


def poll(site: Site, actor: str, cfg: dict) -> list[str]:
    """One poll: the lines for Decision changes and new comments not marked as the desk's; saves the state. The first
    poll (no state) reports the Decisions already set and takes the existing comments as seen. Newly opened issues
    not by the desk come after (`_issue_lines`)."""
    costs: list[dict] = []
    try:
        b = board(cfg, WATCH_BOARD_QUERY, costs)
        old = _load_state(site, cfg)
        issue_lines, issue_state = _issue_lines(site, actor, cfg, old, costs)
    finally:
        note_usage(site, costs)
    if old and old.get("decisions") and not b["items"]:
        # GitHub at times answers a board query with no items (2026-10-22); saving that would replay every Decision
        # and comment as new on the next poll. A failed read: the poll is skipped and the state kept.
        raise GhError(f"the board {cfg['org']}/{cfg['project']} answered with no items; kept the last state")
    first = old is None
    # stored values of either set read as the current names: a migrated option (Approved -> Approve 1) is no change
    seen_dec = {} if first else {k: decision_value(v) for k, v in (old.get("decisions") or {}).items()}
    seen_com = {} if first else {k: set(v) for k, v in (old.get("comments") or {}).items()}
    lines, links, led = [], None, Ledger(site)
    new_dec, new_com, changed = {}, {}, set()
    for it in b["items"]:
        iid, dec = it["id"], it["decision"]
        new_dec[iid] = dec
        if dec != seen_dec.get(iid) and not (first and dec is None):
            changed.add(iid)
            links = _linked(site) if links is None else links
            link = links.get(iid, {})
            was = seen_dec.get(iid)
            layer = f" [layer {it['layer']}]" if it.get("layer") else ""
            wi = f" {link['wi']}" if link.get("wi") and link["wi"] not in (it["title"] or "") else ""
            said = director_comments(it, cfg.get("director_login")) if dec else []
            hold = (" | Discuss: hold, the work does not proceed; answer the Director in the item's comments"
                    if dec == DISCUSS else "")
            lines.append(f"decision: {_ref(it)}{wi} {it['title'] or ''} -> {dec or 'none'} (was {was or 'none'})"
                         f"{layer} {it['url'] or ''}".rstrip() + (hold + replies_text(said) if dec else ""))
            led.event(DECIDED, actor, link.get("wi"), {"item_id": iid, "url": it["url"], "from": was, "to": dec,
                                                        "layer": it.get("layer"), "first_poll": first,
                                                        "director_comments": [c["url"] for c in said]})
            if not first:
                lines += _follow_decision(site, actor, cfg, it, dec, said, link.get("wi"))
        ids = [c["id"] for c in it["comments"] if c.get("id")]
        before = seen_com.get(iid, set())
        if not first:
            for c in it["comments"]:
                if c.get("id") and c["id"] not in before and DESK_MARK not in c["body"]:
                    lines.append(f"comment: {_ref(it)} by {c['author'] or '?'}: {_one_line(c['body'])} "
                                 f"{c['url'] or ''}".rstrip())
        new_com[iid] = sorted(before | set(ids))[-200:]
    follow, pending = _followups(b["items"], {} if first else dict(old.get("followups") or {}), first, changed)
    lines += follow
    _save_state(site, {"board": f"{cfg['org']}/{cfg['project']}", "decisions": new_dec, "comments": new_com,
                       "followups": pending,
                       "polled": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "polled_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **issue_state})
    return lines + issue_lines


def usage_path(site: Site) -> Path:
    """M1/M2 (release 79): the watch's measured GitHub cost, one JSON line per query and one per rate-limit hit; a
    runtime cache next to watch.json (not a record)."""
    return site.root / "cache" / "github" / "usage.jsonl"


USAGE_MAX_BYTES = 2_000_000


def _append_usage(site: Site, rows: list[dict]) -> None:
    if not rows:
        return
    path = usage_path(site)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > USAGE_MAX_BYTES:  # keep the newer half
            kept = path.read_text(encoding="utf-8").splitlines()
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(f"{x}\n" for x in kept[len(kept) // 2:]), encoding="utf-8")
            os.replace(tmp, path)
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass  # measurement must never stop the watch


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def note_usage(site: Site, costs: list[dict], caller: str = "watch") -> None:
    """M1: one line {ts, caller, query, cost, remaining, reset_at} per GraphQL query that reported its cost."""
    ts = _now_utc()
    _append_usage(site, [{"ts": ts, "caller": caller, **c} for c in costs])


def note_exhausted(site: Site, cfg: dict, err: Exception, caller: str = "watch") -> dict:
    """M2: one `exhausted` line when a poll hits a rate limit: the time, the error (redacted, short) and, from
    `gh api rate_limit` (which GitHub does not count), the GraphQL and core remaining and reset times."""
    row = {"ts": _now_utc(), "caller": caller, "event": "exhausted", "error": str(err)[:200]}
    try:
        res = (json.loads(gh(cfg, ["api", "rate_limit"], timeout=30)) or {}).get("resources") or {}
        for kind in ("graphql", "core"):
            r = res.get(kind) or {}
            if r:
                row[kind] = {"remaining": r.get("remaining"), "used": r.get("used"), "limit": r.get("limit"),
                             "reset_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(r["reset"])))
                             if r.get("reset") is not None else None}
    except (GhError, DurehError, ValueError, TypeError, AttributeError):
        pass
    _append_usage(site, [row])
    return row


WATCH_INTERVAL = 120.0  # release 73: the team and the desks poll as one GitHub user; 30 s hit the secondary limit
BACKOFF_CAP = 900.0
RATE_LIMIT_MARKS = ("api rate limit already exceeded", "api rate limit exceeded", "secondary rate limit")


def rate_limited(err: Exception) -> bool:
    """True when a gh failure is GitHub's primary or secondary rate limit."""
    low = str(err).lower()
    return any(m in low for m in RATE_LIMIT_MARKS)


def _retry_after(err: Exception) -> float | None:
    m = re.search(r"retry[- ]after[:= ]*(\d+)", str(err), re.I)
    return float(m.group(1)) if m else None


APPROVAL_LABELS = ("needs-approval", "needs-review")
KEEP_ON_DECISION = ("In progress", "Waiting for Director", "Done")


def board_status(*, state=None, merged=False, labels=(), decision=None, status=None, director_merges_pr=False):
    """Release 86 (dureh-team#91 item 1): the boards' one Status rule, used by `github watch` here and by the team's
    status_sync and heartbeat. Returns (Status required or None to leave it, why, conflict note or None):

      closed or merged                                         Done
      label waiting-director                                   Waiting for Director (a conflict when needs-approval
                                                                 or needs-review is also set and no Decision is)
      label needs-approval or needs-review, no Decision        Needs approval
      label on-hold                                            Today
      `director_merges_pr` (an open, non-draft PR only the      Waiting for Director
        Director merges)
      Decision Approve 1-3 or Rejected                         Today, unless In progress, Waiting for Director or
                                                                 Done (a Done item open for the sweep)
      otherwise (Discuss, a hold, included)                    None (unchanged; In progress is never inferred)"""
    labels, current = {str(x).lower() for x in labels or ()}, status
    dec = decision_value(decision)
    held = dec == DISCUSS
    if held:
        dec = None
    if merged or state in ("CLOSED", "MERGED"):
        return "Done", "merged" if (merged or state == "MERGED") else "closed", None
    approval = sorted(labels & set(APPROVAL_LABELS))
    if "waiting-director" in labels:
        if approval and not dec:
            return None, "", f"labels waiting-director and {'+'.join(approval)} with no Decision: remove one"
        return "Waiting for Director", "label waiting-director", None
    if approval and not dec:
        return "Needs approval", f"label {approval[0]}", None
    if ON_HOLD_LABEL in labels:
        return "Today", "label on-hold", None
    if director_merges_pr:
        return "Waiting for Director", "open pull request: only the Director merges", None
    if held or not dec:
        return None, "", None
    if current in KEEP_ON_DECISION:
        return current, f"Decision {dec}, {current}", None
    return "Today", f"Decision {dec}", None


def decision_status(decision, status) -> str | None:
    """Release 85 (a company's request, Director 2026-10-29 "제가 결정하면, 위치를 옮기는게 자동화되었으면"): the Status
    a Decision moves an open item to, by `board_status` (labels are not read here): Approve 1-3 or Rejected -> Today
    unless the item is already In progress, Waiting for Director, Done or Today; Discuss or no Decision -> None."""
    want = board_status(decision=decision, status=status)[0]
    return want if want and want != status else None


def _follow_decision(site: Site, actor: str, cfg: dict, it: dict, dec, said: list, wi) -> list[str]:
    """Move the item to `decision_status` when its Decision changes; a fail-closed ask moves only with a Director
    comment beside the Decision (as the upstream heartbeat holds it). One line when moved or held; a failed write is
    reported, never raised (the next decision line is not lost)."""
    want = decision_status(dec, it.get("status"))
    if not want or not it.get("id"):
        return []
    if decision_value(dec) != REJECT and ask_fail_closed(site, it.get("url") or "") and not said:
        return [f"status: {_ref(it)} held: a fail-closed ask moves with a Director comment beside the Decision"]
    try:
        _edit(cfg, _meta(cfg), it["id"], {FIELD_STATUS: want})
    except (GhError, DurehError) as e:
        return [f"status: {_ref(it)} not moved to {want}: {e}"]
    Ledger(site).event(UPDATED, actor, wi, {"item_id": it["id"], "url": it.get("url"), "fields": {FIELD_STATUS: want},
                                            "by_decision": decision_value(dec)}, [wi] if wi else None)
    return [f"status: {_ref(it)} -> {want} (Decision {decision_value(dec)})"]


def watch(site: Site, actor: str, interval: float = WATCH_INTERVAL, emit=print, sleep=time.sleep,
          polls: int | None = None) -> None:
    """Poll until stopped (or `polls` times); emit only the lines of `poll`. A failed gh call skips that poll. On a
    rate-limit failure one line is emitted and the wait doubles from `interval` (capped at BACKOFF_CAP, at least a
    Retry-After the error names) until a poll succeeds, which emits one "resumed" line."""
    _require_desk(site, actor)
    cfg = config(site)
    interval = max(5.0, float(interval))
    n, backoff, limited = 0, None, 0
    while True:
        wait = interval
        try:
            lines = poll(site, actor, cfg)
        except GhError as e:
            if rate_limited(e):
                limited += 1
                if limited == 1:
                    note_exhausted(site, cfg, e)
                backoff = interval if backoff is None else min(BACKOFF_CAP, backoff * 2)
                backoff = min(BACKOFF_CAP, max(backoff, _retry_after(e) or 0))
                wait = backoff
                if limited == 1:
                    emit(f"github watch: rate limited by GitHub; backing off (next poll in {wait:g} s, doubling up "
                         f"to {BACKOFF_CAP:g} s) until a poll succeeds")
            # any other failure is transient (network, API hiccup); the next poll sees the same changes
        else:
            if limited:
                emit(f"github watch: resumed after {limited} rate-limited poll{'s' if limited != 1 else ''}")
            limited, backoff = 0, None
            for line in lines:
                emit(line)
            if any(x.startswith("status: ") and " -> " in x for x in lines):  # release 87: a Decision moved an item
                note = refresh_digest(site, actor)
                if note:
                    emit(note)
        n += 1
        if polls is not None and n >= polls:
            return
        sleep(wait)


# ------------------------------------------------------------------ items
def _require_desk(site: Site, actor: str) -> None:
    if site.org.role(actor).kind != "desk":
        fail(f"only the desk writes to the company's GitHub board (actor {actor!r} is not the desk); report to the desk")


def _clean(text: str, what: str, limit: int = MAX_BODY) -> str:
    text = text.strip()
    if not text:
        fail(f"{what} is empty")
    if len(text) > limit:
        fail(f"{what} is {len(text)} characters; the limit is {limit}")
    low = text.lower()
    if any(m in low for m in SECRET_MARKS):
        fail(f"refused: {what} contains a GitHub token prefix; never post secrets")
    return text


def _option(meta: dict, field: str, value) -> tuple[str, str]:
    """(field id, option id) for a single-select value; a Layer of 3 matches an option named "3" or "3 ..."."""
    f = meta["fields"].get(field)
    if not f:
        fail(f"the board has no field {field!r} (fields: {', '.join(meta['fields']) or 'none'}; docs/github.md)")
    want = str(value).strip().lower()
    for name, oid in f["options"].items():
        n = name.strip().lower()
        if n == want or (field in (FIELD_LAYER, FIELD_PRIORITY) and re.match(rf"^{re.escape(want)}\b", n)):
            return f["id"], oid
    if field == FIELD_DECISION:  # a board not yet migrated: the old option of the same meaning (Approve 1 = Approved)
        for name, oid in f["options"].items():
            if decision_value(name) == decision_value(value):
                return f["id"], oid
    fail(f"the board's {field} has no option {value!r} (options: {', '.join(f['options']) or 'none'})")


def _edits(meta: dict, fields: dict) -> list[list[str]]:
    """`gh project item-edit` arguments (after --id/--project-id) for each field to set."""
    out = []
    for name, value in fields.items():
        if value is None:
            continue
        if name == FIELD_UNITS:
            f = meta["fields"].get(FIELD_UNITS)
            if not f:
                fail(f"the board has no field {FIELD_UNITS!r}")
            out.append(["--field-id", f["id"], "--number", f"{float(value):g}"])
        elif name in (FIELD_START, FIELD_TARGET):
            f = meta["fields"].get(name)
            if not f:
                fail(f"the board has no field {name!r}")
            out.append(["--field-id", f["id"], "--date", value])
        else:
            fid, oid = _option(meta, name, value)
            out.append(["--field-id", fid, "--single-select-option-id", oid])
    return out


def _fields(status=None, project=None, layer=None, units=None, decision=None, priority=None, start=None,
            target=None) -> dict:
    out = {}
    if status is not None:
        match = [s for s in STATUSES if s.lower() == str(status).strip().lower()]
        if not match:
            fail(f"--status must be one of {', '.join(STATUSES)}, not {status!r}")
        out[FIELD_STATUS] = match[0]
    if project is not None:
        out[FIELD_PROJECT] = str(project)
    if layer is not None:
        if int(layer) not in LAYERS:
            fail("--layer must be 1 (company), 2 (DUREH) or 3 (Director)")
        out[FIELD_LAYER] = str(int(layer))
    if units is not None:
        out[FIELD_UNITS] = units
    if decision is not None:
        canon = decision_value(decision)
        if canon not in DECISIONS:
            fail(f"--decision must be one of {', '.join(DECISIONS)} (old names Approved, Option 1-3, Later and Reject "
                 f"are read as Approve 1, Approve 1-3, Rejected, Discuss; Reject as Rejected), not {decision!r}")
        out[FIELD_DECISION] = canon
    if priority is not None:
        want = str(priority).strip().upper()
        if want not in PRIORITIES:
            fail(f"--priority must be one of {', '.join(PRIORITIES)}, not {priority!r}")
        out[FIELD_PRIORITY] = want
    for name, opt, value in ((FIELD_START, "--start", start), (FIELD_TARGET, "--target", target)):
        if value is not None:
            try:
                out[name] = date.fromisoformat(str(value).strip()).isoformat()
            except ValueError:
                fail(f"{opt} must be a date YYYY-MM-DD, not {value!r}")
    return out


def _meta(cfg: dict) -> dict:
    q = BOARD_QUERY.replace("items(first: 100, after: $after)", "items(first: 1, after: $after)")
    data = graphql(cfg, q, {"org": cfg["org"], "number": cfg["project"], "after": None})
    proj = (data.get("organization") or {}).get("projectV2")
    if not proj:
        raise GhError(f"organization {cfg['org']} has no Project {cfg['project']} the token can read")
    return _parse_meta(proj)


def _repo(cfg: dict, repo: str | None) -> str:
    name = repo or cfg["office_repo"]
    if "/" in name:
        owner, name = name.split("/", 1)
        if owner != cfg["org"]:
            fail(f"--repo must be a repository of the company's organization {cfg['org']}, not {owner}/{name}")
    if not _REPO.match(name):
        fail(f"--repo must be a repository name, not {name!r}")
    return f"{cfg['org']}/{name}"


def _issue_number(cfg: dict, url: str) -> tuple[str, int]:
    m = re.match(rf"^https://github\.com/({re.escape(cfg['org'])}/[A-Za-z0-9._-]+)/(?:issues|pull)/(\d+)$", url.strip())
    if not m:
        fail(f"not an issue or pull request URL of organization {cfg['org']}: {url!r}")
    return m.group(1), int(m.group(2))


def _create_issue(cfg: dict, repo: str, title: str, body: str, labels=(), assignee=None) -> str:
    args = ["issue", "create", "--repo", repo, "--title", title, "--body-file", "-"]
    for lab in labels:
        args += ["--label", lab]
    if assignee:
        args += ["--assignee", assignee]
    out = gh(cfg, args, input_text=body)
    urls = re.findall(r"https://github\.com/\S+/issues/\d+", out)
    if not urls:
        raise GhError("gh issue create: no issue URL in the answer")
    return urls[-1]


def _add_to_board(cfg: dict, meta: dict, url: str, fields: dict) -> str:
    try:
        out = gh(cfg, ["project", "item-add", str(cfg["project"]), "--owner", cfg["org"], "--url", url,
                       "--format", "json"])
        item_id = json.loads(out).get("id")
    except (GhError, ValueError, AttributeError) as exc:
        fail(f"{url} exists but is not on the board: {exc}; run `item add --url {url}` again")
    if not item_id:
        fail(f"{url} exists but gh item-add gave no item id; run `item add --url {url}` again")
    _edit(cfg, meta, item_id, fields)
    return item_id


def _edit(cfg: dict, meta: dict, item_id: str, fields: dict) -> None:
    for extra in _edits(meta, fields):
        gh(cfg, ["project", "item-edit", "--id", item_id, "--project-id", meta["id"], *extra])


def linked_item(site: Site, wi: str | None = None, url: str | None = None) -> dict:
    """The board item recorded for a WI (or an issue URL): {item_id, url, wi, layer}. Release 81 (finding
    cc81a566): a decision ask about the WI (`item ask --wi`) is never the WI's item, so `item set/close/comment --wi`
    do not move or close the ask; the ask is named by `--url`."""
    links = _linked(site)
    hits = [(iid, row) for iid, row in links.items()
            if (wi and row.get("wi") == wi and not row.get("ask")) or (url and row.get("url") == url.strip())]
    if not hits:
        asks = [row.get("url") for row in links.values() if wi and row.get("wi") == wi and row.get("ask")]
        also = (f" (the decision ask {', '.join(asks)} is not the WI's item; name it with --url)" if asks else "")
        fail(f"no board item is recorded for {wi or url}{also}; `dureh github item add --wi <WI>` links one")
    iid, row = hits[-1]
    return {"item_id": iid, **row}


def item_add(site: Site, actor: str, wi: str, status: str | None = "Backlog", project=None, layer=None, units=None,
             repo: str | None = None, url: str | None = None, priority=None, start=None, target=None) -> dict:
    """Create an issue for a work item (title and goal only) in the office repository or `repo` (or link the
    existing issue `url`), add it to the board, set its fields; records github.item.added."""
    _require_desk(site, actor)
    cfg = config(site)
    led = Ledger(site)
    payload = led.get("work-item", wi)["payload"]
    if any(e["type"] == ADDED and e["wi"] == wi for e in led.events(wi)):
        fail(f"{wi} already has a board item; change it with `dureh github item set --wi {wi} ...`")
    fields = _fields(status, project, layer, units, priority=priority, start=start, target=target)
    if fields.get(FIELD_STATUS) == "In progress":  # release 82: the same limit as `item set`
        _refuse_wip(cfg, board(cfg)["items"], url.strip() if url else None)
    meta = _meta(cfg)
    _edits(meta, fields)  # every field and option exists before anything is created
    if url:
        repo_name, _ = _issue_number(cfg, url)
    else:
        repo_name = _repo(cfg, repo)
        title = _clean(f"{wi} {short_title(payload) or payload['title']}", "the title", 256)
        goal = " ".join(str(payload.get("goal") or "").split())[:2000]
        body = _clean(f"Work item **{wi}**: {payload['title']}\n\n{goal}\n\nRole: {payload.get('role')}"
                      f" · lane: {payload.get('lane')}\n\n<!-- dureh-wi: {wi} -->\n{DESK_MARK}", "the issue body")
        url = _create_issue(cfg, repo_name, title, body)
    item_id = _add_to_board(cfg, meta, url, fields)
    _, number = _issue_number(cfg, url)
    rec = {"url": url, "repo": repo_name, "number": number, "item_id": item_id, "fields": fields}
    led.event(ADDED, actor, wi, rec, [wi])
    return {"added": True, "wi": wi, **rec}


def _in_progress(cfg: dict, items: list[dict], exclude_url: str | None = None) -> list[dict]:
    """The company's open board items In progress (repository of the company's organization, or a draft), the
    item `exclude_url` aside."""
    return [it for it in items if it.get("status") == "In progress" and it.get("state") not in ("CLOSED", "MERGED")
            and (not it.get("repo") or it["repo"].startswith(f"{cfg['org']}/"))
            and not (exclude_url and it.get("url") == exclude_url)]


def _refuse_wip(cfg: dict, items: list[dict], url: str | None, also: str = "") -> None:
    """Release 82 (dureh-team#74): a third In progress item of the company is refused, naming the running ones."""
    running = _in_progress(cfg, items, url)
    if len(running) >= WIP_IN_PROGRESS:
        names = "; ".join(f"{_ref(it)} {it.get('title') or ''}".strip() + (f" ({it['url']})" if it.get("url") else "")
                          for it in running)
        fail(f"refused: the board already has {len(running)} open items In progress for {cfg['org']} (limit"
             f" {WIP_IN_PROGRESS}, Director 2026-10-26): {names}. Finish one, or move one back with"
             f" `dureh github item set --url <url> --status Today --on-hold`{also}")


def start_item(site: Site, actor: str, wi: str) -> str | None:
    """Release 86 (dureh-team#91 item 2; #102 stayed Today while it was worked): when the desk dispatches a WI, its
    board item moves to In progress; with the company's In progress limit full it goes to Today with on-hold. None
    when GitHub is off or the WI has no board item; a failed write is a line, never an error (the dispatch stands)."""
    if not enabled(site):
        return None
    try:
        item = linked_item(site, wi)
    except DurehError:
        return None
    try:
        item_set(site, actor, wi=wi, status="In progress")
        return f"board: {item.get('url') or item['item_id']} -> In progress"
    except DurehError as e:
        if "In progress for" not in str(e):
            return f"board: {wi} not moved to In progress: {e}"
    try:
        item_set(site, actor, wi=wi, on_hold=True)
        return f"board: {item.get('url') or item['item_id']} -> Today + on-hold (the In progress limit is full)"
    except DurehError as e:
        return f"board: {wi} not moved: {e}"


def item_set(site: Site, actor: str, wi: str | None = None, url: str | None = None, status=None, project=None,
             layer=None, units=None, decision=None, priority=None, start=None, target=None,
             on_hold: bool = False) -> dict:
    """Change fields of a linked item. The desk records a Decision only on a layer 1-2 item: on a layer-3 item the
    Decision column is the Director's alone. Release 82 (dureh-team#74): Status In progress is refused while the
    company already has WIP_IN_PROGRESS open items In progress (one board read); `on_hold` sets Status Today and
    adds the label `on-hold`, which a later move to In progress removes."""
    _require_desk(site, actor)
    if not wi and not url:
        fail("name the item with --wi or --url")
    cfg = config(site)
    item = linked_item(site, wi, url)
    if on_hold:
        if status is not None and str(status).strip().lower() != "today":
            fail("--on-hold sets Status Today; it does not go with --status " + repr(status))
        status = "Today"
    fields = _fields(status, project, layer, units, decision, priority, start, target)
    if not fields:
        fail("nothing to set: give --status, --project, --layer, --units, --decision, --priority, --start or --target")
    labels = {}
    if fields.get(FIELD_STATUS) == "In progress":
        items = board(cfg)["items"]
        _refuse_wip(cfg, items, item.get("url"))
        it = next((i for i in items if item.get("url") and i.get("url") == item["url"]), None)
        if it and ON_HOLD_LABEL in (it.get("labels") or []):
            labels["remove"] = [ON_HOLD_LABEL]
    if on_hold:
        if not item.get("url"):
            fail("--on-hold needs an issue (a draft carries no label)")
        labels["add"] = [ON_HOLD_LABEL]
    if FIELD_DECISION in fields and str(fields.get(FIELD_LAYER) or item.get("layer") or "") not in ("1", "2"):
        fail("refused: the desk records a Decision only on a layer 1 or 2 item (set --layer 1|2 with it); on a layer-3 "
             "item only the Director decides, in the board's Decision column")
    if fields.get(FIELD_STATUS) == "Done":
        _refuse_open_fail_closed(site, cfg, item.get("url"), "set Status Done on")
    meta = _meta(cfg)
    _edit(cfg, meta, item["item_id"], fields)
    if labels.get("add"):
        repo_name, _ = _issue_number(cfg, item["url"])
        gh(cfg, ["label", "create", ON_HOLD_LABEL, "--repo", repo_name, "--color", "fbca04", "--description",
                 "moved back to Today: waits for a free In progress slot", "--force"])
        gh(cfg, ["issue", "edit", item["url"], "--add-label", ON_HOLD_LABEL])
    if labels.get("remove"):
        gh(cfg, ["issue", "edit", item["url"], "--remove-label", ON_HOLD_LABEL])
    Ledger(site).event(UPDATED, actor, item.get("wi"), {"item_id": item["item_id"], "url": item.get("url"),
                                                         "fields": fields, **({"labels": labels} if labels else {})},
                       [item["wi"]] if item.get("wi") else None)
    return {"updated": True, "wi": item.get("wi"), "item_id": item["item_id"], "url": item.get("url"), "fields": fields,
            **({"labels": labels} if labels else {})}


WAITING_LABEL = "waiting-director"
DIRECTOR_STEP = "github.director-step.created"


def _director_step(site: Site, actor: str, title: str, body: str, command: str | None, wi: str | None, project,
                   repo: str | None, priority=None) -> dict:
    """Release 85 (`item ask --class D`, approval-balance plan 3.1/3.4): a step only the Director's hands can do (an App
    or web setting, a login, a command refused to the desk after approval, a dataset link). No Decision options: the
    issue opens with 현재 상태 and 대표님이 하실 일 (the exact command), is assigned to the Director, carries the label
    waiting-director and Status Waiting for Director, and is closed by the desk once the step is done. Release 87
    (dureh-team#108 item 5): it is a layer-3 item, so it needs `priority` like a layer-3 decision ask."""
    if not (command or "").strip():
        fail("--class D needs --command: the exact command or step the Director runs (one block he can paste)")
    if priority is None:
        fail("refused: a layer-3 item needs --priority P0..P3 (release 87, dureh-team#108; desk rule: P2 unless asked)")
    cfg = config(site)
    title = _clean(title, "the title", 256)
    text = (f"## 현재 상태\n\n{_clean(body, 'the body')}\n\n## 대표님이 하실 일\n\n```\n{_clean(command, '--command', 4000)}\n```"
            f"\n\nWhen it is done, reply here; the desk checks the result and closes this item.\n\n{DESK_MARK}")
    fields = _fields("Waiting for Director", project, 3, None, priority=priority)
    meta = _meta(cfg)
    _edits(meta, fields)
    repo_name = _repo(cfg, repo)
    gh(cfg, ["label", "create", WAITING_LABEL, "--repo", repo_name, "--color", "d93f0b", "--description",
             "a step only the Director's hands can do", "--force"])
    url = _create_issue(cfg, repo_name, title, text, labels=[WAITING_LABEL], assignee=cfg.get("director_login"))
    item_id = _add_to_board(cfg, meta, url, fields)
    _, number = _issue_number(cfg, url)
    rec = {"url": url, "repo": repo_name, "number": number, "item_id": item_id, "class": "D",
           "assignee": cfg.get("director_login"), "fields": fields, "chars": len(text)}
    Ledger(site).event(DIRECTOR_STEP, actor, wi, rec, [wi] if wi else None)
    return rec


def item_ask(site: Site, actor: str, title: str, body: str, options: list[str] | None = None, layer: int = 3,
             wi: str | None = None, project=None, repo: str | None = None, units=None,
             fail_closed: bool = False, decide: str | None = None, recommend: str | None = None,
             otherwise: str | None = None, on_hold: bool = False, ask_class: str | None = None,
             command: str | None = None, state: str | None = None, priority=None) -> dict:
    """A decision ask: a `kind:decision` issue (layer 3: assigned to config github.director_login, Status Waiting for
    Director), on the board with its Layer; the answer is the board's Decision, read by `watch`/`status`.
    Release 80 (dureh-team#30 Option 1, the Director's three-line ask of dureh-team#49): a layer-3 ask opens with three
    lines, 결정할 것 (`decide`, one yes/no or choice sentence), 추천 (`recommend`, with the reason) and 답이 없으면
    (`otherwise`); the body follows as background. A layer-3 ask without them is refused.
    Release 87 (Director, dureh-team#108 items 4-6; his item format of 2026-10-27): the body is `## 현재 상태` (`state`,
    one line; default the body's first paragraph), `## 결정할 것` with the question and right under it the options as
    `- **Approve N:** ...` lines (then Rejected and Discuss), `**추천:**`, `**답이 없으면:**`, `---` and the body as
    background. `decide` naming an option ("Approve 1", "Option 2") or longer than DECIDE_MAX is refused (the choices
    go in `options`); a layer-3 ask needs `priority` (P0..P3), and a layer-3 option that is a step only the Director's
    hands do (DIRECTOR_HANDS) is refused with the hint to file it with `--class D`.
    `fail_closed` (release 78, v0.4 bundle 1): the ask also carries the `fail-closed` label and a line saying it has
    no fallback; a body or option naming a fallback option is refused before anything is created, and the record
    keeps `fail_closed: true` so `day decide --ask <url>` refuses a provisional choice on it.
    Release 83 (the release-82 WIP limit, dureh-team#74): a layer 1-2 ask opens In progress, so it is refused like
    `item set --status "In progress"` while the company already has WIP_IN_PROGRESS open items In progress (one
    board read, before anything is created); `on_hold` opens it with Status Today and the label `on-hold` instead."""
    from .records import FAIL_CLOSED_LABEL, fallback_lines

    _require_desk(site, actor)
    if ask_class is not None and ask_class not in ("C", "D"):
        fail(f"--class takes C (a decision: Needs approval) or D (a step only the Director's hands do), not {ask_class!r};"
             " classes A and B are not asked (approval-balance plan 3.1)")
    if ask_class == "D":
        return _director_step(site, actor, title, body, command, wi, project, repo, priority)
    if command:
        fail("--command goes with --class D")
    cfg = config(site)
    if on_hold and int(layer) == 3:
        fail("--on-hold is for a layer 1-2 ask (it would open In progress); a layer-3 ask opens Needs approval")
    options = [o.strip() for o in options or [] if o.strip()]
    if len(options) > 3:
        fail("at most 3 options (the Decision column has Approve 1, Approve 2 and Approve 3)")
    if fail_closed:
        named = fallback_lines("\n".join([body or "", *options]))
        if named:
            fail("refused: a fail-closed ask names no fallback option (release 78; no 30-minute fallback, no timeout "
                 f"approval): remove or rewrite {named[0]!r} (for example \"Fallback: none, this waits for the answer\")")
    question = " ".join((decide or "").split())
    named = _DECIDE_OPTION.search(question)
    if named or len(question) > DECIDE_MAX:
        why = f"names an option ({named.group(0)!r})" if named else f"is {len(question)} characters (at most {DECIDE_MAX})"
        fail(f"refused: --decide {why}; it holds the question only, one short sentence (release 87, dureh-team#108):"
             " put the choices in --options (they become Approve 1..3 under the question)")
    if int(layer) == 3:
        hands = next((o for o in options if any(c.search(o) for c in DIRECTOR_HANDS)), None)
        if hands:
            fail(f"refused: the option {hands!r} is a step only the Director's hands do, not a decision (release 87,"
                 " dureh-team#108): file it with `github item ask --class D --command \"...\" --priority P2` (Waiting for"
                 " Director) and ask only the decision here")
    if wi:
        Ledger(site).get("work-item", wi)
    title = _clean(title, "the title", 256)
    text = _clean(body, "the body")
    lines = {"decide": decide, "recommend": recommend, "otherwise": otherwise}
    missing = [f"--{k}" for k, v in lines.items() if not (v or "").strip()]
    if int(layer) == 3 and missing:
        fail(f"refused: a layer-3 ask opens with three lines (release 80, dureh-team#30): give {', '.join(missing)} "
             "(결정할 것 / 추천 / 답이 없으면, one sentence each, Korean for the Director)")
    if fail_closed and (otherwise or "").strip():
        named = fallback_lines(otherwise or "")
        if named:
            fail(f"refused: a fail-closed ask names no fallback option: rewrite --otherwise {named[0]!r}")
    if int(layer) == 3 and priority is None:
        fail("refused: a layer-3 ask needs --priority P0..P3 (release 87, dureh-team#108; desk rule: P2 unless asked)")
    now = _one_line(_clean(state, "--state", 500) if (state or "").strip() else re.split(r"\n\s*\n", text.strip())[0])
    # Director 2026-10-27: a multiple choice in the Decision column's own names, right under the question
    approves = [f"- **Approve {i}:** {o}" for i, o in enumerate(options, 1)] or ["- **Approve 1:** approve as asked"]
    choice = "\n".join([*approves, "- **Rejected:** no; nothing here proceeds",
                        "- **Discuss:** hold; nothing proceeds, the desk answers your comments on this item"])
    tail = "\n".join(f"**{k}:** {_clean(v, '--' + n, 500)}" for k, v, n in
                     (("추천", recommend, "recommend"), ("답이 없으면", otherwise, "otherwise")) if (v or "").strip())
    text = (f"## 현재 상태\n\n{now}\n\n## 결정할 것\n\n{_clean(question, '--decide') if question else title}\n\n{choice}"
            + (f"\n\n{tail}" if tail else "") + f"\n\n---\n\n**배경 · Background**\n\n{text}")
    choices = [*[f"Approve {i}" for i in range(1, max(len(options), 1) + 1)], REJECT, DISCUSS]
    who = "the Director" if int(layer) == 3 else LAYERS.get(int(layer), "?")
    if fail_closed:
        text += (f"\n\n**Fail-closed**: no 30-minute fallback and no timeout approval; nothing here proceeds until "
                 f"{who} answers.")
    text += (f"\n\nLayer {layer} ({who}). Decide in the board's **Decision** column: {', '.join(choices)}. "
             f"The Decision column is the decision; your comments on this item are read with it.\n\n{DESK_MARK}")
    text = _clean(text, "the issue body")
    status = "Needs approval" if int(layer) == 3 else "Today" if on_hold else "In progress"
    fields = _fields(status, project, layer, units, priority=priority)
    if status == "In progress":  # release 83: the release-82 limit, before anything is created
        _refuse_wip(cfg, board(cfg)["items"], None, ", or file this ask with `--on-hold` (Status Today)")
    meta = _meta(cfg)
    _edits(meta, fields)
    if FIELD_DECISION not in meta["fields"]:
        fail(f"the board has no field {FIELD_DECISION!r}; the Director adds it (docs/github.md)")
    repo_name = _repo(cfg, repo)
    assignee = cfg.get("director_login") if int(layer) == 3 else None
    gh(cfg, ["label", "create", DECISION_LABEL, "--repo", repo_name, "--color", "5319e7", "--description",
             "a decision asked on the board's Decision column", "--force"])
    labels = [DECISION_LABEL]
    if on_hold:
        gh(cfg, ["label", "create", ON_HOLD_LABEL, "--repo", repo_name, "--color", "fbca04", "--description",
                 "moved back to Today: waits for a free In progress slot", "--force"])
        labels.append(ON_HOLD_LABEL)
    if fail_closed:
        gh(cfg, ["label", "create", FAIL_CLOSED_LABEL, "--repo", repo_name, "--color", "b60205", "--description",
                 "no fallback, no timeout approval: waits for an explicit answer", "--force"])
        labels.append(FAIL_CLOSED_LABEL)
    url = _create_issue(cfg, repo_name, title, text, labels=labels, assignee=assignee)
    item_id = _add_to_board(cfg, meta, url, fields)
    _, number = _issue_number(cfg, url)
    rec = {"url": url, "repo": repo_name, "number": number, "item_id": item_id, "layer": int(layer),
           "options": len(options), "assignee": assignee, "fields": fields, "chars": len(text)}
    if fail_closed:
        rec["fail_closed"] = True
    if on_hold:
        rec["labels"] = {"add": [ON_HOLD_LABEL]}
    Ledger(site).event(ASKED, actor, wi, rec, [wi] if wi else None)
    return {"asked": True, "wi": wi, **rec}


def comment(site: Site, actor: str, text: str, wi: str | None = None, url: str | None = None) -> dict:
    """The desk's comment on a linked item's issue (an answer to an opinion), carrying the desk marker so `watch`
    skips it; records github.comment.posted {url, chars}."""
    _require_desk(site, actor)
    if not wi and not url:
        fail("name the item with --wi or --url")
    cfg = config(site)
    target = linked_item(site, wi, url)["url"] if wi else url.strip()
    _issue_number(cfg, target)
    body = _clean(text, "the comment") + f"\n\n{DESK_MARK}"
    gh(cfg, ["issue", "comment", target, "--body-file", "-"], input_text=body)
    Ledger(site).event(COMMENTED, actor, wi, {"url": target, "chars": len(body)}, [wi] if wi else None)
    return {"commented": True, "url": target, "chars": len(body)}


def _refuse_open_fail_closed(site: Site, cfg: dict, url: str | None, what: str) -> None:
    """Release 81 (finding cc81a566, DUREH's provisional choice): the desk may not set Status Done on, or close, a
    fail-closed ask (label `fail-closed` on the board, or recorded `fail_closed`) whose Decision is empty; an
    undecided ask that is not fail-closed stays allowed by hand. One board read, only for a Done or a close."""
    from .records import FAIL_CLOSED_LABEL

    if not url:
        return
    it = next((i for i in board(cfg)["items"] if i.get("url") == url.strip()), None)
    labelled = bool(it) and FAIL_CLOSED_LABEL in (it.get("labels") or [])
    if not (labelled or ask_fail_closed(site, url)) or (it and it.get("decision") not in (None, DISCUSS)):
        return
    fail(f"refused: {url} is a fail-closed ask and its {FIELD_DECISION} column is empty or Discuss; the desk cannot {what} it"
         f" until the answer is in the board's {FIELD_DECISION} column (no fallback, no timeout approval)")


def item_close(site: Site, actor: str, wi: str | None = None, url: str | None = None, text: str | None = None) -> dict:
    """#34: close a linked item's issue (the desk's rule: at promote), with an optional closing comment carrying the
    desk marker; records github.item.closed {url}."""
    _require_desk(site, actor)
    if not wi and not url:
        fail("name the item with --wi or --url")
    cfg = config(site)
    target = linked_item(site, wi, url)["url"] if wi else url.strip()
    _issue_number(cfg, target)
    _refuse_open_fail_closed(site, cfg, target, "close")
    args = ["issue", "close", target]
    if text and text.strip():
        args += ["--comment", _clean(text, "the comment") + f"\n\n{DESK_MARK}"]
    gh(cfg, args)
    Ledger(site).event(CLOSED, actor, wi, {"url": target}, [wi] if wi else None)
    return {"closed": True, "wi": wi, "url": target}


def _sweep_keeps(it: dict, fail_closed_urls: set[str], asks: set[str]) -> bool:
    """Whether the sweep leaves a Done item open: a HOLD_LABELS label; release 81 (finding cc81a566): a fail-closed
    ask (label, or recorded fail-closed) is never closed automatically, and a decision ask (`kind:decision`, or
    recorded by `item ask`) stays open until its Decision is set."""
    from .records import FAIL_CLOSED_LABEL

    labels = set(it.get("labels") or [])
    if labels & set(HOLD_LABELS) or FAIL_CLOSED_LABEL in labels or it["url"] in fail_closed_urls:
        return True
    return (DECISION_LABEL in labels or it["url"] in asks) and it.get("decision") in (None, DISCUSS)  # Discuss: no answer yet


def sweep(site: Site, actor: str, dry_run: bool = False) -> dict:
    """#34: close every open issue of the company's organization whose board Status is Done and that carries none of
    HOLD_LABELS (day close runs it); records github.sweep.done {closed, kept}. Release 81: a decision ask without a
    Decision and any fail-closed ask are kept (_sweep_keeps)."""
    _require_desk(site, actor)
    cfg = config(site)
    done = [it for it in board(cfg)["items"] if it["type"] == "Issue" and it["state"] == "OPEN"
            and it["status"] == "Done" and str(it.get("repo") or "").startswith(cfg["org"] + "/")]
    links = _linked(site).values()
    fail_closed = {row["url"] for row in links if row.get("fail_closed") and row.get("url")}
    asks = {row["url"] for row in links if row.get("ask") and row.get("url")}
    kept = [it["url"] for it in done if _sweep_keeps(it, fail_closed, asks)]
    todo = [it["url"] for it in done if it["url"] not in kept]
    if not dry_run:
        for url in todo:
            gh(cfg, ["issue", "close", url, "--comment", f"Done on the board; closed by the desk's sweep.\n\n{DESK_MARK}"])
        if todo or kept:
            Ledger(site).event(SWEPT, actor, None, {"closed": todo, "kept": kept})
    return {"dry_run": dry_run, "closed" if not dry_run else "would_close": todo, "kept": kept}

