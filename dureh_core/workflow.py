"""Work-item lifecycle tools: wi, dispatch, verify (per WI), promote, purge."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path

from .paths import target_root
from .records import ACTIVE, Ledger
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
            wt = {"worktree": str(site.worktrees / rec["id"]), "base": "0" * 40}
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
        err = worktree_problem(wt, wi)
        if err:
            fail(f"worktree {wt} is broken ({err}); repair it with `dureh worktree repair --wi {wi}`")
        base = git(["rev-parse", f"wi/{wi}"], root, check=False) or None
        return {"worktree": str(wt), "base": _base_of(site, wi) or base, "note": "reused existing worktree"}
    site.worktrees.mkdir(exist_ok=True)
    base = git(["rev-parse", "HEAD"], root)
    exists = git(["branch", "--list", f"wi/{wi}"], root)
    if exists:
        git(["worktree", "add", str(wt), f"wi/{wi}"], root)
    else:
        git(["worktree", "add", "-b", f"wi/{wi}", str(wt), base], root)
    return {"worktree": str(wt), "base": base, "note": "new worktree"}


def worktree_problem(wt: Path, wi: str) -> str | None:
    """None when wt is a usable worktree on branch wi/<wi>; otherwise what is wrong."""
    if not Path(wt).is_dir():
        return f"{wt} does not exist"
    proc = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt, capture_output=True, text=True)
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout).strip()[-300:] or "not a git worktree"
    if proc.stdout.strip() != f"wi/{wi}":
        return f"checked out {proc.stdout.strip()}, not wi/{wi}"
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
        state = "pruned"
    elif worktree_problem(wt, wi) is None:
        return {"wi": wi, "worktree": str(wt), "state": "ok"}
    else:
        proc = subprocess.run(["git", "worktree", "repair", str(wt)], cwd=root, capture_output=True, text=True)
        err = worktree_problem(wt, wi)
        if err:
            fail(f"repair of {wt} failed: {err} {(proc.stderr or '').strip()[-300:]}")
        state = "repaired"
    led.event("worktree.repaired", actor, wi, {"worktree": str(wt), "target": p["target"], "state": state})
    return {"wi": wi, "worktree": str(wt), "state": state}


def _base_of(site: Site, wi: str) -> str | None:
    for ev in reversed(Ledger(site).events(wi)):
        if ev["type"] == "dispatch.prepared" and ev["payload"].get("base"):
            return ev["payload"]["base"]
    return None


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
    if checking:
        if st != "staged":
            fail(f"{wi} is {st}; the checker is dispatched only for staged work")
    elif st == "dispatched":
        evs = led.events(wi)
        last = max((i for i, e in enumerate(evs) if e["type"] == "wi.dispatched"), default=None)
        finished = last is not None and any(e["type"] == "agent.finished" for e in evs[last + 1:])
        if not finished:
            fail(f"{wi} is dispatched and its agent has not finished; stop it before re-dispatching")
    elif st not in ("approved", "rework"):
        fail(f"{wi} is {st}; only approved or rework items can be dispatched")
    entry = site.model_for_tier("writer-academic" if p.get("academic") and not checking else role.tier)
    ok, why = _qualified(entry)
    if not ok:
        fail(f"tier {role.tier} model {entry.get('model')} cannot be dispatched: {why}")
    if not checking and not p.get("parent"):
        # lanes count top-level work; items inside a bundle share their bundle's lane
        active = [w for w in led.work_items() if w["status"] in ACTIVE
                  and not led.get("work-item", w["id"])["payload"].get("parent") and w["id"] != wi]
        if len(active) >= int(site.limit("max_concurrent_lanes", 2)):
            fail(f"lane limit reached: {', '.join(w['id'] for w in active)} are active")
        if p["lane"] == "reorg":
            if any(w["lane"] == "reorg" for w in active):
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
                                            "worktree": wt["worktree"], "base": wt["base"], "by": by})
    return {"header": header, "brief": header + "\n\n" + text, "model": entry["model"], "worktree": wt["worktree"],
            "subagent_type": role.name}


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
        lines.append(f"Work only in: {wt['worktree']} (branch wi/{rec['id']}{base})")
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
            fail(f"{wi} worktree has uncommitted changes; commit them on wi/{wi} before staging:\n{dirty}")
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
    names = set(filter(None, git(["diff", "--name-only", f"{base}..{head}"], wt).splitlines()))
    # untracked and ignored files are outputs too; only interpreter caches are exempt
    for line in git(["status", "--porcelain", "--untracked-files=all", "--ignored"], wt).splitlines():
        if line:
            name = line[3:].strip()
            if not CACHE_RE.search(name):
                names.add(name)
    return sorted(names), head


def verify_wi(site: Site, actor: str, wi: str, timeout: int = 1800) -> dict:
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
    missing = [o for o in p.get("outputs", []) if not (Path(cwd) / o).exists()]  # the work root only
    for cmd in p.get("checks") or []:
        t0 = time.monotonic()
        try:
            env = {**site.env(), "PYTHONDONTWRITEBYTECODE": "1", "DUREH_WORK_ROOT": str(cwd),
                   "DUREH_TARGET_ROOT": str(troot), "DUREH_WI": wi}
            proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
            rc, out = proc.returncode, (proc.stdout + proc.stderr)[-2000:]
        except subprocess.TimeoutExpired:
            rc, out = "timeout", ""
        results.append({"command": cmd, "exit": rc, "seconds": round(time.monotonic() - t0, 2), "tail": out})
    hashes = {}
    for x in paths:
        f = Path(cwd) / x
        if f.is_file() and not f.is_symlink():
            hashes[x] = sha256_file(f)
    passed = not violations and not missing and all(r["exit"] == 0 for r in results)
    if not p.get("checks"):
        note = "no declared checks; only ownership and declared outputs were verified"
    else:
        note = f"{sum(r['exit'] == 0 for r in results)}/{len(results)} declared checks passed"
    payload = {"result": "pass" if passed else "fail", "head": head, "changed": paths, "violations": violations,
               "missing_outputs": missing, "checks": results, "artifact_sha256": hashes,
               "seconds": round(time.monotonic() - start, 2), "note": note}
    run = led.append("run", "verify", actor, wi, {"run_type": "verify", "status": payload["result"], **payload})
    led.event("verification.recorded", actor, wi, {"result": payload["result"], "run": run["id"], "head": head}, [run["id"]])
    if passed:
        led.event("wi.verified", actor, wi, {"run": run["id"], "head": head}, [run["id"]])
    else:
        led.event("rework.requested", actor, wi, {"run": run["id"], "reason": note,
                                                  "violations": violations, "missing_outputs": missing}, [run["id"]])
    return payload


def _verify_bundle(site: Site, actor: str, wi: str, start: float) -> dict:
    """A bundle passes when it has children and every child is verified or promoted."""
    led = Ledger(site)
    kids = _children(site, wi)
    states = {k["id"]: led.wi_status(k["id"])["status"] for k in kids}
    open_items = {k: v for k, v in states.items() if v not in ("verified", "promoted")}
    passed = bool(kids) and not open_items
    note = (f"{len(kids) - len(open_items)}/{len(kids)} bundle items verified or promoted" if kids
            else "bundle has no work items")
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
        if git(["status", "--porcelain", "--untracked-files=no"], root):
            fail(f"target {root} has uncommitted changes; promotion needs a clean main tree")
        git(["merge", "--no-ff", "-m", f"Promote {wi}: {rec['payload']['title']}", f"wi/{wi}"], root)
        merge = git(["rev-parse", "HEAD"], root)
    dec = led.append("decision", "gate2", actor, wi, {"subject": f"Gate 2 {wi}", "disposition": "approve",
                                                        "by": "desk", "memo": memo,
                                                        "binding": {"head": verified["payload"].get("head"), "merge": merge}})
    ev = led.event("wi.promoted", actor, wi, {"merge": merge, "decision": dec["id"],
                                              "action_class": "compensatable" if merge else "reversible",
                                              "compensation": f"git revert -m 1 {merge}" if merge else "append wi.reverted"},
                   [dec["id"]])
    return ev


# ------------------------------------------------------------------ purge
def purge(site: Site, actor: str, wi: str, dry_run: bool = False) -> dict:
    """Remove a WI's worktree and scratch checkpoints. Validate the whole set first;
    symlinks are removed as links and their targets are never followed."""
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
    for it in items:  # validate everything before removing anything
        if not (str(it).startswith(str(site.worktrees) + os.sep) or "/checkpoints/" in str(it)):
            fail(f"refusing to purge outside WI scratch paths: {it}")
    removed = []
    if not dry_run:
        for it in items:
            if it.is_symlink() or it.is_file():
                it.unlink()
            elif it == wt:
                root = target_root(site, tgt)
                if root is not None and (root / ".git").exists():
                    git(["worktree", "remove", "--force", str(wt)], root)
                    if status != "promoted":
                        git(["branch", "-D", f"wi/{wi}"], root, check=False)
                if wt.exists():
                    shutil.rmtree(wt)
            else:
                shutil.rmtree(it)
            removed.append(str(it))
        led.event("purged", actor, wi, {"removed": removed})
    return {"wi": wi, "planned": [str(i) for i in items], "removed": removed, "dry_run": dry_run}
