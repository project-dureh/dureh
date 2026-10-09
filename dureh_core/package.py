"""The startup package interface (startup.yaml), release 46: schema 2 loading and validation.

A package declares everything that is about one company or one domain: its roles (kind, tier, isolation, write
scopes, capabilities, skill, instructions), protected paths, labels (item levels and grammar, layout words), loop
policy text, project template and config defaults. The harness knows roles only by kind (desk, manager, worker) and
by capability (CAPABILITIES, a closed set). A package can tighten what the harness allows; it never loosens an
invariant (proposals/boundary-design-2026-10-08.md sections 3 and 5).

Schema 1 (and its `organization` enable list) loaded in release 46 only; from release 49 it is refused with a pointer
to schema 2, as are unknown top-level keys (design 3.4 item 1).
"""
from __future__ import annotations

import re
from pathlib import Path

from . import HARNESS_RELEASE, yamlsub
from .util import fail

SCHEMA_VERSIONS = (2,)
CAPABILITIES = ("submit_jobs", "verify", "network")
RESERVED_ROLES = ("desk", "manager", "worker", "checker")
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
WORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROTECT_LEVELS = ("no-read", "read-only", "none")  # none: lifts the package's own rules (most specific wins)
LAYOUT_KEYS = ("staging", "final", "reports")
# the harness's own layout words when a package names none (`final` has no harness default)
DEFAULT_LAYOUT = {"staging": "checkpoints", "final": None, "reports": "reports"}
# top-level keys of a schema-2 startup.yaml; anything else is refused (warned in releases 46-48)
KNOWN_KEYS = ("schema_version", "package", "desk", "roles", "values", "skills", "policies", "protected", "labels",
              "templates", "guards", "config_defaults", "lessons", "bootstrap", "dependencies", "state", "projects",
              "scheduler_tools")
SCHEMA1_REFUSAL = ("startup.yaml schema 1 is no longer supported (it loaded in release 46 only): move the package to"
                   " schema 2 (`schema_version: 2`, package `roles`, `labels`, `protected`; docs/config.md)")
ORGANIZATION_REFUSAL = ("startup.yaml organization (the managers/workers enable list) is no longer read (release 46"
                        " only): the package's `roles` is authoritative; remove the key")
# commands no agent runs directly, whatever a package declares (the Bash guard's always-deny list and the harness's
# scheduler-only defaults): a scheduler tool's command may not start with one of them
HARNESS_DIRECT_COMMANDS = ("sbatch", "srun", "salloc", "scancel", "nohup", "setsid", "disown", "screen", "tmux",
                           "apptainer", "singularity", "docker", "podman", "dureh", "bin/dureh", "dureh.py")
ROLE_KEYS = ("kind", "tier", "description", "output", "never", "isolation", "write_scopes", "may_dispatch",
             "capabilities", "project", "skill", "instructions", "value")
CHECKER_RESKIN_KEYS = ("skill", "instructions", "description", "output", "never", "project")
SCOPE_PREFIXES = ("modules/", "projects/", "package/", "harness/")

def schema_of(manifest: dict | None) -> int:
    raw = (manifest or {}).get("schema_version", 1)
    try:
        return int(raw)
    except (TypeError, ValueError):
        fail(f"startup.yaml schema_version must be 2, not {raw!r}")


def require_schema2(manifest: dict | None) -> None:
    """Refuse a manifest this harness no longer loads: schema 1 (or none), the `organization` enable list."""
    schema = schema_of(manifest)
    if schema == 1:
        fail(SCHEMA1_REFUSAL)
    if schema not in SCHEMA_VERSIONS:
        fail(f"startup.yaml schema_version {schema} is not supported by this harness (2)")
    if "organization" in (manifest or {}):
        fail(ORGANIZATION_REFUSAL)


def _glob_ok(glob: str) -> bool:
    """A segment-aware glob: relative, no empty, `.` or `..` segments, `**` only as a whole segment."""
    if not isinstance(glob, str) or not glob or glob.startswith("/") or glob.endswith("/"):
        return False
    for seg in glob.split("/"):
        if seg in ("", ".", "..") or ("**" in seg and seg != "**"):
            return False
    return True


