#!/usr/bin/env python3
"""Cut and prune DUREH harness releases (decision D1).

A release is a tagged revision checked out read-only under `<releases>/dureh-<tag>/`, a plain clone
of the harness repository whose `origin` is the harness remote. Sites run that checkout (their
`bin/dureh` execs `<release>/dureh`), so editing, checking out or rebasing the integration clone never
changes a running company. Harness WIs of a site branch from the release it runs; their
`contrib/c-<8 hex>` branches end up in that release clone, where upstream fetches them.

    tools/release.py cut [--rev REV]            tag REV (default HEAD) as rYYYY.MM.DD-N, push the tag,
                                                create <releases>/dureh-<tag>/ and make it read-only
    tools/release.py list                       releases, their tags and which sites run them
    tools/release.py prune [--dry-run]          delete releases no site runs, keeping for each release
                                                in use the one cut just before it

Defaults: the repository is the one holding this script; `<releases>` is `<workspace>/releases` where
`<workspace>` is the parent of `repos/`; sites are `<workspace>/../DUREH-Companies/*`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

TAG_RE = re.compile(r"^r(\d{4})\.(\d{2})\.(\d{2})-(\d+)$")
REPO = Path(__file__).resolve().parent.parent


def git(args: list[str], cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        sys.exit(f"release: git {' '.join(args)} failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def tag_key(tag: str) -> tuple:
    m = TAG_RE.match(tag)
    return (int(m[1]), int(m[2]), int(m[3]), int(m[4])) if m else (0, 0, 0, 0)


def next_tag(repo: Path, today: dt.date) -> str:
    prefix = f"r{today:%Y.%m.%d}-"
    git(["fetch", "--tags", "--quiet", "origin"], repo, check=False)
    taken = [tag_key(t) for t in git(["tag", "--list", prefix + "*"], repo).splitlines() if TAG_RE.match(t)]
    return f"{prefix}{max((k[3] for k in taken), default=0) + 1}"


def set_readonly(root: Path) -> None:
    """Working tree files and directories lose write bits; .git keeps them (worktrees and branches need it)."""
    for p in sorted(root.rglob("*"), reverse=True):
        if ".git" in p.relative_to(root).parts:
            continue
        p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    root.chmod(root.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def set_writable(root: Path) -> None:
    for p in [root, *root.rglob("*")]:
        if p.is_symlink():
            continue
        p.chmod(p.stat().st_mode | stat.S_IWUSR)


def releases_in(releases: Path) -> list[Path]:
    return sorted((p for p in releases.glob("dureh-r*") if (p / ".git").exists()),
                  key=lambda p: tag_key(p.name[len("dureh-"):]))


def sites_running(sites: Path) -> dict[Path, list[str]]:
    """release root -> site names, from each site's config.json harness.root."""
    out: dict[Path, list[str]] = {}
    for cfg in sorted(sites.glob("*/config.json")):
        try:
            root = Path(json.loads(cfg.read_text(encoding="utf-8"))["harness"]["root"]).resolve()
        except (OSError, KeyError, ValueError, TypeError):
            continue
        out.setdefault(root, []).append(cfg.parent.name)
    return out


def attached(release: Path) -> list[str]:
    """Worktrees other than the checkout itself, and local branches: work a site still holds."""
    wts = [ln[9:] for ln in git(["worktree", "list", "--porcelain"], release).splitlines() if ln.startswith("worktree ")]
    branches = [b.strip("* ") for b in git(["branch", "--list"], release).splitlines() if b.strip()]
    return [f"worktree {w}" for w in wts[1:]] + [f"branch {b}" for b in branches if b != "(no branch)" and not b.startswith("(HEAD")]


