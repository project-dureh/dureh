"""Work-item lifecycle tools: wi, dispatch, verify (per WI), promote, purge."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import stat
import subprocess
import time
from datetime import date
from pathlib import Path

from .paths import target_root
from .records import Ledger
from .site import Site
from .util import fail, git, sha256_file

BRIEF_TAG = "DUREH-BRIEF"
CACHE_RE = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.venv)(/|$)|\.py[co]$")


# --------------------------------------------------------------------- wi
def wi_new(site: Site, actor: str, title: str, goal: str, role: str, target: str, acceptance: list[str],
           lane: str = "work", bundle: str | None = None, value: str | None = None, checks: list[str] | None = None,
           outputs: list[str] | None = None, constraints: list[str] | None = None, parent: str | None = None,
           source: str | None = None, academic: bool = False) -> dict:
    org = site.org
    if org.role(actor).kind != "desk":
        fail("only the desk creates work items")
    r = org.role(role)
    if r.kind == "desk":
        fail("a work item is owned by a manager or worker role, not the desk")
    if target not in ("harness", "package", "site") and target_root(site, target) is None:
        fail(f"target must be harness, package, site, modules/<name>, or projects/<name>: {target!r}")
    if not acceptance:
        fail("a work item needs at least one acceptance criterion")
    led = Ledger(site)
    prec = led.get("work-item", parent) if parent else None
    if prec:
        owner = prec["payload"]["role"]
        if role not in org.role(owner).may_dispatch and role not in org.role(actor).may_dispatch:
            fail(f"parent {parent} is owned by {owner}, which cannot dispatch {role}")
    elif role not in org.role(actor).may_dispatch:
        managers = [m for m in org.dispatcher_of(role) if org.role(m).is_manager]
        fail(f"desk cannot dispatch {role}; create it with --parent <bundle> owned by one of: {', '.join(managers) or 'none'}")
    payload = {"title": title, "goal": goal, "role": role, "target": target, "acceptance": acceptance,
               "lane": lane, "bundle": bundle, "value": value, "checks": list(checks or []),
               "constraints": list(constraints or []), "parent": parent, "source": source,
               "scope_frozen": lane == "reorg", "academic": bool(academic)}
    if outputs:
        payload["outputs"] = list(outputs)
    draft = {"id": "WI-0000", "payload": payload}
    n, limit = brief_bytes(site, draft), _brief_limit(site, r)
    if n > limit:
        fail(f"brief would be {n} bytes, over the {limit}-byte limit; shorten goal, acceptance, checks or constraints, "
             "or split the work item")
    if prec and org.role(prec["payload"]["role"]).is_manager:
        prole = org.role(prec["payload"]["role"])
        n, limit = brief_bytes(site, prec, extra_children=[draft]), _brief_limit(site, prole)
        if n > limit:
            fail(f"bundle {parent} brief would be {n} bytes, over the {limit}-byte limit with this item; "
                 "shorten the item titles or split the bundle")
    return led.new_work_item(actor, payload)


def _brief_limit(site: Site, role) -> int:
    return int(site.limit("manager_brief_bytes" if role.is_manager else "worker_brief_bytes", 2048))


def brief_bytes(site: Site, rec: dict, extra_children: list[dict] | None = None) -> int:
    """Size of the brief `dispatch brief` would render for this record, without creating a worktree."""
    role = site.org.role(rec["payload"]["role"])
    wt = {"worktree": None, "base": None}
    if role.is_worker and role.write_scopes and role.isolation == "worktree":
        root = target_root(site, rec["payload"]["target"])
        if root is not None and (root / ".git").exists():
            wt = {"worktree": str(site.worktrees / rec["id"]), "base": "0" * 40, "branch": wi_branch(site, rec["id"], root)}
    return len(_render_brief(site, rec, role.name, wt, {}, extra_children).encode("utf-8"))


def wi_approve(site: Site, actor: str, wi: str, memo: str, by: str = "Director") -> dict:
    """Gate 1. The desk records the Director's approval as a decision, then the event."""
    led = Ledger(site)
    led.get("work-item", wi)
    dec = led.append("decision", "gate1", actor, wi, {"subject": f"Gate 1 {wi}", "disposition": "approve",
                                                        "by": by, "memo": memo})
    return led.event("wi.approved", actor, wi, {"decision": dec["id"], "by": by}, [dec["id"]])


# --------------------------------------------------------------- dispatch
def _qualified(entry: dict) -> tuple[bool, str]:
    q = entry.get("qualification")
    if q == "waived-bootstrap":
        exp = entry.get("waiver_expires")
        if not exp:
            return False, "bootstrap waiver has no expiry"
        if date.fromisoformat(exp) < date.today():
            return False, f"bootstrap waiver expired on {exp}"
        return True, f"bootstrap waiver until {exp} (not a qualification)"
    if isinstance(q, str) and q.startswith("run-"):
        return True, f"qualification record {q}"
    return False, "no qualification record or waiver"


def _children(site: Site, bundle_wi: str) -> list[dict]:
    return [w for w in Ledger(site).load("work-item") if w["payload"].get("parent") == bundle_wi]


def _create_worktree(site: Site, wi: str, target: str) -> dict:
    root = target_root(site, target)
    if root is None or not (root / ".git").exists():
        return {"worktree": None, "base": None, "note": "target is not a git repository; no isolated worktree"}
    wt = site.worktrees / wi
    if wt.exists():
        err = worktree_problem(wt, wi, site)
        if err:
            fail(f"worktree {wt} is broken ({err}); repair it with `dureh worktree repair --wi {wi}`")
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], wt)
        base = git(["rev-parse", branch], root, check=False) or None
        return {"worktree": str(wt), "base": _base_of(site, wi) or base, "branch": branch, "note": "reused existing worktree"}
    site.worktrees.mkdir(exist_ok=True)
    base = git(["rev-parse", "HEAD"], root)
    branch = wi_branch(site, wi, root)
    if git(["branch", "--list", branch], root):
        git(["worktree", "add", str(wt), branch], root)
    else:
        git(["worktree", "add", "-b", branch, str(wt), base], root)
    return {"worktree": str(wt), "base": base, "branch": branch, "note": "new worktree"}


def wi_branches(site: Site, wi: str) -> tuple[str, str]:
    """(current name, legacy name). New WIs use wi/<site-id>/<WI> so sites never share a branch (D4)."""
    return f"wi/{site.site_id}/{wi}", f"wi/{wi}"