def _seg_overlap(a: str, b: str) -> bool:
    """Can one path segment match both segment globs? Exact when either side has no wildcard; otherwise a
    conservative check on the literal prefix and suffix of each side."""
    from fnmatch import fnmatchcase

    wild = lambda s: any(c in s for c in "*?[")  # noqa: E731
    if not wild(a) or not wild(b):
        return fnmatchcase(a, b) if not wild(a) else fnmatchcase(b, a)

    def ends(s):
        i = min([s.index(c) for c in "*?[" if c in s])
        j = max([s.rindex(c) for c in "*?]" if c in s])
        return s[:i], s[j + 1:]

    pa, sa = ends(a)
    pb, sb = ends(b)
    return (pa.startswith(pb) or pb.startswith(pa)) and (sa.endswith(sb) or sb.endswith(sa))


def globs_overlap(a: str, b: str) -> bool:
    """Whether some path can match both segment-aware globs (`**` spans any number of segments)."""
    x, y = a.split("/"), b.split("/")

    def go(i: int, j: int) -> bool:
        if i == len(x) and j == len(y):
            return True
        if i < len(x) and x[i] == "**":
            return go(i + 1, j) or (j < len(y) and go(i, j + 1)) or (j < len(y) and y[j] == "**" and go(i + 1, j + 1))
        if j < len(y) and y[j] == "**":
            return go(i, j + 1) or (i < len(x) and go(i + 1, j))
        if i == len(x) or j == len(y):
            return False
        return _seg_overlap(x[i], y[j]) and go(i + 1, j + 1)

    return go(0, 0)


def labels(manifest: dict | None) -> dict:
    """{levels: [finest, ..., coarsest], layout: {staging, final, reports}, item_grammar: {level: compiled regex}}.
    The package's `labels` over DEFAULT_LAYOUT (no levels, no grammar by default)."""
    raw = (manifest or {}).get("labels") or {}
    layout = dict(DEFAULT_LAYOUT)
    layout.update({k: v for k, v in (raw.get("layout") or {}).items() if v})
    return {"levels": list(raw.get("levels") or []), "layout": layout,
            "item_grammar": {lv: re.compile(rx) for lv, rx in (raw.get("item_grammar") or {}).items()}}


def protected_rules(manifest: dict | None) -> list[dict]:
    """The package's protected paths [{path, level, why}]."""
    if manifest is None:
        return []
    return [dict(r) for r in manifest.get("protected") or []]


def loop_policy(manifest: dict | None) -> dict:
    """`policies.loop_rule` {text, refuse}: the package's own loop rule wording (desk/agent text, refusal text)."""
    if manifest is None:
        return {}
    return dict(((manifest.get("policies") or {}).get("loop_rule") or {}))


def _check_labels(raw) -> None:
    if raw in (None, {}):
        return
    if not isinstance(raw, dict) or set(raw) - {"levels", "layout", "item_grammar"}:
        fail("startup.yaml labels takes only levels, layout and item_grammar")
    levels = raw.get("levels") or []
    if not isinstance(levels, list) or not all(isinstance(x, str) and WORD_RE.match(x) for x in levels) \
            or len(set(levels)) != len(levels) or {"any", "plain", "group", "single"} & set(levels):
        fail("startup.yaml labels.levels must be a list of distinct words (finest first); any, plain, group and "
             "single are reserved")
    layout = raw.get("layout") or {}
    if not isinstance(layout, dict) or set(layout) - set(LAYOUT_KEYS):
        fail(f"startup.yaml labels.layout takes only {', '.join(LAYOUT_KEYS)}")
    for k, v in layout.items():
        if not isinstance(v, str) or not WORD_RE.match(v):
            fail(f"startup.yaml labels.layout.{k} must be one directory name: {v!r}")
    grammar = raw.get("item_grammar") or {}
    if not isinstance(grammar, dict):
        fail("startup.yaml labels.item_grammar must map a level to a regular expression")
    for lv, rx in grammar.items():
        if lv not in levels:
            fail(f"startup.yaml labels.item_grammar.{lv}: not a level in labels.levels")
        try:
            pat = re.compile(str(rx))
        except re.error as exc:
            fail(f"startup.yaml labels.item_grammar.{lv}: not a regular expression: {exc}")
        bad = [g for g in pat.groupindex if g in ("item", "by", "index")]
        if bad:
            fail(f"startup.yaml labels.item_grammar.{lv}: group name {bad[0]!r} is reserved")


