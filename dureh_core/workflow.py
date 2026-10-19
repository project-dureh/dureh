"""Work-item lifecycle tools: wi, dispatch, verify (per WI), promote, purge."""
from __future__ import annotations

import fnmatch
import hashlib
import json
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
from .util import DurehError, fail, git, is_within, sha256_file, times_word

BRIEF_TAG = "DUREH-BRIEF"
# interpreter caches, the virtualenv and the `.venv.stale-<time>` copies a rebuild (or the rename step) moved aside
CACHE_RE = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.venv(\.stale-[^/]*)?)(/|$)"
                      r"|\.py[co]$")


# --------------------------------------------------------------------- wi
def wi_new(site: Site, actor: str, title: str, goal: str, role: str, target: str, acceptance: list[str],
           lane: str = "work", bundle: str | None = None, value: str | None = None, checks: list[str] | None = None,
           outputs: list[str] | None = None, constraints: list[str] | None = None, parent: str | None = None,
           source: str | None = None, academic: bool = False, fix_for: str | None = None,
           checks_runner_choice: str | None = None, requirements: str | None = None,
           label: str | None = None) -> dict:
    """Create a work item. `fix_for` names the WI whose problem this one fixes (recorded as `fix_for`): after the
    day's end time a fix for a day plan item goes on under the finish-plan rule (workday.py).
    `checks_runner_choice` (`--checks-runner local`, release 45) runs this item's checks in the verify process;
    every declared check must match config `checks.local_allow`. `requirements` (`--requirements <file>`, release
    45) pins a requirements file inside the site by its sha256; the brief carries one line naming it. `label`
    (`--label`, UR-20261008-02) is the short title shown with the WI id to the Director instead of the cut title."""
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
    if fix_for:
        led.get("work-item", fix_for)
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
    if fix_for:
        payload["fix_for"] = fix_for
    if outputs:
        payload["outputs"] = list(outputs)
    if checks_runner_choice:
        payload["checks_runner"] = _wi_runner_choice(site, checks_runner_choice, payload["checks"])
    if requirements:
        payload["requirements"] = requirements_pin(site, requirements)
    if label is not None:
        from .records import TITLE_CHARS

        label = " ".join(label.split())
        if not label or len(label) > TITLE_CHARS:
            fail(f"--label must be 1-{TITLE_CHARS} characters: {label!r}")
        payload["label"] = label
    from .members import output_problem

    why = output_problem(site, r, payload)
    if why:  # release 65 (UR-20261015-03): refused here, not at `member run`'s preflight after approval
        fail(f"{role} runs on the local runtime and {why}: declare them with `--output <path>` (exact files inside"
             f" its write scopes: {', '.join(r.write_scopes)})")
    draft = {"id": "WI-0000", "payload": payload}
    n, limit = brief_bytes(site, draft), _brief_limit(site, r)
    if n > limit:
        fail(f"brief would be {n} bytes, over the {limit}-byte limit; shorten goal, acceptance, checks or constraints, "
             "or split the work item")
    _checker_brief_check(site, draft, "brief")
    if prec and org.role(prec["payload"]["role"]).is_manager:
        prole = org.role(prec["payload"]["role"])
        n, limit = brief_bytes(site, prec, extra_children=[draft]), _brief_limit(site, prole)
        if n > limit:
            fail(f"bundle {parent} brief would be {n} bytes, over the {limit}-byte limit with this item; "
                 "shorten the item titles or split the bundle")
    return led.new_work_item(actor, payload)


REQUIREMENTS_BYTES_DEFAULT = 65536


def requirements_pin(site: Site, raw: str) -> dict:
    """`--requirements <file>` (release 45, 6g): a file inside the site (relative paths are taken from the site
    root), at most `limits.requirements_bytes` (default 65536), recorded as {path (site-relative), sha256, bytes}.
    Only the brief's one line naming it counts toward the brief limit; the file itself does not."""
    p = Path(raw).expanduser()
    p = (p if p.is_absolute() else site.root / p).resolve()
    if not is_within(p, site.root.resolve()):
        fail(f"--requirements must name a file inside the site ({site.root}): {raw}")
    if not p.is_file():
        fail(f"--requirements: no such file: {p}")
    size = p.stat().st_size
    limit = int(site.limit("requirements_bytes", REQUIREMENTS_BYTES_DEFAULT))
    if size > limit:
        fail(f"--requirements: {p} is {size} bytes, over the {limit}-byte limit (limits.requirements_bytes)")
    return {"path": p.relative_to(site.root.resolve()).as_posix(), "sha256": sha256_file(p), "bytes": size}


def requirements_problem(site: Site, wi: str, payload: dict) -> str | None:
    """Why a WI's pinned requirements file no longer matches its record (missing or a different sha256), or None."""
    req = payload.get("requirements")
    if not req:
        return None
    p = site.root / req["path"]
    now = sha256_file(p) if p.is_file() else None
    if now == req["sha256"]:
        return None
    what = "is missing" if now is None else f"changed (sha256 {now[:12]}, recorded {req['sha256'][:12]})"
    return (f"{wi}: the requirements file {p} {what} since it was pinned; nothing was recorded. Restore it, or pin the"
            f" new content with `dureh wi amend --wi {wi} --memo \"<Director's words>\" --requirements {req['path']}`")


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


def checker_roles(site: Site) -> list:
    """The roles a checker brief is rendered for: every non-desk role with the verify capability that some role may
    dispatch (release 70)."""
    org = site.org
    return [r for r in org.with_capability("verify") if r.kind != "desk" and org.dispatcher_of(r.name)]


def checker_brief_bytes(site: Site, rec: dict, checker: str) -> int:
    """Size of the budgeted part of the brief `dispatch brief --role <checker>` would render for this record once it
    is staged (release 70, UR-20261016-01): the WI worktree line when the owner works in one, and the longest of the
    checker's run instructions (submit, collect, or resubmit after a stale run). The staging summary rides outside
    the budget (staging_summary_block), so nothing a worker writes at `wi stage` can push it over."""
    owner = site.org.role(rec["payload"]["role"])
    wt = {"worktree": None, "base": None}
    if owner.is_worker and owner.write_scopes and owner.isolation == "worktree":
        root = target_root(site, rec["payload"]["target"])
        if root is not None and (root / ".git").exists():
            wt = {"worktree": str(site.worktrees / rec["id"]), "base": None}  # the checker brief names the site branch
    return len(_render_brief(site, rec, checker, wt, {}, estimate=True).encode("utf-8"))


def _checker_brief_check(site: Site, draft: dict, what: str) -> None:
    """Refuse an item whose checker brief could never be prepared (release 70, UR-20261016-01): before, only the
    owner's brief was measured, and an item that staged fine was refused at `dispatch brief --role checker`."""
    for c in checker_roles(site):
        n, limit = checker_brief_bytes(site, draft, c.name), _brief_limit(site, c)
        if n > limit:
            fail(f"{what} fits, but the {c.name} brief would be {n} bytes, over the {limit}-byte limit, so the item"
                 " could not be verified once staged; shorten goal, acceptance, checks or constraints, or split the"
                 " work item")


def wi_approve(site: Site, actor: str, wi: str, memo: str, by: str = "Director") -> dict:
    """Gate 1. The desk records the Director's approval as a decision, then the event."""
    led = Ledger(site)
    rec = led.get("work-item", wi)
    _local_output_check(site, wi, rec["payload"])
    dec = led.append("decision", "gate1", actor, wi, {"subject": f"Gate 1 {wi}", "disposition": "approve",
                                                        "by": by, "memo": memo})
    return led.event("wi.approved", actor, wi, {"decision": dec["id"], "by": by}, [dec["id"]])


def _local_output_check(site: Site, wi: str, payload: dict) -> None:
    """Release 65 (UR-20261015-03): refuse approving an item of a `local` role that declares no output inside the
    role's write scopes (the reason `member run` gives)."""
    from .members import output_problem

    role = site.org.role(payload["role"])
    why = output_problem(site, role, payload)
    if why:
        fail(f"{wi}: {role.name} runs on the local runtime and {why}: declare them first with `dureh wi amend --wi {wi}"
             f" --memo ... --output <path>` (exact files inside its write scopes: {', '.join(role.write_scopes)})")


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


def worktree_repair(site: Site, actor: str, wi: str, dry_run: bool = False, envs: bool = True,
                    force: bool = False) -> dict:
    """Re-link a WI worktree after the site moved, keeping branch and commits; prune it if its directory is gone.

    `dry_run` reports the state the repair would reach without running git or the rebuild and without a record;
    `envs=False` leaves the worktree's `.venv` alone; `force` rebuilds the `.venv` even when it is not stale
    (the three are `worktree repair --all` options; `--wi` keeps the defaults)."""
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
        if not dry_run:
            git(["worktree", "prune"], root)
        state, env = "pruned", {"env": "ok"}
    else:
        if worktree_problem(wt, wi, site) is None:
            state = "ok"
        elif dry_run:
            state = "repaired"
        else:
            proc = subprocess.run(["git", "worktree", "repair", str(wt)], cwd=root, capture_output=True, text=True)
            err = worktree_problem(wt, wi, site)
            if err:
                fail(f"repair of {wt} failed: {err} {(proc.stderr or '').strip()[-300:]}")
            state = "repaired"
        env = _repair_env(site, wt, p["target"], force=force, dry_run=dry_run) if envs else {"env": "skipped"}
    if not dry_run and (state != "ok" or env["env"] == "rebuilt"):
        led.event("worktree.repaired", actor, wi, {"worktree": str(wt), "target": p["target"], "state": state,
                                                   **{k: v for k, v in env.items() if k != "hint"}})
    return {"wi": wi, "worktree": str(wt), "state": state, **env}


WI_ID_RE = re.compile(r"^WI-\d{4,}$")
EXEC_TREE_RE = re.compile(r"^(WI-\d{4,})-(.+)-[0-9a-f]{7}-\d{8}T\d{12}Z$")
REPAIR_STATES = ("repaired", "pruned", "rebuilt", "unchanged", "skipped", "error")


def _site_repos(site: Site) -> list[tuple[str, Path]]:
    """(target, root) for every modules/<m> and projects/<p> directory of the site (git repositories or not)."""
    out = []
    for kind in ("modules", "projects"):
        d = site.root / kind
        if d.is_dir():
            out += [(f"{kind}/{p.name}", p) for p in sorted(d.iterdir()) if p.is_dir() and not p.name.startswith(".")]
    return out


def _registered_worktrees(root: Path) -> list[Path]:
    """The linked worktrees a repository registers (`git worktree list`), at the paths it recorded (after a move:
    the old ones), whether or not those directories still exist."""
    proc = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    paths = [Path(ln[len("worktree "):]) for ln in proc.stdout.splitlines() if ln.startswith("worktree ")]
    return [p for p in paths[1:]]  # the first entry is the main tree


def _exec_venvs(site: Site) -> list[tuple[Path, Path, str | None]]:
    """(venv, tree, module) for every exec-tree sibling environment `<tree>.venv` / `<tree>.venv-<task>`."""
    from .exectree import exec_root

    root = exec_root(site)
    if not root.is_dir():
        return []
    out = []
    for venv in sorted(root.iterdir()):
        if ".venv" not in venv.name or ".stale-" in venv.name or not venv.is_dir():
            continue  # a `.stale-<time>` copy is a rebuild's leftover (see _repair_env), not an environment
        tree = root / venv.name.split(".venv", 1)[0]
        m = EXEC_TREE_RE.match(tree.name)
        out.append((venv, tree, m.group(2) if m else None))
    return out


def _env_state(r: dict) -> dict:
    """A repair-all row's state from a _repair_env result."""
    lock = {"stale_lock": r["stale_lock"]} if r.get("stale_lock") else {}
    if r["env"] == "rebuilt":
        return {"state": "rebuilt", "command": r.get("command"), "stale": r.get("stale_interpreter"), **lock,
                **({"detail": "created (no .venv)"} if r.get("missing") else {})}
    if r["env"] == "stale":
        return {"state": "skipped", "reason": "no env_rebuild", "stale": r.get("stale_interpreter"), **lock}
    return {"state": "unchanged"}


