"""The company's GitHub board (release 69): one organization per company, a private repository `office` for issues
not tied to a project, and the organization Project (v2) board with the fields Status, Project, Layer, Decision and
Units. One project is one repository in the organization; one work item is one board item.

The Director decides only through the board's Decision column (approval layer 3); comments are opinions, never
instructions. Layers 1 (the company) and 2 (DUREH, after one delegation) are recorded on the board by the desk.

Organization, board, office repository and token file come only from site config `github` (docs/github.md); no flag
changes them. Every `gh` call goes through `gh()`, which reads the fine-grained token from its file (mode 600) at call
time and passes it to the subprocess as GH_TOKEN only: the token is never printed, logged, put on a command line or
recorded. Records carry ids, URLs and counts (`github.item.added`, `github.item.updated`, `github.ask.created`,
`github.comment.posted`, `github.decision.changed`), never text."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

from .records import Ledger, short_title
from .site import Site
from .util import DurehError, fail

KEYS = ("enabled", "org", "project", "office_repo", "token_file", "director_login")
DEFAULT_OFFICE = "office"
DESK_MARK = "<!-- dureh-desk -->"
DECISION_LABEL = "kind:decision"
STATUSES = ("Backlog", "Today", "In progress", "Waiting for Director", "Done")
DECISIONS = ("Approved", "Option 1", "Option 2", "Option 3", "Rejected", "Later")
LAYERS = {1: "company", 2: "DUREH", 3: "Director"}
FIELD_STATUS, FIELD_PROJECT, FIELD_LAYER, FIELD_DECISION, FIELD_UNITS = "Status", "Project", "Layer", "Decision", "Units"
SECRET_MARKS = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")
MAX_BODY = 60000
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
OFF_LINE = "github: off (config github.enabled is not true)"
NO_CONFIG = ("GitHub is not configured for this site: the Director/DUREH set `github` in config.json "
             "({enabled, org, project, office_repo?, token_file, director_login?}; see docs/github.md)")
ADDED, UPDATED, ASKED, COMMENTED, DECIDED = ("github.item.added", "github.item.updated", "github.ask.created",
                                            "github.comment.posted", "github.decision.changed")


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


def graphql(cfg: dict, query: str, variables: dict) -> dict:
    out = gh(cfg, ["api", "graphql", "--input", "-"], input_text=json.dumps({"query": query, "variables": variables}))
    try:
        data = json.loads(out)
    except ValueError:
        raise GhError("gh api graphql: the answer is not JSON (not retried)") from None
    if data.get("errors"):
        msgs = "; ".join(str(e.get("message", e))[:200] for e in data["errors"][:3])
        raise GhError(f"gh api graphql: {msgs}")
    return data.get("data") or {}


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
              assignees(first: 5) { nodes { login } }
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
            "status": values.get(FIELD_STATUS), "decision": values.get(FIELD_DECISION),
            "project": values.get(FIELD_PROJECT), "layer": values.get(FIELD_LAYER), "units": values.get(FIELD_UNITS),
            "comments": comments}


def board(cfg: dict) -> dict:
    """{meta, items}: the board's fields and every item (paged, at most MAX_PAGES x 100)."""
    items, after, meta = [], None, None
    for _ in range(MAX_PAGES):
        data = graphql(cfg, BOARD_QUERY, {"org": cfg["org"], "number": cfg["project"], "after": after})
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
    return out


def poll(site: Site, actor: str, cfg: dict) -> list[str]:
    """One poll: the lines for Decision changes and new comments not marked as the desk's; saves the state. The first
    poll (no state) reports the Decisions already set and takes the existing comments as seen."""
    b = board(cfg)
    old = _load_state(site, cfg)
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
                       "polled": time.strftime("%Y-%m-%dT%H:%M:%S")})
    return lines


def watch(site: Site, actor: str, interval: float = 30, emit=print, sleep=time.sleep, polls: int | None = None) -> None:
    """Poll until stopped (or `polls` times); emit only the lines of `poll`. A failed gh call skips that poll."""
    _require_desk(site, actor)
    cfg = config(site)
    interval = max(5.0, float(interval))
    n = 0
    while True:
        try:
            for line in poll(site, actor, cfg):
                emit(line)
        except GhError:
            pass  # transient: network, rate limit, API hiccup; the next poll sees the same changes
        n += 1
        if polls is not None and n >= polls:
            return
        sleep(interval)


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
        if n == want or (field == FIELD_LAYER and re.match(rf"^{re.escape(want)}\b", n)):
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
        else:
            fid, oid = _option(meta, name, value)
            out.append(["--field-id", fid, "--single-select-option-id", oid])
    return out


