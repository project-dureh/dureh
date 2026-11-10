"""Site cleanup (release 98, UR-20261109-02 of two sites): retire finished WIs' worktrees and branches
(`worktree retire`), delete a finished WI's run directory under `scratch/<WI>/` with a bound approval
(`purge --scratch-wi`), and the read-only `cleanup report` that `day close` summarises in one line.

Company-agnostic: only WI records, the site layout (worktrees/, scratch/, modules/*, projects/*) and git are read."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import time
from pathlib import Path

from .paths import target_root
from .records import Ledger
from .site import Site
from .util import DurehError, fail, git

RETIRABLE = ("promoted", "stopped", "reverted")
RETIRED = "worktree.retired"
RETIRE_APPROVAL = "retire-approval"
SCRATCH_PURGED = "scratch.purged"
WI_NAME = re.compile(r"^WI-[0-9]{4,}$")  # records.WI_ID; a run directory is scratch/<WI> exactly


def _desk(site: Site, actor: str, what: str) -> None:
    if site.org.role(actor).kind != "desk":
        fail(f"only the desk runs {what}")


# ------------------------------------------------------------------ sizes
def tree_size(path: Path, deadline: float | None = None) -> tuple[int, bool]:
    """(bytes, complete): the files under `path` by their own size (symlinks as links, nothing followed); stops at
    `deadline` (time.monotonic) and says so."""
    total = 0
    try:
        st = os.lstat(path)
    except OSError:
        return 0, True
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size, True
    for dirpath, dirnames, filenames in os.walk(path):
        if deadline is not None and time.monotonic() > deadline:
            return total, False
        for n in filenames + dirnames:
            try:
                s = os.lstat(os.path.join(dirpath, n))
            except OSError:
                continue
            if not stat.S_ISDIR(s.st_mode):
                total += s.st_size
    return total, True


def human(n: int | None) -> str:
    if n is None:
        return "?"
    x = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if x < 1024 or unit == "T":
            return f"{x:.0f}{unit}" if unit == "B" else f"{x:.1f}{unit}"
        x /= 1024
    return f"{x:.1f}T"


# --------------------------------------------------------------- citations
SHA256_HEX = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
CITE_MAX_BYTES = 2 * 1024 * 1024


def citation_files(site: Site) -> list[Path]:
    """The text the desk and research keep about results: `notes/**` and `projects/*/research-notes/**` (plain files,
    no symlinks, at most 2 MB, not a no-read protected path)."""
    from . import protect

    roots = [site.root / "notes", *sorted((site.root / "projects").glob("*/research-notes"))]
    out = []
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".git"))
            for n in sorted(filenames):
                f = Path(dirpath) / n
                try:
                    st = os.lstat(f)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_size <= CITE_MAX_BYTES and not protect.read_refusal(site, f):
                    out.append(f)
    return out


def citations(site: Site, needles: list[str], time_cap: float = 30.0) -> tuple[dict[str, list[dict]], bool]:
    """Lines of the site's notes that cite a path being removed (`needles`: site-relative paths; a citation is the
    path, or a path under it, as text, relative or absolute). A line that also carries a sha256 (64 hex digits) pins
    it. Returns ({needle: [{"where": "<file>:<line>", "pinned": bool}]}, complete)."""
    needles = sorted({n.strip("/") for n in needles if n and n.strip("/")})
    found: dict[str, list[dict]] = {n: [] for n in needles}
    if not needles:
        return found, True
    pat = re.compile("(" + "|".join(re.escape(n) for n in sorted(needles, key=len, reverse=True)) + r")(?![A-Za-z0-9_.-])")
    deadline = time.monotonic() + time_cap
    for f in citation_files(site):
        if time.monotonic() > deadline:
            return found, False
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192] or not any(n.encode() in data for n in needles):
            continue
        rel = os.path.relpath(f, site.root)
        for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            for m in pat.finditer(line):
                start = m.start()
                if start and line[start - 1] not in " \t`'\"(<[/=:,":
                    continue  # part of a longer path (another/scratch/WI-1)
                if start and line[start - 1] == "/" and not line[:start].rstrip("/").endswith((site.root.name, ".")):
                    continue  # an absolute path must be this site's
                found[m.group(1)].append({"where": f"{rel}:{i}", "pinned": bool(SHA256_HEX.search(line))})
    for n in found:
        found[n] = [dict(t) for t in {tuple(sorted(c.items())) for c in found[n]}]
        found[n].sort(key=lambda c: c["where"])
    return found, True


def cite_text(cites: list[dict]) -> str:
    return "; ".join(f"{'pinned' if c['pinned'] else 'cited'} by {c['where']}" for c in cites)


# ------------------------------------------------------------- repositories
class _Repo:
    """One module or project repository: its main head, wi/* branches and those merged into the main head."""

    def __init__(self, root: Path):
        self.root = root
        self.head = git(["rev-parse", "HEAD"], root, check=False) or None
        self.main = git(["rev-parse", "--abbrev-ref", "HEAD"], root, check=False) or "HEAD"
        self.branches: dict[str, str] = {}
        for line in git(["for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/wi/"], root,
                        check=False).splitlines():
            name, _, sha = line.partition(" ")
            if name:
                self.branches[name] = sha
        self.merged = set(git(["for-each-ref", "--merged", "HEAD", "--format=%(refname:short)", "refs/heads/wi/"],
                              root, check=False).split()) if self.head else set()


def _repo(repos: dict, site: Site, target: str) -> _Repo | None:
    parts = (target or "").split("/")
    if len(parts) != 2 or parts[0] not in ("modules", "projects") or not parts[1]:
        return None
    root = target_root(site, target)
    if root is None or not (root / ".git").exists():
        return None
    key = str(root)
    if key not in repos:
        repos[key] = _Repo(root)
    return repos[key]


def _bases(events: list[dict]) -> dict[str, str]:
    """Each WI's dispatch base (or the base a `wi amend --rebase` moved it to), the latest one recorded."""
    out: dict[str, str] = {}
    for ev in events:
        p = ev.get("payload") or {}
        if ev.get("wi") and ((ev["type"] == "wi.amended" and p.get("rebase_onto"))
                             or (ev["type"] == "dispatch.prepared" and p.get("base"))):
            out[ev["wi"]] = p.get("rebase_onto") or p["base"]
    return out


def _dirty(wt: Path) -> list[str]:
    """Uncommitted changes in a worktree: tracked changes and untracked files, except interpreter caches, the
    virtualenv and the WI scratch directory (workflow.CACHE_RE)."""
    from .workflow import CACHE_RE

    proc = subprocess.run(["git", "--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=normal"],
                          cwd=wt, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        return [f"(git status failed: {proc.stderr.strip()[:200]})"]
    out, entries, i = [], proc.stdout.split("\0"), 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        if e[0] in "RC":
            i += 1
        if not CACHE_RE.search(e[3:]):
            out.append(e)
    return out


def _plan(site: Site, led: Ledger, wi: str, status: str | None, target: str, base: str | None, repos: dict,
          live: dict, check_dirty: bool = True) -> dict:
    """What `worktree retire` does with one WI: verdict retire, needs-approval, refused or nothing."""
    from .workflow import _own_branch, wi_branches, worktree_problem

    wt = site.worktrees / wi
    has_wt = os.path.lexists(wt)
    row = {"wi": wi, "status": status, "target": target, "worktree": str(wt) if has_wt else None, "branch": None,
           "head": None, "merged": None, "verdict": "nothing", "reason": None}

    def refuse(why: str) -> dict:
        row.update(verdict="refused", reason=why)
        return row

    if status not in RETIRABLE:
        return refuse(f"{wi} is {status}; only a promoted, stopped or reverted item retires")
    repo = _repo(repos, site, target)
    if repo is None:
        if has_wt:
            return refuse(f"target {target} is not a module or project repository of the site; remove this worktree"
                          f" with `purge --wi {wi}`")
        return row
    branch = None
    if has_wt:
        prob = worktree_problem(wt, wi, site)
        if prob:
            return refuse(f"broken worktree ({prob}); run `worktree repair --wi {wi}` first")
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], wt, check=False) or None
    else:
        new, legacy = wi_branches(site, wi)
        branch = new if new in repo.branches else legacy if legacy in repo.branches else None
    if branch and (branch not in repo.branches or not _own_branch(site, led, wi, repo.root, branch)):
        row["kept_branch"] = branch  # not this site's branch for the WI: never deleted
        branch = None
    row["branch"] = branch
    if not has_wt and not branch:
        return row
    if wi in live:
        return refuse(f"{wi} still has scheduler jobs that are not known to have ended ({', '.join(live[wi])})")
    if has_wt and check_dirty:
        dirty = _dirty(wt)
        if dirty:
            more = f" and {len(dirty) - 5} more" if len(dirty) > 5 else ""
            return refuse(f"uncommitted changes in {wt}: {'; '.join(dirty[:5])}{more}")
    if branch:
        row["head"] = repo.branches[branch]
        row["merged"] = branch in repo.merged
        if not row["merged"]:
            if status == "promoted":
                return refuse(f"{branch} is not merged into {repo.main} of {target}; a promoted item's branch is kept"
                              " until it is")
            if base and row["head"] == base:
                row["empty"] = True  # a stopped item with nothing committed past its base
            else:
                n = git(["rev-list", "--count", f"HEAD..{branch}"], repo.root, check=False) or "?"
                row.update(verdict="needs-approval", commits=n,
                           reason=f"{status} item with {n} commit(s) on {branch} not in {repo.main}; retiring deletes"
                                  " them: needs the Director's approval (`--approve`, then `--approval <id>`)")
                return row
    row["verdict"] = "retire"
    return row


def _retire_binding(rows: list[dict]) -> dict:
    return {"retire": sorted(({"wi": r["wi"], "branch": r["branch"], "head": r["head"],
                               **({"pinned": r["pinned"]} if r.get("pinned") else {})} for r in rows),
                             key=lambda x: x["wi"])}


def _selection(site: Site, led: Ledger, wis: list[str] | None, states: list[str] | None) -> tuple[list[tuple], list]:
    if bool(wis) == bool(states):
        fail("worktree retire takes --wi <WI>... or --state promoted[,stopped,reverted] (or --all-promoted), not both")
    events = led.events()
    items = {w["id"]: w for w in led.work_items()}
    if wis:
        for w in wis:
            led.get("work-item", w)
        chosen = list(dict.fromkeys(wis))
    else:
        bad = [s for s in states if s not in RETIRABLE]
        if bad:
            fail(f"--state takes {', '.join(RETIRABLE)}, not {', '.join(bad)}")
        chosen = [i for i, w in items.items() if w["status"] in states]
    return [(w, items[w]["status"], items[w]["target"]) for w in chosen], events


def plan_retire(site: Site, led: Ledger, wis: list[str] | None, states: list[str] | None,
                check_dirty: bool = True, probe: bool = True) -> list[dict]:
    from .ops import live_job_runs

    chosen, events = _selection(site, led, wis, states)
    live: dict[str, list[str]] = {}
    for r in live_job_runs(site, led, {w for w, _, _ in chosen}, events, probe=probe):
        live.setdefault(r["wi"], []).append(f"job {r['job_id']} {r['state']}")
    bases, repos = _bases(events), {}
    rows = [_plan(site, led, w, st, tgt, bases.get(w), repos, live, check_dirty) for w, st, tgt in chosen]
    _cite_rows(site, [r for r in rows if r["worktree"] and r["verdict"] in ("retire", "needs-approval")],
               lambda r: os.path.relpath(r["worktree"], site.root))
    for r in rows:
        if r.get("pinned") and r["verdict"] == "retire":
            r.update(verdict="needs-approval", reason=f"files in the worktree are pinned by sha256 ({', '.join(r['pinned'])});"
                     " retiring needs the Director's approval naming them (`--approve`, then `--approval <id>`)")
    return rows


def _cite_rows(site: Site, rows: list[dict], needle) -> None:
    """Attach `cited` (all citing lines) and `pinned` (those with a sha256) to each row; `cite_partial` when capped."""
    if not rows:
        return
    found, complete = citations(site, [needle(r) for r in rows])
    for r in rows:
        cs = found.get(needle(r).strip("/"), [])
        if cs:
            r["cited"] = cite_text(cs)
            pins = [c["where"] for c in cs if c["pinned"]]
            if pins:
                r["pinned"] = pins
        if not complete:
            r["cite_partial"] = True


def retire_approve(site: Site, actor: str, wis: list[str], memo: str, by: str) -> dict:
    """Record the Director's approval to retire these stopped or reverted items whose branches hold commits not in
    main, bound to each branch and its tip (like `purge --approve`)."""
    _desk(site, actor, "`worktree retire --approve`")
    if not wis:
        fail("--approve goes with --wi <WI>...")
    if not (memo or "").strip():
        fail("--approve needs --memo: where and when the Director approved (e.g. the board item or message)")
    led = Ledger(site)
    rows = plan_retire(site, led, wis, None)
    refused = [r for r in rows if r["verdict"] == "refused"]
    if refused:
        fail("refused, nothing recorded:\n" + "\n".join(f"{r['wi']}: {r['reason']}" for r in refused))
    need = [r for r in rows if r["verdict"] == "needs-approval"]
    if not need:
        fail("none of these items needs an approval (their branches are merged or empty); retire them without one")
    return led.append("decision", RETIRE_APPROVAL, actor, None, {
        "subject": f"retire {len(need)} item(s) with unmerged commits: {', '.join(r['wi'] for r in need)}",
        "disposition": "approve", "by": by, "memo": memo, "binding": _retire_binding(need)})


def retire(site: Site, actor: str, wis: list[str] | None = None, states: list[str] | None = None,
           approval: str | None = None, dry_run: bool = False, size_seconds: float = 60.0) -> dict:
    """`worktree retire`: remove finished WIs' worktrees (with their .venv, `git worktree remove`) and delete their
    `wi/*` branches. A branch is deleted only when it is merged into its repository's main head, or (stopped or
    reverted item) has nothing committed past its recorded base, or with an approval bound to its tip. Refused: an
    item that is not promoted, stopped or reverted, one with jobs not known to have ended, a dirty or broken worktree,
    a promoted item's unmerged branch. With --wi every item must pass or nothing is done; with --state the passing
    ones retire and the rest are listed. Records one `worktree.retired` event per item."""
    from .workflow import remove_wi_worktree

    _desk(site, actor, "`worktree retire`")
    led = Ledger(site)
    rows = plan_retire(site, led, wis, states)
    need = [r for r in rows if r["verdict"] == "needs-approval"]
    if approval:
        if not wis:
            fail("--approval goes with --wi <WI>... (the items it approves)")
        dec = led.get("decision", approval)
        if dec["type"] != RETIRE_APPROVAL or dec["payload"].get("disposition") != "approve":
            fail(f"decision {approval} is not a retire approval")
        if dec["payload"].get("binding") != _retire_binding(need):
            fail(f"decision {approval} does not approve exactly these unmerged branches at these tips")
        used = [e for e in led.events() if e["type"] == RETIRED and approval in (e.get("refs") or [])]
        if used:
            fail(f"decision {approval} was already used ({used[0]['id']})")
        for r in need:
            r.update(verdict="retire", approval=approval, reason=None)
    if dry_run:
        deadline = time.monotonic() + size_seconds
        partial = False
        for r in rows:
            if r["worktree"] and r["verdict"] in ("retire", "needs-approval"):
                r["size"], done = tree_size(Path(r["worktree"]), deadline)
                partial = partial or not done
        return {"rows": rows, "summary": _summary(rows, partial), "dry_run": True}
    if wis:
        blocked = [r for r in rows if r["verdict"] in ("refused", "needs-approval")]
        if blocked:
            fail("nothing retired; refused:\n" + "\n".join(f"{r['wi']}: {r['reason']}" for r in blocked))
    for r in rows:
        if r["verdict"] != "retire":
            continue
        try:
            root = target_root(site, r["target"])
            if r["worktree"]:
                remove_wi_worktree(site, led, r["wi"], Path(r["worktree"]), root, drop_branch=False)
            if r["branch"]:
                tip = git(["rev-parse", "--verify", "-q", f"refs/heads/{r['branch']}"], root, check=False)
                if tip and tip != r["head"]:
                    fail(f"{r['branch']} moved to {tip[:12]} since it was checked; kept")
                if tip:
                    git(["branch", "-D", r["branch"]], root)
        except DurehError as exc:
            r.update(verdict="error", reason=str(exc))
            continue
        ev = led.event(RETIRED, actor, r["wi"], {
            k: r.get(k) for k in ("status", "target", "worktree", "branch", "head", "merged", "empty", "approval")
            if r.get(k) is not None}, [r["approval"]] if r.get("approval") else None)
        r.update(verdict="retired", event=ev["id"])
    return {"rows": rows, "summary": _summary(rows, False), "dry_run": False}


def _summary(rows: list[dict], partial: bool) -> dict:
    out: dict = {}
    for r in rows:
        out[r["verdict"]] = out.get(r["verdict"], 0) + 1
    out["worktrees"] = sum(1 for r in rows if r["worktree"] and r["verdict"] in ("retire", "retired"))
    out["branches"] = sum(1 for r in rows if r["branch"] and r["verdict"] in ("retire", "retired"))
    sizes = [r["size"] for r in rows if r.get("size") is not None and r["verdict"] == "retire"]
    if sizes:
        out["bytes"] = sum(sizes)
    if partial:
        out["size_partial"] = True
    return out


def retire_lines(r: dict) -> list[str]:
    out = []
    for x in r["rows"]:
        if x["verdict"] == "nothing":
            continue
        size = f" {human(x['size'])}" if x.get("size") is not None else ""
        what = " + ".join(filter(None, [x["worktree"] and f"worktree{size}",
                                        x["branch"] and f"{x['branch']} ({'merged' if x['merged'] else 'empty' if x.get('empty') else 'unmerged'})"]))
        out.append(f"{x['wi']} {x['status']} {x['verdict']}: {what or '-'}" + (f" -- {x['reason']}" if x["reason"] else "")
                   + (f" [{x['cited']}]" if x.get("cited") else ""))
    s = r["summary"]
    counts = ", ".join(f"{k} {v}" for k, v in s.items() if k not in ("bytes", "size_partial", "worktrees", "branches"))
    word = "would retire" if r["dry_run"] else "retired"
    tail = f", {human(s['bytes'])}{' (partial: size scan capped)' if s.get('size_partial') else ''}" if "bytes" in s else ""
    out.append(f"summary: {word} {s['worktrees']} worktree(s) and {s['branches']} branch(es){tail}; {counts or 'nothing'}")
    return out


# ------------------------------------------------------- purge --scratch-wi
def _scratch_paths(site: Site, led: Ledger, wi: str, paths: list[str] | None) -> tuple[Path, list[tuple[str, Path]]]:
    """Validate a scratch purge: the WI is finished and has no live jobs; each path lies under `scratch/<WI>/`
    (whole directory without paths). Symlinks are never followed: `scratch/<WI>` itself must not be one, a path whose
    parent resolves outside it is refused, and a symlink path is removed as a link."""
    from .ops import live_job_runs
    from . import protect

    if not WI_NAME.match(wi or ""):
        fail(f"--scratch-wi takes a WI id, not {wi!r}")
    led.get("work-item", wi)
    status = led.wi_status(wi)["status"]
    if status not in RETIRABLE:
        fail(f"{wi} is {status}; purge --scratch-wi takes a promoted, stopped or reverted item only")
    live = live_job_runs(site, led, {wi}, led.events())
    if live:
        fail(f"{wi} still has scheduler jobs that are not known to have ended "
             f"({', '.join(r['job_id'] + ' ' + r['state'] for r in live)})")
    scratch = site.root / "scratch"
    base = scratch / wi
    if not os.path.lexists(base):
        fail(f"no scratch directory {base}")
    if base.is_symlink() or not base.is_dir():
        fail(f"refusing {base}: not a plain directory (a symlink is never followed)")
    real_base = os.path.realpath(base)
    if real_base != os.path.join(os.path.realpath(scratch), wi):
        fail(f"refusing {base}: it resolves to {real_base}")
    planned: list[tuple[str, Path]] = []
    for p in paths or ["."]:
        if not p or os.path.isabs(p):
            fail(f"--path takes a path relative to scratch/{wi}/, not {p!r}")
        if ".." in Path(p).parts:
            fail(f"refusing a path with `..`: {p}")
        norm = os.path.normpath(p)
        item = Path(real_base) if norm == "." else Path(real_base) / norm
        if not os.path.lexists(item):
            fail(f"no such path in scratch/{wi}/: {p}")
        parent = os.path.realpath(item.parent) if norm != "." else real_base
        if norm != "." and parent != real_base and not parent.startswith(real_base + os.sep):
            fail(f"refusing a path that resolves outside scratch/{wi}/: {p} (-> {os.path.join(parent, item.name)})")
        item = Path(parent) / item.name if norm != "." else item
        rel = "." if norm == "." else os.path.relpath(item, real_base)
        why = protect.write_refusal(site, base / rel if rel != "." else base)
        if why:
            fail(f"refusing {p}: {why}")
        planned.append((rel, item))
    rels = [r for r, _ in planned]
    for r in rels:
        for o in rels:
            if r != o and (o == "." or r.startswith(o + os.sep)):
                fail(f"overlapping paths: {r} lies inside {o}; give the outer one only")
    if len(set(rels)) != len(rels):
        fail("a path is given twice")
    return base, planned


def _manifest(site: Site, base: Path, rel: str, item: Path) -> dict:
    """The approval binding of one scratch path: every file (and symlink, as a link) under it by relative path, size
    and mtime (ns); the digest is sha256 over the sorted lines. Content hashes are not taken: a run directory can be
    100+ GB (2026-11-09: 162 GB in ten), so a change is caught by size or mtime, not by content."""
    from . import protect

    check = bool(protect.rules(site))
    lines: list[str] = []
    total = 0
    st = os.lstat(item)
    if not stat.S_ISDIR(st.st_mode):
        lines.append(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}")
        total = st.st_size
    else:
        for dirpath, dirnames, filenames in os.walk(item):
            for n in filenames + dirnames:
                full = os.path.join(dirpath, n)
                try:
                    s = os.lstat(full)
                except OSError:
                    continue
                if stat.S_ISDIR(s.st_mode):
                    continue
                r = os.path.relpath(full, os.path.realpath(base))
                if check:
                    why = protect.write_refusal(site, base / r)
                    if why:
                        fail(f"refusing {rel}: it holds {r}: {why}")
                lines.append(f"{r}\0{s.st_size}\0{s.st_mtime_ns}")
                total += s.st_size
            if check:
                why = protect.write_refusal(site, base / os.path.relpath(dirpath, os.path.realpath(base)))
                if why:
                    fail(f"refusing {rel}: {why}")
    lines.sort()
    digest = hashlib.sha256("\n".join(lines).encode("utf-8", "surrogateescape")).hexdigest()
    return {"path": rel, "files": len(lines), "bytes": total, "manifest": digest}


def _top(item: Path, deadline: float) -> list[dict]:
    """The entries directly under a directory with their sizes (largest first), for the dry run."""
    if item.is_symlink() or not item.is_dir():
        return []
    rows = []
    for e in sorted(os.scandir(item), key=lambda e: e.name):
        size, done = tree_size(Path(e.path), deadline)
        rows.append({"name": e.name + ("/" if e.is_dir(follow_symlinks=False) else ""), "size": size,
                     **({} if done else {"partial": True})})
    return sorted(rows, key=lambda r: -r["size"])


def _scratch_binding(wi: str, rows: list[dict]) -> dict:
    keys = ("path", "files", "bytes", "manifest", "pinned")
    return {"scratch_wi": wi, "paths": sorted(({k: r[k] for k in keys if r.get(k)} for r in rows),
                                              key=lambda r: r["path"])}


def _scratch_rows(site: Site, wi: str, base: Path, planned: list[tuple[str, Path]]) -> list[dict]:
    """The manifest rows of a scratch purge with the notes that cite each path (`cited`, `pinned`: see citations)."""
    rows = [_manifest(site, base, rel, item) for rel, item in planned]
    _cite_rows(site, rows, lambda r: f"scratch/{wi}" + ("" if r["path"] == "." else f"/{r['path']}"))
    return rows


def scratch_approve(site: Site, actor: str, wi: str, paths: list[str] | None, memo: str, by: str) -> dict:
    from .workflow import CACHE_APPROVAL

    _desk(site, actor, "purge")
    if not (memo or "").strip():
        fail("--approve needs --memo: where and when the Director approved (e.g. the board item or message)")
    led = Ledger(site)
    base, planned = _scratch_paths(site, led, wi, paths)
    rows = _scratch_rows(site, wi, base, planned)
    return led.append("decision", CACHE_APPROVAL, actor, wi, {
        "subject": f"delete {sum(r['files'] for r in rows)} file(s), {sum(r['bytes'] for r in rows)} bytes, from"
                   f" scratch/{wi}/ ({', '.join(r['path'] for r in rows)})",
        **({"pinned": [p for r in rows for p in r.get("pinned", [])]} if any(r.get("pinned") for r in rows) else {}),
        "disposition": "approve", "by": by, "memo": memo, "binding": _scratch_binding(wi, rows)})


def purge_scratch(site: Site, actor: str, wi: str, paths: list[str] | None, approval: str | None,
                  dry_run: bool = False, size_seconds: float = 60.0) -> dict:
    """`purge --scratch-wi <WI> [--path <p>...] --dry-run | --approval <id>`: delete `scratch/<WI>/` (or paths in it)
    of a finished WI. Fail-closed like `purge --project`: a `purge-approval` decision bound to the manifest (relative
    path, size and mtime of every file; see `_manifest`), used once. Records `scratch.purged`."""
    from .exectree import _rmtree
    from .workflow import CACHE_APPROVAL, CACHE_PURGED

    _desk(site, actor, "purge")
    led = Ledger(site)
    base, planned = _scratch_paths(site, led, wi, paths)
    rows = _scratch_rows(site, wi, base, planned)
    out = {"wi": wi, "dir": str(base), "paths": rows, "files": sum(r["files"] for r in rows),
           "bytes": sum(r["bytes"] for r in rows), "binding": "manifest of relative path + size + mtime (no content hash)"}
    if dry_run:
        deadline = time.monotonic() + size_seconds
        for (_rel, item), r in zip(planned, rows):
            r["top"] = _top(item, deadline)[:20]
        pp = " ".join(f"--path {r['path']}" for r in rows if r["path"] != ".")
        return {**out, "dry_run": True, "approval": approval,
                "next": f"the Director approves, then `purge --scratch-wi {wi} {pp} --approve --by Director --memo '...'`"
                        " records it and `--approval <decision>` deletes"}
    if not approval:
        fail(f"purge --scratch-wi deletes data: --approval <decision id> is required (record the Director's approval"
             f" with `purge --scratch-wi {wi} [--path ...] --approve --by Director --memo '...'`); there is no default")
    dec = led.get("decision", approval)
    if dec["type"] != CACHE_APPROVAL or dec["payload"].get("disposition") != "approve":
        fail(f"decision {approval} is not a purge approval")
    if dec["payload"].get("binding") != _scratch_binding(wi, rows):
        pins = [p for r in rows for p in r.get("pinned", [])]
        named = [p for b in dec["payload"].get("binding", {}).get("paths", []) for p in b.get("pinned", [])]
        extra = (f"; pinned by sha256 in {', '.join(p for p in pins if p not in named)}, which the approval does not"
                 " name") if any(p not in named for p in pins) else ""
        fail(f"decision {approval} does not approve deleting exactly these files (paths, sizes and mtimes must match)"
             + extra)
    used = [e for e in led.events() if e["type"] in (SCRATCH_PURGED, CACHE_PURGED) and approval in (e.get("refs") or [])]
    if used:
        fail(f"decision {approval} was already used ({used[0]['id']})")
    removed, left = [], None
    for (rel, item) in planned:
        if item.is_symlink() or item.is_file():
            item.unlink()
        else:
            _rmtree(item)
        if os.path.lexists(item):
            left = str(item)
            break
        removed.append(rel)
    ev = led.event(SCRATCH_PURGED, actor, wi, {"dir": str(base), "paths": rows, "files": out["files"],
                                               "bytes": out["bytes"], "removed": removed, "decision": approval,
                                               **({"failed": left} if left else {})}, [approval])
    if left:
        fail(f"could not remove {left} (removed so far: {', '.join(removed) or 'nothing'}); recorded {ev['id']}")
    return {**out, "removed": removed, "event": ev["id"], "dry_run": False}


# ------------------------------------------------------------ cleanup report
def report(site: Site, time_cap: float = 60.0, sizes: bool = True) -> dict:
    """Read-only (allowed with --observe): retire candidates (worktrees and merged branches of finished items),
    stopped or unmerged items to review, and `scratch/WI-*` directories of finished items with sizes. Nothing is
    written; no scheduler call; worktrees are not checked for uncommitted changes (`worktree retire --dry-run`
    does). Sizes stop at `time_cap` seconds (`partial`)."""
    led = Ledger(site)
    rows = plan_retire(site, led, None, list(RETIRABLE), check_dirty=False, probe=False)
    deadline = time.monotonic() + time_cap
    partial = False
    status = {w["id"]: w["status"] for w in led.work_items()}
    scratch_rows = []
    sd = site.root / "scratch"
    if sd.is_dir():
        for e in sorted(os.scandir(sd), key=lambda e: e.name):
            if not WI_NAME.match(e.name) or not e.is_dir(follow_symlinks=False):
                continue
            wi = e.name
            if status.get(wi) not in RETIRABLE:
                continue
            scratch_rows.append({"dir": f"scratch/{e.name}", "wi": wi, "status": status[wi], "size": None})
    if sizes:
        for r in rows:
            if r["worktree"] and r["verdict"] in ("retire", "needs-approval"):
                r["size"], done = tree_size(Path(r["worktree"]), deadline)
                partial = partial or not done
        for r in scratch_rows:
            r["size"], done = tree_size(site.root / r["dir"], deadline)
            partial = partial or not done
    _cite_rows(site, scratch_rows, lambda r: r["dir"])
    retire_rows = [r for r in rows if r["verdict"] == "retire"]
    review = [{k: r.get(k) for k in ("wi", "status", "worktree", "branch", "verdict", "reason", "size", "cited")}
              for r in rows if r["verdict"] in ("needs-approval", "refused")]
    out = {
        "site": site.root.name,
        "retire": {"items": len(retire_rows), "worktrees": sum(1 for r in retire_rows if r["worktree"]),
                   "branches": sum(1 for r in retire_rows if r["branch"]),
                   "merged_branches": sum(1 for r in retire_rows if r["branch"] and r["merged"]),
                   "bytes": sum(r.get("size") or 0 for r in retire_rows) if sizes else None,
                   "wis": [r["wi"] for r in retire_rows]},
        "review": review,
        "scratch": {"dirs": scratch_rows, "bytes": sum(r["size"] or 0 for r in scratch_rows) if sizes else None},
        "partial": partial, "time_cap": time_cap,
    }
    out["summary"] = summary_line(out)
    return out


def summary_line(r: dict) -> str:
    ret, sc = r["retire"], r["scratch"]
    size = lambda b: f" ({human(b)})" if b is not None else ""  # noqa: E731
    line = (f"cleanup: {ret['worktrees']} worktree(s) and {ret['branches']} branch(es) of finished items to retire"
            f"{size(ret['bytes'])}; {len(r['review'])} stopped or unmerged item(s) to review; {len(sc['dirs'])}"
            f" scratch/WI-* dir(s) of finished items{size(sc['bytes'])}")
    if r["partial"]:
        line += f"; partial: size scan capped at {r['time_cap']:.0f} s"
    return line


def report_lines(r: dict) -> list[str]:
    out = [r["summary"]]
    if r["retire"]["wis"]:
        out.append("retire (`worktree retire --all-promoted` or `--wi`): " + " ".join(r["retire"]["wis"]))
    for x in r["review"]:
        out.append(f"review {x['wi']} {x['status']} {x['verdict']}: {x['reason']}")
    for x in r["scratch"]["dirs"]:
        out.append(f"scratch {x['dir']} ({x['wi']} {x['status']}) {human(x['size']) if x['size'] is not None else ''}"
                   " -- `purge --scratch-wi` with the Director's approval" + (f" [{x['cited']}]" if x.get("cited") else ""))
    return out


def close_step(site: Site, actor: str, time_cap: float = 20.0) -> dict:
    """At `day close`: the report's one-line summary and, with config `cleanup.retire_on_close` true (default
    false), `worktree retire --all-promoted`. Never refuses the close: an error becomes part of the summary.
    Scratch deletion is never automatic."""
    out: dict = {}
    if (site.config.get("cleanup") or {}).get("retire_on_close") is True:
        try:
            r = retire(site, actor, states=["promoted"])
            out["retired"] = [x["wi"] for x in r["rows"] if x["verdict"] == "retired"]
            errors = [f"{x['wi']}: {x['reason']}" for x in r["rows"] if x["verdict"] == "error"]
            if errors:
                out["retire_errors"] = errors
        except DurehError as exc:
            out["retire_errors"] = [str(exc)]
    try:
        out["summary"] = report(site, time_cap=time_cap)["summary"]
    except (DurehError, OSError) as exc:
        out["summary"] = f"cleanup: report failed ({exc})"
    if "retired" in out:
        out["summary"] = f"retired {len(out['retired'])} promoted item(s) at close; " + out["summary"]
    return out
