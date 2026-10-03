"""The session runtime interface: how a working session (the desk, later others) is started.

A runtime is a subscription service's command-line client. The harness only needs the command that
starts a session, the environment it runs in, and a check that it can start here; everything
provider-specific stays in config.json `session` and one small module per runtime.
"""
from __future__ import annotations

import os

from ..site import Site

PURPOSES = ("desk",)


class SessionRuntime:
    name = ""
    talks_to_director = False  # the Director can talk to a running session (remote control)

    def launch_command(self, site: Site, purpose: str = "desk") -> list[str]:
        raise NotImplementedError

    def env(self, site: Site) -> dict:
        """Variables the session needs beyond the caller's environment (site, harness, config env)."""
        return {k: v for k, v in site.env().items() if os.environ.get(k) != v}

    def check(self, site: Site) -> list[str]:
        """Problems that would stop a session from starting here (availability, authentication)."""
        return []
