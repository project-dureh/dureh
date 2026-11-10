"""Company permissions switched at the site (`dureh site permit`), release 89.

Director 2026-10-30 (dureh-team#111 Approve 1, proposal site-permissions-2026-10-30 sections 2, 3 and 5, accepted with
dureh-team#112): the harness carries one catalog of the company config keys a site may switch, with a class per
change (B tightening or display, C widening and fail-closed, D the Director's own keys) and who may switch it:

- the Director's own terminal (no agent session: DUREH_ACTOR and CLAUDECODE unset) may switch any catalog key;
- the desk (inside an agent session) applies B changes alone and C changes only with `--approval`, a board Decision
  bound to the exact change (`github item ask --permit <plan>`: the sha256 of the canonical plan), re-read live from
  GitHub when it is used (fail-closed);
- class D keys (GitHub identity and credentials) are the Director's terminal only; harness-owned keys are refused.

Every write validates the whole resulting config (`config_errors`), backs up the previous config.json under
`state/config-history/`, replaces config.json atomically, records one `site.permit.*` event (old -> new, class, by,
via, memo, approval, binding, backup) and regenerates the adapter files. A refusal writes and records nothing.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

from .records import Ledger
from .site import Site
from .util import DurehError, fail, utc_compact, write_replace

CLASS_RANK = {"B": 1, "C": 2, "D": 3}
HARNESS_OWNED = ("site", "harness", "startup", "schema_version", "state", "index", "references", "runtime",
                 "upstream", "session", "env")
LIST_KEYS = ("push.policy", "push.allowed_remotes")
GITHUB_D_KEYS = ("enabled", "org", "project", "office_repo", "token_file", "app", "director_login",
                 "push_transport")
# running-behaviour keys not in the catalog yet (planned for release 90, which shipped exams instead; release 91 added
# `models.*` only): `site permit` refuses them and names the rule that still holds (release 93 wording)
LATER_KEYS = ("members", "office_hours", "containers", "slurm", "limits", "checks")
# Release 91 (docs/exams.md): `models.<tier>` is in the catalog with the quiet-point check (no agent running):
# waiver -> `run-<id>` for the same model is class B (config_errors resolves the run id to a passing qualification
# record of that tier and model), a new `model` is class C (a board Decision), a waiver set or extended is the
# Director's own (class D here: his terminal), `models.routing` / `models.fallback_tiers` / `models.candidates` class C.
MODEL_TABLE_KEYS = ("routing", "fallback_tiers", "candidates")
MODEL_TIER_FIELDS = ("model", "qualification", "waiver_expires", "note")
LATER_MODULE_KEYS = ("env_rebuild", "submit", "containers_cmd")
CONFIG_OPS = ("set", "unset", "add", "remove")
LINK_OPS = ("link-module", "new-module", "link-project", "new-project")
STEP_KEYS = {"set": ("op", "key", "value"), "unset": ("op", "key"), "add": ("op", "key", "value", "at"),
             "remove": ("op", "key", "item"),
             "link-module": ("op", "id", "source", "rev", "branch"),
             "new-module": ("op", "id", "package", "source", "python", "depends"),
             "link-project": ("op", "name", "source"), "new-project": ("op", "id", "purpose")}
STEP_REQUIRED = {"set": ("key", "value"), "unset": ("key",), "add": ("key", "value"), "remove": ("key", "item"),
                 "link-module": ("id", "source"), "new-module": ("id", "package"),
                 "link-project": ("name", "source"), "new-project": ("id",)}
PERMIT_OPTION = "Approve 1"  # the Decision a `github item ask --permit` binds its plan to
EVENT_PREFIX = "site.permit."
HISTORY_DIR = "config-history"

# The catalog (proposal section 2). T = the Director's terminal, D+ = the desk with --approval, Desk = the desk alone.
CATALOG = [
    {"key": "push.policy", "class": "C (B when it only tightens)", "fail_closed": True, "T": True, "D+": True,
     "Desk": "tightening only", "notes": "ordered GitHub push rules (release 89); add/remove/set; a public rule from"
                                         " the desk with a board decision (proposal Q3)"},
    {"key": "push.allowed_remotes", "class": "C (B when it only tightens)", "fail_closed": True, "T": True,
     "D+": True, "Desk": "tightening only", "notes": "exact and release-86 pattern entries; add/remove/set"},
    {"key": "push.standing_ff", "class": "C on, B off", "fail_closed": True, "T": True, "D+": True,
     "Desk": "off only", "notes": "site-wide standing fast-forward of string entries"},
    {"key": "push.standing_ff_new_branches", "class": "C on, B off", "fail_closed": True, "T": True, "D+": True,
     "Desk": "off only", "notes": "with push.standing_ff, new branches too"},
    {"key": "modules.<id>.push", "class": "B false, C otherwise", "fail_closed": True, "T": True, "D+": True,
     "Desk": "false only", "notes": "push-off narrows; anything else widens"},
    {"key": "github.dashboard_issue", "class": "B", "fail_closed": False, "T": True, "D+": True, "Desk": True,
     "notes": "display only; checked: an open issue of office_repo"},
    {"key": "link-module", "class": "B", "fail_closed": False, "T": True, "D+": True, "Desk": True,
     "notes": "plan step; reversible with retire-module; its push rule is a separate step"},
    {"key": "new-module", "class": "B", "fail_closed": False, "T": True, "D+": True, "Desk": True,
     "notes": "plan step"},
    {"key": "new-project", "class": "B", "fail_closed": False, "T": True, "D+": True, "Desk": True,
     "notes": "plan step; a records project where the package allows it"},
    {"key": "link-project", "class": "C", "fail_closed": True, "T": True, "D+": True, "Desk": False,
     "notes": "plan step, or `site link-project --approval`; a data project link"},
    {"key": "models.<tier>", "class": "B run id for the same model, C new model, D waiver", "fail_closed": True,
     "T": True, "D+": True, "Desk": "waiver -> passing run id of the same model; note",
     "notes": "release 91; only at a quiet point (no agent running); a waiver set or extended is the Director's terminal"},
    *[{"key": f"models.{k}", "class": "C", "fail_closed": True, "T": True, "D+": True, "Desk": False,
       "notes": "release 91; only at a quiet point (no agent running)"} for k in MODEL_TABLE_KEYS],
    *[{"key": f"github.{k}", "class": "D", "fail_closed": True, "T": True, "D+": False, "Desk": False,
       "notes": "GitHub identity and credentials: the Director's terminal only"} for k in GITHUB_D_KEYS],
    *[{"key": k, "class": "C (not yet)", "fail_closed": True, "T": False, "D+": False, "Desk": False,
       "notes": "running behaviour: not in the catalog in this release (a later release, with the quiet-point check);"
                " refused here, a config change under the Director's rule"}
      for k in (*LATER_KEYS, *(f"modules.<id>.{m}" for m in LATER_MODULE_KEYS))],
    *[{"key": k, "class": "-", "fail_closed": None, "T": False, "D+": False, "Desk": False,
       "notes": "harness-owned: install / adapter sync"} for k in HARNESS_OWNED],
]


# ------------------------------------------------------------------ who
def agent_session() -> bool:
    """Inside an agent session (the same guard as `site workday`): the run is the desk's, never the Director's."""
    return bool(os.environ.get("DUREH_ACTOR") or os.environ.get("CLAUDECODE"))


def _who(site: Site, actor: str) -> str:
    if agent_session():
        if site.org.role(actor).kind != "desk":
            fail(f"only the desk switches company permissions (actor {actor!r} is not the desk); report to the desk")
        return "desk"
    return "Director"


# ------------------------------------------------------------------ keys and values
def _parts(key: str) -> list[str]:
    if not isinstance(key, str) or not key.strip() or any(not p for p in key.split(".")):
        fail(f"a permit key is a dotted config path such as push.policy, not {key!r}")
    return key.split(".")


def catalog_key(key: str) -> str:
    """The catalog row a dotted key falls under; refuses keys outside this release's catalog."""
    p = _parts(key)
    if p[0] in HARNESS_OWNED:
        fail(f"{key} is harness-owned (install / `adapter sync`); `site permit` never changes it")
    if p[0] in LATER_KEYS or (p[0] == "modules" and len(p) == 3 and p[2] in LATER_MODULE_KEYS):
        fail(f"{key} changes running behaviour and is not in this release's permit catalog: `site permit` does not"
             " switch it yet (only push.*, modules.<id>.push, github.* and models.* are; the running-behaviour keys"
             " come in a later release with the quiet-point check). Until then it stays a config change made under"
             " the Director's rule (a merge window or the desk's OK); nothing was written")
    if p[0] == "models":  # release 91
        if len(p) == 2 and p[1] in MODEL_TABLE_KEYS:
            return key
        if len(p) == 2 or (len(p) == 3 and p[2] in MODEL_TIER_FIELDS and p[1] not in MODEL_TABLE_KEYS):
            return "models.<tier>"
        fail(f"{key} is not in the permit catalog: models.<tier>[.{'|'.join(MODEL_TIER_FIELDS)}] and"
             f" models.{{{','.join(MODEL_TABLE_KEYS)}}} are")
    if p[0] == "push" and len(p) == 2 and p[1] in ("policy", "allowed_remotes", "standing_ff", "standing_ff_new_branches"):
        return key
    if p[0] == "modules" and len(p) == 3 and p[2] == "push":
        return "modules.<id>.push"
    if p[0] == "github" and len(p) == 2 and (p[1] == "dashboard_issue" or p[1] in GITHUB_D_KEYS):
        return key
    fail(f"{key} is not in the permit catalog (`dureh site permit list`): catalog keys are push.policy,"
         " push.allowed_remotes, push.standing_ff, push.standing_ff_new_branches, modules.<id>.push,"
         " github.dashboard_issue and the Director's github keys")


