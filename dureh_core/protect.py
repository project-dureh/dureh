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


def specificity(glob: str) -> list:
    """Segment-aware specificity of a rule's glob, compared segment by segment from the left: a literal segment beats
    one with a wildcard, which beats `*`, which beats `**`; at an equal prefix the longer glob is more specific. The
    most specific matching rule wins; on a tie the later rule wins."""
    def rank(seg: str) -> int:
        if seg == "**":
            return 0
        if seg == "*":
            return 1
        return 2 if any(c in seg for c in "*?[") else 3

    return [rank(g) for g in glob.split("/")]


def best_rule(rs: list[dict], forms: list[str]) -> dict | None:
    best, key = None, None
    for r in rs:
        if _matches(forms, r["path"]):
            k = specificity(r["path"])
            if key is None or k >= key:  # >=: on a tie the later rule wins
                best, key = r, k
    return best


LINKS_REL = ".claude/dureh-protected-links.json"  # generated area: written by `adapter sync` and install
LINK_WALK_MAX_DEPTH = 8
LINK_WALK_MAX_ENTRIES = 20000
LINK_MAX = 500


def stored_links(site) -> list[dict]:
    """The resolved symlink targets of protected areas recorded at the last `adapter sync`/install."""
    cache = getattr(site, "_protected_links", None)
    if cache is None:
        p = site.root / LINKS_REL
        try:
            import json

            cache = json.loads(p.read_text(encoding="utf-8")).get("links") or [] if p.is_file() else []
        except (OSError, ValueError):
            cache = []
        site._protected_links = cache
    return cache


def rule_for(site, path, logical: str | None = None, levels=("read-only", "no-read")) -> dict | None:
    """The rule that decides `path`: the most specific of the package's rules matching the path as given, its
    resolved real path or (inside a WI worktree) its logical target path, else a stored symlink prefix the real
    path lies under. Returned only when its level is one of `levels` (a `none` rule lifts the package's own rules)."""
    rs = rules(site)
    if not rs:
        return None
    r = best_rule(rs, _candidates(site, path, logical))
    if r is None:
        real = Path(os.path.realpath(os.path.abspath(path)))
        hits = [x for x in stored_links(site) if is_within(real, Path(x["target"]))]
        if hits:
            x = max(hits, key=lambda x: len(x["target"]))
            r = {"path": x["rule"], "level": x["level"], "why": f"{x.get('why') or ''} (via the symlink {x['link']})".strip()}
    return r if r is not None and r.get("level") in levels else None


def _expand(base: Path, segs: list[str], depth: int, budget: list[int]):
    """Directories and files under `base` matching the glob segments, by listing names only (bounded)."""
    from fnmatch import fnmatchcase

    if budget[0] <= 0 or depth > LINK_WALK_MAX_DEPTH:
        return
    if not segs:
        yield base
        return
    seg = segs[0]
    if seg == "**":
        yield from _expand(base, segs[1:], depth, budget)
    try:
        names = sorted(os.listdir(base)) if base.is_dir() and not base.is_symlink() else []
    except OSError:
        return
    for name in names:
        budget[0] -= 1
        if budget[0] <= 0:
            return
        child = base / name
        if seg == "**":
            if child.is_dir() and not child.is_symlink():
                yield from _expand(child, segs, depth + 1, budget)
        elif fnmatchcase(name, seg):
            yield from _expand(child, segs[1:], depth + 1, budget)


def resolve_links(site) -> list[dict]:
    """For each protected area (rules other than `none`), the symlinks inside it and their resolved real paths:
    a names-only walk (no file is opened), bounded in depth, entries and links. A link whose own site path a more
    specific `none` rule lifts is skipped. [{rule, level, why, link, target}]"""
    rs = rules(site)
    out: list[dict] = []
    seen: set[tuple] = set()
    budget = [LINK_WALK_MAX_ENTRIES]
    for r in rs:
        if r.get("level") == "none":
            continue
        glob = r["path"][:-3] if r["path"].endswith("/**") else r["path"]
        for area in _expand(site.root, glob.split("/"), 0, budget):
            todo = [area]
            if not area.is_symlink() and area.is_dir():
                for base, dirs, files in os.walk(area, followlinks=False):
                    if len(Path(base).relative_to(area).parts) >= LINK_WALK_MAX_DEPTH or budget[0] <= 0:
                        dirs[:] = []
                    budget[0] -= len(dirs) + len(files)
                    todo += [Path(base) / n for n in dirs + files if (Path(base) / n).is_symlink()]
            for link in todo:
                if not link.is_symlink() or len(out) >= LINK_MAX:
                    continue
                rel = link.relative_to(site.root).as_posix()
                decider = best_rule(rs, [rel])
                if decider is not None and decider.get("level") == "none":
                    continue
                target = os.path.realpath(link)
                key = (rel, target)
                if key in seen or is_within(Path(target), site.root):
                    continue  # a target inside the site is matched by its own site path
                seen.add(key)
                out.append({"rule": r["path"], "level": r["level"], "why": r.get("why"), "link": rel, "target": target})
    return out


def links_file_text(site) -> str | None:
    """The generated file of resolved protected symlink targets, or None when the package declares no rule."""
    import json

    if not [r for r in rules(site) if r.get("level") != "none"]:
        return None
    return json.dumps({"generator": "dureh protect.resolve_links", "links": resolve_links(site)}, indent=2,
                      sort_keys=True) + "\n"


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
    return {"rules": rs, "source": source, "links": stored_links(site),
            "note": None if rs else "none declared: interim default (workspace rule 2)"}


def render_listing(out: dict) -> str:
    if not out["rules"]:
        return f"Protected paths: {out['note']}"
    src = out["source"]
    tag = f"{src.get('package')} {src.get('version') or ''} rev {str(src.get('rev') or 'unpinned')[:12]}".replace("  ", " ")
    lines = [f"Protected paths ({len(out['rules'])}, from package {tag}):"]
    for r in out["rules"]:
        lines.append(f"- {r['path']}  {r['level']}" + (f"  ({r['why']})" if r.get("why") else ""))
    if out.get("links"):
        lines.append(f"Resolved symlink targets ({len(out['links'])}, recorded at the last adapter sync):")
        for x in out["links"]:
            lines.append(f"- {x['target']}  {x['level']}  (link {x['link']}, rule {x['rule']})")
    return "\n".join(lines)
