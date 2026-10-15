"""Release 64 test helpers: a copy of the minimal package with member roles (a `local` worker, a `codex` checker),
a fake sbatch (records its arguments, runs nothing), and a work item ready for a member run. Nothing here runs a real
scheduler job, a real model server or a real codex."""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

from helpers import SiteCase

PROFILE = """FROM base-model
SYSTEM You are a careful worker. Use the tools.
PARAMETER temperature 0
PARAMETER num_ctx 8192
PARAMETER stop "<end>"
"""
MEMBER_ROLES = """  py-local:
    kind: worker
    tier: worker
    runtime: local
    profile: profiles/coder.Modelfile
    output: Python code for one module, written by the local model.
    never: Verification verdicts.
    write_scopes:
      - modules/*/src/**
      - modules/*/tests/**
  checker:
    runtime: codex
"""
JOB_ID = "7100"


def add_member_roles(package: Path) -> None:
    m = Path(package) / "startup.yaml"
    text = m.read_text()
    text = text.replace("\nroles:\n", "\nroles:\n" + MEMBER_ROLES, 1)
    text = text.replace("may_dispatch: [py-writer, doc-writer, researcher, checker]",
                        "may_dispatch: [py-writer, py-local, doc-writer, researcher, checker]")
    m.write_text(text)
    (Path(package) / "profiles").mkdir(exist_ok=True)
    (Path(package) / "profiles" / "coder.Modelfile").write_text(PROFILE)


class MemberCase(SiteCase):
    """A site whose package has `py-local` (runtime local) and a codex checker; fake sbatch/squeue/scancel/sacct."""

    def setUp(self):
        super().setUp()
        add_member_roles(self.package)
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        self.tool("sbatch", f'echo "$@" >> {self.fake}/sbatch.args\necho {JOB_ID}\n')
        self.tool("squeue", f"cat {self.fake}/squeue.out 2>/dev/null\n")
        self.tool("scancel", f'echo "$@" >> {self.fake}/scancel.args\n')
        self.tool("sacct", f"cat {self.fake}/sacct.out 2>/dev/null || echo 'RUNNING|0:0|00:00:02'\n")
        self.install(env={"path_prepend": [str(self.fake)]},
                     members={"codex": {"sandbox_read": [], "wait_seconds": 30},
                              "local": {"quiet_seconds": 0.5, "wall_seconds": 20, "grace_windows": 1}},
                     office_hours={"partition": "gpu", "gres": "gpu:1", "mem": "32G", "cpus": 8,
                                   "server": {"bin": "srv", "models_dir": "cache/models", "port": 11999},
                                   "profiles": [{"name": "coder-test", "file": "profiles/coder.Modelfile",
                                                 "base": "base-model"}]})
        self.make_module()
        from dureh_core import workflow as wf
        from dureh_core.records import Ledger

        self.wf = wf
        self.s = self.site()
        self.led = Ledger(self.s)
        self.bundle = wf.wi_new(self.s, "desk", "Bundle", "g", "pm-modules", "modules/demo", ["ok"])["id"]
        wf.wi_approve(self.s, "desk", self.bundle, "ok")
        self.led.event("wi.dispatched", "desk", self.bundle, {"role": "pm-modules", "brief": "0" * 16, "by": "desk"})

    def tool(self, name, body):
        (self.fake / name).write_text("#!/bin/bash\n" + body)
        (self.fake / name).chmod(0o755)

    def py_tool(self, name, body):
        (self.fake / name).write_text(f"#!{sys.executable}\n" + body)
        (self.fake / name).chmod(0o755)

    def item(self, role="py-local", **kw):
        kw.setdefault("outputs", ["src/demo.py"])
        wi = self.wf.wi_new(self.s, "desk", "Add g()", "g", role, "modules/demo", ["g() returns 2"],
                            parent=self.bundle, **kw)["id"]
        self.wf.wi_approve(self.s, "desk", wi, "ok")
        return wi

    def config(self, **patch):
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg.update(patch)
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.s = self.site()
        from dureh_core.records import Ledger

        self.led = Ledger(self.s)

    def pass_canary(self, version="codex-cli 9.9.9"):
        self.led.event("member.canary", "desk", None, {"runtime": "codex", "host": socket.gethostname(),
                                                       "version": version, "result": "pass", "rows": []})

    def events(self, wi=None, type_=None):
        return [e for e in self.led.events(wi) if type_ is None or e["type"] == type_]