def _module(cfg: dict, mid: str, must: bool = True) -> dict | None:
    m = next((e for e in cfg.get("modules") or [] if isinstance(e, dict) and e.get("id") == mid), None)
    if m is None and must:
        fail(f"{mid} is not a registered module (config.json modules[].id)")
    return m


_ABSENT = object()


def get_value(cfg: dict, key: str):
    p = _parts(key)
    if p[0] == "modules" and len(p) == 3:
        m = _module(cfg, p[1], must=False)
        return _ABSENT if m is None or p[2] not in m else m[p[2]]
    cur = cfg
    for part in p:
        if not isinstance(cur, dict) or part not in cur:
            return _ABSENT
        cur = cur[part]
    return cur


def _set_value(cfg: dict, key: str, value) -> None:
    p = _parts(key)
    if p[0] == "modules" and len(p) == 3:
        _module(cfg, p[1])[p[2]] = value
        return
    cur = cfg
    for part in p[:-1]:
        if cur.get(part) is None:
            cur[part] = {}
        if not isinstance(cur[part], dict):
            fail(f"config {part} is not an object; {key} cannot be set")
        cur = cur[part]
    cur[p[-1]] = value


def _unset_value(cfg: dict, key: str) -> None:
    p = _parts(key)
    if p[0] == "modules" and len(p) == 3:
        _module(cfg, p[1]).pop(p[2], None)
        return
    cur = cfg
    for part in p[:-1]:
        cur = cur.get(part) if isinstance(cur, dict) else None
        if not isinstance(cur, dict):
            return
    cur.pop(p[-1], None)


def _shown(v):
    return None if v is _ABSENT else v


