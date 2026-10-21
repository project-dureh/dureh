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
adds an `exhausted` line (M2)."""
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

KEYS = ("enabled", "org", "project", "office_repo", "token_file", "director_login")
DEFAULT_OFFICE = "office"
DESK_MARK = "<!-- dureh-desk -->"
DECISION_LABEL = "kind:decision"
REQUEST_LABEL = "kind:request"
# dureh-team#28 A (Director 2026-10-20): Needs approval = the Director approves, chooses or comments (decision asks,
# review requests); Waiting for Director = a step only the Director does
STATUSES = ("Backlog", "Today", "In progress", "Needs approval", "Waiting for Director", "Done")
DECISIONS = ("Approved", "Option 1", "Option 2", "Option 3", "Rejected", "Later")
LAYERS = {1: "company", 2: "DUREH", 3: "Director"}
FIELD_STATUS, FIELD_PROJECT, FIELD_LAYER, FIELD_DECISION, FIELD_UNITS = "Status", "Project", "Layer", "Decision", "Units"
# #31: roadmap fields; a Priority of P1 matches an option named "P1" or "P1 ..."; dates are YYYY-MM-DD
FIELD_PRIORITY, FIELD_START, FIELD_TARGET = "Priority", "Start date", "Target date"
PRIORITIES = ("P0", "P1", "P2", "P3")
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
    if on:
        missing = [k for k, v in (("org", org), ("project", number), ("token_file", token_file)) if v is None]
        if missing:
            out.append(f"config github.enabled is true but {', '.join('github.' + k for k in missing)} "
                       f"{'is' if len(missing) == 1 else 'are'} missing (docs/github.md)")
        if isinstance(token_file, str) and token_file.strip():
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
            "token_file": site.expand(raw["token_file"]), "director_login": raw.get("director_login")}


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
            "status": values.get(FIELD_STATUS), "decision": values.get(FIELD_DECISION),
            "project": values.get(FIELD_PROJECT), "layer": values.get(FIELD_LAYER), "units": values.get(FIELD_UNITS),
            "comments": comments}


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
        groups.setdefault(it["status"] or "(no status)", []).append(row)
    return {"org": cfg["org"], "project": cfg["project"], "title": b["meta"]["title"],
            "groups": {k: v for k, v in groups.items() if v}, "count": len(b["items"])}


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
    """{item_id: {wi, layer}} from the records of this site's board items."""
    out: dict[str, dict] = {}
    for e in Ledger(site).events():
        if e["type"] in (ADDED, ASKED, UPDATED) and e["payload"].get("item_id"):
            row = out.setdefault(e["payload"]["item_id"], {})
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
    first = old is None
    seen_dec = {} if first else dict(old.get("decisions") or {})
    seen_com = {} if first else {k: set(v) for k, v in (old.get("comments") or {}).items()}
    lines, links, led = [], None, Ledger(site)
    new_dec, new_com = {}, {}
    for it in b["items"]:
        iid, dec = it["id"], it["decision"]
        new_dec[iid] = dec
        if dec != seen_dec.get(iid) and not (first and dec is None):
            links = _linked(site) if links is None else links
            link = links.get(iid, {})
            was = seen_dec.get(iid)
            layer = f" [layer {it['layer']}]" if it.get("layer") else ""
            wi = f" {link['wi']}" if link.get("wi") and link["wi"] not in (it["title"] or "") else ""
            lines.append(f"decision: {_ref(it)}{wi} {it['title'] or ''} -> {dec or 'none'} (was {was or 'none'})"
                         f"{layer} {it['url'] or ''}".rstrip())
            led.event(DECIDED, actor, link.get("wi"), {"item_id": iid, "url": it["url"], "from": was, "to": dec,
                                                        "layer": it.get("layer"), "first_poll": first})
        ids = [c["id"] for c in it["comments"] if c.get("id")]
        before = seen_com.get(iid, set())
        if not first:
            for c in it["comments"]:
                if c.get("id") and c["id"] not in before and DESK_MARK not in c["body"]:
                    lines.append(f"comment: {_ref(it)} by {c['author'] or '?'}: {_one_line(c['body'])} "
                                 f"{c['url'] or ''}".rstrip())
        new_com[iid] = sorted(before | set(ids))[-200:]
    _save_state(site, {"board": f"{cfg['org']}/{cfg['project']}", "decisions": new_dec, "comments": new_com,
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
        match = [d for d in DECISIONS if d.lower() == str(decision).strip().lower()]
        if not match:
            fail(f"--decision must be one of {', '.join(DECISIONS)}, not {decision!r}")
        out[FIELD_DECISION] = match[0]
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
    """The board item recorded for a WI (or an issue URL): {item_id, url, wi, layer}."""
    hits = [(iid, row) for iid, row in _linked(site).items()
            if (wi and row.get("wi") == wi) or (url and row.get("url") == url.strip())]
    if not hits:
        fail(f"no board item is recorded for {wi or url}; `dureh github item add --wi <WI>` links one")
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


def item_set(site: Site, actor: str, wi: str | None = None, url: str | None = None, status=None, project=None,
             layer=None, units=None, decision=None, priority=None, start=None, target=None) -> dict:
    """Change fields of a linked item. The desk records a Decision only on a layer 1-2 item: on a layer-3 item the
    Decision column is the Director's alone."""
    _require_desk(site, actor)
    if not wi and not url:
        fail("name the item with --wi or --url")
    cfg = config(site)
    item = linked_item(site, wi, url)
    fields = _fields(status, project, layer, units, decision, priority, start, target)
    if not fields:
        fail("nothing to set: give --status, --project, --layer, --units, --decision, --priority, --start or --target")
    if FIELD_DECISION in fields and str(fields.get(FIELD_LAYER) or item.get("layer") or "") not in ("1", "2"):
        fail("refused: the desk records a Decision only on a layer 1 or 2 item (set --layer 1|2 with it); on a layer-3 "
             "item only the Director decides, in the board's Decision column")
    meta = _meta(cfg)
    _edit(cfg, meta, item["item_id"], fields)
    Ledger(site).event(UPDATED, actor, item.get("wi"), {"item_id": item["item_id"], "url": item.get("url"),
                                                         "fields": fields}, [item["wi"]] if item.get("wi") else None)
    return {"updated": True, "wi": item.get("wi"), "item_id": item["item_id"], "url": item.get("url"), "fields": fields}


def item_ask(site: Site, actor: str, title: str, body: str, options: list[str] | None = None, layer: int = 3,
             wi: str | None = None, project=None, repo: str | None = None, units=None,
             fail_closed: bool = False) -> dict:
    """A decision ask: a `kind:decision` issue (layer 3: assigned to config github.director_login, Status Waiting for
    Director), on the board with its Layer; the answer is the board's Decision, read by `watch`/`status`.
    `fail_closed` (release 78, v0.4 bundle 1): the ask also carries the `fail-closed` label and a line saying it has
    no fallback; a body or option naming a fallback option is refused before anything is created, and the record
    keeps `fail_closed: true` so `day decide --ask <url>` refuses a provisional choice on it."""
    from .records import FAIL_CLOSED_LABEL, fallback_lines

    _require_desk(site, actor)
    cfg = config(site)
    options = [o.strip() for o in options or [] if o.strip()]
    if len(options) > 3:
        fail("at most 3 options (the Decision column has Option 1, Option 2 and Option 3)")
    if fail_closed:
        named = fallback_lines("\n".join([body or "", *options]))
        if named:
            fail("refused: a fail-closed ask names no fallback option (release 78; no 30-minute fallback, no timeout "
                 f"approval): remove or rewrite {named[0]!r} (for example \"Fallback: none, this waits for the answer\")")
    if wi:
        Ledger(site).get("work-item", wi)
    title = _clean(title, "the title", 256)
    text = _clean(body, "the body")
    if options:
        text += "\n\n**Options**\n" + "\n".join(f"- **Option {i}**: {o}" for i, o in enumerate(options, 1))
    choices = ["Approved", *[f"Option {i}" for i in range(1, len(options) + 1)], "Rejected", "Later"]
    who = "the Director" if int(layer) == 3 else LAYERS.get(int(layer), "?")
    if fail_closed:
        text += (f"\n\n**Fail-closed**: no 30-minute fallback and no timeout approval; nothing here proceeds until "
                 f"{who} answers.")
    text += (f"\n\nLayer {layer} ({who}). Decide in the board's **Decision** column: {', '.join(choices)}. "
             f"Comments are opinions; the Decision column is the decision.\n\n{DESK_MARK}")
    text = _clean(text, "the issue body")
    fields = _fields("Needs approval" if int(layer) == 3 else "In progress", project, layer, units)
    meta = _meta(cfg)
    _edits(meta, fields)
    if FIELD_DECISION not in meta["fields"]:
        fail(f"the board has no field {FIELD_DECISION!r}; the Director adds it (docs/github.md)")
    repo_name = _repo(cfg, repo)
    assignee = cfg.get("director_login") if int(layer) == 3 else None
    gh(cfg, ["label", "create", DECISION_LABEL, "--repo", repo_name, "--color", "5319e7", "--description",
             "a decision asked on the board's Decision column", "--force"])
    labels = [DECISION_LABEL]
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


def item_close(site: Site, actor: str, wi: str | None = None, url: str | None = None, text: str | None = None) -> dict:
    """#34: close a linked item's issue (the desk's rule: at promote), with an optional closing comment carrying the
    desk marker; records github.item.closed {url}."""
    _require_desk(site, actor)
    if not wi and not url:
        fail("name the item with --wi or --url")
    cfg = config(site)
    target = linked_item(site, wi, url)["url"] if wi else url.strip()
    _issue_number(cfg, target)
    args = ["issue", "close", target]
    if text and text.strip():
        args += ["--comment", _clean(text, "the comment") + f"\n\n{DESK_MARK}"]
    gh(cfg, args)
    Ledger(site).event(CLOSED, actor, wi, {"url": target}, [wi] if wi else None)
    return {"closed": True, "wi": wi, "url": target}


def sweep(site: Site, actor: str, dry_run: bool = False) -> dict:
    """#34: close every open issue of the company's organization whose board Status is Done and that carries none of
    HOLD_LABELS (day close runs it); records github.sweep.done {closed, kept}."""
    _require_desk(site, actor)
    cfg = config(site)
    done = [it for it in board(cfg)["items"] if it["type"] == "Issue" and it["state"] == "OPEN"
            and it["status"] == "Done" and str(it.get("repo") or "").startswith(cfg["org"] + "/")]
    kept = [it["url"] for it in done if set(it.get("labels") or []) & set(HOLD_LABELS)]
    todo = [it["url"] for it in done if it["url"] not in kept]
    if not dry_run:
        for url in todo:
            gh(cfg, ["issue", "close", url, "--comment", f"Done on the board; closed by the desk's sweep.\n\n{DESK_MARK}"])
        if todo or kept:
            Ledger(site).event(SWEPT, actor, None, {"closed": todo, "kept": kept})
    return {"dry_run": dry_run, "closed" if not dry_run else "would_close": todo, "kept": kept}