def worktree_repair_all(site: Site, actor: str, dry_run: bool = False, envs: bool = True,
                        envs_only: bool = False, force: bool = False) -> dict:
    """`worktree repair --all`: every WI worktree of the site and every virtualenv it owns, after a move or rename
    (proposals/site-rename-independence.md rows 13-14, step 3).

    Worktrees: the directories under `worktrees/` and the entries every modules/* and projects/* repository
    registers (a registered path whose directory is gone is pruned); each is handled as `--wi` handles one. A
    `worktrees/<WI>` directory its checkout no longer registers is skipped (`not registered: <path>`, release 59),
    in the env pass too, and never deleted. Then a
    plain `git worktree repair` in each repository with linked worktrees, for worktrees that live elsewhere.
    Environments (`envs`): the main tree of every module and project, the WI worktrees' (through the worktree
    pass; in the env pass with `envs_only`) and the exec-tree siblings `<tree>.venv`; only stale ones are rebuilt
    unless `force`, and a worktree with no `.venv` at all gets one created; no `env_rebuild` for the target is
    "skipped", not an error. The pass never stops at an error:
    each item ends repaired, pruned, rebuilt, unchanged, skipped or error. Records: `worktree.repaired` and
    `env.repaired` per changed item as the single forms write them, plus one `worktree.repair-all` with the
    counts; `dry_run` writes nothing and runs neither git nor a rebuild."""
    from .ops import agent_state
    from .util import DurehError

    if site.org.role(actor).kind != "desk":
        fail("only the desk repairs worktrees")
    led = Ledger(site)
    targets = {rec["id"]: rec["payload"].get("target") for rec in led.load("work-item")}
    repos = _site_repos(site)
    git_repos = [(t, r) for t, r in repos if (r / ".git").exists()]
    rows: list[dict] = []
    # release 59: the WI worktrees each checkout registers; a `worktrees/<WI>` directory its checkout no longer
    # lists (a deregistered leftover) is skipped, never repaired, pruned or deleted
    registered: dict[str, set[str]] = {}
    for target, root in git_repos:
        registered[target] = {p.name for p in _registered_worktrees(root)
                              if p.parent.name == "worktrees" and WI_ID_RE.match(p.name)}

    def unregistered(wi: str) -> bool:
        return (site.worktrees / wi).is_dir() and targets.get(wi) in registered and wi not in registered[targets[wi]]

    if not envs_only:
        found: dict[str, str | None] = {}
        for target, names in registered.items():
            for name in names:
                found.setdefault(name, target)
        if site.worktrees.is_dir():
            for d in sorted(site.worktrees.iterdir()):
                if d.is_dir():
                    found.setdefault(d.name, None)
        for wi in sorted(found):
            row = {"kind": "worktree", "name": wi, "target": targets.get(wi) or found[wi],
                   "path": str(site.worktrees / wi)}
            rows.append(row)
            if wi not in targets:
                row.update(state="error", error="no work-item record")
                continue
            if unregistered(wi):
                row.update(state="skipped", reason=f"not registered: {site.worktrees / wi}")
                continue
            try:
                if led.wi_status(wi)["status"] == "dispatched" and agent_state(led, wi) == "running":
                    row.update(state="skipped", reason="agent running")
                    continue
                r = worktree_repair(site, actor, wi, dry_run=dry_run, envs=envs, force=force)
            except DurehError as exc:
                row.update(state="error", error=str(exc))
                continue
            row.update(state="unchanged" if r["state"] == "ok" else r["state"])
            if envs and r["state"] != "pruned":
                row["env"] = {"ok": "unchanged", "stale": "skipped", "rebuilt": "rebuilt"}[r["env"]]
                if r["env"] == "stale":
                    row["env_reason"] = "no env_rebuild"
                elif r.get("missing"):
                    row["env_reason"] = "created (no .venv)"
                if r.get("stale_interpreter"):
                    row["stale"] = r["stale_interpreter"]
                if r.get("stale_lock"):
                    row["stale_lock"] = r["stale_lock"]
        if not dry_run:
            for target, root in git_repos:
                if not _registered_worktrees(root):
                    continue
                proc = subprocess.run(["git", "worktree", "repair"], cwd=root, capture_output=True, text=True)
                fixed = [ln for ln in (proc.stderr or "").splitlines() if ln.startswith("repair:")]
                if proc.returncode != 0:
                    rows.append({"kind": "repo", "name": target, "target": target, "state": "error",
                                 "error": (proc.stderr or proc.stdout).strip()[-300:]})
                elif fixed:
                    rows.append({"kind": "repo", "name": target, "target": target, "state": "repaired",
                                 "detail": "; ".join(fixed)})
    if envs:
        venvs: list[tuple[str, str | None, Path, Path]] = []  # name, target, cwd (where env_rebuild runs), venv
        for target, root in repos:
            if (root / ".venv").is_dir():
                venvs.append((target, target, root, root / ".venv"))
        if envs_only and site.worktrees.is_dir():
            for d in sorted(site.worktrees.iterdir()):
                if d.is_dir():  # with or without a .venv: a missing one is created
                    venvs.append((f"worktrees/{d.name}", targets.get(d.name), d, d / ".venv"))
        for venv, tree, module in _exec_venvs(site):
            venvs.append((str(venv.relative_to(site.root)), f"modules/{module}" if module else None, tree, venv))
        for name, target, cwd, venv in venvs:
            row = {"kind": "env", "name": name, "target": target, "path": str(venv)}
            rows.append(row)
            if target is None:
                row.update(state="skipped", reason="no work-item record" if name.startswith("worktrees/")
                           else "unrecognised exec tree name")
                continue
            if name.startswith("worktrees/") and unregistered(name[len("worktrees/"):]):
                row.update(state="skipped", reason=f"not registered: {cwd}")
                continue
            if not cwd.is_dir():
                row.update(state="skipped", reason="its tree is gone")
                continue
            try:
                r = _repair_env(site, cwd, target, venv=venv, force=force, dry_run=dry_run)
            except DurehError as exc:
                row.update(state="error", error=str(exc))
                continue
            row.update(_env_state(r))
            if row["state"] == "rebuilt" and not dry_run:
                led.event("env.repaired", actor, None, {"target": target, "venv": str(venv),
                                                        **{k: v for k, v in r.items() if k != "hint"}})
    worktrees = {k: 0 for k in REPAIR_STATES if k != "rebuilt"}
    envs_c = {k: 0 for k in REPAIR_STATES if k not in ("repaired", "pruned")}
    for row in rows:
        if row["kind"] in ("worktree", "repo"):
            worktrees[row["state"]] += 1
            if row.get("env") in envs_c:
                envs_c[row["env"]] += 1
        else:
            envs_c[row["state"]] += 1
    summary = {"dry_run": dry_run, "envs": envs, "envs_only": envs_only, "force": force, "items": len(rows),
               "worktrees": worktrees, "venvs": envs_c}
    if not dry_run:
        led.event("worktree.repair-all", actor, None, summary)
    return {"items": rows, "summary": summary}


def _dir_size(path: Path) -> int:
    """Bytes in the files under `path` (symlinks counted by their own size, nothing followed)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for n in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_size
            except OSError:
                pass
    return total


def prune_stale_venvs(site: Site, actor: str, target: str | None = None, remove: bool = False) -> dict:
    """`worktree repair --prune-stale`: the `.venv.stale-<time>` copies that a rebuild (_repair_env) or the site
    rename step moved aside, under every `worktrees/*` directory, the main tree of every module and project and
    the exec root (`<tree>.venv.stale-*`): the roots `--all` visits; `target` restricts it to that main tree.
    Listed with their size; removed only with `remove`. A plain `.venv` is never touched. Each removed copy is
    recorded as `env.repaired` with `env: pruned`, as the rebuild records its own row."""
    from .exectree import exec_root

    if site.org.role(actor).kind != "desk":
        fail("only the desk prunes environments")
    led = Ledger(site)
    targets = {rec["id"]: rec["payload"].get("target") for rec in led.load("work-item")}
    roots: list[tuple[str, str | None, Path, str]] = []  # name, target, directory, glob
    if target:
        root = target_root(site, target)
        if root is None or not root.is_dir():
            fail(f"unknown target {target!r}")
        roots.append((target, target, root, ".venv.stale-*"))
    else:
        roots += [(t, t, r, ".venv.stale-*") for t, r in _site_repos(site)]
        if site.worktrees.is_dir():
            roots += [(f"worktrees/{d.name}", targets.get(d.name), d, ".venv.stale-*")
                      for d in sorted(site.worktrees.iterdir()) if d.is_dir()]
        if exec_root(site).is_dir():
            roots.append(("scratch/exec", None, exec_root(site), "*.venv.stale-*"))
    rows: list[dict] = []
    for name, tgt, d, pattern in roots:
        for p in sorted(d.glob(pattern)):
            if p.is_symlink() or not p.is_dir():
                continue
            row = {"kind": "stale-venv", "name": str(p.relative_to(site.root)), "target": tgt, "path": str(p),
                   "size": _dir_size(p), "state": "listed"}
            rows.append(row)
            if not remove:
                continue
            try:
                shutil.rmtree(p)
            except OSError as exc:
                row.update(state="error", error=str(exc)[-300:])
                continue
            row["state"] = "removed"
            wi = d.name if name.startswith("worktrees/") and d.name in targets else None
            led.event("env.repaired", actor, wi, {"target": tgt, "venv": str(p), "env": "pruned", "size": row["size"]})
    summary = {"remove": remove, "items": len(rows), "size": sum(r["size"] for r in rows),
               "removed": sum(r["state"] == "removed" for r in rows),
               "errors": sum(r["state"] == "error" for r in rows)}
    return {"items": rows, "summary": summary}


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


AMENDABLE = ("acceptance", "checks", "constraints", "outputs", "checks_runner", "requirements")


def wi_amend(site: Site, actor: str, wi: str, memo: str, fields: dict, rebase: str | None = None,
             add_constraints: list[str] | None = None, reopen: bool = False) -> dict:
    """Change a WI's acceptance, checks, constraints or outputs, or move its base, without a replacement WI.

    The goal, role and target never change (that is a new WI). The amendment is approved like Gate 1:
    --memo carries the Director's words (or, in workday mode, the plan approval that covers it).
    `add_constraints` (`--add-constraint`, release 37, UR-20261005-02) appends to the constraints (after any
    `--constraint` replacement); it alone is allowed on a staged item, since it only adds context for a rework.
    `reopen` (`--reopen`, release 45) re-opens a verified item that is not promoted: the amendment records
    `reopened_from: verified`, a `wi.reopened` event voids the verification and moves the item back to approved
    for a new dispatch, with no `dispatch stop`. Fields are optional with it. A promoted item never changes."""
    from .ops import agent_state

    if site.org.role(actor).kind != "desk":
        fail("only the desk amends work items")
    if not memo.strip():
        fail("--memo must carry the Director's words (or the day plan approval that covers the amendment)")
    led = Ledger(site)
    rec = led.get("work-item", wi)
    st = led.wi_status(wi)["status"]
    adds = [c for c in (add_constraints or []) if c is not None]
    if any(not c.strip() for c in adds):
        fail("--add-constraint needs text")
    if reopen and st != "verified":
        fail(f"{wi} is {st}; --reopen is for a verified item that is not promoted"
             + (" (amend this item without it)" if st in ("approved", "rework", "dispatched", "proposed", "stopped") else ""))
    if st == "verified" and not reopen:
        fail(f"{wi} is verified; to change it before promotion re-open it: `dureh wi amend --wi {wi} --reopen --memo"
             " \"<Director's words>\" [--add-constraint ...] [--check ...]` (the verification is void and the item goes"
             " back to approved for a new dispatch)")
    parent = rec["payload"].get("parent")
    if reopen and parent and led.wi_status(parent)["status"] in ("verified", "promoted"):
        fail(f"{wi} belongs to bundle {parent}, which is {led.wi_status(parent)['status']}; re-open the bundle first")
    only_adds = bool(adds) and not any(v is not None for v in fields.values()) and rebase is None
    # release 45: the checks runner alone may change on a staged item (it changes where checks run, not the work)
    only_runner = not adds and rebase is None and [k for k, v in fields.items() if v is not None] in (["checks_runner"],
                                                                                                       ["requirements"])
    pend = pending_verify(led, wi) if st == "staged" and only_runner else None
    # release 61 (UR-20261013-02): a pending job whose head moved is stale; the next `verify --submit` supersedes it
    if pend and not _stale_verify(site, wi, pend):
        fail(f"{wi} has a submitted verify job; collect it before changing its checks runner or requirements")
    if st == "staged" and not (only_adds or only_runner):
        fail(f"{wi} is staged; only --add-constraint (which appends) is allowed on a staged item. To change"
             f" acceptance, checks, constraints, outputs or the base: `dureh dispatch stop --wi {wi} --reason ...`, then"
             f" `dureh wi amend --wi {wi} --memo ...` with the change (the amendment re-opens the stopped item)")
    if st not in ("approved", "rework", "dispatched", "proposed", "stopped", "staged", "verified"):
        fail(f"{wi} is {st}; amend only proposed, approved, rework, stopped, staged (--add-constraint only),"
             " verified-not-promoted (--reopen) or dispatched-and-ended items")
    if st == "dispatched" and agent_state(led, wi) in ("running", "starting"):
        fail(f"an agent is running for {wi}; stop it at a safe point before amending")
    bad = [k for k in fields if k not in AMENDABLE]
    if bad:
        fail(f"cannot amend {', '.join(bad)}; a different goal, role or target is a new work item")
    fields = {k: (v if k in ("checks_runner", "requirements") else list(v)) for k, v in fields.items() if v is not None}
    if "requirements" in fields:
        fields["requirements"] = requirements_pin(site, fields["requirements"])
    if adds:
        fields["constraints"] = list(fields.get("constraints", rec["payload"].get("constraints") or [])) + adds
    if "acceptance" in fields and not fields["acceptance"]:
        fail("a work item needs at least one acceptance criterion")
    if "checks_runner" in fields or ("checks" in fields and rec["payload"].get("checks_runner") == "local"):
        choice = fields.pop("checks_runner", None) or rec["payload"].get("checks_runner")
        fields["checks_runner"] = _wi_runner_choice(site, choice, fields.get("checks", rec["payload"].get("checks") or []))
    rebase_onto = None
    if rebase is not None:
        root = target_root(site, rec["payload"]["target"])
        if root is None or not (root / ".git").exists():
            fail(f"{wi} targets {rec['payload']['target']}, which is not a git repository")
        rebase_onto = git(["rev-parse", rebase or "HEAD"], root)
    if not fields and not rebase_onto and not reopen:
        fail("nothing to amend: give --acceptance, --check, --constraint, --add-constraint, --output, --checks-runner,"
             " --requirements or --rebase")
    draft = {"id": wi, "payload": {**rec["payload"], **fields}}
    role = site.org.role(rec["payload"]["role"])
    n, limit = brief_bytes(site, draft), _brief_limit(site, role)
    if n > limit:
        fail(f"amended brief would be {n} bytes, over the {limit}-byte limit")
    _checker_brief_check(site, draft, "amended brief")
    if st == "stopped":  # it is re-approved below: refuse before anything is recorded
        _local_output_check(site, wi, draft["payload"])
    before = {k: rec["payload"].get(k) for k in fields}
    ev = led.event("wi.amended", actor, wi, {"fields": fields, "before": before, "rebase_onto": rebase_onto,
                                              "memo": memo, "by": "Director",
                                              **({"added_constraints": adds, "status": st} if adds else {}),
                                              **({"reopened_from": "verified"} if reopen else {})})
    if st == "stopped":  # an amended stopped item is re-opened with the same approval words
        wi_approve(site, actor, wi, f"amended ({ev['id']}): {memo}")
    if reopen:
        verified = [e for e in led.events(wi) if e["type"] == "wi.verified"]
        led.event("wi.reopened", actor, wi, {"amendment": ev["id"], "memo": memo, "by": "Director",
                                             "voids": verified[-1]["payload"].get("run") if verified else None},
                  [ev["id"]])
    return ev


def _skill_for(site: Site, role: str, value: str | None) -> str | None:
    """The role's own skill (package `roles.<r>.skill`), else the package's `skills` map (role -> skill), else its value's skill."""
    r = site.org.get(role)
    skills = (site.package_manifest().get("skills") or {})
    rel = (r.skill if r else None) or skills.get(role)
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
    checking = bool(as_role) and org.has(as_role, "verify")
    if as_role and not checking and as_role != p["role"]:
        fail(f"{wi} is owned by {p['role']}; only a role with the verify capability can be dispatched for another"
             " role's item")
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
    problem = requirements_problem(site, wi, p)
    if problem:
        fail(problem.replace("nothing was recorded", "no brief was prepared"))
    supersedes = None
    if checking:
        if st != "staged":
            fail(f"{wi} is {st}; the checker is dispatched only for staged work")
    elif st == "dispatched":
        supersedes = _superseded_dispatch(site, led, wi)
    elif st not in ("approved", "rework"):
        fail(f"{wi} is {st}; only approved or rework items can be dispatched")
    # release 54 (D3): routing picks a tier by the work unit's kind; the academic and rework escalations below
    # keep precedence; a kind routed to null, or no `models.routing` at all, keeps the role's own tier
    kind, kind_reason = dispatch_kind_reason(site, p, role, checking)
    tier = site.routing().get(kind) or role.tier
    entry = site.model_for_tier("writer-academic" if p.get("academic") and not checking else tier)
    ok, why = _qualified(entry)
    if not ok:
        fail(f"tier {tier} model {entry.get('model')} cannot be dispatched: {why}")
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
        if wt.get("worktree"):
            wt["env"] = _dispatch_worktree_env(site, led, wi, by, Path(wt["worktree"]), p["target"])
    else:
        wt = {"worktree": str(site.worktrees / wi) if checking and (site.worktrees / wi).is_dir() else None,
              "base": None, "note": "role writes no files"}
    text = _render_brief(site, rec, role.name, wt, entry)
    limit = _brief_limit(site, role)
    body = text.encode("utf-8")
    if len(body) > limit:  # the budget covers the desk-written part only
        fail(f"brief for {wi} is {len(body)} bytes, over the {limit}-byte limit; split the work item")
    # release 37 (UR-20261005-02): a rework brief carries the last failed verification, outside the budget
    block = "" if checking or led.wi_status(wi)["rework"] <= 0 else last_verification_block(led, wi)
    # release 70 (UR-20261016-01): a checker brief carries the staging summary the same way, with its own cap
    staging = staging_summary_block(led, wi) if checking else ""
    block += staging
    block += _env_brief_line(wt)  # release 47: outside the budget, as the verification block
    full = text + block
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()[:16]
    header = f"{BRIEF_TAG} {wi} role={role.name} brief={digest}"
    led.event("dispatch.prepared", by, wi, {"role": role.name, "brief": digest, "bytes": len(body), "checking": checking,
                                            "model": entry["model"], "kind": kind, "kind_reason": kind_reason,
                                            "qualification": why,
                                            "worktree": wt["worktree"], "base": wt["base"], "by": by,
                                            **({"worktree_env": wt["env"]} if wt.get("env") else {}),
                                            **({"verification_bytes": len(block.encode("utf-8"))} if block else {}),
                                            **({"staging_bytes": len(staging.encode("utf-8"))} if staging else {}),
                                            **({"supersedes": supersedes} if supersedes else {})})
    if role.is_member:  # release 64: the member job reads the brief's text (checked against its digest)
        from .members import save_brief

        save_brief(site, wi, digest, header + "\n\n" + full)
    return {"header": header, "brief": header + "\n\n" + full, "model": entry["model"], "kind": kind,
            "worktree": wt["worktree"], "subagent_type": role.name, "runtime": role.runtime,
            **({"worktree_env": wt["env"]} if wt.get("env") else {})}