def wi_branch(site: Site, wi: str, root: Path | None = None) -> str:
    """The branch a WI works on: a legacy wi/<WI> branch that already exists keeps being used, else the site name."""
    new, legacy = wi_branches(site, wi)
    if root is not None and (root / ".git").exists() and git(["branch", "--list", legacy], root, check=False) \
            and not git(["branch", "--list", new], root, check=False):
        return legacy
    return new


def worktree_problem(wt: Path, wi: str, site: Site | None = None) -> str | None:
    """None when wt is a usable worktree on its WI branch; otherwise what is wrong."""
    if not Path(wt).is_dir():
        return f"{wt} does not exist"
    proc = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt, capture_output=True, text=True)
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout).strip()[-300:] or "not a git worktree"
    names = wi_branches(site, wi) if site else (f"wi/{wi}",)
    if proc.stdout.strip() not in names:
        return f"checked out {proc.stdout.strip()}, not {' or '.join(names)}"
    return None


def worktree_repair(site: Site, actor: str, wi: str) -> dict:
    """Re-link a WI worktree after the site moved, keeping branch and commits; prune it if its directory is gone."""
    from .ops import agent_state

    if site.org.role(actor).kind != "desk":
        fail("only the desk repairs worktrees")
    led = Ledger(site)
    p = led.get("work-item", wi)["payload"]
    root = target_root(site, p["target"])
    if root is None or not (root / ".git").exists():
        fail(f"{wi} targets {p['target']}, which is not a git repository")
    if led.wi_status(wi)["status"] == "dispatched" and agent_state(led, wi) == "running":
        fail(f"an agent is running for {wi}; stop it before repairing its worktree")
    wt = site.worktrees / wi
    if not wt.exists():
        git(["worktree", "prune"], root)
        state, env = "pruned", {"env": "ok"}
    else:
        if worktree_problem(wt, wi, site) is None:
            state = "ok"
        else:
            proc = subprocess.run(["git", "worktree", "repair", str(wt)], cwd=root, capture_output=True, text=True)
            err = worktree_problem(wt, wi, site)
            if err:
                fail(f"repair of {wt} failed: {err} {(proc.stderr or '').strip()[-300:]}")
            state = "repaired"
        env = _repair_env(site, wt, p["target"])
    if state != "ok" or env["env"] == "rebuilt":
        led.event("worktree.repaired", actor, wi, {"worktree": str(wt), "target": p["target"], "state": state,
                                                   **{k: v for k, v in env.items() if k != "hint"}})
    return {"wi": wi, "worktree": str(wt), "state": state, **env}


def _base_of(site: Site, wi: str) -> str | None:
    for ev in reversed(Ledger(site).events(wi)):
        if ev["type"] == "wi.amended" and ev["payload"].get("rebase_onto"):
            return ev["payload"]["rebase_onto"]
        if ev["type"] == "dispatch.prepared" and ev["payload"].get("base"):
            return ev["payload"]["base"]
    return None


def _pending_rebase(site: Site, wi: str) -> str | None:
    """The base an amendment moved this WI to, if no dispatch has carried it yet."""
    for ev in reversed(Ledger(site).events(wi)):
        if ev["type"] == "dispatch.prepared" and not ev["payload"].get("checking"):
            return None
        if ev["type"] == "wi.amended" and ev["payload"].get("rebase_onto"):
            return ev["payload"]["rebase_onto"]
    return None


AMENDABLE = ("acceptance", "checks", "constraints", "outputs")


def wi_amend(site: Site, actor: str, wi: str, memo: str, fields: dict, rebase: str | None = None) -> dict:
    """Change a WI's acceptance, checks, constraints or outputs, or move its base, without a replacement WI.

    The goal, role and target never change (that is a new WI). The amendment is approved like Gate 1:
    --memo carries the Director's words (or, in workday mode, the plan approval that covers it)."""
    from .ops import agent_state

    if site.org.role(actor).kind != "desk":
        fail("only the desk amends work items")
    if not memo.strip():
        fail("--memo must carry the Director's words (or the day plan approval that covers the amendment)")
    led = Ledger(site)
    rec = led.get("work-item", wi)
    st = led.wi_status(wi)["status"]
    if st not in ("approved", "rework", "dispatched", "proposed", "stopped"):
        fail(f"{wi} is {st}; amend only proposed, approved, rework, stopped or dispatched-and-ended items")
    if st == "dispatched" and agent_state(led, wi) in ("running", "starting"):
        fail(f"an agent is running for {wi}; stop it at a safe point before amending")
    bad = [k for k in fields if k not in AMENDABLE]
    if bad:
        fail(f"cannot amend {', '.join(bad)}; a different goal, role or target is a new work item")
    fields = {k: list(v) for k, v in fields.items() if v is not None}
    if "acceptance" in fields and not fields["acceptance"]:
        fail("a work item needs at least one acceptance criterion")
    rebase_onto = None
    if rebase is not None:
        root = target_root(site, rec["payload"]["target"])
        if root is None or not (root / ".git").exists():
            fail(f"{wi} targets {rec['payload']['target']}, which is not a git repository")
        rebase_onto = git(["rev-parse", rebase or "HEAD"], root)
    if not fields and not rebase_onto:
        fail("nothing to amend: give --acceptance, --check, --constraint, --output or --rebase")
    draft = {"id": wi, "payload": {**rec["payload"], **fields}}
    role = site.org.role(rec["payload"]["role"])
    n, limit = brief_bytes(site, draft), _brief_limit(site, role)
    if n > limit:
        fail(f"amended brief would be {n} bytes, over the {limit}-byte limit")
    before = {k: rec["payload"].get(k) for k in fields}
    ev = led.event("wi.amended", actor, wi, {"fields": fields, "before": before, "rebase_onto": rebase_onto,
                                              "memo": memo, "by": "Director"})
    if st == "stopped":  # an amended stopped item is re-opened with the same approval words
        wi_approve(site, actor, wi, f"amended ({ev['id']}): {memo}")
    return ev


def _skill_for(site: Site, role: str, value: str | None) -> str | None:
    skills = (site.package_manifest().get("skills") or {})
    rel = skills.get(role)
    if not rel and value:
        for v in site.package_manifest().get("values") or []:
            if v.get("id") == value:
                rel = v.get("skill")
    if rel and (site.startup / rel).is_file():
        return str(site.startup / rel)
    return None