def _check_protected(raw) -> None:
    if raw in (None, []):
        return
    if not isinstance(raw, list):
        fail("startup.yaml protected must be a list of {path, level, why}")
    for i, r in enumerate(raw):
        where = f"startup.yaml protected[{i}]"
        if not isinstance(r, dict) or set(r) - {"path", "level", "why"} or not r.get("path"):
            fail(f"{where} must be {{path: <site-relative glob>, level: no-read | read-only, why: <text>}}")
        if not _glob_ok(str(r["path"])):
            fail(f"{where}.path {r['path']!r} is not a site-relative, segment-aware glob")
        if r.get("level") not in PROTECT_LEVELS:
            fail(f"{where}.level must be no-read, read-only or none, not {r.get('level')!r}")


def scheduler_tools(manifest: dict | None) -> list[dict]:
    """The package's scheduler tools [{name, commands, exclude, job_match, description}] (Director 2026-10-09,
    compute route B): the company's own commands that schedule its own compute."""
    if manifest is None:
        return []
    return [dict(t) for t in manifest.get("scheduler_tools") or []]


def _check_scheduler_tools(raw) -> None:
    from fnmatch import fnmatchcase

    if raw in (None, []):
        return
    if not isinstance(raw, list):
        fail("startup.yaml scheduler_tools must be a list of {name, commands, exclude, job_match, description}")
    names = set()
    for i, t in enumerate(raw):
        where = f"startup.yaml scheduler_tools[{i}]"
        if not isinstance(t, dict) or set(t) - {"name", "commands", "exclude", "job_match", "description"}:
            fail(f"{where} must be {{name, commands, exclude, job_match, description}}")
        if not isinstance(t.get("name"), str) or not WORD_RE.match(t["name"]) or t["name"] in names:
            fail(f"{where}.name must be a distinct word")
        names.add(t["name"])
        cmds = t.get("commands")
        if not isinstance(cmds, list) or not cmds or not all(isinstance(c, str) and c.split() for c in cmds):
            fail(f"{where}.commands must be a non-empty list of Bash command globs, e.g. [\"<tool> workflow *\"]")
        for cmd in cmds:
            first = cmd.split()[0]
            if any(c in first for c in "*?["):
                fail(f"{where}.commands: {cmd!r} must start with a command word, not {first!r}")
            base = first.rsplit("/", 1)[-1]
            if any(fnmatchcase(base, h) or fnmatchcase(first, h) for h in HARNESS_DIRECT_COMMANDS):
                fail(f"{where}.commands: {cmd!r} may not start with {first!r}: agents never run it directly (harness rule)")
        ex = t.get("exclude") or []
        if not isinstance(ex, list) or not all(isinstance(c, str) and c.strip() for c in ex):
            fail(f"{where}.exclude must be a list of Bash command globs")
        jm = t.get("job_match")
        if not isinstance(jm, list) or not jm or not all(isinstance(x, str) and x.strip() for x in jm):
            fail(f"{where}.job_match must be a non-empty list of job-name globs")


def _check_policies(raw) -> None:
    if raw in (None, {}):
        return
    if not isinstance(raw, dict) or set(raw) - {"loop_rule"}:
        fail("startup.yaml policies takes only loop_rule")
    lr = raw.get("loop_rule") or {}
    if not isinstance(lr, dict) or set(lr) - {"text", "refuse"} or not all(isinstance(v, str) for v in lr.values()):
        fail("startup.yaml policies.loop_rule must be {text: <desk/agent text>, refuse: <refusal text>}")


def _inside(root: Path, rel, what: str) -> None:
    p = root / str(rel)
    try:
        p.resolve().relative_to(root.resolve())
    except ValueError:
        fail(f"startup.yaml {what} {rel!r} lies outside the package")
    if p.is_symlink() or not p.exists():
        fail(f"startup.yaml names a missing {what}: {rel}")