# ------------------------------------------------------------------ plan steps
def canonical(steps: list[dict]) -> str:
    return json.dumps(steps, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def binding(steps: list[dict]) -> str:
    """The sha256 a board ask binds and an approval must match: the canonical JSON of the plan's steps."""
    return hashlib.sha256(canonical(steps).encode("utf-8")).hexdigest()


def check_steps(steps) -> list[dict]:
    """The plan's shape: a non-empty JSON list of steps {op, ...} with the keys of their op only."""
    if not isinstance(steps, list) or not steps:
        fail("a permit plan is a non-empty JSON list of steps, each {\"op\": set|unset|add|remove|link-module|"
             "new-module|link-project|new-project, ...}")
    for i, s in enumerate(steps):
        where = f"plan step {i + 1}"
        if not isinstance(s, dict) or s.get("op") not in STEP_KEYS:
            fail(f"{where} must be an object whose op is one of {', '.join(STEP_KEYS)}")
        unknown = sorted(set(s) - set(STEP_KEYS[s["op"]]))
        if unknown:
            fail(f"{where} ({s['op']}) has unknown key(s) {', '.join(unknown)} (allowed: {', '.join(STEP_KEYS[s['op']])})")
        missing = [k for k in STEP_REQUIRED[s["op"]] if k not in s]
        if missing:
            fail(f"{where} ({s['op']}) needs {', '.join(missing)}")
        if s["op"] in ("add", "remove") and s["key"] not in LIST_KEYS:
            fail(f"{where}: {s['op']} works on the list keys {', '.join(LIST_KEYS)}, not {s['key']!r}; use set")
        if "at" in s and (isinstance(s["at"], bool) or not isinstance(s["at"], int) or s["at"] < 0):
            fail(f"{where}: at must be a list index (an integer >= 0)")
        for k in ("id", "name", "source", "rev", "branch", "package", "python", "purpose", "item"):
            if k in s and (not isinstance(s[k], str) or not s[k].strip()):
                fail(f"{where}: {k} must be a non-empty string")
        if "depends" in s and not (isinstance(s["depends"], list) and all(isinstance(d, str) for d in s["depends"])):
            fail(f"{where}: depends must be a list of module ids")
    return steps


def load_plan(path) -> list[dict]:
    p = Path(path)
    try:
        steps = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"no plan file {p}")
    except json.JSONDecodeError as exc:
        fail(f"{p} is not JSON: {exc}")
    return check_steps(steps)


# ------------------------------------------------------------------ tightening (class B) or widening (class C)
def _rank_le(new, old, order) -> bool:
    return order.index(new) <= order.index(old)


def _globs_within(new: list[str] | None, old: list[str] | None) -> bool:
    """Every branch the new globs cover, the old covered (conservative: by string, `*` in old covers all)."""
    if old is None:
        return True
    if new is None:
        return False
    return "*" in old or set(new) <= set(old)


def _owner(url: str):
    from .ops import GITHUB_REMOTE

    m = GITHUB_REMOTE.match(url or "")
    return (m.group(1).lower(), m.group(2)) if m else None


def push_widening(old_p: dict, new_p: dict) -> list[str]:
    """Why the new `push` block may allow a push the old one refused (empty: the change only tightens, class B).
    Conservative: anything not proven narrower is a widening (class C)."""
    from .ops import push_policy, push_rules

    old_p, new_p = old_p or {}, new_p or {}
    out = []
    for k in ("standing_ff", "standing_ff_new_branches"):
        if new_p.get(k) is True and old_p.get(k) is not True:
            out.append(f"push.{k} turns on")
    old_r, new_r = push_rules(old_p), push_rules(new_p)
    old_by = {}
    for r in old_r:
        old_by.setdefault(r["url"], r)
    new_by = {}
    for r in new_r:
        new_by.setdefault(r["url"], r)
    new_pol = push_policy(new_p)
    for url, n in new_by.items():
        o = old_by.get(url)
        if o is None:
            out.append(f"push.allowed_remotes gains {url}")
            continue
        if bool(n.get("pattern")) != bool(o.get("pattern")) or not _globs_within(n["branches"], o["branches"]) \
                or (n["standing_ff"] and not o["standing_ff"]) or (n["tags"] and not o["tags"]) \
                or not _globs_within(n["new_branches"], o["new_branches"] if o["new_branches"] else []):
            out.append(f"push.allowed_remotes {url} allows more")
    for url, o in old_by.items():
        if url in new_by:
            continue
        own = _owner(url)
        later = [r for r in new_r if r.get("pattern") and own and _owner(r["url"]) and _owner(r["url"])[0] == own[0]]
        later += [r for r in new_pol if not r["deny"] and own and own[0] in [x.lower() for x in r["match"]["owner"]]]
        if later:
            out.append(f"removing push.allowed_remotes {url} lets a later rule of {own[0]} cover it")
    old_pol = push_policy(old_p)
    old_ids = [r["id"] for r in old_pol]
    old_pos = {r["id"]: i for i, r in enumerate(old_pol)}
    old_rule = {r["id"]: r for r in old_pol}
    kept = [r["id"] for r in new_pol if r["id"] in old_pos]
    if kept != sorted(kept, key=lambda i: old_pos[i]):
        out.append("push.policy rules change order")
    dec, tags, cl = ("never", "first", "always"), (False, "decision"), (False, None, True)
    for r in new_pol:
        o = old_rule.get(r["id"])
        if o is None:
            if not r["deny"]:
                out.append(f"push.policy gains rule {r['id']}")
            continue
        if r["match"] != o["match"]:
            out.append(f"push.policy rule {r['id']} changes its match")
            continue
        if o["deny"] and not r["deny"]:
            out.append(f"push.policy rule {r['id']} no longer denies")
            continue
        if r["deny"]:
            continue
        a, b, q, qo = r["allow"], o["allow"], r["require"], o["require"]
        if (a["standing_ff"] and not b["standing_ff"]) or (a["squash"] and not b["squash"]) \
                or not set(a["new_branches"]) <= set(b["new_branches"]) or not _rank_le(a["tags"], b["tags"], tags) \
                or dec.index(q["decision"]) < dec.index(qo["decision"]) or cl.index(q["cleanup"]) < cl.index(qo["cleanup"]):
            out.append(f"push.policy rule {r['id']} allows more")
    new_ids = {r["id"] for r in new_pol}
    for rid in old_ids:
        if rid in new_ids:
            continue
        o = old_rule[rid]
        owners = {x.lower() for x in o["match"]["owner"]}
        after = [r for r in new_pol if not r["deny"] and r["id"] in old_pos and old_pos[r["id"]] > old_pos[rid]
                 and owners & {x.lower() for x in r["match"]["owner"]}]
        if after:
            out.append(f"removing push.policy rule {rid} lets {after[0]['id']} cover what it {'denied' if o['deny'] else 'matched'}")
    return out