def dispatch_brief(site: Site, wi: str, by: str, as_role: str | None = None) -> dict:
    led = Ledger(site)
    org = site.org
    rec = led.get("work-item", wi)
    p = rec["payload"]
    checking = as_role == "checker"
    if as_role and not checking and as_role != p["role"]:
        fail(f"{wi} is owned by {p['role']}; only the checker can be dispatched for another role's item")
    role = org.role(as_role or p["role"])
    caller = org.role(by)
    if role.name not in caller.may_dispatch:
        fail(f"{by} may not dispatch {role.name}")
    if caller.is_manager:
        bundle = wi if checking and p["role"] == caller.name else p.get("parent")
        if not bundle or led.get("work-item", bundle)["payload"]["role"] != caller.name:
            fail(f"{by} may dispatch only work items in its own bundle")
        if led.wi_status(bundle)["status"] != "dispatched":
            fail(f"bundle {bundle} is not dispatched to {by}")
    st = led.wi_status(wi)["status"]
    supersedes = None
    if checking:
        if st != "staged":
            fail(f"{wi} is {st}; the checker is dispatched only for staged work")
    elif st == "dispatched":
        supersedes = _superseded_dispatch(site, led, wi)
    elif st not in ("approved", "rework"):
        fail(f"{wi} is {st}; only approved or rework items can be dispatched")
    entry = site.model_for_tier("writer-academic" if p.get("academic") and not checking else role.tier)
    ok, why = _qualified(entry)
    if not ok:
        fail(f"tier {role.tier} model {entry.get('model')} cannot be dispatched: {why}")
    if not checking and not p.get("parent"):
        # lanes count top-level work with a live agent or a running job; items inside a bundle share their
        # bundle's lane (release 30: orphaned bundles and items waiting for the desk hold none)
        from .ops import lane_holders

        limit = int(site.limit("max_concurrent_lanes", 2))
        holders = lane_holders(site, led, exclude=wi)
        if len(holders) >= limit:
            if not site.observe:
                led.event("lane.refused", by, wi, {"count": len(holders), "limit": limit, "lane": p["lane"], "by": by,
                                                    "holders": [{"wi": h["wi"], "lane": h["lane"], "by": h["by"]}
                                                                for h in holders]})
            held = "; ".join(f"{h['wi']} held by {', '.join(h['by'])}" for h in holders)
            fail(f"lane limit reached ({len(holders)}/{limit}): {held}. Only a live agent or a job not known to have"
                 f" ended holds a lane; an ended job is released once its state is recorded (`dureh slurm watch --once`)")
        if p["lane"] == "reorg":
            if any(h["lane"] == "reorg" for h in holders):
                fail("a reorganization lane is already active")
            reorg = [f for f in led.load("finding") if "reorg" in (f["payload"].get("tags") or [])]
            if len(reorg) > int(site.limit("reorg_finding_budget", 3)):
                fail("reorganization finding budget exceeded; report to the Director before continuing")
    rework_limit = int((org.tiers.get("worker", {}).get("bounds") or {}).get("rework_limit", 2))
    if not checking and role.is_worker and led.wi_status(wi)["rework"] >= rework_limit:
        entry = dict(site.model_for_tier("pm"))
        why += f"; escalated after {rework_limit} reworks"
    if role.is_worker and role.write_scopes and not checking and role.isolation == "worktree":
        wt = _create_worktree(site, wi, p["target"])
    else:
        wt = {"worktree": str(site.worktrees / wi) if checking and (site.worktrees / wi).is_dir() else None,
              "base": None, "note": "role writes no files"}
    text = _render_brief(site, rec, role.name, wt, entry)
    limit = _brief_limit(site, role)
    body = text.encode("utf-8")
    if len(body) > limit:
        fail(f"brief for {wi} is {len(body)} bytes, over the {limit}-byte limit; split the work item")
    digest = hashlib.sha256(body).hexdigest()[:16]
    header = f"{BRIEF_TAG} {wi} role={role.name} brief={digest}"
    led.event("dispatch.prepared", by, wi, {"role": role.name, "brief": digest, "bytes": len(body), "checking": checking,
                                            "model": entry["model"], "qualification": why,
                                            "worktree": wt["worktree"], "base": wt["base"], "by": by,
                                            **({"supersedes": supersedes} if supersedes else {})})
    return {"header": header, "brief": header + "\n\n" + text, "model": entry["model"], "worktree": wt["worktree"],
            "subagent_type": role.name}


def _superseded_dispatch(site: Site, led: Ledger, wi: str) -> dict:
    """What a new dispatch of an already dispatched WI supersedes (release 30, desk finding of 2026-10-04):
    the latest dispatch when its agent finished, or when no agent started for it within
    `dispatch_start_grace_seconds` (a spawn that never happened; a deferred start of the role still counts as
    starting). Refused while an agent runs or may still be binding."""
    from datetime import datetime, timezone

    from .ops import _deferred_start

    evs = led.events(wi)
    last = max((i for i, e in enumerate(evs) if e["type"] == "wi.dispatched"), default=None)
    if last is None:  # dispatched by status only (a repaired record): nothing to supersede
        return {}
    later = [e["type"] for e in evs[last + 1:]]
    disp = evs[last]
    if "agent.finished" in later:
        return {"dispatched": disp["id"], "agent": "ended"}
    if "agent.started" in later or _deferred_start(led, disp):
        fail(f"{wi} is dispatched and its agent has not finished; stop it before re-dispatching")
    age = int((datetime.now(timezone.utc) - datetime.fromisoformat(disp["ts"])).total_seconds())
    grace = int(site.limit("dispatch_start_grace_seconds", 120))
    if age < grace:
        fail(f"{wi} was dispatched {age} s ago and no agent has started for it yet; after {grace} s a new dispatch"
             f" supersedes it (or release it now with `dispatch stop --wi {wi} --reason ...`)")
    return {"dispatched": disp["id"], "agent": "not started", "age_seconds": age}


