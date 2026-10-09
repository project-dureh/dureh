"""Protected paths (release 46, proposals/boundary-design-2026-10-08.md 3.6).

A package declares site-relative, segment-aware globs with a level:
  read-only  no agent writes there (Write/Edit/NotebookEdit and Bash writes through the write guard)
  no-read    also no content reads: Read and Grep with a path inside, and Bash content readers (cat, head, grep,
             python <file>, cp from it, ...) by the same best-effort word scan the Bash guard does
Listing names (Glob, `ls`) stays allowed. Every role is bound, the desk included. Harness commands (scheduler jobs,
`path-refs`) are not agent reads or writes; their own rules stay. The harness itself declares no protected path: until
a package declares some, upstream applies its interim default (workspace rule 2), which is not a harness rule.
"""
from __future__ import annotations

import os
from pathlib import Path

from .org import scope_match
from .util import is_within

# Bash commands that read file contents (checked against no-read areas); listing (`ls`, `find`, `du`) is not reading
CONTENT_READERS = {"cat", "tac", "head", "tail", "less", "more", "nl", "od", "xxd", "hexdump", "strings", "grep",
                   "egrep", "fgrep", "rg", "ag", "awk", "gawk", "sed", "cut", "sort", "uniq", "wc", "diff", "cmp",
                   "md5sum", "sha1sum", "sha256sum", "base64", "zcat", "gzip", "gunzip", "bzcat", "xzcat", "tar",
                   "zip", "unzip", "cp", "rsync", "scp", "python", "python3", "perl", "ruby", "node", "Rscript",
                   "bash", "sh", "source", ".", "jq", "yq", "file", "vim", "vi", "nano", "view", "iconv", "tr", "paste",
                   "join", "split", "dd"}


def rules(site) -> list[dict]:
    return site.protected()


def _site_rel(site, path) -> str | None:
    p = Path(os.path.abspath(path))
    for cand in (p, p.resolve(strict=False)):
        if is_within(cand, site.root):
            return cand.relative_to(site.root).as_posix()
    return None


def _candidates(site, path, logical: str | None) -> list[str]:
    """The site-relative forms a path is matched in: its own, and inside a WI worktree the logical target path
    (`worktrees/WI-1/inputs/x` of a projects/p item is `projects/p/inputs/x`)."""
    out = []
    rel = _site_rel(site, path)
    if rel:
        out.append(rel)
    if logical and logical not in out:
        out.append(logical)
    return out


def _matches(forms: list[str], glob: str) -> bool:
    for f in forms:
        if scope_match(f, glob):
            return True
        # the area itself (a directory named by the glob's fixed part) is inside it too
        if glob.endswith("/**") and scope_match(f, glob[:-3]):
            return True
    return False


def rule_for(site, path, logical: str | None = None, levels=("read-only", "no-read")) -> dict | None:
    rs = [r for r in rules(site) if r.get("level") in levels]
    if not rs:
        return None
    forms = _candidates(site, path, logical)
    for r in rs:
        if _matches(forms, r["path"]):
            return r
    return None


def write_refusal(site, path, logical: str | None = None) -> str | None:
    """Why a write to `path` is refused by a protected rule (both levels forbid writes), or None."""
    r = rule_for(site, path, logical)
    if r is None:
        return None
    return (f"{r['path']} is protected ({r['level']}, declared by the package: {r.get('why') or 'no reason given'});"
            " no role writes there")


def read_refusal(site, path, logical: str | None = None) -> str | None:
    r = rule_for(site, path, logical, levels=("no-read",))
    if r is None:
        return None
    return (f"{r['path']} is protected (no-read, declared by the package: {r.get('why') or 'no reason given'});"
            " its contents are not read by any agent (listing names is allowed)")


def has_no_read(site) -> bool:
    return any(r.get("level") == "no-read" for r in rules(site))


def listing(site) -> dict:
    """`dureh protected list`: every rule with its level and source (the package id and revision)."""
    cfg = site.config.get("startup") or {}
    source = {"package": cfg.get("id"), "version": cfg.get("version"), "rev": cfg.get("rev")}
    rs = [{**r, "source": source} for r in rules(site)]
    return {"rules": rs, "source": source,
            "note": None if rs else "none declared: interim default (workspace rule 2)"}


def render_listing(out: dict) -> str:
    if not out["rules"]:
        return f"Protected paths: {out['note']}"
    src = out["source"]
    tag = f"{src.get('package')} {src.get('version') or ''} rev {str(src.get('rev') or 'unpinned')[:12]}".replace("  ", " ")
    lines = [f"Protected paths ({len(out['rules'])}, from package {tag}):"]
    for r in out["rules"]:
        lines.append(f"- {r['path']}  {r['level']}" + (f"  ({r['why']})" if r.get("why") else ""))
    return "\n".join(lines)