def step_class(old_cfg: dict, new_cfg: dict, step: dict) -> tuple[str, list[str]]:
    """(class, reasons) of one step: B tightening/display/accept, C widening, D the Director's keys."""
    op = step["op"]
    if op == "link-project":
        return "C", ["a data project link (fail-closed)"]
    if op in LINK_OPS:
        return "B", []
    row = catalog_key(step["key"])
    if row.startswith("push."):
        why = push_widening(old_cfg.get("push") or {}, new_cfg.get("push") or {})
        return ("C", why) if why else ("B", [])
    if row == "modules.<id>.push":
        return ("B", []) if op == "set" and step["value"] is False else ("C", ["a module's push turns on or is cleared"])
    if row == "github.dashboard_issue":
        return "B", []
    if row.startswith("models."):
        return model_class(old_cfg, new_cfg, step["key"])
    return "D", [f"{step['key']} is the Director's key (GitHub identity and credentials)"]


def model_class(old_cfg: dict, new_cfg: dict, key: str) -> tuple[str, list[str]]:
    """Release 91: the class of a `models.*` change (docs/exams.md, Permit catalog)."""
    p = key.split(".")
    if p[1] in MODEL_TABLE_KEYS:
        return "C", [f"models.{p[1]} changes which model runs which work"]
    tier = p[1]
    old = ((old_cfg.get("models") or {}).get(tier) or {})
    new = ((new_cfg.get("models") or {}).get(tier) or {})
    if not isinstance(old, dict) or not isinstance(new, dict):
        return "C", [f"models.{tier} is not a mapping"]
    if not new:
        return "B", []  # the tier is unmapped: its dispatch is refused (narrowing)
    q_old, q_new = old.get("qualification"), new.get("qualification")
    if q_new == "waived-bootstrap" and (q_old != "waived-bootstrap"
                                        or str(new.get("waiver_expires") or "") > str(old.get("waiver_expires") or "")
                                        or new.get("model") != old.get("model")):
        return "D", [f"a bootstrap waiver for {tier} is set or extended: the Director's own switch"]
    if new.get("model") != old.get("model"):
        return "C", [f"{tier} moves from {old.get('model')} to {new.get('model')}"]
    if q_new != q_old and not str(q_new or "").startswith("run-"):
        return "C", [f"{tier} qualification becomes {q_new!r}"]
    return "B", []  # waiver -> a passing run id of the same model (config_errors checks the record), note, end date


def quiet_point(site: Site) -> list[str]:
    """Release 91: the agents running now (a models change waits for none)."""
    from .ops import status
    from .upstream import _live_agents

    return _live_agents(status(site))


# ------------------------------------------------------------------ simulate
def _apply_config_step(cfg: dict, step: dict) -> None:
    op, key = step["op"], step["key"]
    if op == "set":
        _set_value(cfg, key, copy.deepcopy(step["value"]))
    elif op == "unset":
        if get_value(cfg, key) is _ABSENT:
            fail(f"{key} is not set; nothing to unset")
        _unset_value(cfg, key)
    elif op == "add":
        cur = get_value(cfg, key)
        items = [] if cur is _ABSENT or cur is None else cur
        if not isinstance(items, list):
            fail(f"config {key} is not a list")
        at = step.get("at", len(items))
        items = list(items)
        items.insert(min(at, len(items)), copy.deepcopy(step["value"]))
        _set_value(cfg, key, items)
    else:  # remove: a policy rule by id, an allowed_remotes entry by url (string or object)
        cur = get_value(cfg, key)
        items = cur if isinstance(cur, list) else []
        want = step["item"]

        def hit(e):
            if key == "push.policy":
                return isinstance(e, dict) and e.get("id") == want
            return e == want or (isinstance(e, dict) and e.get("url") == want)

        left = [e for e in items if not hit(e)]
        if len(left) == len(items):
            fail(f"{key} has no {'rule' if key == 'push.policy' else 'entry'} {want!r}")
        _set_value(cfg, key, left)


def _check_link_step(site: Site, cfg: dict, step: dict) -> None:
    from .sitelinks import NAME, records_projects

    op = step["op"]
    name = step.get("id") or step.get("name")
    if not NAME.match(name):
        fail(f"{op}: invalid name {name!r}")
    if op in ("link-module", "new-module"):
        if _module(cfg, name, must=False) is not None or (site.root / "modules" / name).exists():
            fail(f"{op}: module {name} is already linked")
    else:
        if any(isinstance(e, dict) and e.get("id") == name for e in cfg.get("projects") or []):
            fail(f"{op}: project {name} is already registered")
        if op == "link-project" and (site.root / "projects" / name).exists():
            fail(f"{op}: projects/{name} already exists")
        if op == "new-project" and not records_projects(site):
            fail("new-project: this package does not allow records projects (startup.yaml `projects: {records: true}`)")


def _check_values(site: Site, cfg: dict, step: dict, verify_github: bool) -> None:
    if step["op"] not in ("set", "add"):
        return
    row = catalog_key(step["key"])
    if row == "modules.<id>.push" and not isinstance(step["value"], bool):
        fail(f"{step['key']} must be true or false")
    if row == "github.dashboard_issue" and verify_github:
        dashboard_issue_open(site, cfg, step["value"])


