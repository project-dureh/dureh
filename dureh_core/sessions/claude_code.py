"""Claude Code as the session runtime. The argv comes from the config.json `session` template:
`command` plus each option in `options` mapped through `flags` (true -> the flag alone, false or null
-> left out, any other value -> the flag and the value)."""
from __future__ import annotations

import shutil

from ..site import Site
from ..util import fail
from .base import PURPOSES, SessionRuntime

DEFAULTS = {"runtime": "claude-code", "command": ["claude"],
            "options": {"permission_mode": "auto", "remote": True},
            "flags": {"permission_mode": "--permission-mode", "remote": "--remote"}}


def settings(site: Site) -> dict:
    cfg = site.config.get("session") or {}
    out = {**DEFAULTS, **cfg}
    out["options"] = {**DEFAULTS["options"], **(cfg.get("options") or {})}
    out["flags"] = {**DEFAULTS["flags"], **(cfg.get("flags") or {})}
    if isinstance(out["command"], str):
        out["command"] = [out["command"]]
    return out


class ClaudeCode(SessionRuntime):
    name = "claude-code"
    talks_to_director = True

    def _unmapped(self, st: dict) -> list[str]:
        return [k for k, v in st["options"].items() if v not in (False, None) and not st["flags"].get(k)]

    def launch_command(self, site: Site, purpose: str = "desk") -> list[str]:
        if purpose not in PURPOSES:
            fail(f"unknown session purpose {purpose!r}; available: {', '.join(PURPOSES)}")
        st = settings(site)
        missing = self._unmapped(st)
        if missing:
            fail(f"config.json session.options {', '.join(missing)} have no flag in session.flags")
        argv = [str(x) for x in st["command"]]
        for key, value in st["options"].items():
            if value is True:
                argv.append(st["flags"][key])
            elif value not in (False, None):
                argv += [st["flags"][key], str(value)]
        return argv

    def check(self, site: Site) -> list[str]:
        st = settings(site)
        problems = []
        if not st["command"] or not shutil.which(str(st["command"][0]), path=site.env().get("PATH")):
            problems.append(f"session command {st['command'][0] if st['command'] else '(empty)'!r} is not on PATH")
        missing = self._unmapped(st)
        if missing:
            problems.append(f"session.options {', '.join(missing)} have no flag in session.flags")
        return problems