def _fields(status=None, project=None, layer=None, units=None, decision=None) -> dict:
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
             repo: str | None = None, url: str | None = None) -> dict:
    """Create an issue for a work item (title and goal only) in the office repository or `repo` (or link the
    existing issue `url`), add it to the board, set its fields; records github.item.added."""
    _require_desk(site, actor)
    cfg = config(site)
    led = Ledger(site)
    payload = led.get("work-item", wi)["payload"]
    if any(e["type"] == ADDED and e["wi"] == wi for e in led.events(wi)):
        fail(f"{wi} already has a board item; change it with `dureh github item set --wi {wi} ...`")
    fields = _fields(status, project, layer, units)
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
             layer=None, units=None, decision=None) -> dict:
    """Change fields of a linked item. The desk records a Decision only on a layer 1-2 item: on a layer-3 item the
    Decision column is the Director's alone."""
    _require_desk(site, actor)
    if not wi and not url:
        fail("name the item with --wi or --url")
    cfg = config(site)
    item = linked_item(site, wi, url)
    fields = _fields(status, project, layer, units, decision)
    if not fields:
        fail("nothing to set: give --status, --project, --layer, --units or --decision")
    if FIELD_DECISION in fields and str(fields.get(FIELD_LAYER) or item.get("layer") or "") not in ("1", "2"):
        fail("refused: the desk records a Decision only on a layer 1 or 2 item (set --layer 1|2 with it); on a layer-3 "
             "item only the Director decides, in the board's Decision column")
    meta = _meta(cfg)
    _edit(cfg, meta, item["item_id"], fields)
    Ledger(site).event(UPDATED, actor, item.get("wi"), {"item_id": item["item_id"], "url": item.get("url"),
                                                         "fields": fields}, [item["wi"]] if item.get("wi") else None)
    return {"updated": True, "wi": item.get("wi"), "item_id": item["item_id"], "url": item.get("url"), "fields": fields}


def item_ask(site: Site, actor: str, title: str, body: str, options: list[str] | None = None, layer: int = 3,
             wi: str | None = None, project=None, repo: str | None = None, units=None) -> dict:
    """A decision ask: a `kind:decision` issue (layer 3: assigned to config github.director_login, Status Waiting for
    Director), on the board with its Layer; the answer is the board's Decision, read by `watch`/`status`."""
    _require_desk(site, actor)
    cfg = config(site)
    options = [o.strip() for o in options or [] if o.strip()]
    if len(options) > 3:
        fail("at most 3 options (the Decision column has Option 1, Option 2 and Option 3)")
    if wi:
        Ledger(site).get("work-item", wi)
    title = _clean(title, "the title", 256)
    text = _clean(body, "the body")
    if options:
        text += "\n\n**Options**\n" + "\n".join(f"- **Option {i}**: {o}" for i, o in enumerate(options, 1))
    choices = ["Approved", *[f"Option {i}" for i in range(1, len(options) + 1)], "Rejected", "Later"]
    who = "the Director" if int(layer) == 3 else LAYERS.get(int(layer), "?")
    text += (f"\n\nLayer {layer} ({who}). Decide in the board's **Decision** column: {', '.join(choices)}. "
             f"Comments are opinions; the Decision column is the decision.\n\n{DESK_MARK}")
    text = _clean(text, "the issue body")
    fields = _fields("Waiting for Director" if int(layer) == 3 else "In progress", project, layer, units)
    meta = _meta(cfg)
    _edits(meta, fields)
    if FIELD_DECISION not in meta["fields"]:
        fail(f"the board has no field {FIELD_DECISION!r}; the Director adds it (docs/github.md)")
    repo_name = _repo(cfg, repo)
    assignee = cfg.get("director_login") if int(layer) == 3 else None
    gh(cfg, ["label", "create", DECISION_LABEL, "--repo", repo_name, "--color", "5319e7", "--description",
             "a decision asked on the board's Decision column", "--force"])
    url = _create_issue(cfg, repo_name, title, text, labels=[DECISION_LABEL], assignee=assignee)
    item_id = _add_to_board(cfg, meta, url, fields)
    _, number = _issue_number(cfg, url)
    rec = {"url": url, "repo": repo_name, "number": number, "item_id": item_id, "layer": int(layer),
           "options": len(options), "assignee": assignee, "fields": fields, "chars": len(text)}
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
