"""Runtime-neutral organization: roles, dispatch authority, tiers, write scopes, capabilities.

Precedence (release 46, proposals/boundary-design-2026-10-08.md 3.5): the harness's generic roles (org/roles.yaml:
desk, manager, worker, checker), then the package's `roles` (which add roles or replace `manager`/`worker`; the
checker may only be re-skinned), then the package's `desk:` additions. A schema-1 package resolves its
`organization` enable list against the generic roles plus org/legacy/schema1-roles.yaml (release 46 only).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import yamlsub
from .util import fail

HARNESS_ROOT = Path(__file__).resolve().parent.parent
KINDS = ("desk", "manager", "worker")


@lru_cache(maxsize=None)
def _scope_regex(glob: str) -> re.Pattern:
    """Compile a write-scope glob segment by segment: `*` and `?` stay inside one path segment, `**` as a
    whole segment spans any depth (one or more segments at the end, zero or more in the middle)."""
    parts = glob.split("/")
    out = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**":
            out.append(".+" if last else "(?:[^/]+/)*")
            continue
        seg = "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in part)
        out.append(seg if last else seg + "/")
    return re.compile("".join(out))


def scope_match(path: str, glob: str) -> bool:
    return _scope_regex(glob).fullmatch(path) is not None


@dataclass
class Role:
    name: str
    kind: str
    tier: str
    description: str = ""
    output: str = ""
    never: str = ""
    value: str | None = None
    isolation: str = "worktree"
    may_dispatch: list[str] = field(default_factory=list)
    write_scopes: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    skill: str | None = None              # package-relative skill file for the role's brief and agent file
    instructions: list[str] = field(default_factory=list)  # extra agent-file lines from the package
    project: bool = False                 # also generate a project-level agent file (projectgen)
    source: str = "harness"               # harness | legacy | package

    @property
    def is_manager(self) -> bool:
        return self.kind == "manager"

    @property
    def is_worker(self) -> bool:
        return self.kind == "worker"

    def has(self, capability: str) -> bool:
        return capability in self.capabilities


def _texts(value, where: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        fail(f"{where} must be a list of strings")
    return list(value)


def _role(name: str, spec, where: str, source: str) -> Role:
    from .package import CAPABILITIES

    if not isinstance(spec, dict) or spec.get("kind") not in KINDS:
        fail(f"{where}: role {name!r} needs kind desk, manager, or worker")
    caps = _texts(spec.get("capabilities"), f"{where}: role {name} capabilities")
    unknown = [c for c in caps if c not in CAPABILITIES]
    if unknown:
        fail(f"{where}: role {name}: unknown capability {unknown[0]!r} (the harness defines {', '.join(CAPABILITIES)})")
    isolation = spec.get("isolation") or "worktree"
    if isolation not in ("worktree", "scratch"):
        fail(f"{where}: role {name}: isolation must be worktree or scratch, not {isolation!r}")
    return Role(
        name=name,
        kind=spec["kind"],
        tier=spec.get("tier") or fail(f"{where}: role {name} has no tier"),
        description=spec.get("description") or "",
        output=spec.get("output") or "",
        never=spec.get("never") or "",
        value=spec.get("value"),
        isolation=isolation,
        may_dispatch=_texts(spec.get("may_dispatch"), f"{where}: role {name} may_dispatch"),
        write_scopes=_texts(spec.get("write_scopes"), f"{where}: role {name} write_scopes"),
        capabilities=caps,
        skill=spec.get("skill") or None,
        instructions=_texts(spec.get("instructions"), f"{where}: role {name} instructions"),
        project=bool(spec.get("project")),
        source=source,
    )


def _desk_additions(desk: Role, block, where: str) -> None:
    if block in (None, {}):
        return
    if not isinstance(block, dict) or set(block) - {"may_dispatch", "write_scopes"}:
        fail(f"{where} desk: takes only may_dispatch and write_scopes")
    for t in _texts(block.get("may_dispatch"), f"{where} desk.may_dispatch"):
        if t not in desk.may_dispatch:
            desk.may_dispatch.append(t)
    for s in _texts(block.get("write_scopes"), f"{where} desk.write_scopes"):
        if s.startswith("site:"):
            fail(f"{where} desk.write_scopes: {s} — site:* scopes are harness-owned; a package adds logical scopes only")
        if s not in desk.write_scopes:
            desk.write_scopes.append(s)


def _check_scopes(role: Role, where: str) -> None:
    from .package import SCOPE_PREFIXES, _glob_ok

    for s in role.write_scopes:
        if s.startswith("site:") and role.kind == "desk" and role.source == "harness":
            continue
        if not _glob_ok(s) or not s.startswith(SCOPE_PREFIXES):
            fail(f"{where}: role {role.name}: write scope {s!r} must be a segment-aware glob under "
                 f"{', '.join(p.rstrip('/') for p in SCOPE_PREFIXES)}")


class Org:
    def __init__(self, roles: dict[str, Role], tiers: dict, limits: dict, event_authority: dict, max_depth: int,
                 capability_authority: dict | None = None, warnings: list[str] | None = None):
        self.roles = roles
        self.tiers = tiers
        self.limits = limits
        self.event_authority = event_authority
        self.capability_authority = capability_authority or {}
        self.max_depth = max_depth
        self.warnings = list(warnings or [])

    @classmethod
    def load(cls, harness_root: Path = HARNESS_ROOT, enabled: list[str] | None = None,
             manifest: dict | None = None) -> "Org":
        from .package import CHECKER_RESKIN_KEYS, ROLE_KEYS, ROLE_NAME_RE, schema_of

        data = yamlsub.load_file(harness_root / "org" / "roles.yaml")
        tiers_doc = yamlsub.load_file(harness_root / "org" / "tiers.yaml")
        tiers = tiers_doc.get("tiers") or {}
        warnings: list[str] = []
        roles: dict[str, Role] = {}
        for name, spec in (data.get("roles") or {}).items():
            roles[name] = _role(name, spec, "org/roles.yaml", "harness")
        if "desk" not in roles:
            fail("org/roles.yaml must define the desk")
        strict = True
        if manifest is not None:
            schema = schema_of(manifest)
            organization = manifest.get("organization") or {}
            listed = list(organization.get("managers") or []) + list(organization.get("workers") or [])
            if schema == 1:
                strict = False
                legacy = yamlsub.load_file(harness_root / "org" / "legacy" / "schema1-roles.yaml")
                for name, spec in (legacy.get("roles") or {}).items():
                    roles[name] = _role(name, spec, "org/legacy/schema1-roles.yaml", "legacy")
                _desk_additions(roles["desk"], legacy.get("desk"), "org/legacy/schema1-roles.yaml")
            else:
                pkg_roles = manifest.get("roles") or {}
                if not isinstance(pkg_roles, dict):
                    fail("startup.yaml roles must map a role name to its definition")
                for name, spec in pkg_roles.items():
                    where = "startup.yaml roles"
                    if not ROLE_NAME_RE.match(str(name)):
                        fail(f"{where}: role name {name!r} must match {ROLE_NAME_RE.pattern}")
                    if not isinstance(spec, dict):
                        fail(f"{where}: role {name} must be a mapping")
                    extra = sorted(set(spec) - set(ROLE_KEYS))
                    if extra:
                        fail(f"{where}: role {name}: unknown key(s) {', '.join(extra)}")
                    if name == "desk":
                        fail(f"{where}: desk is reserved; extend it through the top-level `desk:` block")
                    if name == "checker":
                        bad = sorted(set(spec) - set(CHECKER_RESKIN_KEYS))
                        if bad:
                            fail(f"{where}: checker is reserved: a package may re-skin it ({', '.join(CHECKER_RESKIN_KEYS)})"
                                 f" but not change {', '.join(bad)} (it keeps verify and no write scopes)")
                        c = roles["checker"]
                        c.skill = spec.get("skill") or c.skill
                        c.instructions = _texts(spec.get("instructions"), f"{where}: checker instructions")
                        c.description = spec.get("description") or c.description
                        c.output = spec.get("output") or c.output
                        c.never = spec.get("never") or c.never
                        c.project = bool(spec.get("project", c.project))
                        c.source = "package"
                        continue
                    if spec.get("kind") not in ("manager", "worker"):
                        fail(f"{where}: role {name}: kind must be manager or worker")
                    roles[name] = _role(name, spec, where, "package")
                    _check_scopes(roles[name], where)
                # Director 2026-10-09: a package that declares its own managers (workers) does not get the generic
                # `manager` (`worker`) as well, so the generic worker's module scopes cannot bypass the package's own
                # narrower ones; a package may still declare `manager`/`worker` itself. Desk and checker always stay.
                for kind in ("manager", "worker"):
                    if kind not in pkg_roles and any(isinstance(sp, dict) and sp.get("kind") == kind and n != "checker"
                                                     for n, sp in pkg_roles.items()):
                        del roles[kind]
                        for other in roles.values():
                            other.may_dispatch = [t for t in other.may_dispatch if t != kind]
                _desk_additions(roles["desk"], manifest.get("desk"), "startup.yaml")
                _check_scopes(roles["desk"], "startup.yaml desk")
            if listed and enabled is None:
                enabled = listed
        defined = set(roles)
        if enabled is not None:
            unknown = [n for n in enabled if n not in roles]
            if unknown and strict:
                fail(f"startup package enables roles unknown to this harness: {', '.join(unknown)}")
            if unknown:
                warnings.append(f"schema-1 package enables roles this harness no longer defines (release 46 removed or"
                                f" moved them to packages): {', '.join(unknown)}; they are dropped")
            keep = set(enabled) | {"desk"}
            roles = {n: r for n, r in roles.items() if n in keep}
        desk = roles["desk"]
        for r in roles.values():
            # a target disabled by the enable list is dropped; an undefined one stays and fails validation
            r.may_dispatch = [t for t in r.may_dispatch if t in roles or t not in defined]
        for r in roles.values():
            if r.kind == "manager" or (r.kind == "worker" and r.has("verify")):
                if r.name not in desk.may_dispatch:
                    desk.may_dispatch.append(r.name)
        org = cls(roles, tiers, tiers_doc.get("limits") or {}, data.get("event_authority") or {},
                  int(data.get("max_depth") or 2), data.get("capability_authority") or {}, warnings)
        org._validate(strict)
        return org

    def _validate(self, strict: bool) -> None:
        roles = self.roles
        verifiers = [r.name for r in roles.values() if r.kind != "desk" and r.has("verify")]
        for role in roles.values():
            if role.tier not in self.tiers:
                fail(f"role {role.name} uses unknown tier {role.tier}")
            for target in role.may_dispatch:
                if target not in roles:
                    fail(f"role {role.name} may dispatch unknown role {target}")
                if target == "desk":
                    fail(f"role {role.name} may not dispatch the desk")
            if role.kind == "worker" and role.may_dispatch:
                fail(f"role {role.name} is a worker; only the desk and managers dispatch (depth desk -> manager -> worker)")
            if role.kind == "manager":
                for target in role.may_dispatch:
                    if roles[target].kind != "worker":
                        fail(f"manager {role.name} may dispatch only workers, not {target} "
                             f"(depth desk -> manager -> worker = {self.max_depth})")
                if role.may_dispatch and not any(roles[t].has("verify") for t in role.may_dispatch):
                    fail(f"manager {role.name} dispatches workers but no role with the verify capability"
                         f" ({', '.join(verifiers) or 'none defined'})")
            if role.kind != "desk" and role.has("verify") and role.write_scopes:
                fail(f"role {role.name} has the verify capability and may not have write scopes")
        if not verifiers:
            fail("the organization needs at least one role with the verify capability (the checker)")

    def role(self, name: str) -> Role:
        r = self.roles.get(name)
        if r is None:
            fail(f"unknown role: {name!r}")
        return r

    def get(self, name: str | None) -> Role | None:
        """The role or None (a role recorded before it was removed or disabled)."""
        return self.roles.get(name) if name else None

    def has(self, name: str, capability: str) -> bool:
        """Whether role `name` holds `capability` (the desk: its own capabilities in org/roles.yaml)."""
        r = self.roles.get(name)
        return bool(r) and r.has(capability)

    def with_capability(self, capability: str) -> list[Role]:
        return [r for r in self.roles.values() if r.kind != "desk" and r.has(capability)]

    def may_append(self, actor: str, event_type: str) -> bool:
        role = self.role(actor)
        if role.kind == "desk":
            return True
        allowed = set(self.event_authority.get(role.kind, []))
        for cap in role.capabilities:
            allowed |= set(self.capability_authority.get(cap, []))
        return event_type in allowed

    def scope_allows(self, role_name: str, logical_path: str) -> bool:
        role = self.role(role_name)
        return any(scope_match(logical_path, s) for s in role.write_scopes if not s.startswith("site:"))

    def site_scope_allows(self, role_name: str, site_rel: str) -> bool:
        role = self.role(role_name)
        return any(scope_match(site_rel, s[5:]) for s in role.write_scopes if s.startswith("site:"))

    def dispatcher_of(self, role_name: str) -> list[str]:
        return [r.name for r in self.roles.values() if role_name in r.may_dispatch]