def dashboard_issue_open(site: Site, cfg: dict, number) -> None:
    """The dashboard issue must be an open issue of office_repo (asked on GitHub; a failure refuses)."""
    from . import github

    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        fail(f"github.dashboard_issue must be an issue number of office_repo, not {number!r}")
    raw = cfg.get("github") or {}
    try:
        gcfg = github.config(site)
        repo = f"{raw.get('org') or gcfg['org']}/{raw.get('office_repo') or gcfg['office_repo']}"
        state = github.gh(gcfg, ["api", f"repos/{repo}/issues/{number}", "--jq", ".state"], timeout=30).strip()
    except DurehError as exc:
        fail(f"github.dashboard_issue {number}: GitHub could not be asked whether the issue is open: {exc}"
             " (refused, fail-closed)")
    if state != "open":
        fail(f"github.dashboard_issue {number}: issue {repo}#{number} is not an open issue (GitHub answered {state!r})")


def plan(site: Site, steps: list[dict], verify_github: bool = True) -> dict:
    """Dry run: every step applied in order to a copy of the config, each step's class and diff, the whole result
    through config_errors (errors the change adds refuse), the plan's class and the approval it needs, and the
    binding sha256 a board ask carries. Link steps are checked for their names and the package only."""
    from .ops import config_errors

    check_steps(steps)
    raw = json.loads(site.config_path.read_text(encoding="utf-8"))
    cfg = copy.deepcopy(raw)
    rows, cls, why_all = [], "B", []
    for i, step in enumerate(steps):
        if step["op"] in CONFIG_OPS:
            catalog_key(step["key"])
            before = copy.deepcopy(cfg)
            _check_values(site, cfg, step, verify_github)
            _apply_config_step(cfg, step)
            c, why = step_class(before, cfg, step)
            rows.append({"step": i + 1, "op": step["op"], "key": step["key"], "class": c, "why": why,
                         "old": _shown(get_value(before, step["key"])), "new": _shown(get_value(cfg, step["key"]))})
        else:
            _check_link_step(site, cfg, step)
            c, why = step_class(cfg, cfg, step)
            rows.append({"step": i + 1, "op": step["op"], "name": step.get("id") or step.get("name"),
                         "source": step.get("source"), "class": c, "why": why})
            if step["op"] in ("link-module", "new-module"):  # a later step may name the module (modules.<id>.push)
                cfg.setdefault("modules", []).append({"id": step["id"], "path": f"modules/{step['id']}",
                                                      **({"source": step["source"]} if step.get("source") else {})})
            else:
                cfg.setdefault("projects", []).append({"id": step.get("id") or step["name"]})
        if CLASS_RANK[c] > CLASS_RANK[cls]:
            cls = c
        why_all += why
    before_errs = set(config_errors(site, config=raw))
    new_errs = [e for e in config_errors(site, config=cfg) if e not in before_errs]
    if new_errs:
        fail("refused: the resulting config is invalid (nothing written): " + "; ".join(new_errs))
    need = {"B": "none: the desk applies it alone (recorded, one dashboard line)",
            "C": "a board Decision bound to this plan (`github item ask --class C --fail-closed --permit <plan>`, then"
                 " `--approval <item URL>`), or the Director runs it in his own terminal",
            "D": "the Director's own terminal only"}[cls]
    return {"steps": rows, "class": cls, "fail_closed": cls != "B", "why": why_all, "approval": need,
            "binding": binding(steps), "warnings": sorted(before_errs), "notes": checked_out_notes(site, raw, cfg),
            "_config": cfg, "_raw": raw}


def checked_out_notes(site: Site, old_cfg: dict, new_cfg: dict) -> list[str]:
    """Release 96 (UR-20261106-03): a new push.allowed_remotes entry for a local non-bare repository whose
    checked-out branch the entry covers: `dureh push` to that branch is refused (git's receive.denyCurrentBranch), so
    the plan says so before anyone approves it."""
    from .ops import _globs, checked_out_remote, push_rules

    old = {r["url"] for r in push_rules(old_cfg.get("push") or {})}
    out = []
    for r in push_rules(new_cfg.get("push") or {}):
        if r["url"] in old or r.get("pattern"):
            continue
        info = checked_out_remote(r["url"], site.root)
        heads = sorted(b[len("refs/heads/"):] for b in (info or {}).get("checked_out", {}) if b.startswith("refs/heads/"))
        heads = [b for b in heads if _globs(r["branches"], b)]
        if heads:
            out.append(f"{r['url']} is a local non-bare repository with {', '.join(heads)} checked out: `dureh push`"
                       " to " + ("that branch" if len(heads) == 1 else "those branches") + " is refused (git's"
                       " receive.denyCurrentBranch); it is delivered by a fast-forward pull on that side (`git -C <that"
                       " checkout> pull --ff-only <site copy> <ref>`), the Director's step, or by a bare remote instead")
    return out


# ------------------------------------------------------------------ approvals
def approval_used(site: Site, record_id: str) -> list[dict]:
    """Permit and project-link events that already used the approval record `record_id`. Other events carry
    `approval` in other shapes (release 93, UR-20261106-01: `slack.approval.decided` stores the decided event id as a
    string), so only a dict payload is read and every other shape is not a use."""
    def record(e: dict):
        a = (e.get("payload") or {}).get("approval")
        return a.get("record") if isinstance(a, dict) else None

    return [e for e in Ledger(site).events() if record(e) == record_id
            and (e["type"].startswith(EVENT_PREFIX) or e["type"] == "site.project-linked")]