def dispatch_kind(site: Site, payload: dict, role, checking: bool) -> str:
    """The kind of a work unit (release 54, D3): `check` for a checker dispatch, `doc` when the role is in
    `models.routing_doc_roles` (default doc-writer), `triage` when the WI lane is `desk`, else `build`."""
    return dispatch_kind_reason(site, payload, role, checking)[0]


KIND_REASONS = ("check", "role in routing_doc_roles", "lane desk", "default")  # release 59 (D4): why a kind was chosen


def dispatch_kind_reason(site: Site, payload: dict, role, checking: bool) -> tuple[str, str]:
    """(kind, reason) of a work unit; the reason (release 59, D4 follow-up) rides on `dispatch.prepared` and the
    spawn events as `kind_reason`: `check`, `role in routing_doc_roles`, `lane desk` or `default`."""
    if checking:
        return "check", "check"
    if role.name in site.routing_doc_roles():
        return "doc", "role in routing_doc_roles"
    if payload.get("lane") == "desk":
        return "triage", "lane desk"
    return "build", "default"


def _dispatch_worktree_env(site: Site, led: Ledger, wi: str, by: str, wt: Path, target: str) -> dict | None:
    """Release 47 (UR-20261009-01): a WI worktree of a target with `env_rebuild` gets its Python environment
    before the brief returns (provision_worktree_env). A worktree that already has a `.venv` keeps it (a stale one
    is the desk's `worktree repair`, as before). A failed build refuses the brief and is recorded as
    `worktree.env-failed`."""
    from .util import DurehError

    if not _env_target(target) or not _env_rebuild_command(site, target):
        return None
    venv = wt / ".venv"
    if venv.exists():
        mark = shared_env_of(venv)
        return {"env": "existing", "env_mode": "shared-venv" if mark is not None else "own", "venv": str(venv),
                **({"shared": mark.get("venv")} if mark else {})}
    try:
        return provision_worktree_env(site, wt, target)
    except DurehError as exc:
        if not site.observe:
            led.event("worktree.env-failed", by, wi, {"worktree": str(wt), "target": target, "error": str(exc)[-1500:],
                                                      "command": _env_rebuild_command(site, target)})
        fail(f"{wi}: the worktree has no Python environment: {exc}. No brief was prepared. Fix the cause (config"
             f" env_rebuild, env.rebuild_timeout, network or cache), then run `dureh worktree repair --wi {wi}` and"
             " dispatch again")


def _env_brief_line(wt: dict) -> str:
    """One line telling the worker which Python environment its worktree has (release 47)."""
    env = wt.get("env") or {}
    venv = env.get("venv")
    if not venv:
        return ""
    if env.get("env_mode") == "shared-venv":
        return (f"\nPython env: {venv} shares the target main tree's environment ({env.get('shared')}, same uv.lock),"
                " your package directory first. Use `.venv/bin/python`, `source .venv/bin/activate` or `uv run --no-sync`;"
                " a plain `uv run`, `uv sync` or `uv add` here installs the full lock into this worktree's own .venv"
                " (network and time), never into the shared one.\n")
    return f"\nPython env: {venv} (this worktree's own; built with env_rebuild). Use `.venv/bin/python` or `uv run`.\n"


VERIFICATION_BLOCK_BYTES = 4096
VERIFICATION_TAIL_LINES = 20


STAGING_BLOCK_BYTES = 1024  # release 70: the checker brief's staging summary, outside the brief budget


def staging_summary_block(led: Ledger, wi: str) -> str:
    """The "Staged" block of a checker brief (release 70, UR-20261016-01): the latest `wi.staged` record's head, who
    staged it and its summary, at most STAGING_BLOCK_BYTES (the summary is cut first). Outside the brief budget, as
    the rework brief's last failed verification; empty when nothing is staged."""
    staged = [e for e in led.events(wi) if e["type"] == "wi.staged"]
    if not staged:
        return ""
    ev = staged[-1]
    p = ev["payload"]
    head = (f"\nStaged ({ev['id']}, {ev['ts'][:19]}Z, head {str(p.get('head') or '-')[:12]}, by {ev.get('actor')}); the"
            " summary is the stager's own account, not evidence; verify measures the head:\n")
    tail = "\n"
    text = " ".join(str(p.get("summary") or "").split()) or "-"
    room = STAGING_BLOCK_BYTES - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    raw = text.encode("utf-8")
    if len(raw) > room:
        cut = " ... (truncated)"
        text = raw[:max(0, room - len(cut.encode("utf-8")))].decode("utf-8", "ignore") + cut
    return head + text + tail


def last_verification_block(led: Ledger, wi: str) -> str:
    """The "Last verification" block of a rework brief: from the latest failed verify run of the WI, the failed
    check ids with exit codes, their last output lines (at most 20 each) and log paths, ownership violations and
    missing outputs. At most VERIFICATION_BLOCK_BYTES; empty when no failed verification is recorded."""
    runs = [r for r in led.load("run") if r["wi"] == wi and r["payload"].get("run_type") == "verify"
            and r["payload"].get("status") == "fail"]
    if not runs:
        return ""
    run = runs[-1]
    p = run["payload"]
    job = p.get("job") or {}
    here = lambda x: str(led.site.relocate(x)) if x else x  # noqa: E731 - log paths recorded before a site move

    def render(tail_lines: int) -> str:
        lines = ["", f"Last verification (run {run['id']}, {run['ts'][:19]}Z, head {str(p.get('head') or '-')[:12]}):"
                     f" FAIL, {p.get('note', '')}"]
        for i, c in enumerate(p.get("checks") or [], 1):
            if c.get("exit") == 0:
                continue
            secs = f", {c['seconds']} s" if c.get("seconds") is not None else ""
            lines.append(f"- check {i} `{c['command']}`: exit {c['exit']}{secs}"
                         + (f"; log {here(c['log'])}" if c.get("log") else ""))
            tail = [ln for ln in (c.get("tail") or "").rstrip("\n").splitlines()][-tail_lines:] if tail_lines else []
            lines += [f"    {ln}" for ln in tail]
        if p.get("violations"):
            lines.append("- outside the role's write scope: " + ", ".join(p["violations"]))
        if p.get("missing_outputs"):
            lines.append("- declared outputs missing: " + ", ".join(p["missing_outputs"]))
        if job.get("log_out"):
            lines.append(f"- job {job.get('job_id')} ({job.get('state')}) logs: {here(job['log_out'])},"
                         f" {here(job.get('log_err'))}")
        lines.append("Fix these before staging again; the same head fails the same way.")
        return "\n".join(lines) + "\n"

    for n in (VERIFICATION_TAIL_LINES, 10, 5, 2, 0):
        out = render(n)
        if len(out.encode("utf-8")) <= VERIFICATION_BLOCK_BYTES:
            return out
    cut = "\n... (truncated)\n"
    return out.encode("utf-8")[:VERIFICATION_BLOCK_BYTES - len(cut)].decode("utf-8", "ignore") + cut


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
    if disp["payload"].get("member_run"):  # release 64: the harness job runs it; its end is recorded, not timed out
        fail(f"{wi} is dispatched to member run {disp['payload']['member_run']} ({disp['payload'].get('runtime')}),"
             f" which has not finished; see `dureh member status --wi {wi}`, collect it when it ends (`member collect"
             f" --wi {wi}`), or release it with `dispatch stop --wi {wi} --reason ...`")
    if "agent.started" in later or _deferred_start(led, disp):
        fail(f"{wi} is dispatched and its agent has not finished; stop it before re-dispatching")
    age = int((datetime.now(timezone.utc) - datetime.fromisoformat(disp["ts"])).total_seconds())
    grace = int(site.limit("dispatch_start_grace_seconds", 120))
    if age < grace:
        fail(f"{wi} was dispatched {age} s ago and no agent has started for it yet; after {grace} s a new dispatch"
             f" supersedes it (or release it now with `dispatch stop --wi {wi} --reason ...`)")
    return {"dispatched": disp["id"], "agent": "not started", "age_seconds": age}