def _render_brief(site: Site, rec: dict, role: str, wt: dict, entry: dict,
                  extra_children: list[dict] | None = None) -> str:
    p = rec["payload"]
    org = site.org
    r = org.role(role)
    dureh = site.root / "bin" / "dureh"
    from .util import utc_now

    lines = [f"Work item {rec['id']}: {p['title']}", f"Goal: {p['goal']}", f"Target: {p['target']}",
             f"Prepared: {utc_now()} for {role}"]
    if wt.get("worktree"):
        base = f", base {wt['base'][:12]}" if wt.get("base") else ""
        lines.append(f"Work only in: {wt['worktree']} (branch {wt.get('branch') or wi_branch(site, rec['id'])}{base})")
        pending = _pending_rebase(site, rec["id"]) if rec["id"] != "WI-0000" else None
        if pending and role == p["role"]:
            lines.append(f"First: the base moved. In your worktree run `git rebase {pending[:12]}`, resolve conflicts,"
                         " and commit; then do the work against the new base.")
    scopes = [s for s in r.write_scopes if not s.startswith("site:")]
    if scopes:
        lines.append("You may write only: " + ", ".join(scopes))
    lines.append("Acceptance:")
    lines += [f"- {a}" for a in p["acceptance"]]
    if p.get("constraints"):
        lines.append("Constraints:")
        lines += [f"- {c}" for c in p["constraints"]]
    if p.get("checks"):
        lines.append("Checks the checker will run (cwd = your work root; $DUREH_TARGET_ROOT = the target's main tree): "
                     + "; ".join(p["checks"]))
    if p.get("outputs"):
        lines.append("Declared outputs (relative to your work root): " + ", ".join(p["outputs"]))
    if r.isolation == "scratch":
        lines.append(f"Work root: {target_root(site, p['target'])} (no worktree; write only under its checkpoints/ or cache/)")
    skill = _skill_for(site, role, p.get("value"))
    if skill:
        lines.append(f"Skill: read {skill}")
    if r.is_manager:
        values = site.startup / "CORE-VALUES.md"
        if values.is_file():
            lines.append("\nCORE VALUES\n" + values.read_text(encoding="utf-8").strip())
        kids = _children(site, rec["id"])
        led = Ledger(site)
        lines.append("\nBundle work items (dispatch each with the brief tool, one worker per item):")
        for k in kids:
            lines.append(f"- {k['id']} [{led.wi_status(k['id'])['status']}] {k['payload']['role']}: {k['payload']['title']}")
        for k in extra_children or []:
            lines.append(f"- {k['id']} [proposed] {k['payload']['role']}: {k['payload']['title']}")
        kids = kids + list(extra_children or [])
        if not kids:
            lines.append("- none yet; report back to the desk with the split you propose")
        lines += ["", "How to run the bundle:",
                  f"1. For each item: `{dureh} dispatch brief --wi <WI>` then spawn the named subagent with the printed brief as the whole prompt.",
                  f"2. After a worker staged its item, run `{dureh} dispatch brief --wi <WI> --role checker` and spawn `checker` with that brief.",
                  "3. Re-dispatch at most twice per item after a failed check; then stop and report.",
                  f"4. When every item is verified, run `{dureh} wi stage --wi {rec['id']} --summary ...` and return a summary of at most 30 lines."]
    elif role == "checker":
        lines += ["", f"Run `{dureh} verify --wi {rec['id']}` and return its measured output. Do not edit files."]
    elif role == "data-steward":
        lines += ["", f"Never write derivatives/ yourself. Promote a verified checkpoint with `{dureh} promote --wi {rec['id']} "
                  "--run <completed slurm run id> --source checkpoints/<step>/<path> --dataset <derivatives name>`;"
                  " update dataset_description.json with `--metadata dataset_description.json --input <file in $TMPDIR>`.",
                  f"Commit any metadata edits on the WI branch, then run `{dureh} wi stage --wi {rec['id']} --summary \"...\"`.",
                  "Return at most 30 lines: promotions and paths, commands run with results, open assumptions."]
    elif role == "researcher":
        lines += ["", f"Record each result with `{dureh} ledger add finding --wi {rec['id']} --summary ... --evidence-level ...`.",
                  "Keep sources (identity, retrieval time, passage or hash). Do not write files."]
    else:
        lines += ["", f"When done, commit on the WI branch, then run `{dureh} wi stage --wi {rec['id']} --summary \"...\"`.",
                  "Return at most 30 lines: changed paths, commands run with results, open assumptions. The checker owns validation."]
    return "\n".join(lines) + "\n"


def dispatch_stop(site: Site, actor: str, wi: str, reason: str) -> dict:
    return Ledger(site).event("wi.stopped", actor, wi, {"reason": reason})


# --------------------------------------------------------------- staging
def wi_stage(site: Site, actor: str, wi: str, summary: str) -> dict:
    led = Ledger(site)
    rec = led.get("work-item", wi)
    if actor != rec["payload"]["role"] and actor != "desk":
        fail(f"{wi} is owned by {rec['payload']['role']}, not {actor}")
    head = None
    wt = site.worktrees / wi
    if wt.is_dir():
        head = git(["rev-parse", "HEAD"], wt)
        dirty = git(["status", "--porcelain"], wt)
        if dirty:
            fail(f"{wi} worktree has uncommitted changes; commit them on its branch before staging:\n{dirty}")
    return led.event("wi.staged", actor, wi, {"summary": summary[:4000], "head": head})


# ------------------------------------------------------------- verify WI
def changed_paths(site: Site, wi: str) -> tuple[list[str], str | None]:
    wt = site.worktrees / wi
    if not wt.is_dir():
        return [], None
    base = _base_of(site, wi)
    if not base:
        fail(f"no recorded base revision for {wi}")
    head = git(["rev-parse", "HEAD"], wt)
    # -z: paths come unquoted and exact (git quotes names with spaces or special characters otherwise)
    names = {n for n in _git_z(["diff", "--name-only", "-z", f"{base}..{head}"], wt) if not CACHE_RE.search(n)}
    # untracked and ignored files are outputs too; only interpreter caches and virtualenvs are exempt
    entries = _git_z(["status", "--porcelain", "-z", "--untracked-files=all", "--ignored"], wt)
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        if entry[0] in "RC":
            i += 1  # the next field is the rename/copy source
        name = entry[3:]
        if not CACHE_RE.search(name):
            names.add(name)
    return sorted(names), head