def check(manifest: dict, root: Path | None = None) -> list[str]:
    """Validate a startup manifest (install, `adapter sync`, `verify --self`); returns warnings, fails on errors.
    Roles are checked by `Org.load(manifest=...)`, which the caller runs too."""
    if not isinstance(manifest, dict):
        fail("startup.yaml must be a mapping")
    warnings: list[str] = []
    require_schema2(manifest)
    pkg = manifest.get("package")
    if not isinstance(pkg, dict) or not pkg.get("id") or not pkg.get("name"):
        fail("startup.yaml must declare package.id and package.name")
    unknown = sorted(set(manifest) - set(KNOWN_KEYS))
    if unknown:
        fail(f"startup.yaml: unknown top-level key(s) {', '.join(unknown)} (refused since release 49; known keys:"
             f" {', '.join(KNOWN_KEYS)})")
    hmin = pkg.get("harness_min")
    if hmin is not None:
        try:
            need = int(hmin)
        except (TypeError, ValueError):
            fail(f"startup.yaml package.harness_min must be a release number, not {hmin!r}")
        if need > HARNESS_RELEASE:
            fail(f"package {pkg['id']} needs harness release {need} or later; this harness is release {HARNESS_RELEASE}")
    _check_labels(manifest.get("labels"))
    _check_protected(manifest.get("protected"))
    rs = manifest.get("protected") or []
    for r in rs:
        if r.get("level") == "none" and not any(o.get("level") != "none" and globs_overlap(r["path"], o["path"])
                                                for o in rs):
            warnings.append(f"startup.yaml protected: `{r['path']} none` lies under no stricter rule of the package"
                            " (it lifts nothing)")
    _check_policies(manifest.get("policies"))
    _check_scheduler_tools(manifest.get("scheduler_tools"))
    templates = manifest.get("templates") or {}
    if not isinstance(templates, dict) or set(templates) - {"project"}:
        fail("startup.yaml templates takes only project: <dir in the package>")
    if root is not None:
        if templates.get("project"):
            _inside(root, templates["project"], "project template")
        for name, spec in (manifest.get("roles") or {}).items():
            if isinstance(spec, dict) and spec.get("skill"):
                _inside(root, spec["skill"], f"skill of role {name}")
    return warnings


WITNESS_FILLER = "w"
WITNESS_MAX = 20000


def _concrete(glob: str, vocab: list[str]) -> list[str]:
    """Concrete paths a glob matches, built from `vocab` names: each wildcard segment takes a vocabulary name it
    matches (or a filler), each `**` zero to two such names. Bounded; used only to find witnesses."""
    from fnmatch import fnmatchcase

    out = [[]]
    for seg in glob.split("/"):
        if seg == "**":
            names = vocab + [WITNESS_FILLER]
            ext = [[]] + [[a] for a in names] + [[a, b] for a in names for b in names]
            out = [p + e for p in out for e in ext][:WITNESS_MAX]
        elif any(c in seg for c in "*?["):
            names = [n for n in vocab if fnmatchcase(n, seg)]
            filler = seg.replace("*", WITNESS_FILLER).replace("?", "x")
            if not any(c in filler for c in "[]") and filler not in names:
                names.append(filler)
            out = [p + [n] for p in out for n in names][:WITNESS_MAX]
        else:
            out = [p + [seg] for p in out]
    return ["/".join(p) for p in out if p]


def effective_rule(rules: list[dict], path: str) -> dict | None:
    """The rule that decides a site-relative path at run time (protect.best_rule: most specific, later on a tie)."""
    from .protect import best_rule

    return best_rule(rules, [path])


def role_scope_conflicts(manifest: dict, roles) -> list[str]:
    """Write scopes (of package roles and desk additions) that cover a protected path: refused (3.4 rule 4).

    A scope conflicts only where the effective level is read-only or no-read, decided exactly as the runtime guard
    decides (the most specific matching rule wins, the later one on a tie; `none` lifts the package's own rules):
    the harness builds witness paths in the overlap of the scope and each protective rule from the names the
    scope, that rule and the `none` rules spell, and refuses when any witness is protected. So `projects/p-test/**
    none` lets a scope `projects/p-test/checkpoints/**` through even under `projects/*/**/.git/** read-only`, because
    `projects/p-test/**` is more specific at the second segment; a scope `projects/*/checkpoints/**` still
    conflicts (other projects stay protected)."""
    from .org import scope_match

    rules = protected_rules(manifest)
    lifts = [r for r in rules if r.get("level") == "none"]
    out = []
    for r in roles:
        for s in r.write_scopes:
            if s.startswith("site:") or r.source == "harness":
                continue
            for p in rules:
                if p.get("level") == "none" or not globs_overlap(s, p["path"]):
                    continue
                vocab = sorted({seg for g in [s, p["path"], *(x["path"] for x in lifts)] for seg in g.split("/")
                                if seg and not any(c in seg for c in "*?[")})
                witnesses = [w for w in set(_concrete(s, vocab) + _concrete(p["path"], vocab))
                             if scope_match(w, s) and scope_match(w, p["path"])]
                hit = next((w for w in sorted(witnesses) if (effective_rule(rules, w) or {}).get("level")
                            in ("read-only", "no-read")), None)
                if hit is not None or not witnesses:  # no witness found: refuse, as before (conservative)
                    out.append(f"role {r.name}: write scope {s} covers protected path {p['path']} ({p['level']})"
                               + (f", e.g. {hit}" if hit else ""))
    return out