def _render_brief(site: Site, rec: dict, role: str, wt: dict, entry: dict,
                  extra_children: list[dict] | None = None, estimate: bool = False) -> str:
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
    if p.get("requirements"):
        req = p["requirements"]
        lines.append(f"Requirements (read first; part of the acceptance): {site.root / req['path']}"
                     f" (sha256 {req['sha256'][:12]}, {req['bytes']} bytes; do not edit it)")
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
        own = [x.split("/", 2)[2] for x in r.write_scopes if x.count("/") >= 2 and not x.startswith("site:")]
        lines.append(f"Work root: {target_root(site, p['target'])} (no worktree; write only {', '.join(own) or 'nothing'}"
                     " there)")
    skill = _skill_for(site, role, p.get("value"))
    if skill:
        lines.append(f"Skill: read {skill}")
    if r.is_member:  # release 64: run by the harness (`member run`); as a Claude subagent only after a recorded fallback
        lines.append(f"Runtime: {r.runtime} (the harness runs this brief with `dureh member run`; a Claude subagent runs it"
                     " only as the recorded fallback)")
        if r.runtime == "local":
            from .members import write_allowlist

            files = write_allowlist(site, r, p)
            lines.append("Write allowlist (exact files, relative to your work root): " + (", ".join(files) or
                         "none declared (declare them with `wi amend --output`; the local runtime refuses without them)"))
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
                  f"3. Re-dispatch at most {times_word(int(site.limit('max_redispatch', 2)))} per item after a failed check;"
                  " then stop and report.",
                  f"4. When every item is verified, run `{dureh} wi stage --wi {rec['id']} --summary ...` and return a summary"
                  f" of at most {int(site.limit('manager_report_lines', 30))} lines."]
    elif r.has("verify"):
        lines += [""] + _checker_lines(site, rec, dureh, estimate)
    elif r.has("network") and not scopes:
        lines += ["", f"Record each result with `{dureh} ledger add finding --wi {rec['id']} --summary ... --evidence-level ...`.",
                  "Keep sources (identity, retrieval time, passage or hash). Do not write files."]
    else:
        lines += ["", f"When done, commit on the WI branch, then run `{dureh} wi stage --wi {rec['id']} --summary \"...\"`.",
                  f"Return at most {int(site.limit('worker_report_lines', 30))} lines: changed paths, commands run with"
                  " results, open assumptions. The checker owns validation."]
    return "\n".join(lines) + "\n"


def _checker_lines(site: Site, rec: dict, dureh: Path, estimate: bool = False) -> list[str]:
    """The checker's run instructions (release 37): with the slurm runner and declared checks, submit the checks
    job and end the turn, then collect on re-dispatch; never run verify in the background. `estimate` (release 70)
    returns the longest variant, with placeholder run, job and head ids, for the size check at `wi new`/`wi amend`."""
    wi = rec["id"]
    never = "Never run verify in the background. Do not edit files."
    try:
        slurm = bool(rec["payload"].get("checks")) and not site.org.role(rec["payload"]["role"]).is_manager \
            and wi_checks_runner(site, rec["payload"])[0] == "slurm"
    except Exception:  # an invalid checks.runner fails at verify time with its own message
        slurm = False
    if not slurm:
        return [f"Run `{dureh} verify --wi {wi}` in the foreground and return its measured output. {never}"]
    if estimate:  # the stale variant is the longest; ids as long as real ones (run-<22>-<8 hex>, a 10-digit job)
        pend, stale = {"id": "run-" + "0" * 22 + "-" + "0" * 8, "payload": {"job_id": "9" * 10, "head": "0" * 40}}, True
    else:
        pend = pending_verify(Ledger(site), wi) if wi != "WI-0000" else None
        try:
            stale = bool(pend) and _stale_verify(site, wi, pend)
        except DurehError:
            stale = False
    if pend and stale:  # release 61 (UR-20261013-02): the item moved since the job was submitted; --collect would refuse
        return [f"Verify run {pend['id']} (job {pend['payload']['job_id']}) was submitted at head"
                f" {str(pend['payload'].get('head'))[:12]}, but {wi} has moved since, so its results do not describe the"
                f" current head and `--collect` would refuse it. Run `{dureh} verify --wi {wi} --submit` (it supersedes the"
                f" stale run), report the run id it prints and end your turn; the desk re-dispatches you when the job"
                f" ends, then run `{dureh} verify --wi {wi} --collect` and return its measured output. {never}"]
    if pend:
        return [f"Verify run {pend['id']} (job {pend['payload']['job_id']}) was submitted: run `{dureh} verify --wi {wi}"
                f" --collect` and return its measured output. If it says the job is still running, report that and end"
                f" your turn. {never}"]
    return [f"Run `{dureh} verify --wi {wi} --submit`, report the run id it prints and end your turn; the desk"
            f" re-dispatches you when the job ends, then run `{dureh} verify --wi {wi} --collect` and return its"
            f" measured output. {never}"]


def dispatch_stop(site: Site, actor: str, wi: str, reason: str) -> dict:
    """Record the stop. A member run of the item still queued or running (release 64) is the harness's own job:
    it is cancelled and the stop names it."""
    from .members import cancel_live_runs

    led = Ledger(site)
    cancelled = cancel_live_runs(site, led, wi)
    return led.event("wi.stopped", actor, wi, {"reason": reason, **({"member_runs_cancelled": cancelled}
                                                                     if cancelled else {})})


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


def tracked_changes(site: Site, wi: str) -> tuple[list[str], str | None, str | None]:
    """(paths, head, base): the files the item's commits changed, `git diff --name-only <base>..<head>` only (release
    65, UR-20261015-05). Untracked and ignored files (for example build output the declared checks create) are never
    listed. A reviewer's evidence is built from this, captured before the checks run."""
    wt = site.worktrees / wi
    if not wt.is_dir():
        return [], None, None
    base = _base_of(site, wi)
    if not base:
        fail(f"no recorded base revision for {wi}")
    head = git(["rev-parse", "HEAD"], wt)
    return sorted(set(_git_z(["diff", "--name-only", "-z", f"{base}..{head}"], wt))), head, base


