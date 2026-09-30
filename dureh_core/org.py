"""Runtime-neutral organization: roles, dispatch authority, tiers, write scopes."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from . import yamlsub
from .util import fail

HARNESS_ROOT = Path(__file__).resolve().parent.parent


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

    @property
    def is_manager(self) -> bool:
        return self.kind == "manager"

    @property
    def is_worker(self) -> bool:
        return self.kind == "worker"


class Org:
    def __init__(self, roles: dict[str, Role], tiers: dict, limits: dict, event_authority: dict, max_depth: int):
        self.roles = roles
        self.tiers = tiers
        self.limits = limits
        self.event_authority = event_authority
        self.max_depth = max_depth

    @classmethod
    def load(cls, harness_root: Path = HARNESS_ROOT, enabled: list[str] | None = None) -> "Org":
        data = yamlsub.load_file(harness_root / "org" / "roles.yaml")
        tiers_doc = yamlsub.load_file(harness_root / "org" / "tiers.yaml")
        roles = {}
        for name, spec in (data.get("roles") or {}).items():
            if not isinstance(spec, dict) or spec.get("kind") not in {"desk", "manager", "worker"}:
                fail(f"org/roles.yaml: role {name!r} needs kind desk, manager, or worker")
            roles[name] = Role(
                name=name,
                kind=spec["kind"],
                tier=spec.get("tier") or fail(f"role {name} has no tier"),
                description=spec.get("description") or "",
                output=spec.get("output") or "",
                never=spec.get("never") or "",
                value=spec.get("value"),
                isolation=spec.get("isolation") or "worktree",
                may_dispatch=list(spec.get("may_dispatch") or []),
                write_scopes=list(spec.get("write_scopes") or []),
            )
        if "desk" not in roles:
            fail("org/roles.yaml must define the desk")
        for role in roles.values():
            for target in role.may_dispatch:
                if target not in roles:
                    fail(f"role {role.name} may dispatch unknown role {target}")
            if role.tier not in (tiers_doc.get("tiers") or {}):
                fail(f"role {role.name} uses unknown tier {role.tier}")
        if enabled is not None:
            unknown = sorted(set(enabled) - set(roles))
            if unknown:
                fail(f"startup package enables roles unknown to this harness: {', '.join(unknown)}")
            keep = set(enabled) | {"desk"}
            roles = {n: r for n, r in roles.items() if n in keep}
            for r in roles.values():
                r.may_dispatch = [t for t in r.may_dispatch if t in keep]
        return cls(roles, tiers_doc.get("tiers") or {}, tiers_doc.get("limits") or {},
                   data.get("event_authority") or {}, int(data.get("max_depth") or 2))

    def role(self, name: str) -> Role:
        r = self.roles.get(name)
        if r is None:
            fail(f"unknown role: {name!r}")
        return r

    def may_append(self, actor: str, event_type: str) -> bool:
        role = self.role(actor)
        if role.kind == "desk":
            return True
        allowed = set(self.event_authority.get(role.name, [])) | set(
            self.event_authority.get(role.kind, []))
        return event_type in allowed

    def scope_allows(self, role_name: str, logical_path: str) -> bool:
        role = self.role(role_name)
        return any(fnmatch.fnmatchcase(logical_path, s) for s in role.write_scopes if not s.startswith("site:"))

    def site_scope_allows(self, role_name: str, site_rel: str) -> bool:
        role = self.role(role_name)
        return any(fnmatch.fnmatchcase(site_rel, s[5:]) for s in role.write_scopes if s.startswith("site:"))

    def dispatcher_of(self, role_name: str) -> list[str]:
        return [r.name for r in self.roles.values() if role_name in r.may_dispatch]