def _git_z(args: list[str], cwd: Path) -> list[str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        fail(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[-300:]}")
    return [x for x in proc.stdout.split("\0") if x]


def stale_venv(root: Path) -> str | None:
    """The first missing interpreter named by a script shebang in <root>/.venv/bin, or None."""
    bindir = Path(root) / ".venv" / "bin"
    if not bindir.is_dir():
        return None
    for f in sorted(bindir.iterdir()):
        if f.is_symlink() or not f.is_file():
            continue
        try:
            with open(f, "rb") as fh:
                first = fh.readline(4096)
        except OSError:
            continue
        if first.startswith(b"#!"):
            interp = first[2:].strip().split(b" ")[0].decode("utf-8", "replace")
            if interp.startswith("/") and not os.path.exists(interp):
                return interp
    return None


def _env_rebuild_command(site: Site, target: str) -> str | None:
    for key in ("modules", "projects"):
        for entry in site.config.get(key) or []:
            if entry.get("path") == target and entry.get("env_rebuild"):
                return entry["env_rebuild"]
    return (site.config.get("env") or {}).get("rebuild")


def _repair_env(site: Site, root: Path, target: str) -> dict:
    stale = stale_venv(root)
    if not stale:
        return {"env": "ok"}
    cmd = _env_rebuild_command(site, target)
    if not cmd:
        return {"env": "stale", "stale_interpreter": stale,
                "hint": f"set `env_rebuild` on the {target} entry in config.json (or `env.rebuild`), for example"
                        " \"uv sync --frozen --all-groups\", then run the repair again; the Director owns config.json"}
    proc = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=3600, env=site.env())
    if proc.returncode != 0:
        fail(f"environment rebuild `{cmd}` failed in {root} (exit {proc.returncode}): "
             f"{(proc.stderr or proc.stdout).strip()[-600:]}")
    still = stale_venv(root)
    if still:
        fail(f"environment rebuild `{cmd}` ran but {root}/.venv still names {still}")
    return {"env": "rebuilt", "command": cmd, "stale_interpreter": stale}


def env_repair(site: Site, actor: str, target: str) -> dict:
    """Rebuild the virtualenv of a target's main tree (for example after the site moved)."""
    if site.org.role(actor).kind != "desk":
        fail("only the desk repairs environments")
    root = target_root(site, target)
    if root is None or not root.is_dir():
        fail(f"unknown target {target!r}")
    r = {"target": target, **_repair_env(site, root, target)}
    if r["env"] == "rebuilt":
        Ledger(site).event("env.repaired", actor, None, {k: v for k, v in r.items() if k != "hint"})
    return r


CHECK_RUNNERS = ("slurm", "local")
CHECKS_TIME_DEFAULT = "01:00:00"


def checks_runner(site: Site) -> str:
    """Where `verify --wi` runs the declared checks: config `checks.runner` (slurm|local). Without it: slurm
    when the site has a scheduler (a `cluster` profile or `slurm` settings) and `sbatch` is on the site's
    PATH, else local (tests and installs without a cluster keep working)."""
    cfg = site.config.get("checks") or {}
    runner = cfg.get("runner")
    if runner in CHECK_RUNNERS:
        return runner
    if runner:
        fail(f"config checks.runner must be one of {', '.join(CHECK_RUNNERS)}, not {runner!r}")
    scheduler = bool(site.config.get("cluster")) or any((site.config.get("slurm") or {}).values())
    return "slurm" if scheduler and shutil.which("sbatch", path=site.env().get("PATH")) else "local"


def _checks_env(site: Site, wi: str, cwd: Path, troot: Path) -> dict:
    return {**site.env(), "PYTHONDONTWRITEBYTECODE": "1", "DUREH_WORK_ROOT": str(cwd),
            "DUREH_TARGET_ROOT": str(troot), "DUREH_WI": wi}


def _run_checks_local(site: Site, wi: str, cwd: Path, troot: Path, checks: list[str], timeout: int) -> list[dict]:
    results = []
    env = _checks_env(site, wi, cwd, troot)
    for cmd in checks:
        t0 = time.monotonic()
        try:
            proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
            rc, out = proc.returncode, (proc.stdout + proc.stderr)[-2000:]
        except subprocess.TimeoutExpired:
            rc, out = "timeout", ""
        results.append({"command": cmd, "exit": rc, "seconds": round(time.monotonic() - t0, 2), "tail": out})
    return results


def _checks_script(wi: str, cwd: Path, troot: Path, site_root: Path, prefix: Path, checks: list[str]) -> str:
    """One job script: cd into the work root, export the same variables as the in-session run, then run each
    declared check through `sh -c` (as `subprocess.run(shell=True)` does), keeping its exit code, wall time
    and combined output in files next to the job log (`<prefix>.check<i>.{exit,ms,out}`)."""
    import shlex

    lines = ["#!/bin/bash", f"# DUREH verify {wi}: declared checks as one scheduler job (generated)",
             f"cd {shlex.quote(str(cwd))} || exit 96",
             f"export PYTHONDONTWRITEBYTECODE=1 DUREH_SITE={shlex.quote(str(site_root))} DUREH_WORK_ROOT={shlex.quote(str(cwd))}"
             f" DUREH_TARGET_ROOT={shlex.quote(str(troot))} DUREH_WI={shlex.quote(wi)}",
             "unset DUREH_ACTOR DUREH_AGENT_ID", "status=0"]
    for i, cmd in enumerate(checks, 1):
        out = shlex.quote(f"{prefix}.check{i}")
        lines += [f"echo '+ check {i}:' {shlex.quote(cmd)}", "t0=$(date +%s%N)",
                  f"sh -c {shlex.quote(cmd)} > {out}.out 2>&1", "rc=$?", "t1=$(date +%s%N)",
                  f'echo "$rc" > {out}.exit', f"echo $(( (t1 - t0) / 1000000 )) > {out}.ms",
                  'echo "  exit $rc"', '[ "$rc" -eq 0 ] || status=1']
    lines.append("exit $status")
    return "\n".join(lines) + "\n"