def cmd_cut(a) -> None:
    repo, releases = a.repo, a.releases
    if git(["status", "--porcelain"], repo):
        sys.exit(f"release: {repo} has uncommitted changes; commit or stash them first")
    rev = git(["rev-parse", "--verify", f"{a.rev}^{{commit}}"], repo)
    remote = git(["remote", "get-url", "origin"], repo)
    if not git(["branch", "-r", "--contains", rev], repo):
        sys.exit(f"release: {rev[:12]} is on no remote branch; push it first so the tag is fetchable from {remote}")
    existing = [t for t in git(["tag", "--points-at", rev], repo).splitlines() if TAG_RE.match(t)]
    tag = existing[0] if existing else next_tag(repo, dt.datetime.now(dt.timezone.utc).date())
    dest = releases / f"dureh-{tag}"
    if dest.exists():
        sys.exit(f"release: {dest} already exists")
    if not existing:
        git(["tag", "-a", tag, rev, "-m", f"DUREH harness release {tag}"], repo)
        git(["push", "origin", tag], repo)
    releases.mkdir(parents=True, exist_ok=True)
    git(["clone", "--quiet", "--branch", tag, str(repo), str(dest)], releases)
    git(["remote", "set-url", "origin", remote], dest)
    git(["checkout", "--quiet", "--detach", tag], dest)
    set_readonly(dest)
    print(f"release {tag} = {rev[:12]} at {dest} (read-only; tag pushed to {remote})")
    print("next, in a merge window, for each site that should run it:")
    print(f"  <site>/bin/dureh adapter sync --harness {dest}")
    print("  <site>/bin/dureh verify --self")


def cmd_list(a) -> None:
    running = sites_running(a.sites) if a.sites.is_dir() else {}
    for rel in releases_in(a.releases):
        head = git(["rev-parse", "--short=12", "HEAD"], rel)
        who = ", ".join(running.get(rel.resolve(), [])) or "-"
        held = "; ".join(attached(rel)) or "-"
        print(f"{rel.name[len('dureh-'):]}\t{head}\t{rel}\tsites: {who}\tattached: {held}")
    other = {r: s for r, s in running.items() if r.parent != a.releases.resolve()}
    for root, names in other.items():
        print(f"(not a release)\t{root}\tsites: {', '.join(names)}")


def cmd_prune(a) -> None:
    rels = releases_in(a.releases)
    running = sites_running(a.sites) if a.sites.is_dir() else {}
    keep: set[Path] = set()
    for i, rel in enumerate(rels):
        if rel.resolve() in running:
            keep.add(rel)
            if i > 0:
                keep.add(rels[i - 1])
    if not any(rel.resolve() in running for rel in rels):
        sys.exit(f"release: no site under {a.sites} runs a release in {a.releases}; nothing pruned")
    for rel in rels:
        if rel in keep:
            print(f"keep   {rel.name}" + (f" (runs: {', '.join(running[rel.resolve()])})" if rel.resolve() in running else " (previous)"))
            continue
        held = attached(rel)
        if held:
            print(f"hold   {rel.name}: still holds {', '.join(held)}; purge those WIs first")
            continue
        if a.dry_run:
            print(f"would delete {rel.name}")
            continue
        set_writable(rel)
        shutil.rmtree(rel)
        print(f"deleted {rel.name}")


def main(argv=None) -> int:
    ws = REPO.parent.parent if REPO.parent.name == "repos" else None
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", type=Path, default=REPO, help="harness repository to tag and clone from")
    p.add_argument("--releases", type=Path, default=(ws / "releases") if ws else None, help="directory holding dureh-<tag>/")
    p.add_argument("--sites", type=Path, default=(ws.parent / "DUREH-Companies") if ws else None,
                   help="directory of company sites (their config.json names the harness they run)")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("cut", help="tag a revision, push the tag and create its read-only checkout")
    c.add_argument("--rev", default="HEAD")
    c.set_defaults(func=cmd_cut)
    sub.add_parser("list", help="releases, their tags and which sites run them").set_defaults(func=cmd_list)
    d = sub.add_parser("prune", help="delete releases no site runs (keeps each in-use release and the one before it)")
    d.add_argument("--dry-run", action="store_true")
    d.set_defaults(func=cmd_prune)
    a = p.parse_args(argv)
    if a.releases is None or (a.command == "prune" and a.sites is None):
        sys.exit("release: --releases (and --sites for prune) are required when the repository is not under <workspace>/repos/")
    a.repo, a.releases = a.repo.resolve(), a.releases.resolve()
    a.sites = a.sites.resolve() if a.sites else Path("/nonexistent")
    os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
    a.func(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