def check_approval(site: Site, approval: str, steps: list[dict]) -> dict:
    """A board Decision as `--approval` (proposal 3.2): `approval` is a github.decision.changed record id or the item
    URL (its latest decision record). The ask for that item must carry `permit.sha256` equal to this plan's binding;
    the Decision must be the bound option; a live re-read of the board must show the same Decision on an open item.
    Any failure refuses (fail-closed)."""
    from . import github

    led = Ledger(site)
    events = led.events()
    approval = (approval or "").strip()
    if approval.startswith("https://"):
        decs = [e for e in events if e["type"] == github.DECIDED and e["payload"].get("url") == approval]
        if not decs:
            fail(f"--approval {approval}: no recorded Decision for that item (github.decision.changed; the desk's"
                 " `github watch` records it)")
        dec = decs[-1]
    else:
        try:
            dec = led.get("event", approval)
        except DurehError:
            fail(f"--approval {approval}: no such record; give the board item URL or its github.decision.changed id")
        if dec["type"] != github.DECIDED:
            fail(f"--approval {approval} is a {dec['type']} record, not a board Decision (github.decision.changed)")
    url = dec["payload"].get("url")
    later = [e for e in events if e["type"] == github.DECIDED and e["payload"].get("url") == url and e["ts"] > dec["ts"]]
    if later:
        fail(f"--approval {dec['id']}: the Decision on {url} changed since ({later[-1]['id']}); use the item URL")
    asks = [e for e in events if e["type"] == github.ASKED and e["payload"].get("url") == url]
    permit = next((e["payload"]["permit"] for e in reversed(asks) if e["payload"].get("permit")), None)
    if not permit:
        fail(f"--approval: {url} was not asked with `github item ask --permit <plan>`, so no change is bound to it")
    want = binding(steps)
    if permit.get("sha256") != want:
        fail(f"--approval: the Decision on {url} approves plan {str(permit.get('sha256'))[:12]}, not this change"
             f" ({want[:12]}); apply exactly the plan that was asked")
    to = github.decision_value(dec["payload"].get("to"))
    bound = permit.get("option") or PERMIT_OPTION
    if to != bound:
        fail(f"--approval: the Decision on {url} is {to or 'empty'}, not {bound} (Rejected and Discuss hold the change)")
    try:
        items = github.board(github.config(site))["items"]
    except DurehError as exc:
        fail(f"--approval: the board could not be read to confirm the Decision on {url} (refused, fail-closed): {exc}")
    it = next((i for i in items if i.get("url") == url), None)
    if it is None:
        fail(f"--approval: {url} is not on the board (refused, fail-closed)")
    if (it.get("state") or "").upper() != "OPEN":
        fail(f"--approval: {url} is closed; a closed item approves nothing")
    if github.decision_value(it.get("decision")) != to:
        fail(f"--approval: the Decision on {url} is now {it.get('decision') or 'empty'} on the board, not {to}")
    return {"record": dec["id"], "url": url, "decision": to,
            "director_comments": dec["payload"].get("director_comments") or [], "binding": want}


def _approval_for(who: str, cls: str, appr: dict | None, what: str) -> None:
    if cls == "D" and who != "Director":
        fail(f"refused: {what} changes a class D key; that is the Director's switch: run it from your own terminal,"
             " not inside an agent session")
    if cls == "C" and who != "Director" and not appr:
        fail(f"refused: {what} is class C (fail-closed) and needs --approval: ask on the board with `github item ask"
             " --class C --fail-closed --permit <plan file>` and apply with `--approval <item URL>` after the"
             " Director's Decision, or the Director runs this command in his own terminal")


# ------------------------------------------------------------------ write
def _backup(site: Site, text: str) -> str:
    d = site.state / HISTORY_DIR
    d.mkdir(parents=True, exist_ok=True)
    name = f"{utc_compact()}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}.json"
    write_replace(d / name, text)
    return f"state/{HISTORY_DIR}/{name}"