def _run_checks_slurm(site: Site, wi: str, cwd: Path, troot: Path, checks: list[str]) -> tuple[list[dict], dict]:
    """Submit the declared checks as one Slurm job (cwd = the work root, the site's environment), wait for it
    like `slurm wait`, and read the per-check results back. Returns (results, job)."""
    from . import cluster, ops
    from .util import utc_compact

    cfg = site.config.get("checks") or {}
    slurm = site.config.get("slurm") or {}
    logs = ops.wi_log_dir(site, wi)
    logs.mkdir(parents=True, exist_ok=True)
    prefix = logs / f"verify-{utc_compact()}"
    script = Path(f"{prefix}.sh")
    script.write_text(_checks_script(wi, cwd, troot, site.root, prefix, checks), encoding="utf-8")
    script.chmod(0o755)
    wall = str(cfg.get("time") or slurm.get("time") or CHECKS_TIME_DEFAULT)
    cmd = ["sbatch", "--parsable", f"--job-name=dureh-verify-{wi}", f"--time={wall}", f"--chdir={cwd}",
           "--export=ALL"]
    for key, flag in (("account", "--account"), ("partition", "--partition")):
        if slurm.get(key):
            cmd.append(f"{flag}={slurm[key]}")
    # the log options come last so a checks.sbatch_args -o/-e never moves them out of the log dir
    cmd += [str(a) for a in (cfg.get("sbatch_args") or [])] + [f"--output={prefix}.out", f"--error={prefix}.err", str(script)]
    env = _checks_env(site, wi, cwd, troot)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"{wi}: cannot submit the checks as a scheduler job ({exc}); nothing was recorded. Set config"
             " checks.runner to local or run `dureh verify --wi ... --local` on a machine without a scheduler")
    if proc.returncode != 0:
        fail(f"{wi}: sbatch failed for the checks job: {proc.stderr.strip()[-2000:]}; nothing was recorded. Set config"
             " checks.runner to local or run `dureh verify --wi ... --local` on a machine without a scheduler")
    job_id = proc.stdout.strip().split(";")[0]
    limit = cluster.parse_time(wall)
    wait = int(cfg.get("wait") or ((limit.total_seconds() if limit else 3600) + 900))
    interval = int(cfg.get("interval") or 15)
    t0 = time.monotonic()
    state, raw, final = ops.wait_job(site, job_id, wait, interval)
    if not final:
        subprocess.run(["scancel", job_id], capture_output=True, text=True, timeout=60, env=site.env())
        state = f"{state} (not finished after {wait} s; cancelled)"
    results = []
    for i, check in enumerate(checks, 1):
        base = Path(f"{prefix}.check{i}")
        exit_file, ms_file, out_file = Path(f"{base}.exit"), Path(f"{base}.ms"), Path(f"{base}.out")
        if exit_file.is_file() and exit_file.read_text().strip().lstrip("-").isdigit():
            rc = int(exit_file.read_text().strip())
            ms = ms_file.read_text().strip() if ms_file.is_file() else ""
            seconds = round(int(ms) / 1000, 2) if ms.isdigit() else None
            tail = out_file.read_text(encoding="utf-8", errors="replace")[-2000:] if out_file.is_file() else ""
        else:
            rc, seconds, tail = "not-run", None, f"no result from job {job_id} ({state})"
        results.append({"command": check, "exit": rc, "seconds": seconds, "tail": tail})
    job = {"job_id": job_id, "state": state, "raw": raw, "script": str(script), "log_out": f"{prefix}.out",
           "log_err": f"{prefix}.err", "command": cmd, "time": wall, "waited_seconds": round(time.monotonic() - t0, 2)}
    return results, job


def verify_wi(site: Site, actor: str, wi: str, timeout: int = 1800, local: bool = False) -> dict:
    led = Ledger(site)
    org = site.org
    if actor not in ("checker", "desk"):
        fail("verification is recorded by the checker")
    rec = led.get("work-item", wi)
    p = rec["payload"]
    status = led.wi_status(wi)["status"]
    if status != "staged":
        fail(f"{wi} is {status}; only staged work can be verified")
    start = time.monotonic()
    if org.role(p["role"]).is_manager:
        return _verify_bundle(site, actor, wi, start)
    paths, head = changed_paths(site, wi)
    violations = [x for x in paths if not org.scope_allows(p["role"], f"{p['target']}/{x}")]
    results = []
    wt = site.worktrees / wi
    troot = target_root(site, p["target"]) or site.root
    cwd = wt if wt.is_dir() else troot
    stale = stale_venv(cwd)
    if stale:
        fail(f"{wi}: stale virtualenv in {cwd}/.venv (its scripts name {stale}, which does not exist); checks would run"
             f" another environment. Repair first: `dureh worktree repair --wi {wi}`. Nothing was recorded.")
    missing = [o for o in p.get("outputs", []) if not (Path(cwd) / o).exists()]  # the work root only
    checks = list(p.get("checks") or [])
    runner = "local" if local else checks_runner(site)  # release 27: checks are a scheduler job, not session work
    job = None
    if checks and runner == "slurm":
        results, job = _run_checks_slurm(site, wi, cwd, troot, checks)
    elif checks:
        results = _run_checks_local(site, wi, cwd, troot, checks, timeout)
    hashes = {}
    for x in paths:
        f = Path(cwd) / x
        if f.is_file() and not f.is_symlink():
            hashes[x] = sha256_file(f)
    passed = not violations and not missing and all(r["exit"] == 0 for r in results)
    if not checks:
        note = "no declared checks; only ownership and declared outputs were verified"
    else:
        note = f"{sum(r['exit'] == 0 for r in results)}/{len(results)} declared checks passed"
        if job:
            note += f" (scheduler job {job['job_id']}: {job['state']})"
    payload = {"result": "pass" if passed else "fail", "head": head, "changed": paths, "violations": violations,
               "missing_outputs": missing, "checks": results, "artifact_sha256": hashes,
               "seconds": round(time.monotonic() - start, 2), "note": note, "runner": runner}
    if local:
        payload["runner_override"] = True
    if job:
        payload["job"] = job
    run = led.append("run", "verify", actor, wi, {"run_type": "verify", "status": payload["result"], **payload})
    led.event("verification.recorded", actor, wi, {"result": payload["result"], "run": run["id"], "head": head}, [run["id"]])
    if passed:
        led.event("wi.verified", actor, wi, {"run": run["id"], "head": head}, [run["id"]])
    else:
        led.event("rework.requested", actor, wi, {"run": run["id"], "reason": note,
                                                  "violations": violations, "missing_outputs": missing}, [run["id"]])
    return payload


def _verify_bundle(site: Site, actor: str, wi: str, start: float) -> dict:
    """A bundle passes when every child that was not stopped is verified or promoted, and there is one.

    Stopped children (superseded or dropped, each with a recorded reason) are listed but not counted."""
    led = Ledger(site)
    kids = _children(site, wi)
    states = {k["id"]: led.wi_status(k["id"])["status"] for k in kids}
    stopped = sorted(k for k, v in states.items() if v == "stopped")
    counted = {k: v for k, v in states.items() if v != "stopped"}
    open_items = {k: v for k, v in counted.items() if v not in ("verified", "promoted")}
    passed = bool(counted) and not open_items
    note = (f"{len(counted) - len(open_items)}/{len(counted)} bundle items verified or promoted" if counted
            else "bundle has no work items that were not stopped")
    if stopped:
        note += f"; not counted (stopped): {', '.join(stopped)}"
    payload = {"result": "pass" if passed else "fail", "head": None, "changed": [], "violations": [],
               "missing_outputs": [], "checks": [], "children": states, "artifact_sha256": {},
               "seconds": round(time.monotonic() - start, 2), "note": note}
    run = led.append("run", "verify", actor, wi, {"run_type": "verify", "status": payload["result"], **payload})
    led.event("verification.recorded", actor, wi, {"result": payload["result"], "run": run["id"]}, [run["id"]])
    if passed:
        led.event("wi.verified", actor, wi, {"run": run["id"], "head": None}, [run["id"]])
    else:
        led.event("rework.requested", actor, wi, {"run": run["id"], "reason": note, "open": open_items}, [run["id"]])
    return payload


