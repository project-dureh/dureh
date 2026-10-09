"""The startup package interface (startup.yaml), release 46: schema 2 loading and validation.

A package declares everything that is about one company or one domain: its roles (kind, tier, isolation, write
scopes, capabilities, skill, instructions), protected paths, labels (item levels and grammar, layout words), loop
policy text, project template and config defaults. The harness knows roles only by kind (desk, manager, worker) and
by capability (CAPABILITIES, a closed set). A package can tighten what the harness allows; it never loosens an
invariant (proposals/boundary-design-2026-10-08.md sections 3 and 5).

Schema 1 still loads in release 46 with a warning: its `organization` enable list resolves against the generic
roles plus the legacy schema-1 roles (org/legacy/schema1-roles.yaml), and its labels are LEGACY_LABELS. Both go when
schema 1 is refused.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import HARNESS_RELEASE, yamlsub
from .util import fail

SCHEMA_VERSIONS = (1, 2)
CAPABILITIES = ("submit_jobs", "verify", "network")
RESERVED_ROLES = ("desk", "manager", "worker", "checker")
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
WORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROTECT_LEVELS = ("no-read", "read-only")
LAYOUT_KEYS = ("staging", "final", "reports")
# the harness's own layout words when a package names none (`final` has no harness default)
DEFAULT_LAYOUT = {"staging": "checkpoints", "final": None, "reports": "reports"}
# top-level keys of a schema-2 startup.yaml; anything else is warned about in release 46 (refused from release 49)
KNOWN_KEYS = ("schema_version", "package", "desk", "roles", "values", "skills", "policies", "protected", "labels",
              "templates", "guards", "config_defaults", "lessons", "bootstrap", "dependencies", "organization", "state",
              "projects")
ROLE_KEYS = ("kind", "tier", "description", "output", "never", "isolation", "write_scopes", "may_dispatch",
             "capabilities", "project", "skill", "instructions", "value")
CHECKER_RESKIN_KEYS = ("skill", "instructions", "description", "output", "never", "project")
SCOPE_PREFIXES = ("modules/", "projects/", "package/", "harness/")

# Schema-1 packages had no labels: the words the harness carried before release 46. Kept for schema 1 only (one
# release); a schema-2 package declares its own `labels`. Allow-listed in tests/test_no_company_names.py.
LEGACY_LABELS = {
    "levels": ["file", "session", "subject"],
    "layout": {"staging": "checkpoints", "final": "derivatives", "reports": "reports"},
    "item_grammar": {"subject": r"^sub-(?P<sub>[A-Za-z0-9]+)$",
                     "session": r"^sub-(?P<sub>[A-Za-z0-9]+)_ses-(?P<ses>[A-Za-z0-9]+)$"},
}


def schema_of(manifest: dict | None) -> int:
    raw = (manifest or {}).get("schema_version", 1)
    try:
        return int(raw)
    except (TypeError, ValueError):
        fail(f"startup.yaml schema_version must be 1 or 2, not {raw!r}")


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
    Schema 1: LEGACY_LABELS. Schema 2: the package's `labels` over DEFAULT_LAYOUT (no levels, no grammar by default)."""
    if manifest is None or schema_of(manifest) == 1:
        raw = LEGACY_LABELS
    else:
        raw = manifest.get("labels") or {}
    layout = dict(DEFAULT_LAYOUT)
    layout.update({k: v for k, v in (raw.get("layout") or {}).items() if v})
    return {"levels": list(raw.get("levels") or []), "layout": layout,
            "item_grammar": {lv: re.compile(rx) for lv, rx in (raw.get("item_grammar") or {}).items()}}


def protected_rules(manifest: dict | None) -> list[dict]:
    """The package's protected paths [{path, level, why}] (schema 2; schema 1 declares none)."""
    if manifest is None or schema_of(manifest) == 1:
        return []
    return [dict(r) for r in manifest.get("protected") or []]


def loop_policy(manifest: dict | None) -> dict:
    """`policies.loop_rule` {text, refuse}: the package's own loop rule wording (desk/agent text, refusal text)."""
    if manifest is None or schema_of(manifest) == 1:
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
            fail(f"{where}.level must be no-read or read-only, not {r.get('level')!r}")


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
    schema = schema_of(manifest)
    if schema not in SCHEMA_VERSIONS:
        fail(f"startup.yaml schema_version {schema} is not supported by this harness (1 or 2)")
    pkg = manifest.get("package")
    if not isinstance(pkg, dict) or not pkg.get("id") or not pkg.get("name"):
        fail("startup.yaml must declare package.id and package.name")
    if schema == 1:
        warnings.append("startup.yaml schema 1 is deprecated: it loads in release 46 with the legacy roles and labels;"
                        " move the package to schema 2 (roles, labels, protected; docs/config.md)")
        return warnings
    unknown = sorted(set(manifest) - set(KNOWN_KEYS))
    if unknown:
        warnings.append(f"startup.yaml: unknown top-level key(s) {', '.join(unknown)} (ignored; refused from release 49)")
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
    _check_policies(manifest.get("policies"))
    if manifest.get("organization"):
        warnings.append("startup.yaml organization.managers/workers is an enable list for release 46 only; from the"
                        " next release the package's `roles` is authoritative")
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


def role_scope_conflicts(manifest: dict, roles) -> list[str]:
    """Write scopes (of package roles and desk additions) that cover a protected path: refused (3.4 rule 4)."""
    rules = protected_rules(manifest)
    out = []
    for r in roles:
        for s in r.write_scopes:
            if s.startswith("site:") or r.source == "harness":
                continue
            for p in rules:
                if globs_overlap(s, p["path"]):
                    out.append(f"role {r.name}: write scope {s} covers protected path {p['path']} ({p['level']})")
    return out