def _write(site: Site, cfg: dict) -> None:
    write_replace(site.config_path, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", mode=0o644)


def _regenerate(site: Site) -> int:
    from .runtimes import claude_code

    return len(claude_code.generate(Site(site.root))["files"])


def _last_event(site: Site, type_: str, since: str) -> str | None:
    evs = [e for e in Ledger(site).events() if e["type"] == type_ and e["ts"] >= since]
    return evs[-1]["id"] if evs else None


def _run_link(site: Site, actor: str, step: dict, approval: dict | None) -> dict:
    from . import sitelinks
    from .util import utc_now

    since, op = utc_now(), step["op"]
    if op == "link-module":
        out = sitelinks.link_module(site, actor, step["id"], step["source"], step.get("rev") or step.get("branch")
                                    or "HEAD", step.get("branch"))
        ev = "site.module-linked"
    elif op == "new-module":
        out = sitelinks.new_module(site, actor, step["id"], step["package"], step.get("source"), step.get("python"),
                                   step.get("depends") or [])
        ev = "site.module-created"
    elif op == "link-project":
        out = sitelinks.link_project(site, actor, step["name"], step["source"], approved=approval or {"plan": True})
        ev = "site.project-linked"
    else:
        out = sitelinks.new_project(site, actor, step["id"], step.get("purpose"))
        ev = "site.project-created"
    return {"op": op, "name": step.get("id") or step.get("name"), "event": ev, "record": _last_event(site, ev, since),
            "rev": out.get("rev")}


COMPENSATION = {"link-module": "dureh site retire-module --id {name}", "new-module": "dureh site retire-module --id {name}",
                "link-project": "remove projects/{name} and its config.json projects entry (the Director)",
                "new-project": "remove projects/{name} and its config.json projects entry (the Director)"}


def apply(site: Site, actor: str, steps: list[dict], memo: str, approval: str | None = None,
          via: str = "dureh site permit apply", event: str = "site.permit.applied") -> dict:
    """Run a plan: who and class (agent session = the desk: B alone, C with a bound board Decision, D refused;
    otherwise the Director), validation of the whole result, backup, atomic config writes and link steps in order,
    one record, adapter regeneration. A failed step stops the plan and records site.permit.failed with what was done
    and how to undo it."""
    if not (memo or "").strip():
        fail("--memo is required (why this permission changes)")
    who = _who(site, actor)
    appr = None
    if approval:  # first: the same change applied again is a no-op, before the plan is checked against the config
        appr = check_approval(site, approval, check_steps(steps))
        used = approval_used(site, appr["record"])
        other = [e for e in used if (e["payload"].get("approval") or {}).get("binding") != appr["binding"]]
        if other:
            fail(f"refused: the Decision {appr['record']} ({appr['url']}) was already used for another change"
                 f" ({other[-1]['id']}); ask again for this one")
        done_before = [e for e in used if e["type"].startswith(EVENT_PREFIX) and e["type"] != "site.permit.failed"]
        if done_before:
            return {"applied": False, "already": [e["id"] for e in done_before], "binding": appr["binding"],
                    "note": f"the change bound to {appr['url']} was already applied ({done_before[-1]['id']});"
                            " nothing to do"}
    pl = plan(site, steps)
    _approval_for(who, pl["class"], appr, "this change")
    if any(str(s.get("key") or "").startswith("models") for s in steps if s["op"] in CONFIG_OPS):
        live = quiet_point(site)
        if live:
            fail("refused: a models change waits for a quiet point (no agent running); running: " + ", ".join(live))
    from .runtimes.claude_code import require_pinned

    require_pinned(site)  # release 97: before config.json is written (the regeneration below would refuse anyway)
    raw_text = site.config_path.read_text(encoding="utf-8")
    backup = _backup(site, raw_text)
    done, written = [], False
    links = []
    try:
        for i, step in enumerate(steps):
            if step["op"] in CONFIG_OPS:
                cfg = json.loads(site.config_path.read_text(encoding="utf-8"))
                old = _shown(get_value(cfg, step["key"]))
                _apply_config_step(cfg, step)
                _write(site, cfg)
                written = True
                done.append({"step": i + 1, "op": step["op"], "key": step["key"], "old": old,
                             "new": _shown(get_value(cfg, step["key"]))})
            else:
                r = _run_link(Site(site.root), actor, step, appr)
                written = True
                links.append(r)
                done.append({"step": i + 1, **r})
    except Exception as exc:  # record what happened, then fail (as retire-module)
        err = str(exc).strip() or exc.__class__.__name__
        comp = [f"restore config.json from {backup}"] + [COMPENSATION[d["op"]].format(name=d["name"])
                                                          for d in done if d["op"] in COMPENSATION]
        Ledger(site).event("site.permit.failed", actor, None, {
            "failed_step": len(done) + 1, "error": err[:800], "done": done, "compensation": comp, "by": who,
            "via": via, "memo": memo, "backup": backup, "binding": pl["binding"], "approval": appr})
        if written:
            try:
                _regenerate(site)
            except Exception:  # noqa: BLE001 - the failure above is what is reported
                pass
        fail(f"permit plan stopped at step {len(done) + 1}: {err}. Done so far: {len(done)} step(s); recorded"
             f" site.permit.failed. Undo: {'; '.join(comp)}")
    generated = _regenerate(site)
    payload = {"class": pl["class"], "fail_closed": pl["fail_closed"], "by": who, "via": via, "memo": memo,
               "approval": appr, "binding": pl["binding"], "backup": backup, "generated": generated}
    if len(steps) == 1 and steps[0]["op"] in CONFIG_OPS:
        payload = {"key": steps[0]["key"], "op": steps[0]["op"], "old": done[0]["old"], "new": done[0]["new"],
                   **payload}
    else:
        payload["steps"] = done
    refs = [r["record"] for r in links if r.get("record")]
    rec = Ledger(site).event(event, actor, None, payload, refs)
    note = None
    if appr and who == "desk":
        from . import github

        try:
            github.comment(site, actor, f"Applied with `dureh site permit` ({rec['id']}, plan {pl['binding'][:12]}).",
                           url=appr["url"])
        except DurehError as exc:
            note = f"the change is applied and recorded, but the board comment failed: {exc}"
    out = {"applied": True, "record": rec["id"], "event": event, "class": pl["class"], "by": who, "backup": backup,
           "steps": done, "generated": generated}
    if note:
        out["note"] = note
    return out


def set_key(site, actor, key, value, memo, approval=None):
    return apply(site, actor, [{"op": "set", "key": key, "value": value}], memo, approval,
                 "dureh site permit set", "site.permit.set")


def unset_key(site, actor, key, memo, approval=None):
    return apply(site, actor, [{"op": "unset", "key": key}], memo, approval,
                 "dureh site permit unset", "site.permit.unset")


def add_item(site, actor, key, value, memo, at=None, approval=None):
    step = {"op": "add", "key": key, "value": value, **({"at": at} if at is not None else {})}
    return apply(site, actor, [step], memo, approval, "dureh site permit add", "site.permit.added")


def remove_item(site, actor, key, item, memo, approval=None):
    return apply(site, actor, [{"op": "remove", "key": key, "item": item}], memo, approval,
                 "dureh site permit remove", "site.permit.removed")


# ------------------------------------------------------------------ migrate
def _rule_id(owner: str, taken: set) -> str:
    base = f"{owner.lower()}-private"
    rid, n = base, 2
    while rid in taken:
        rid, n = f"{base}-{n}", n + 1
    return rid


def migrate_push(site: Site, actor: str, dry_run: bool = False, memo: str | None = None) -> dict:
    """Release-86 pattern entries of push.allowed_remotes -> push.policy rules with the same meaning (proposal 4.3):
    {url: git@github.com:<owner>/<glob>.git, pattern: true, branches, standing_ff, new_branches} becomes
    {id: <owner>-private, match: {owner, name: [glob], visibility: private, branches}, allow: {standing_ff,
    new_branches, tags: false, squash: false}, require: {decision: never}}, put at the top of push.policy in the
    entries' order (pattern entries came before every policy rule). Where policy rules follow, a deny rule over the
    same repositories keeps what the pattern refused (another branch, a tag) refused. Exact entries and the
    site-wide standing_ff stay as they are. Class B (no behaviour change), recorded site.permit.migrated."""
    from .ops import GITHUB_REMOTE, push_rules

    raw = json.loads(site.config_path.read_text(encoding="utf-8"))
    pcfg = raw.get("push") or {}
    allowed = pcfg.get("allowed_remotes") or []
    patterns = [e for e in allowed if isinstance(e, dict) and e.get("pattern") is True]
    if not patterns:
        return {"migrated": False, "note": "no release-86 pattern entries in push.allowed_remotes; nothing to migrate"}
    policy = list(pcfg.get("policy") or [])
    taken = {r.get("id") for r in policy if isinstance(r, dict)}
    rules = []
    for e in patterns:
        r = next(x for x in push_rules({"allowed_remotes": [e]}))
        owner, name = GITHUB_REMOTE.match(e["url"]).groups()
        rid = _rule_id(owner, taken)
        taken.add(rid)
        match = {"owner": [owner], "name": [name], "visibility": "private"}
        if r["branches"] is not None:
            match["branches"] = r["branches"]
        rules.append({"id": rid, "match": match,
                      "allow": {"standing_ff": r["standing_ff"], "new_branches": r["new_branches"], "tags": False,
                                "squash": False},
                      "require": {"decision": "never"}})
        if policy:
            deny_id = _rule_id(f"{owner}-rest", taken)
            taken.add(deny_id)
            rules.append({"id": deny_id, "match": {"owner": [owner], "name": [name], "visibility": "private"},
                          "deny": True})
    new = copy.deepcopy(raw)
    new["push"]["allowed_remotes"] = [e for e in allowed if not (isinstance(e, dict) and e.get("pattern") is True)]
    new["push"]["policy"] = rules + policy
    out = {"from": patterns, "to": rules, "dry_run": dry_run}
    if dry_run:
        return out
    who = _who(site, actor)
    from .ops import config_errors

    before = set(config_errors(site, config=raw))
    errs = [e for e in config_errors(site, config=new) if e not in before]
    if errs:
        fail("refused: the migrated config is invalid (nothing written): " + "; ".join(errs))
    from .runtimes.claude_code import require_pinned

    require_pinned(site)  # release 97
    backup = _backup(site, site.config_path.read_text(encoding="utf-8"))
    _write(site, new)
    generated = _regenerate(site)
    rec = Ledger(site).event("site.permit.migrated", actor, None, {
        "key": "push", "op": "migrate", "old": pcfg, "new": new["push"], "class": "B", "fail_closed": False,
        "by": who, "via": "dureh site permit migrate push", "memo": memo or "release-86 pattern entries -> push.policy",
        "approval": None, "backup": backup, "generated": generated})
    return {**out, "migrated": True, "record": rec["id"], "backup": backup}


# ------------------------------------------------------------------ read
def _value_line(v) -> str:
    if v is _ABSENT:
        return "(absent)"
    text = json.dumps(v, ensure_ascii=False)
    return text if len(text) <= 100 else text[:97] + "..."


def list_rows(site: Site) -> list[dict]:
    cfg = site.config
    rows = []
    for row in CATALOG:
        k = row["key"]
        if k == "modules.<id>.push":
            cur = {m["id"]: m.get("push", "(absent)") for m in cfg.get("modules") or [] if isinstance(m, dict) and "id" in m}
            value = json.dumps(cur, ensure_ascii=False)
        elif "<id>" in k or k in LINK_OPS or row["class"] == "-":
            value = "-"
        else:
            value = _value_line(get_value(cfg, k))
        rows.append({**row, "value": value})
    return rows


def render_list(rows: list[dict]) -> str:
    def yn(v):
        return {True: "yes", False: "no", None: "-"}.get(v, v)

    out = ["key | class | fail-closed | Director's terminal | desk + --approval | desk alone | value"]
    for r in rows:
        out.append(f"{r['key']} | {r['class']} | {yn(r['fail_closed'])} | {yn(r['T'])} | {yn(r['D+'])} |"
                   f" {yn(r['Desk'])} | {r['value']}")
    return "\n".join(out) + "\n"


def show(site: Site, key: str, history: bool = False) -> dict:
    row = catalog_key(key)
    out = {"key": key, "catalog": next(r for r in CATALOG if r["key"] == row),
           "value": _shown(get_value(site.config, key))}
    if history:
        hist = []
        for e in Ledger(site).events():
            if not e["type"].startswith(EVENT_PREFIX):
                continue
            p = e["payload"]
            keys = [p.get("key")] + [s.get("key") for s in p.get("steps") or []] + (["push"] if p.get("key") == "push" else [])
            if key in keys or (p.get("key") == "push" and key.startswith("push.")):
                hist.append({"id": e["id"], "ts": e["ts"], "type": e["type"], "by": p.get("by"), "memo": p.get("memo"),
                             "approval": p["approval"].get("url") if isinstance(p.get("approval"), dict) else None})
        out["history"] = hist
    return out


def render_plan(pl: dict) -> str:
    lines = [f"plan class {pl['class']} ({'fail-closed' if pl['fail_closed'] else 'not fail-closed'}); approval: "
             f"{pl['approval']}", f"binding sha256 {pl['binding']}"]
    for r in pl["steps"]:
        if "key" in r:
            lines.append(f"  {r['step']}. {r['op']} {r['key']} [{r['class']}]: {_value_line(r['old'] if r['old'] is not None else _ABSENT)}"
                         f" -> {_value_line(r['new'] if r['new'] is not None else _ABSENT)}")
        else:
            lines.append(f"  {r['step']}. {r['op']} {r['name']}" + (f" from {r['source']}" if r.get("source") else "")
                         + f" [{r['class']}]")
        for w in r["why"]:
            lines.append(f"     widens: {w}")
    for w in pl["warnings"]:
        lines.append(f"warning (already in the config, not added by this plan): {w}")
    for n in pl.get("notes") or []:
        lines.append(f"note: {n}")
    return "\n".join(lines) + "\n"


def public_plan(pl: dict) -> dict:
    return {k: v for k, v in pl.items() if not k.startswith("_")}