# ---------------------------------------------------------------- promote
def wi_promote(site: Site, actor: str, wi: str, memo: str) -> dict:
    """Gate 2: the desk merges a verified WI branch into its target's current branch."""
    led = Ledger(site)
    if site.org.role(actor).kind != "desk":
        fail("only the desk promotes")
    rec = led.get("work-item", wi)
    if led.wi_status(wi)["status"] != "verified":
        fail(f"{wi} is not verified")
    verified = [e for e in led.events(wi) if e["type"] == "wi.verified"][-1]
    if site.org.role(rec["payload"]["role"]).is_manager:
        promoted_children = []
        for kid in _children(site, wi):
            if led.wi_status(kid["id"])["status"] == "verified":
                wi_promote(site, actor, kid["id"], f"promoted with bundle {wi}: {memo}")
                promoted_children.append(kid["id"])
        dec = led.append("decision", "gate2", actor, wi, {"subject": f"Gate 2 bundle {wi}", "disposition": "approve",
                                                            "by": "desk", "memo": memo,
                                                            "binding": {"children": promoted_children}})
        return led.event("wi.promoted", actor, wi, {"merge": None, "decision": dec["id"], "children": promoted_children,
                                                    "action_class": "compensatable",
                                                    "compensation": "revert each child merge; append wi.reverted"},
                         [dec["id"]])
    wt = site.worktrees / wi
    merge = None
    if wt.is_dir():
        head = git(["rev-parse", "HEAD"], wt)
        if head != verified["payload"].get("head") or git(["status", "--porcelain", "--untracked-files=no"], wt):
            fail(f"{wi} changed after verification; verify again before promotion")
        root = target_root(site, rec["payload"]["target"])
        if rec["payload"]["target"] in ("harness", "package"):
            return _contribute(site, led, actor, wi, rec, root, head, memo)
        if git(["status", "--porcelain", "--untracked-files=no"], root):
            fail(f"target {root} has uncommitted changes; promotion needs a clean main tree")
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], wt)
        git(["merge", "--no-ff", "-m", f"Promote {wi}: {rec['payload']['title']}", branch], root)
        merge = git(["rev-parse", "HEAD"], root)
    dec = led.append("decision", "gate2", actor, wi, {"subject": f"Gate 2 {wi}", "disposition": "approve",
                                                        "by": "desk", "memo": memo,
                                                        "binding": {"head": verified["payload"].get("head"), "merge": merge}})
    ev = led.event("wi.promoted", actor, wi, {"merge": merge, "decision": dec["id"],
                                              "action_class": "compensatable" if merge else "reversible",
                                              "compensation": f"git revert -m 1 {merge}" if merge else "append wi.reverted"},
                   [dec["id"]])
    return ev


def _contribute(site: Site, led: Ledger, actor: str, wi: str, rec: dict, root: Path, head: str, memo: str) -> dict:
    """Gate 2 for harness/package work (D2): an opaque contrib/ branch for upstream to review; nothing is merged."""
    import secrets

    branch = f"contrib/c-{secrets.token_hex(4)}"
    git(["branch", branch, head], root)
    dec = led.append("decision", "gate2", actor, wi, {"subject": f"Gate 2 {wi}", "disposition": "approve", "by": "desk",
                                                        "memo": memo, "binding": {"head": head, "contribution": branch}})
    ev = led.event("wi.promoted", actor, wi, {"merge": None, "contribution": branch, "head": head, "decision": dec["id"],
                                              "action_class": "reversible", "compensation": f"git branch -D {branch}"},
                   [dec["id"]])
    if (site.config.get("upstream") or {}).get("exchange"):
        from .upstream import notify

        notify(site, f"contribution {branch} in the {rec['payload']['target']} repository {root} at {head[:12]}: "
                     f"{rec['payload']['title']}\n\nUpstream reviews it and merges it under its own message; the site does not merge"
                     " harness or package work.")
    return ev


# ------------------------------------------------------------------ purge
def _own_branch(site: Site, led: Ledger, wi: str, root: Path, branch: str) -> bool:
    """This site's branch for wi: its own namespace, or a legacy name whose tip this site recorded for wi."""
    new, legacy = wi_branches(site, wi)
    if branch == new:
        return True
    if branch != legacy:
        return False
    tip = git(["rev-parse", branch], root, check=False)
    recorded = {e["payload"].get(k) for e in led.events(wi) for k in ("head", "base") if e["payload"].get(k)}
    return bool(tip) and tip in recorded