def _git_z(args: list[str], cwd: Path) -> list[str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        fail(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[-300:]}")
    return [x for x in proc.stdout.split("\0") if x]


def stale_venv(root: Path) -> str | None:
    """The first path <root>/.venv names that is not this environment (see venv_stale), or None."""
    return venv_stale(Path(root) / ".venv")


_ACTIVATE_RE = re.compile(r"""^\s*(?:export\s+)?VIRTUAL_ENV=(['"]?)(/[^'"\s]*)\1\s*$""")


def venv_stale(venv: Path) -> str | None:
    """The first path the environment `venv` names that is not the environment where it now is, or None.

    Checked: the shebang of every script in bin/ (an interpreter that does not exist), `bin/activate`'s
    VIRTUAL_ENV when it is a literal path (must be this directory: it also catches a copy whose old location still
    exists), the `home` of pyvenv.cfg (the base interpreter's directory must exist) and a dangling bin/python
    link. A site move leaves all of them at the old location; `worktree repair` rebuilds such an environment with
    the target's env_rebuild."""
    venv = Path(venv)
    bindir = venv / "bin"
    if not bindir.is_dir():
        return None
    own = {os.path.normpath(venv), os.path.realpath(venv)}
    mark = shared_env_of(venv)
    if mark is not None:  # release 47: the shared form is stale when the environment it layers on is
        main, root = mark.get("venv"), mark.get("root")
        if not main or not os.path.isdir(os.path.join(main, "bin")):
            return main or str(venv / SHARED_MARK)
        if root and os.path.normpath(root) not in {os.path.normpath(venv.parent), os.path.realpath(venv.parent)}:
            return root
        inner = venv_stale(Path(main))
        if inner:
            return inner

    for f in sorted(bindir.iterdir()):
        if f.is_symlink() or not f.is_file():
            continue
        try:
            with open(f, "rb") as fh:
                first = fh.readline(4096)
                rest = fh.read(65536) if f.name == "activate" else b""
        except OSError:
            continue
        if first.startswith(b"#!"):
            interp = first[2:].strip().split(b" ")[0].decode("utf-8", "replace")
            if interp.startswith("/") and not os.path.exists(interp):
                return interp
        if f.name == "activate":
            for line in (first + rest).decode("utf-8", "replace").splitlines():
                m = _ACTIVATE_RE.match(line)
                if m and os.path.normpath(m.group(2)) not in own:
                    return m.group(2)
    cfg = venv / "pyvenv.cfg"
    if cfg.is_file():
        for line in cfg.read_text(errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "home" and value.strip().startswith("/") and not os.path.isdir(value.strip()):
                return value.strip()
    py = bindir / "python"
    if py.is_symlink() and not py.exists():
        return os.path.join(bindir, os.readlink(py))
    return None


SHARED_MARK = "DUREH-SHARED"  # in a worktree `.venv` made by share_worktree_env: the environment it layers on
SHARED_PTH = "_dureh_shared.pth"
ENV_REBUILD_TIMEOUT_DEFAULT = 900


def shared_env_of(venv: Path) -> dict | None:
    """The shared-form marker of a worktree `.venv` (release 47), or None for an ordinary environment."""
    mark = Path(venv) / SHARED_MARK
    if not mark.is_file():
        return None
    try:
        data = json.loads(mark.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def env_rebuild_timeout(site: Site) -> int:
    """Seconds an `env_rebuild` run by `dispatch brief` or `worktree repair` may take (config
    `env.rebuild_timeout`, release 47)."""
    try:
        return max(1, int((site.config.get("env") or {}).get("rebuild_timeout") or ENV_REBUILD_TIMEOUT_DEFAULT))
    except (TypeError, ValueError):
        fail("config env.rebuild_timeout must be a number of seconds")


def _uv_free_env(site: Site) -> dict:
    """The site environment without an active virtualenv or uv project environment of the calling process, so a
    command run for one tree never writes into another tree's environment."""
    env = site.env()
    for k in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONHOME", "UV_NO_SYNC"):
        env.pop(k, None)
    return env


def share_worktree_env(site: Site, wt: Path, troot: Path) -> dict:
    """Give a WI worktree the target main tree's environment in a form its own commands pick up (release 47,
    UR-20261009-01): `<wt>/.venv` becomes a small virtualenv of the same base interpreter (`python -m venv
    --without-pip`, no network) whose site-packages carries one `.pth`: the worktree's package directory first,
    then the main tree's site-packages (with its own `.pth` files, so editable installs of other modules resolve).
    `.venv/bin/python`, an activated `.venv` and `uv run` (which finds the project `.venv`) all import the
    worktree's code with the main tree's dependencies. Nothing in it points uv at the main environment: a `uv
    sync`/`uv add` in the worktree installs into this `.venv` (the worktree's own), never into the shared one,
    whose packages it only reads. A `DUREH-SHARED` marker names the shared environment; venv_stale follows it."""
    from .exectree import package_dir

    main = Path(troot) / ".venv"
    venv = Path(wt) / ".venv"
    py = main / "bin" / "python"
    env = _uv_free_env(site)
    env.pop("PYTHONPATH", None)
    system = False
    cfg = main / "pyvenv.cfg"
    if cfg.is_file():
        for line in cfg.read_text(errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "include-system-site-packages":
                system = value.strip().lower() == "true"

    def run(args: list[str]) -> str:
        proc = subprocess.run(args, capture_output=True, text=True, env=env, timeout=300, cwd=wt)
        if proc.returncode != 0:
            raise OSError(f"`{' '.join(args)}` exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-400:]}")
        return proc.stdout.strip()

    purelib = "import sysconfig; print(sysconfig.get_path('purelib'))"
    try:
        main_sp = run([str(py), "-I", "-c", purelib])
        base = run([str(py), "-I", "-c", "import sys; print(getattr(sys, '_base_executable', '') or sys.executable)"])
        run([base, "-m", "venv", "--without-pip", *(["--system-site-packages"] if system else []), str(venv)])
        own_sp = Path(run([str(venv / "bin" / "python"), "-I", "-c", purelib]))
        own_sp.mkdir(parents=True, exist_ok=True)
        (own_sp / SHARED_PTH).write_text(f"{package_dir(Path(wt))}\nimport site; site.addsitedir({main_sp!r})\n",
                                         encoding="utf-8")
        lock = Path(wt) / "uv.lock"
        (venv / SHARED_MARK).write_text(json.dumps({
            "venv": str(main), "root": str(wt), "site_packages": main_sp,
            "lock_sha256": sha256_file(lock) if lock.is_file() else None}, indent=1) + "\n", encoding="utf-8")
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(venv, ignore_errors=True)
        fail(f"sharing {main} with {wt} failed: {exc}")
    return {"env": "shared", "env_mode": "shared-venv", "venv": str(venv), "shared": str(main)}


def provision_worktree_env(site: Site, wt: Path, target: str) -> dict:
    """The Python environment of a WI worktree that has no `.venv`, for a target with `env_rebuild` (release 47):
    the shared form (share_worktree_env) when the worktree's dependencies are the main tree's (same rule as
    checks_env_plan `shared-venv`) and the main tree's `.venv` exists and is not stale; else the worktree's own
    `.venv` built with `env_rebuild`, bounded by `env.rebuild_timeout`. A failed build leaves no `.venv` (a
    partial one is moved to `.venv.stale-<time>`, which `worktree repair --prune-stale` removes) and fails."""
    wt = Path(wt)
    cmd = _env_rebuild_command(site, target)
    if not cmd or not _env_target(target):
        return {"env": "none"}
    venv = wt / ".venv"
    troot = target_root(site, target)
    main_env = None
    if troot is not None and (troot / ".venv").is_dir() and not stale_venv(troot) and _same_deps(wt, troot):
        main_env = _main_env_lock_state(site, troot, target)  # release 51: a lock-stale main env is not shared
        if main_env is None:
            return share_worktree_env(site, wt, troot)
    timeout = env_rebuild_timeout(site)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True, timeout=timeout,
                              env=_uv_free_env(site))
        err = None if proc.returncode == 0 else f"exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-600:]}"
    except subprocess.TimeoutExpired:
        err = f"timed out after {timeout} s (config env.rebuild_timeout)"
    except OSError as exc:
        err = str(exc)
    if err is None and not venv.is_dir():
        err = f"it did not create {venv}"
    if err is None and venv_stale(venv):
        err = f"{venv} names {venv_stale(venv)}"
    if err:
        kept = ""
        if venv.exists() or venv.is_symlink():
            aside = venv.with_name(f".venv.stale-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-partial")
            os.rename(venv, aside)
            kept = f"; the partial environment was moved to {aside}"
        fail(f"environment build `{cmd}` in {wt} failed ({err}){kept}")
    return {"env": "rebuilt", "env_mode": "rebuild", "venv": str(venv), "command": cmd,
            "seconds": round(time.monotonic() - t0, 1), **({"main_env": main_env} if main_env else {})}


def _env_rebuild_command(site: Site, target: str) -> str | None:
    for key in ("modules", "projects"):
        for entry in site.config.get(key) or []:
            if entry.get("path") == target and entry.get("env_rebuild"):
                return entry["env_rebuild"]
    return (site.config.get("env") or {}).get("rebuild")


LOCK_CHECK_TIMEOUT = 120
_UV_SYNC_RE = re.compile(r"^\s*(?:cd\s+(?:'[^']*'|\"[^\"]*\"|\S+)\s*&&\s*)?(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
                         r"uv\s+sync\b(?![^\n]*(?:&&|\|\||[;|]))")


def lock_check_command(rebuild: str | None) -> str | None:
    """`<env_rebuild> --check` (`uv sync --check` exits non-zero when the environment does not match the lock)
    when env_rebuild is one `uv sync ...` invocation, optionally after a `cd X &&` and VAR=value prefixes; None
    for anything else (another tool, or a chain the flag could not be appended to)."""
    if not rebuild or not _UV_SYNC_RE.match(rebuild):
        return None
    return f"{rebuild.strip()} --check"


def run_lock_check(cmd: str, root: Path, env: dict, timeout: int = LOCK_CHECK_TIMEOUT) -> tuple[int, str]:
    """Run the lock check command in `root` (tests replace this): (exit code, combined output)."""
    proc = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True, env=env, timeout=timeout)
    return proc.returncode, (proc.stderr or "") + (proc.stdout or "")


def lock_stale(site: Site, root: Path, target: str, venv: Path | None = None) -> str | None:
    """Release 51 (UR-20261011-01): why the environment of `root` (default <root>/.venv) does not match the
    target's lock, or None when it does or the check is unavailable (the target's env_rebuild is not a `uv sync`;
    see lock_check_command). An environment can be valid for venv_stale (its interpreter is where it says) and
    still lack packages or extras the lock declares; `uv sync --check` is the installed-vs-lock question, run in
    `root` without the caller's uv environment and without UV_NO_SYNC, bounded by LOCK_CHECK_TIMEOUT. The reason
    is the exit code and the last lines of output. One call per environment per repair or plan."""
    cmd = lock_check_command(_env_rebuild_command(site, target))
    if cmd is None:
        return None
    env = _uv_free_env(site)
    if venv is not None and Path(venv) != Path(root) / ".venv":
        env.update(VIRTUAL_ENV=str(venv), UV_PROJECT_ENVIRONMENT=str(venv))
    try:
        rc, out = run_lock_check(cmd, Path(root), env, LOCK_CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"`{cmd}` timed out after {LOCK_CHECK_TIMEOUT} s"
    except OSError as exc:
        return f"`{cmd}` could not run: {exc}"
    if rc == 0:
        return None
    tail = " / ".join(ln.strip() for ln in out.strip().splitlines()[-3:] if ln.strip())
    return f"`{cmd}` exit {rc}" + (f": {tail}" if tail else "")


def _repair_env(site: Site, root: Path, target: str, venv: Path | None = None, force: bool = False,
                dry_run: bool = False, share: bool = True) -> dict:
    """Rebuild `venv` (default <root>/.venv) with the target's env_rebuild, run in `root`, when it is stale (or
    `force`), and create it when it is missing. An existing environment is moved aside first
    (`<venv>.stale-<time>`): a sync into the old directory keeps the installed scripts and their old shebangs, so
    the rebuild must start from nothing; the copy (and any earlier failed attempt's) is removed once the rebuild
    succeeds and kept, named in the error, when it fails. An
    environment elsewhere than <root>/.venv (an exec tree's sibling) is named to the command through VIRTUAL_ENV
    and UV_PROJECT_ENVIRONMENT, as the job wrapper does. `dry_run` only reports.

    Release 47: for a WI worktree's `.venv` (with `share`), a missing environment or a shared-form one that is
    stale (or forced) is provisioned again as `dispatch brief` does (provision_worktree_env: the shared form when
    the dependencies still match, else `env_rebuild`); an own environment is rebuilt as before.

    Release 51 (UR-20261011-01): an environment is also stale when it does not match the lock (lock_stale: `uv
    sync --check`), so one lacking an extra the lock declares is rebuilt alone, without `--force --all`. The
    result records `stale_interpreter`, `stale_lock` and `lock_check` (ok, stale, unavailable for a non-uv
    env_rebuild, shared for a shared-form worktree environment)."""
    venv = Path(venv) if venv is not None else Path(root) / ".venv"
    stale = venv_stale(venv)
    missing = not venv.exists()
    cmd = _env_rebuild_command(site, target)
    # release 51: the lock-vs-installed check, once per environment, only when it decides anything (the
    # interpreter check passed, not forced) and the environment is its own (the shared form carries no packages:
    # its main tree's own row or `--target` answers for it)
    lock_check: str | None = None
    lock = None
    if force or stale or missing:
        pass
    elif shared_env_of(venv) is None:
        lock_check = "unavailable" if lock_check_command(cmd) is None else "ok"
        if lock_check == "ok":
            lock = lock_stale(site, root, target, venv=venv)
            lock_check = "stale" if lock else "ok"
    else:
        lock_check = "shared"
    stale_info = {"stale_interpreter": stale, "stale_lock": lock, **({"lock_check": lock_check} if lock_check else {})}
    if not stale and not lock and not missing and not force:
        return {"env": "ok", **({"lock_check": lock_check} if lock_check else {})}
    if (share and cmd and _env_target(target) and venv == Path(root) / ".venv"
            and Path(root).parent == site.worktrees and (missing or shared_env_of(venv) is not None)):
        extra = {"missing": True} if missing else {}
        if dry_run:
            return {"env": "rebuilt", "command": cmd, **stale_info, "dry_run": True, **extra}
        aside = None
        if not missing:
            aside = venv.with_name(f"{venv.name}.stale-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}")
            os.rename(venv, aside)
        try:
            r = provision_worktree_env(site, Path(root), target)
        except Exception:
            if aside is not None and not venv.exists():
                os.rename(aside, venv)  # the old shared form stays (stale): nothing is lost
            raise
        for leftover in venv.parent.glob(f"{venv.name}.stale-*"):  # as below: the copy and earlier failed attempts'
            shutil.rmtree(leftover, ignore_errors=True)
        if aside is not None:
            extra["moved_aside"] = f"{aside} (removed)"
        return {"env": "rebuilt", "command": r.get("command") or f"shared {r.get('shared')}",
                **stale_info, "env_mode": r["env_mode"], **extra}
    if not cmd:
        if missing:
            return {"env": "ok"}  # a target without env_rebuild may not use a virtualenv
        return {"env": "stale", **stale_info,
                "hint": f"set `env_rebuild` on the {target} entry in config.json (or `env.rebuild`), for example"
                        " \"uv sync --frozen --all-groups\", then run the repair again; the Director owns config.json"}
    extra = {"missing": True} if missing else {}
    if dry_run:
        return {"env": "rebuilt", "command": cmd, **stale_info, "dry_run": True, **extra}
    aside = None
    if venv.exists():  # stale, or forced: the rebuild starts from nothing either way
        aside = venv.with_name(f"{venv.name}.stale-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}")
        os.rename(venv, aside)
        extra["moved_aside"] = str(aside)
    env = _uv_free_env(site)
    if venv != Path(root) / ".venv":
        env.update(VIRTUAL_ENV=str(venv), UV_PROJECT_ENVIRONMENT=str(venv))
    try:
        proc = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=3600, env=env)
    except subprocess.TimeoutExpired:
        fail(f"environment rebuild `{cmd}` in {root} timed out after 3600 s"
             + (f". The stale environment was moved to {aside}." if aside else ""))
    kept = f" The stale environment was moved to {aside}." if aside else ""
    if proc.returncode != 0:
        fail(f"environment rebuild `{cmd}` failed in {root} (exit {proc.returncode}): "
             f"{(proc.stderr or proc.stdout).strip()[-600:]}{kept}")
    if not venv.is_dir():
        fail(f"environment rebuild `{cmd}` ran in {root} but did not create {venv}{kept}")
    still = venv_stale(venv)
    if still:
        fail(f"environment rebuild `{cmd}` ran but {venv} still names {still}{kept}")
    for leftover in venv.parent.glob(f"{venv.name}.stale-*"):  # the copy moved aside and any earlier failed attempt's
        shutil.rmtree(leftover, ignore_errors=True)
    if aside:
        extra["moved_aside"] = f"{aside} (removed)"
    return {"env": "rebuilt", "command": cmd, **stale_info, **extra}


def env_repair(site: Site, actor: str, target: str, force: bool = False, dry_run: bool = False) -> dict:
    """Rebuild the virtualenv of a target's main tree (for example after the site moved, or when it no longer
    matches the lock); `force` rebuilds it stale or not, `dry_run` only reports (release 51)."""
    if site.org.role(actor).kind != "desk":
        fail("only the desk repairs environments")
    root = target_root(site, target)
    if root is None or not root.is_dir():
        fail(f"unknown target {target!r}")
    r = {"target": target, **_repair_env(site, root, target, force=force, dry_run=dry_run)}
    if r["env"] == "rebuilt" and not dry_run:
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


WI_RUNNERS = ("local", "site")
LOCAL_TIMEOUT_DEFAULT = 60
_SHELL_OPS = re.compile(r"[;&|`<>\n]|\$\(")


def local_allowed(site: Site, check: str) -> bool:
    """A check may run in the verify process for a WI that chose the local runner (release 45, 6e): it matches a
    config `checks.local_allow` glob (fnmatch over the whole command, e.g. `grep -q *`, `test -f *`) and is one
    simple command (no `;`, `&`, `|`, redirection, backquote or `$(`), so a pattern never admits a chain."""
    pats = (site.config.get("checks") or {}).get("local_allow") or []
    return not _SHELL_OPS.search(check) and any(fnmatch.fnmatchcase(check.strip(), str(p)) for p in pats)


def _wi_runner_choice(site: Site, choice: str, checks: list[str]) -> str:
    """Validate a per-WI runner (`--checks-runner`): `site` (config checks.runner) or `local`, which needs every
    declared check to be allowed by `checks.local_allow`."""
    if choice not in WI_RUNNERS:
        fail(f"--checks-runner must be one of {', '.join(WI_RUNNERS)}: {choice!r}")
    if choice == "local":
        if not (site.config.get("checks") or {}).get("local_allow"):
            fail("--checks-runner local needs config checks.local_allow (globs of checks that may run locally); it is unset")
        bad = [c for c in checks if not local_allowed(site, c)]
        if bad:
            fail(f"--checks-runner local: these checks do not match checks.local_allow (or chain commands): {'; '.join(bad)}")
    return choice


def wi_checks_runner(site: Site, payload: dict) -> tuple[str, str]:
    """(runner, source) for a WI's declared checks: the WI's recorded `checks_runner: local` while its checks
    still match `checks.local_allow` (source `wi`), else config `checks.runner` (source `site`)."""
    checks = list(payload.get("checks") or [])
    if payload.get("checks_runner") == "local" and checks and all(local_allowed(site, c) for c in checks):
        return "local", "wi"
    return checks_runner(site), "site"


def _same_deps(cwd: Path, troot: Path) -> bool:
    """The worktree's dependencies are the main tree's: `uv.lock` byte-identical, or neither has one and
    `pyproject.toml` is identical (the shared-venv rule of `slurm submit --module` and of checks_env_plan)."""
    lock, live = Path(cwd) / "uv.lock", Path(troot) / "uv.lock"
    if lock.is_file() and live.is_file():
        return lock.read_bytes() == live.read_bytes()
    a, b = Path(cwd) / "pyproject.toml", Path(troot) / "pyproject.toml"
    return not lock.exists() and not live.exists() and a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes()


def _main_env_lock_state(site: Site, troot: Path, target: str | None) -> str | None:
    """`lock-stale: <reason>` when the target main tree's `.venv` does not match its lock (release 51,
    UR-20261011-01: a byte-identical lock proved nothing about what was installed, so `verify` shared an
    environment missing an extra), else None (it matches, or no `uv sync` env_rebuild to ask)."""
    if not _env_target(target):
        return None
    reason = lock_stale(site, troot, target)
    return f"lock-stale: {reason}" if reason else None


def _env_target(target: str | None) -> bool:
    """A target whose code runs in its own Python environment: a module or a project tree."""
    return bool(target) and target.startswith(("modules/", "projects/"))


def checks_env_plan(site: Site, wi: str, cwd: Path, troot: Path) -> dict:
    """The Python environment a WI's declared checks run in (release 46, company finding 2026-10-09: a new WI
    worktree has no `.venv`, since it is untracked, so checks fell back to a bare `python`). Modes:

    - `own`: the work root has its own `.venv` (or is the target's main tree): the checks run with that `.venv`
      activated as in the other modes (VIRTUAL_ENV, PATH, UV_PROJECT_ENVIRONMENT, UV_NO_SYNC=1 so the checks never
      sync or change it; release 48, company finding d381ae6b: before, nothing was activated and a bare `python`
      was the system one). With no `.venv` at all (a main tree without env_rebuild) nothing is activated.
    - `shared-venv`: the worktree has no own environment (none, or the shared form `dispatch brief` gives it since
      release 47, see share_worktree_env), the target's main tree has a usable (not stale) `.venv`, and the
      worktree's `uv.lock` is byte-identical to the main tree's (or neither has one and `pyproject.toml` is
      identical), and (release 51) that `.venv` matches the lock (lock_stale: `uv sync --check`; a main env
      lacking an extra falls through to `rebuild`, with `main_env: "lock-stale: ..."` in the plan): the checks run
      with that environment activated (VIRTUAL_ENV, PATH, UV_PROJECT_ENVIRONMENT,
      UV_NO_SYNC=1 so nothing syncs or changes it) and the worktree's package directory first on PYTHONPATH, so the
      code under test is the worktree's. The same rule as `slurm submit --module` (exectree env mode `shared-venv`).
    - `rebuild`: the dependencies differ (or the main tree has no usable env) and the target has `env_rebuild`:
      the checks first build the worktree's own `.venv` with it (inside the checks job for the slurm runner, in
      the verify process for the local runner), then run with it activated. A shared-form `.venv` is replaced.
    - `none`: nothing to activate (no env_rebuild, no main-tree env): as before release 46.
    - `missing` (release 47): a module or project target with `env_rebuild` whose work root is its main tree
      and has no `.venv`: `verify --wi` refuses ("worktree env missing") instead of running a bare python.
    No desk repair is needed for a new worktree in any mode; a stale `.venv` is still refused (`_check_venv`)."""
    from .exectree import package_dir
    from .paths import wi_target

    cwd, troot = Path(cwd), Path(troot)
    own = cwd / ".venv"
    shared_form = shared_env_of(own) is not None
    target = wi_target(site, wi)
    cmd = _env_rebuild_command(site, target) if _env_target(target) else None
    if cwd.resolve() == troot.resolve():
        if not own.exists() and cmd:
            return {"mode": "missing", "exports": {}, "target": target}
        return _own_plan(own)
    if own.exists() and not shared_form:
        return _own_plan(own)
    main = troot / ".venv"
    main_env = None
    if main.is_dir() and not stale_venv(troot) and _same_deps(cwd, troot):
        main_env = _main_env_lock_state(site, troot, target)  # release 51: a lock-stale main env is not shared
        if main_env is None:
            return {"mode": "shared-venv", "venv": str(main),
                    **({"worktree_env": "shared"} if shared_form else {}),
                    "exports": {"VIRTUAL_ENV": str(main), "UV_PROJECT_ENVIRONMENT": str(main), "UV_NO_SYNC": "1",
                                "PATH_PREPEND": str(main / "bin"), "PYTHONPATH_PREPEND": str(package_dir(cwd))}}
    if cmd is None and target:
        cmd = _env_rebuild_command(site, target)  # release 46 behaviour for other targets
    if cmd:
        return {"mode": "rebuild", "venv": str(own), "command": cmd, "target": target,
                **({"replace": True} if shared_form else {}), **({"main_env": main_env} if main_env else {}),
                "exports": {"VIRTUAL_ENV": str(own), "PATH_PREPEND": str(own / "bin")}}
    if shared_form:  # env_rebuild was removed from config after the dispatch: the shared form as it stands
        return _own_plan(own)
    return {"mode": "none", "exports": {}}


def _own_plan(venv: Path) -> dict:
    """Mode `own` of checks_env_plan: the work root's own `.venv` activated (release 48), or nothing to activate
    when there is no `.venv` directory."""
    if not venv.is_dir():
        return {"mode": "own", "exports": {}}
    return {"mode": "own", "venv": str(venv),
            "exports": {"VIRTUAL_ENV": str(venv), "UV_PROJECT_ENVIRONMENT": str(venv), "UV_NO_SYNC": "1",
                        "PATH_PREPEND": str(venv / "bin")}}


def _check_env_present(site: Site, wi: str, cwd: Path, troot: Path) -> dict:
    """Release 47 (UR-20261009-01): refuse checks that would run a bare python for a target with env_rebuild.
    Returns the plan, so a verify computes (and lock-checks) it once."""
    plan = checks_env_plan(site, wi, cwd, troot)
    if plan["mode"] == "missing":
        fail(f"{wi}: worktree env missing: run worktree repair (`dureh worktree repair --wi {wi}`, or `--target"
             f" {plan['target']}` for the main tree {troot}); {plan['target']} has env_rebuild but {cwd} has no .venv"
             " and no shared environment applies, so the checks would run a bare python. Nothing was recorded.")
    return plan


def _apply_exports(env: dict, exports: dict) -> dict:
    env = dict(env)
    for k, v in exports.items():
        if k == "PATH_PREPEND":
            env["PATH"] = os.pathsep.join([v, env.get("PATH", "")])
        elif k == "PYTHONPATH_PREPEND":
            env["PYTHONPATH"] = os.pathsep.join([v] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        else:
            env[k] = v
    return env


def _checks_env(site: Site, wi: str, cwd: Path, troot: Path, plan: dict | None = None) -> dict:
    env = {**site.env(), "PYTHONDONTWRITEBYTECODE": "1", "DUREH_WORK_ROOT": str(cwd),
           "DUREH_TARGET_ROOT": str(troot), "DUREH_WI": wi}
    return _apply_exports(env, (plan or {}).get("exports") or {})


def _run_checks_local(site: Site, wi: str, cwd: Path, troot: Path, checks: list[str], timeout: int,
                      plan: dict | None = None) -> list[dict]:
    results = []
    plan = plan if plan is not None else checks_env_plan(site, wi, cwd, troot)
    if plan["mode"] == "rebuild":  # the worktree's own environment, built once before the checks
        _repair_env(site, Path(cwd), plan["target"], force=bool(plan.get("replace")), share=False)
    env = _checks_env(site, wi, cwd, troot, plan)
    for cmd in checks:
        t0 = time.monotonic()
        try:
            proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
            rc, out = proc.returncode, (proc.stdout + proc.stderr)[-2000:]
        except subprocess.TimeoutExpired:
            rc, out = "timeout", ""
        results.append({"command": cmd, "exit": rc, "seconds": round(time.monotonic() - t0, 2), "tail": out})
    return results


def _checks_script(wi: str, cwd: Path, troot: Path, site_root: Path, prefix: Path, checks: list[str],
                   plan: dict | None = None) -> str:
    """One job script: cd into the work root, export the same variables as the in-session run, then run each
    declared check through `sh -c` (as `subprocess.run(shell=True)` does), keeping its exit code, wall time
    and combined output in files next to the job log (`<prefix>.check<i>.{exit,ms,out}`)."""
    import shlex

    lines = ["#!/bin/bash", f"# DUREH verify {wi}: declared checks as one scheduler job (generated)",
             f"cd {shlex.quote(str(cwd))} || exit 96",
             f"export PYTHONDONTWRITEBYTECODE=1 DUREH_SITE={shlex.quote(str(site_root))} DUREH_WORK_ROOT={shlex.quote(str(cwd))}"
             f" DUREH_TARGET_ROOT={shlex.quote(str(troot))} DUREH_WI={shlex.quote(wi)}",
             "unset DUREH_ACTOR DUREH_AGENT_ID", "status=0"]
    plan = plan or {"mode": "none", "exports": {}}
    if plan.get("venv"):
        lines.append(f"# checks environment: {plan['mode']} ({plan.get('venv')})")
    if plan["mode"] == "rebuild":  # the worktree's own .venv, built in the job (scheduler work, not session work)
        if plan.get("replace"):  # the shared form dispatch gave the worktree (release 47): its own env replaces it
            lines.append(f"rm -rf -- {shlex.quote(plan['venv'])}")
        lines += [f"echo '+ env: {plan['mode']}:' {shlex.quote(plan['command'])}",
                  f"( unset VIRTUAL_ENV; sh -c {shlex.quote(plan['command'])} ) > {shlex.quote(f'{prefix}.env.out')} 2>&1"
                  " || { echo 'dureh: env_rebuild failed (see .env.out)' >&2; exit 95; }"]
    for k, v in plan.get("exports", {}).items():
        if k == "PATH_PREPEND":
            lines.append(f'export PATH={shlex.quote(v)}:"$PATH"')
        elif k == "PYTHONPATH_PREPEND":
            lines.append(f'export PYTHONPATH={shlex.quote(v)}"${{PYTHONPATH:+:$PYTHONPATH}}"')
        else:
            lines.append(f"export {k}={shlex.quote(v)}")
    for i, cmd in enumerate(checks, 1):
        out = shlex.quote(f"{prefix}.check{i}")
        lines += [f"echo '+ check {i}:' {shlex.quote(cmd)}", "t0=$(date +%s%N)",
                  f"sh -c {shlex.quote(cmd)} > {out}.out 2>&1", "rc=$?", "t1=$(date +%s%N)",
                  f'echo "$rc" > {out}.exit', f"echo $(( (t1 - t0) / 1000000 )) > {out}.ms",
                  'echo "  exit $rc"', '[ "$rc" -eq 0 ] || status=1']
    lines.append("exit $status")
    return "\n".join(lines) + "\n"


def _submit_checks_job(site: Site, wi: str, cwd: Path, troot: Path, checks: list[str],
                       plan: dict | None = None) -> dict:
    """Submit the declared checks as one Slurm job (cwd = the work root, the site's environment). Returns the job
    description: {job_id, script, prefix, log_out, log_err, command, time}. Nothing is recorded here."""
    from . import ops
    from .util import utc_compact

    cfg = site.config.get("checks") or {}
    slurm = site.config.get("slurm") or {}
    logs = ops.wi_log_dir(site, wi)
    logs.mkdir(parents=True, exist_ok=True)
    prefix = logs / f"verify-{utc_compact()}"
    script = Path(f"{prefix}.sh")
    plan = plan if plan is not None else checks_env_plan(site, wi, cwd, troot)
    script.write_text(_checks_script(wi, cwd, troot, site.root, prefix, checks, plan), encoding="utf-8")
    script.chmod(0o755)
    wall = str(cfg.get("time") or slurm.get("time") or CHECKS_TIME_DEFAULT)
    cmd = ["sbatch", "--parsable", f"--job-name={ops.job_prefix(site)}-verify-{wi}", f"--time={wall}", f"--chdir={cwd}",
           "--export=ALL"]
    for key, flag in (("account", "--account"), ("partition", "--partition")):
        if slurm.get(key):
            cmd.append(f"{flag}={slurm[key]}")
    # the log options come last so a checks.sbatch_args -o/-e never moves them out of the log dir
    cmd += [str(a) for a in (cfg.get("sbatch_args") or [])] + [f"--output={prefix}.out", f"--error={prefix}.err", str(script)]
    env = _checks_env(site, wi, cwd, troot)  # the plan's exports live in the script, after `cd`
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"{wi}: cannot submit the checks as a scheduler job ({exc}); nothing was recorded. Set config"
             " checks.runner to local or run `dureh verify --wi ... --local` on a machine without a scheduler")
    if proc.returncode != 0:
        fail(f"{wi}: sbatch failed for the checks job: {proc.stderr.strip()[-2000:]}; nothing was recorded. Set config"
             " checks.runner to local or run `dureh verify --wi ... --local` on a machine without a scheduler")
    return {"job_id": proc.stdout.strip().split(";")[0], "script": str(script), "prefix": str(prefix),
            "log_out": f"{prefix}.out", "log_err": f"{prefix}.err", "command": cmd, "time": wall,
            "checks_env": plan["mode"]}


def _read_check_results(prefix: str, checks: list[str], job_id: str, state: str) -> list[dict]:
    """Per-check results the job script left next to its log (`<prefix>.check<i>.{exit,ms,out}`)."""
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
        results.append({"command": check, "exit": rc, "seconds": seconds, "tail": tail, "log": str(out_file)})
    return results


def _run_checks_slurm(site: Site, wi: str, cwd: Path, troot: Path, checks: list[str],
                      plan: dict | None = None) -> tuple[list[dict], dict]:
    """Submit the declared checks as one Slurm job, wait for it like `slurm wait`, and read the per-check results
    back. Returns (results, job)."""
    from . import cluster, ops

    cfg = site.config.get("checks") or {}
    job = _submit_checks_job(site, wi, cwd, troot, checks, plan)
    job_id = job["job_id"]
    limit = cluster.parse_time(job["time"])
    wait = int(cfg.get("wait") or ((limit.total_seconds() if limit else 3600) + 900))
    interval = int(cfg.get("interval") or 15)
    t0 = time.monotonic()
    state, raw, final = ops.wait_job(site, job_id, wait, interval)
    if not final:
        subprocess.run(["scancel", job_id], capture_output=True, text=True, timeout=60, env=site.env())
        state = f"{state} (not finished after {wait} s; cancelled)"
    results = _read_check_results(job["prefix"], checks, job_id, state)
    job.update({"state": state, "raw": raw, "waited_seconds": round(time.monotonic() - t0, 2)})
    return results, job


def _verify_context(site: Site, actor: str, wi: str) -> dict:
    """What every per-WI verification checks first: the actor, a staged item, a sane work root."""
    led = Ledger(site)
    if not site.org.has(actor, "verify"):
        fail("verification is recorded by a role with the verify capability (the checker)")
    rec = led.get("work-item", wi)
    p = rec["payload"]
    status = led.wi_status(wi)["status"]
    if status != "staged":
        fail(f"{wi} is {status}; only staged work can be verified")
    problem = requirements_problem(site, wi, p)
    if problem:  # release 45 (6g): the work was briefed against other requirements
        fail(problem)
    wt = site.worktrees / wi
    troot = target_root(site, p["target"]) or site.root
    cwd = wt if wt.is_dir() else troot
    return {"led": led, "rec": rec, "p": p, "troot": troot, "cwd": cwd,
            "bundle": site.org.role(p["role"]).is_manager, "checks": list(p.get("checks") or [])}


def _check_venv(wi: str, cwd: Path):
    stale = stale_venv(cwd)
    if stale:
        fail(f"{wi}: stale virtualenv in {cwd}/.venv (it names {stale}, which is not this environment); checks would run"
             f" another environment. Repair first: `dureh worktree repair --wi {wi}`. Nothing was recorded.")


def verify_wi(site: Site, actor: str, wi: str, timeout: int = 1800, local: bool = False) -> dict:
    ctx = _verify_context(site, actor, wi)
    start = time.monotonic()
    if ctx["bundle"]:
        return _verify_bundle(site, actor, wi, start)
    cwd, troot, checks = ctx["cwd"], ctx["troot"], ctx["checks"]
    _check_venv(wi, cwd)
    plan = _check_env_present(site, wi, cwd, troot) if checks else None  # one plan (and lock check) per verify
    paths, head = changed_paths(site, wi)
    # release 27: checks are a scheduler job, not session work; release 45: unless the WI chose the local runner
    runner, source = ("local", "override") if local else wi_checks_runner(site, ctx["p"])
    if source == "wi":
        timeout = int((site.config.get("checks") or {}).get("local_timeout") or LOCAL_TIMEOUT_DEFAULT)
    results, job = [], None
    if checks and runner == "slurm":
        results, job = _run_checks_slurm(site, wi, cwd, troot, checks, plan)
    elif checks:
        results = _run_checks_local(site, wi, cwd, troot, checks, timeout, plan)
    extra = {"runner_source": source}
    if checks:
        extra["checks_env"] = (job or {}).get("checks_env") or plan["mode"]
    if ctx["p"].get("checks_runner") == "local" and source == "site":
        extra["runner_note"] = "the WI chose local, but its checks no longer match checks.local_allow; ran on the site runner"
    return _record_verification(site, ctx, actor, wi, paths, head, results, runner, job, start, local=local,
                                extra=extra)


def _record_verification(site: Site, ctx: dict, actor: str, wi: str, paths: list[str], head: str | None,
                         results: list[dict], runner: str, job: dict | None, start: float, local: bool = False,
                         extra: dict | None = None, review: dict | None = None, hold: dict | None = None) -> dict:
    """Ownership, declared outputs and check results into one verify run, its verification event, and either
    `wi.verified` or `rework.requested` (the synchronous and the collected path record the same). `review` (release
    64, a member reviewer's verdict: a check row whose exit is 0 for PASS) counts like a check and is named in the
    note. `hold` (release 66, UR-20261015-07: a collected codex verdict): a FAIL is recorded (the verify run and
    `verification.recorded`, bound to the head) but the item stays staged: instead of `rework.requested`, the
    `member.held` event (with `hold` in its payload) waits for the dispatcher's choice (rework or the fallback)."""
    led, p, cwd, checks = ctx["led"], ctx["p"], ctx["cwd"], ctx["checks"]
    org = site.org
    violations = [x for x in paths if not org.scope_allows(p["role"], f"{p['target']}/{x}")]
    missing = [o for o in p.get("outputs", []) if not (Path(cwd) / o).exists()]  # the work root only
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
    if review is not None:
        passed = passed and review["exit"] == 0
        note = (note if checks else "no declared checks") + f"; review {review.get('verdict') or 'no verdict'}" \
            f" ({review['command']})"
        results = list(results) + [review]
    payload = {"result": "pass" if passed else "fail", "head": head, "changed": paths, "violations": violations,
               "missing_outputs": missing, "checks": results, "artifact_sha256": hashes,
               "seconds": round(time.monotonic() - start, 2), "note": note, "runner": runner}
    if local:
        payload["runner_override"] = True
    if job:
        payload["job"] = job
    payload.update(extra or {})
    refs = [extra["collected_from"]] if extra and extra.get("collected_from") else None
    run = led.append("run", "verify", actor, wi, {"run_type": "verify", "status": payload["result"], **payload}, refs)
    led.event("verification.recorded", actor, wi, {"result": payload["result"], "run": run["id"], "head": head}, [run["id"]])
    if passed:
        led.event("wi.verified", actor, wi, {"run": run["id"], "head": head}, [run["id"]])
    elif hold is not None:
        held = led.event("member.held", actor, wi, {**hold, "run": run["id"], "head": head, "reason": note,
                                                     "violations": violations, "missing_outputs": missing},
                         [run["id"]])
        payload["held"] = held["id"]
    else:
        led.event("rework.requested", actor, wi, {"run": run["id"], "reason": note,
                                                  "violations": violations, "missing_outputs": missing}, [run["id"]])
    payload["run"] = run["id"]
    return payload


def _verify_jobs(led: Ledger, wi: str) -> list[dict]:
    """Submitted verify jobs of a WI (`verify --submit`), oldest first."""
    return [r for r in led.load("run") if r["wi"] == wi and r["payload"].get("run_type") == "verify"
            and r["payload"].get("status") == "submitted" and r["payload"].get("job_id")]


def _collected(led: Ledger, run_id: str) -> dict | None:
    return next((r for r in led.load("run") if r["payload"].get("run_type") == "verify"
                 and r["payload"].get("collected_from") == run_id), None)


def _superseded(led: Ledger, run_id: str, events: list[dict] | None = None) -> dict | None:
    """The `verification.superseded` event that voided this verify run (release 61), or None."""
    for e in reversed(events if events is not None else led.events()):
        if e["type"] == "verification.superseded" and e["payload"].get("run") == run_id:
            return e
    return None


def pending_verify(led: Ledger, wi: str) -> dict | None:
    """The latest submitted verify job of this WI not yet collected, or None. A superseded run (release 61,
    UR-20261013-02: its head moved and `verify --submit` or `--collect` voided it) is not pending."""
    events = led.events(wi)
    for r in reversed(_verify_jobs(led, wi)):
        if _superseded(led, r["id"], events):
            continue
        return None if _collected(led, r["id"]) else r
    return None


def _stale_verify(site: Site, wi: str, run: dict) -> bool:
    """A pending verify run is stale when the head it was submitted on is not the item's current head."""
    _, head = changed_paths(site, wi)
    return head != run["payload"].get("head")


def _supersede_verify(site: Site, actor: str, wi: str, run: dict, new_head: str | None) -> dict:
    """Record `verification.superseded` for a stale pending verify run (release 61, UR-20261013-02): the item's
    head moved after submission, so the job's results describe nothing current. A job still pending or running
    is cancelled (`scancel`); its result is ignored either way. The slot is free for `verify --submit`."""
    from . import ops

    led = Ledger(site)
    p = run["payload"]
    job_id = str(p["job_id"])
    recorded = ops._recorded_states(led.events(wi)).get(run["id"])
    if recorded and (recorded[1] or recorded[0] in ops.TERMINAL_STATES):
        state = recorded[0]
    else:
        try:
            state, _ = ops.job_state(site, job_id)
        except (OSError, subprocess.SubprocessError):
            state = "UNKNOWN"
    first = (state.split() or ["UNKNOWN"])[0]
    cancelled = False
    if first not in ops.TERMINAL_STATES:
        try:
            cancelled = subprocess.run(["scancel", job_id], capture_output=True, text=True, timeout=60,
                                       env=site.env()).returncode == 0
        except (OSError, subprocess.SubprocessError):
            cancelled = False
    payload = {"run": run["id"], "old_head": p.get("head"), "new_head": new_head, "job_id": job_id,
               "reason": "head moved", "job_state": first, "cancelled": cancelled}
    ev = led.event("verification.superseded", actor, wi, payload, [run["id"]])
    note = (f"verify run {run['id']} (job {job_id}) was submitted on {str(p.get('head'))[:12]}; {wi} is now at"
            f" {str(new_head)[:12]}, so its results are ignored")
    if first not in ops.TERMINAL_STATES:
        note += f"; job {job_id} was {first} and " + ("was cancelled" if cancelled else "may still run; its result is ignored")
    return {"superseded": run["id"], "event": ev["id"], "job_id": job_id, "old_head": p.get("head"), "new_head": new_head,
            "job_state": first, "cancelled": cancelled, "note": note}


def verify_submit(site: Site, actor: str, wi: str, timeout: int = 1800) -> dict:
    """`verify --wi --submit` (release 37, UR-20261005-02): submit the declared-checks job and return at once with
    a pending verify run; `verify --wi --collect` records the verification after the job ended. With nothing to
    submit (a bundle, no declared checks, or the local runner) the verification runs synchronously as usual."""
    from . import ops

    ctx = _verify_context(site, actor, wi)
    if ctx["bundle"] or not ctx["checks"] or wi_checks_runner(site, ctx["p"])[0] != "slurm":
        r = verify_wi(site, actor, wi, timeout=timeout)
        return {"submitted": False, "why": "nothing to submit (bundle, no declared checks, or the local runner for"
                                           " this item); verified synchronously", **r}
    led = ctx["led"]
    _, head = changed_paths(site, wi)
    pend = pending_verify(led, wi)
    superseded = None
    if pend and pend["payload"].get("head") == head:
        fail(f"{wi} already has verify run {pend['id']} (job {pend['payload']['job_id']}) waiting to be collected;"
             f" run `dureh verify --wi {wi} --collect` {ops.job_end_phrase(site)}")
    if pend:  # release 61 (UR-20261013-02): the head moved since; the stale run is voided and this one replaces it
        superseded = _supersede_verify(site, actor, wi, pend, head)
    cwd, troot = ctx["cwd"], ctx["troot"]
    _check_venv(wi, cwd)
    plan = _check_env_present(site, wi, cwd, troot)
    job = _submit_checks_job(site, wi, cwd, troot, ctx["checks"], plan)
    run = led.append("run", "verify", actor, wi, {
        "run_type": "verify", "status": "submitted", "runner": "slurm", "job_id": job["job_id"], "head": head,
        "checks": ctx["checks"], "script": job["script"], "prefix": job["prefix"], "log_out": job["log_out"],
        "log_err": job["log_err"], "command": job["command"], "time": job["time"], "checks_env": job["checks_env"],
        "note": f"declared checks submitted as scheduler job {job['job_id']}; collect with `verify --wi {wi} --collect`"})
    return {"submitted": True, "run": run["id"], "job_id": job["job_id"], "head": head, "script": job["script"],
            "log_out": job["log_out"], "log_err": job["log_err"],
            **({"superseded": superseded["superseded"], "superseded_note": superseded["note"]} if superseded else {}),
            "next": f"end your turn; {ops.job_end_phrase(site)}, the checker runs `dureh verify --wi {wi} --collect`"}


def verify_collect(site: Site, actor: str, wi: str, run_id: str | None = None) -> dict:
    """Record the verification of a submitted verify job once it ended, exactly as the synchronous path does.
    Refused while the job is pending or running. A run whose head moved since submission (release 61,
    UR-20261013-02) is superseded instead: `verification.superseded` is recorded, a job still running is
    cancelled, and the result (`superseded`, no `result`) tells the caller to run `verify --wi --submit`."""
    from . import ops

    ctx = _verify_context(site, actor, wi)
    led = ctx["led"]
    if run_id:
        run = ops._get_run(led, run_id)
        if run["wi"] != wi or run["payload"].get("run_type") != "verify" or run["payload"].get("status") != "submitted":
            fail(f"{run_id} is not a submitted verify job of {wi}")
        done = _collected(led, run_id)
        if done:
            fail(f"verify run {run_id} was already collected as {done['id']} ({done['payload']['status']})")
        gone = _superseded(led, run_id, led.events(wi))
        if gone:
            fail(f"verify run {run_id} was superseded ({gone['id']}: {wi} moved from {str(gone['payload'].get('old_head'))[:12]}"
                 f" to {str(gone['payload'].get('new_head'))[:12]}); run `dureh verify --wi {wi} --submit`")
    else:
        run = pending_verify(led, wi) or fail(f"{wi} has no submitted verify job to collect; run `dureh verify --wi {wi}"
                                              " --submit` first")
    p = run["payload"]
    job_id = str(p["job_id"])
    paths, head = changed_paths(site, wi)
    if head != p.get("head"):  # release 61: the stale run is voided, whatever its job's state
        r = _supersede_verify(site, actor, wi, run, head)
        r["next"] = f"run `dureh verify --wi {wi} --submit` (nothing was verified)"
        r["note"] += f". Run `dureh verify --wi {wi} --submit`: nothing was verified"
        return r
    prefix = str(site.relocate(p["prefix"]))  # the job wrote next to its log; the site may have moved since
    recorded = ops._recorded_states(led.events(wi)).get(run["id"])
    if recorded and (recorded[1] or recorded[0] in ops.TERMINAL_STATES):
        state, raw = recorded[0], ""
    else:
        state, raw = ops.job_state(site, job_id)
    first = (state.split() or ["UNKNOWN"])[0]
    have_all = all(Path(f"{prefix}.check{i}.exit").is_file() for i in range(1, len(p["checks"]) + 1))
    if first not in ops.TERMINAL_STATES and not (first == "UNKNOWN" and have_all):
        fail(f"verify run {run['id']}: job {job_id} is still {first}; nothing was recorded. End your turn: the desk"
             f" re-dispatches the checker {ops.job_end_phrase(site)}, then run `verify --wi {wi} --collect`")
    if p.get("checks") != ctx["checks"]:
        fail(f"the declared checks of {wi} changed after verify run {run['id']} was submitted; submit again")
    if not recorded or not recorded[1]:
        led.event("run.status", "desk", wi, {"run": run["id"], "job_id": job_id, "scheduler_state": state, "raw": raw,
                                             "final": True, "collected": True}, [run["id"]])
    results = _read_check_results(prefix, p["checks"], job_id, state)
    job = {k: p[k] for k in ("job_id", "script", "prefix", "log_out", "log_err", "command", "time")}
    job.update({k: str(site.relocate(job[k])) for k in ("script", "prefix", "log_out", "log_err") if job.get(k)})
    job.update({"state": state, "raw": raw})
    return _record_verification(site, ctx, actor, wi, paths, head, results, "slurm", job, time.monotonic(),
                                extra={"collected_from": run["id"], "submitted_ts": run["ts"], "runner_source": "site"})


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
    return payload | {"run": run["id"]}


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
        target_head = git(["rev-parse", "HEAD"], root)
        base = git(["merge-base", target_head, head], root, check=False) or None
        # release 60 (UR-20261013-01): a conflict is found before the merge touches the main tree
        conflicts = _merge_conflicts(root, target_head, branch)
        if conflicts:
            _promote_failed(led, actor, wi, rec, root, base, head, target_head, conflicts, "pre-check")
        try:
            git(["merge", "--no-ff", "-m", f"Promote {wi}: {rec['payload']['title']}", branch], root)
        except DurehError as exc:
            # the pre-check passed (or gave no verdict) and the merge still failed: abort it so the main tree is
            # back where it was (nothing to abort when git refused before starting, e.g. a file in the way)
            unmerged = [l for l in git(["diff", "--name-only", "--diff-filter=U"], root, check=False).splitlines() if l]
            git(["merge", "--abort"], root, check=False)
            _promote_failed(led, actor, wi, rec, root, base, head, target_head, sorted(unmerged), "merge", str(exc))
        merge = git(["rev-parse", "HEAD"], root)
    dec = led.append("decision", "gate2", actor, wi, {"subject": f"Gate 2 {wi}", "disposition": "approve",
                                                        "by": "desk", "memo": memo,
                                                        "binding": {"head": verified["payload"].get("head"), "merge": merge}})
    ev = led.event("wi.promoted", actor, wi, {"merge": merge, "decision": dec["id"],
                                              "action_class": "compensatable" if merge else "reversible",
                                              "compensation": f"git revert -m 1 {merge}" if merge else "append wi.reverted"},
                   [dec["id"]])
    return ev


def _merge_conflicts(root: Path, target_head: str, branch: str) -> list[str] | None:
    """The paths `git merge <branch>` would leave conflicted in `root`, without touching its index, HEAD or files.

    Uses `git merge-tree --write-tree --name-only` (git >= 2.38; the merged tree goes to the object store only):
    exit 0 is a clean merge ([]), exit 1 with a tree id lists the conflicted paths after it up to a blank line. Any other
    outcome (an older git, a timeout) is None: the pre-check is skipped and the merge itself decides (wi_promote
    aborts a failed merge)."""
    try:
        proc = subprocess.run(["git", "merge-tree", "--write-tree", "--name-only", target_head, branch], cwd=root,
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        return []
    first, _, rest = proc.stdout.partition("\n")
    if proc.returncode != 1 or not re.fullmatch(r"[0-9a-f]{40,64}", first):
        return None  # exit 1 without a tree id is an error ("not something we can merge"), not a verdict
    paths = []
    for line in rest.split("\n"):
        if not line:
            break
        paths.append(line)
    return sorted(set(paths))


def _promote_failed(led: Ledger, actor: str, wi: str, rec: dict, root: Path, base: str | None, head: str,
                    target_head: str, conflicts: list[str], stage: str, error: str | None = None) -> None:
    """Record `wi.promote-failed` (the item stays verified; no status change) and fail with the recovery path."""
    target = rec["payload"]["target"]
    led.event("wi.promote-failed", actor, wi, {"target": target, "base": base, "head": head, "target_head": target_head,
                                               "conflicts": conflicts, "stage": stage,
                                               **({"error": error} if error else {})})
    what = (f"conflicts in: {', '.join(conflicts)}" if conflicts
            else f"the merge failed: {error or 'unknown error'}")
    fail(f"{wi} cannot be promoted: merging its branch into {target} ({target_head[:12]}) {what}. Nothing changed in"
         f" {root} ({wi} stays verified; recorded as wi.promote-failed, stage {stage}). Next: `dureh wi amend --wi {wi}"
         f" --reopen --rebase {target_head[:12]} --memo \"<Director's words>\"`, then dispatch {wi} again so the worker"
         " rebases onto the current main and resolves the conflict; verify and promote after that")


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
    here = lambda x: str(site.relocate(x)) if x else x  # noqa: E731 - paths recorded before a site move
    for run in led.load("run"):
        if run["wi"] != wi or run["type"] != "slurm" or not run["payload"].get("job_id"):
            continue
        p = {**run["payload"], **{k: here(run["payload"].get(k)) for k in ("script", "chdir", "log_out", "log_err")}}
        if p.get("array"):
            p["array"] = {**p["array"], **{k: here(p["array"].get(k)) for k in ("items_file", "wrapper")}}
        views = [p]
        if p.get("via") and p.get("jobs"):
            # a via submission: one view per recorded job (its own id, log patterns, task count and job name); the
            # module's rendered scripts are removed only when they lie in the WI's log dir, like --each wrappers
            views = []
            for jb in p["jobs"]:
                script = here(jb.get("script"))
                if script and os.path.realpath(Path(script).parent) in log_dirs:
                    try:
                        if stat.S_ISREG(os.lstat(script).st_mode) and Path(script) not in found:
                            found.append(Path(script))
                    except OSError:
                        pass
                views.append({**p, "job_id": jb["job_id"], "log_out": here(jb.get("log_out")), "log_err": here(jb.get("log_err")),
                              "script": script, "command": [], "job_name": jb.get("name"),
                              "array": {"count": jb.get("count") or 0} if jb.get("kind") == "array" else None})
        for p in views:
            job = str(p["job_id"]).split(";")[0].strip()
            jname = p.get("job_name") or _scan_opt([str(w) for w in p.get("command") or []], "-J", "--job-name") \
                or f"dureh-{wi}"  # records before release 44 always named the job dureh-<WI>
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


def remove_wi_worktree(site: Site, led: Ledger, wi: str, wt: Path, root: Path | None,
                       drop_branch: bool = True) -> str | None:
    """Remove a WI's worktree at `wt` (registered in the target repository `root` when it is one) and, with
    `drop_branch`, the site's own WI branch; shared by `purge` and `site retire-module` (release 55).
    Release 57 (UR-20261012-01): `git worktree remove --force` deregisters the worktree even when it fails to
    delete the directory (an untracked `.venv` on NFS: a file held open is silly-renamed, its directory is not
    empty, git exits non-zero), so a failed or skipped git removal falls back to the harness's own tree removal
    (symlinks removed as links, targets never followed, read-only trees made writable), followed by
    `git worktree prune`; a directory git no longer registers (no `.git` file) is removed that way without
    asking git. Fails when the directory is still there afterwards. Returns the branch deleted, or None."""
    from .exectree import _rmtree

    dropped = None
    if root is not None and (root / ".git").exists():
        if (wt / ".git").exists():  # registered worktree: its branch is HEAD
            candidates = [git(["rev-parse", "--abbrev-ref", "HEAD"], wt, check=False)]
            proc = subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=root,
                                  capture_output=True, text=True, timeout=600)
            if proc.returncode != 0 and (wt.exists() or wt.is_symlink()):
                _rmtree(wt)
        else:  # a deregistered leftover (or nothing): the site's own branch names for the WI
            candidates = list(wi_branches(site, wi))
            _rmtree(wt)
        subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True, text=True, timeout=600)
        if drop_branch:
            for branch in candidates:
                if branch and branch != "HEAD" and git(["branch", "--list", branch], root, check=False) \
                        and _own_branch(site, led, wi, root, branch):
                    git(["branch", "-D", branch], root, check=False)
                    dropped = dropped or branch
    if wt.exists() or wt.is_symlink():
        _rmtree(wt)
    if wt.exists() or wt.is_symlink():
        fail(f"could not remove the worktree directory {wt}; check what is left there (a file held open on NFS "
             f"keeps its directory) and remove it by hand")
    return dropped


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
                remove_wi_worktree(site, led, wi, wt, target_root(site, tgt), drop_branch=status != "promoted")
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


PATH_PURGE_STATUSES = ("dispatched", "staged")


def purge_paths(site: Site, actor: str, wi: str, paths: list[str], dry_run: bool = False) -> dict:
    """Remove untracked or ignored paths (files or directories) from an active WI's worktree (release 72,
    project-dureh/dureh#23, option A): a stray output a test wrote under the worktree fails verify's ownership
    check, and a worker cannot delete it. Only for a dispatched or staged WI; each path is relative to its
    worktree. Refused: an absolute path or one with `..`, the worktree itself or `.git`, a missing path, a path
    whose parent or target resolves outside the worktree (symlinks), and any path that is or holds a tracked
    file. Validate every path first; symlinks are removed as links and their targets are never followed.
    Records one `purged` event with `paths` (relative) and `removed` (absolute)."""
    from .exectree import _rmtree

    if site.org.role(actor).kind != "desk":
        fail("only the desk purges")
    led = Ledger(site)
    led.get("work-item", wi)
    status = led.wi_status(wi)["status"]
    if status not in PATH_PURGE_STATUSES:
        fail(f"{wi} is {status}; purge --path only takes a dispatched or staged WI (purge a finished WI whole "
             f"with `purge --wi {wi}`)")
    if not paths:
        fail("purge --path needs at least one path")
    wt = site.worktrees / wi
    if not (wt / ".git").exists():
        fail(f"{wi} has no worktree at {wt}")
    wt_real = os.path.realpath(wt)
    planned: list[tuple[str, Path]] = []  # (path relative to the worktree, path to remove)
    for p in paths:
        if not p or os.path.isabs(p):
            fail(f"purge --path takes a path relative to {wi}'s worktree, not {p!r}")
        if ".." in Path(p).parts:
            fail(f"refusing a path with `..`: {p}")
        norm = os.path.normpath(p)
        if norm == ".":
            fail(f"{p!r} names the worktree itself; purge a finished WI whole with `purge --wi {wi}`")
        item = wt / norm
        if not os.path.lexists(item):
            fail(f"no such path in {wi}'s worktree: {p}")
        parent = os.path.realpath(item.parent)  # a symlinked directory on the way may lead out of the tree
        if parent != wt_real and not parent.startswith(wt_real + os.sep):
            fail(f"refusing a path that resolves outside {wi}'s worktree: {p} (-> {os.path.join(parent, item.name)})")
        real = os.path.realpath(item)
        if not real.startswith(wt_real + os.sep):
            fail(f"refusing a path that resolves outside {wi}'s worktree: {p} (-> {real})")
        rel = os.path.relpath(os.path.join(parent, item.name), wt_real)
        if rel.split(os.sep)[0] == ".git":
            fail(f"refusing to purge the worktree's git metadata: {p}")
        tracked = [t for t in git(["--literal-pathspecs", "ls-files", "-z", "--", rel], wt).split("\0") if t]
        if tracked:
            more = f" and {len(tracked) - 3} more" if len(tracked) > 3 else ""
            fail(f"refusing to purge {p}: it is or holds tracked files ({', '.join(tracked[:3])}{more}); purge --path "
                 "removes only untracked or ignored paths, tracked files change through the WI's own commits")
        planned.append((rel, Path(wt_real) / rel))
    # a path inside another planned directory goes with it
    planned = [(r, x) for r, x in dict(planned).items()
               if not any(r != o and r.startswith(o + os.sep) for o, _ in planned)]
    removed: list[str] = []
    if not dry_run:
        left = None
        for _rel, it in planned:
            if it.is_symlink() or it.is_file():
                it.unlink()
            else:
                _rmtree(it)  # read-only trees: chmod u+w first
            if os.path.lexists(it):
                left = it  # a file held open on NFS keeps its directory
                break
            removed.append(str(it))
        led.event("purged", actor, wi, {"paths": [r for r, _ in planned], "removed": removed, "removed_logs": [],
                                         **({"failed": str(left)} if left else {})})
        if left:
            fail(f"could not remove {left} (removed so far: {', '.join(removed) or 'nothing'}); check what is left there")
    return {"wi": wi, "worktree": str(wt), "planned": [str(x) for _, x in planned], "removed": removed,
            "dry_run": dry_run}
