"""Claude Code as the session runtime. The argv comes from the config.json `session` template:
`command` plus each option in `options` mapped through `flags` (true -> the flag alone, false or null
-> left out, any other value -> the flag and the value)."""
from __future__ import annotations

import json
import shutil

from ..site import Site
from ..util import fail
from .base import PURPOSES, SessionRuntime

# Director (2026-10-03): a session the Director can talk to is Claude Code's remote control, in auto mode.
# Measured 2026-10-04 (labs/session-test): the flag form `claude --remote-control <name>` registers from a batch
# job; the `claude remote-control` subcommand did not. String options are templates: {site} is the session
# name (config `session.name`, default the site directory name; the default remote-control name), {purpose} the purpose, {name} the desk's name.
# First prompt (Director, 2026-10-04), passed last: none for the day's session (`desk`; the DUREH team sends the
# start message to the booted remote-control session itself); an `overtime` session gets "<name>, 추가근무 출근"
# with the desk's name from state/persona/persona.json (the registry adapter sync renders into CLAUDE.md),
# "데스크, 추가근무 출근" without one. Config `session.prompts.<purpose>`: a template, or false for none.
DEFAULTS = {"runtime": "claude-code", "command": ["claude"],
            "options": {"remote_control": "{site}", "permission_mode": "auto"},
            "flags": {"remote_control": "--remote-control", "permission_mode": "--permission-mode"},
            "prompts": {"desk": False, "overtime": "{name}, 추가근무 출근"}}
DEFAULT_NAME = "데스크"


def desk_name(site: Site) -> str | None:
    """The desk's name from state/persona/persona.json (`name`), None when the file or the key is missing."""
    p = site.state / "persona" / "persona.json"
    try:
        name = json.loads(p.read_text(encoding="utf-8")).get("name") if p.is_file() else None
    except (OSError, ValueError, AttributeError):
        return None
    return str(name).strip() or None if name is not None else None


def settings(site: Site) -> dict:
    cfg = site.config.get("session") or {}
    out = {**DEFAULTS, **cfg}
    out["options"] = {**DEFAULTS["options"], **(cfg.get("options") or {})}
    out["flags"] = {**DEFAULTS["flags"], **(cfg.get("flags") or {})}
    out["prompts"] = {**DEFAULTS["prompts"], **(cfg.get("prompts") or {})}
    if isinstance(out["command"], str):
        out["command"] = [out["command"]]
    return out


class ClaudeCode(SessionRuntime):
    name = "claude-code"
    talks_to_director = True

    def _unmapped(self, st: dict) -> list[str]:
        return [k for k, v in st["options"].items() if v not in (False, None) and not st["flags"].get(k)]

    def _fields(self, site: Site, purpose: str) -> dict:
        return {"site": site.session_name, "purpose": purpose, "name": desk_name(site) or DEFAULT_NAME}

    def first_prompt(self, site: Site, purpose: str = "desk") -> str | None:
        """The session's default first prompt for this purpose: config `session.prompts.<purpose>` (a template
        with {name}, {site}, {purpose}; false or empty for none), else none for `desk` and "<desk name>,
        추가근무 출근" for `overtime` ("데스크, 추가근무 출근" when no name is recorded)."""
        cfg = settings(site)["prompts"].get(purpose)
        if not cfg:
            return None
        return str(cfg).format(**self._fields(site, purpose)) or None

    def launch_command(self, site: Site, purpose: str = "desk", prompt: str | None = None) -> list[str]:
        if purpose not in PURPOSES:
            fail(f"unknown session purpose {purpose!r}; available: {', '.join(PURPOSES)}")
        st = settings(site)
        missing = self._unmapped(st)
        if missing:
            fail(f"config.json session.options {', '.join(missing)} have no flag in session.flags")
        fields = self._fields(site, purpose)
        argv = [str(x) for x in st["command"]]
        for key, value in st["options"].items():
            if value is True:
                argv.append(st["flags"][key])
            elif value not in (False, None):
                argv += [st["flags"][key], str(value).format(**fields)]
        if prompt:
            argv.append(prompt)
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