def _log_name(pattern: str, job: str, name: str, task: int | None = None) -> str | None:
    """An sbatch filename pattern with %j/%x (and %%; for an array task also %A and %a) filled in; None when
    another pattern is left."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c != "%":
            out.append(c)
            i += 1
            continue
        nxt = pattern[i + 1:i + 2]
        if nxt == "j":
            out.append(job)
        elif nxt == "x":
            out.append(name)
        elif nxt == "A" and task is not None:
            out.append(job)
        elif nxt == "a" and task is not None:
            out.append(str(task))
        elif nxt == "%":
            out.append("%")
        else:
            return None
        i += 2
    return "".join(out)


def _wi_job_logs(site: Site, led: Ledger, wi: str, rec: dict) -> tuple[list[Path], list[dict]]:
    """Scheduler log files of this WI's own jobs (UR-20261004-01, option A).

    Job ids come only from the WI's slurm run records. A record from release 19 on names its files
    (log_out/log_err); for an older one the candidates are `dureh-<WI>-<job>.out|.err` (the job name
    submit always set, through the `%x-%j` pattern the site's scripts used), sbatch's default
    `slurm-<job>.out`, and the script's own #SBATCH -o/-e name when it carries the job id; they are
    looked for in the recorded chdir, the WI target root, the site root and any directory the recorded
    script names with -o/-e. A file is taken only when its name holds the job id, it is a regular file
    (not a symlink) and its real directory lies inside the site tree. Returns (files, skipped)."""
    from .ops import _scan_opt, _script_opt

    from .ops import wi_log_dir

    site_real = os.path.realpath(site.root)
    # the configured slurm.log_dir and the release-31 default (items files of older --each runs)
    log_dirs = {os.path.realpath(wi_log_dir(site, wi)), os.path.realpath(site.root / "logs" / "slurm" / wi)}
    target = rec["payload"].get("target")
    troot = target_root(site, target) if target not in (None, "site", "harness", "package") else None
    found: list[Path] = []
    skipped: list[dict] = []
    for run in led.load("run"):
        if run["wi"] != wi or run["type"] != "slurm" or not run["payload"].get("job_id"):
            continue
        p = run["payload"]
        job = str(p["job_id"]).split(";")[0].strip()
        jname = _scan_opt([str(w) for w in p.get("command") or []], "-J", "--job-name") or f"dureh-{wi}"
        candidates: list[str] = []
        arr = p.get("array") or {}
        tasks: list[int | None] = list(range(int(arr.get("count") or 0))) if arr else [None]
        for f in (arr.get("items_file"), arr.get("wrapper")):  # --each: the item list and the generated wrapper
            if f and os.path.realpath(Path(f).parent) in log_dirs:
                try:
                    if stat.S_ISREG(os.lstat(f).st_mode) and Path(f) not in found:
                        found.append(Path(f))
                except OSError:
                    pass
        if p.get("log_out") or p.get("log_err"):
            candidates = [x for x in (p.get("log_out"), p.get("log_err")) if x]
        else:
            bases = [Path(b) for b in (p.get("chdir"), troot, site.root) if b]
            names = [f"dureh-{wi}-{job}.out", f"dureh-{wi}-{job}.err", f"slurm-{job}.out"]
            dirs = list(bases)
            script = Path(p["script"]) if p.get("script") else None
            if script is not None and not script.is_absolute():
                script = next((b / script for b in bases if (b / script).is_file()), None)
            if script is not None and script.is_file() and \
                    os.path.realpath(script).startswith(site_real + os.sep):
                for short, long in (("-o", "--output"), ("-e", "--error")):
                    val = _script_opt(script, short, long)
                    if not val:
                        continue
                    sub = Path(val).parent
                    dirs += [sub] if sub.is_absolute() else [b / sub for b in bases]
                    nm = _log_name(Path(val).name, job, jname)
                    if nm:
                        names.append(nm)
            candidates = [str(d / n) for d in dirs for n in dict.fromkeys(names)]
        for c, task in [(c, t) for c in dict.fromkeys(candidates) for t in tasks]:
            name = _log_name(Path(c).name, job, jname, task)
            if name is None or job not in name:
                skipped.append({"path": c, "job_id": job, "reason": "name does not resolve to one holding the job id"})
                continue
            if not Path(c).is_absolute():
                skipped.append({"path": c, "job_id": job, "reason": "relative path"})
                continue
            parent = os.path.realpath(Path(c).parent)
            if parent != site_real and not parent.startswith(site_real + os.sep):
                if p.get("log_out") or p.get("log_err"):
                    skipped.append({"path": c, "job_id": job, "reason": "outside the site tree"})
                continue
            f = Path(parent) / name
            try:
                st = os.lstat(f)
            except OSError:
                continue  # missing: nothing to remove (an array task's %j log is not resolvable: no task job id)
            if stat.S_ISREG(st.st_mode) and f not in found:
                found.append(f)
    unique = []
    for s in skipped:
        if s not in unique:
            unique.append(s)
    return found, unique


def purge(site: Site, actor: str, wi: str, dry_run: bool = False) -> dict:
    """Remove a WI's worktree, scratch checkpoints and leftover module exec trees (scratch/exec/<WI>-*), and the
    scheduler logs of its own jobs.
    Validate the whole set first; symlinks are removed as links and their targets are never followed."""
    if site.org.role(actor).kind != "desk":
        fail("only the desk purges")
    led = Ledger(site)
    rec = led.get("work-item", wi)
    status = led.wi_status(wi)["status"]
    if status not in ("promoted", "stopped", "reverted"):
        fail(f"{wi} is {status}; purge only promoted, stopped, or reverted work")
    items: list[Path] = []
    wt = site.worktrees / wi
    if wt.exists() or wt.is_symlink():
        items.append(wt)
    tgt = rec["payload"]["target"]
    if tgt.startswith("projects/"):
        ck = site.root / tgt / "checkpoints"
        if ck.is_dir():
            items += [c for c in ck.iterdir() if fnmatch.fnmatch(c.name, f"{wi}*")]
    from .exectree import exec_root, purge_targets, _rmtree

    exec_trees = purge_targets(site, wi)  # leftover pinned module trees of its jobs (UR-20261004-04)
    items += exec_trees
    for it in items:  # validate everything before removing anything
        if it in exec_trees:
            if it.parent != exec_root(site):
                fail(f"refusing to purge outside the exec scratch directory: {it}")
            continue
        if not (str(it).startswith(str(site.worktrees) + os.sep) or "/checkpoints/" in str(it)):
            fail(f"refusing to purge outside WI scratch paths: {it}")
    logs, skipped_logs = _wi_job_logs(site, led, wi, rec)
    removed, removed_logs = [], []
    if not dry_run:
        for f in logs:
            f.unlink()
            removed_logs.append(str(f))
        for it in items:
            if it in exec_trees:
                _rmtree(it)  # read-only trees: chmod u+w first
            elif it.is_symlink() or it.is_file():
                it.unlink()
            elif it == wt:
                root = target_root(site, tgt)
                if root is not None and (root / ".git").exists():
                    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], wt, check=False)
                    git(["worktree", "remove", "--force", str(wt)], root)
                    if status != "promoted" and branch and _own_branch(site, led, wi, root, branch):
                        git(["branch", "-D", branch], root, check=False)
                if wt.exists():
                    shutil.rmtree(wt)
            else:
                shutil.rmtree(it)
            removed.append(str(it))
        led.event("purged", actor, wi, {"removed": removed, "removed_logs": removed_logs,
                                         **({"skipped_logs": skipped_logs} if skipped_logs else {})})
    out = {"wi": wi, "planned": [str(i) for i in items], "planned_logs": [str(f) for f in logs],
           "removed": removed, "removed_logs": removed_logs, "dry_run": dry_run}
    if skipped_logs:
        out["skipped_logs"] = skipped_logs
    return out
