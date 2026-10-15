"""Release 64, section A: the package interface of member runtimes (role keys `runtime`, `runtime_fallback`,
`profile`), its validation, the generated agent file of a non-Claude role and the spawn hook's refusal."""
import unittest

from helpers import HARNESS, FIXTURE_PACKAGE, denied, hook
from member_helpers import MemberCase

from dureh_core import package as pkg
from dureh_core import yamlsub
from dureh_core.org import Org
from dureh_core.util import DurehError


def org_with(roles: dict) -> Org:
    m = yamlsub.load_file(FIXTURE_PACKAGE / "startup.yaml")
    m["roles"].update(roles)
    return Org.load(HARNESS, manifest=m)


LOCAL = {"kind": "worker", "tier": "worker", "runtime": "local", "profile": "profiles/x.Modelfile",
         "write_scopes": ["modules/*/src/**"]}


class Validation(unittest.TestCase):
    def test_defaults_are_claude_code(self):
        org = org_with({})
        for r in org.roles.values():
            self.assertEqual((r.runtime, r.runtime_fallback, r.profile, r.is_member), ("claude-code", "claude-code",
                                                                                       None, False))

    def test_role_keys_and_checker_reskin_take_the_runtime(self):
        self.assertTrue({"runtime", "runtime_fallback", "profile"} <= set(pkg.ROLE_KEYS))
        self.assertTrue({"runtime", "runtime_fallback"} <= set(pkg.CHECKER_RESKIN_KEYS))
        self.assertNotIn("profile", pkg.CHECKER_RESKIN_KEYS)
        org = org_with({"checker": {"runtime": "codex", "runtime_fallback": "none"}})
        c = org.role("checker")
        self.assertEqual((c.runtime, c.runtime_fallback, c.is_member), ("codex", "none", True))
        self.assertTrue(c.has("verify") and not c.write_scopes)

    def test_codex_only_for_a_verify_role(self):
        with self.assertRaisesRegex(DurehError, "runtime codex is read-only and only for a role with the verify"):
            org_with({"coder": {"kind": "worker", "tier": "worker", "runtime": "codex",
                                "write_scopes": ["modules/*/src/**"]}})
        with self.assertRaisesRegex(DurehError, "runtime codex"):
            org_with({"pm-modules": {"kind": "manager", "tier": "pm", "runtime": "codex",
                                     "may_dispatch": ["py-writer", "checker"]}})

    def test_local_only_for_a_worktree_worker_with_scopes_and_a_profile(self):
        self.assertEqual(org_with({"coder": LOCAL}).role("coder").runtime, "local")
        with self.assertRaisesRegex(DurehError, "runtime local needs profile"):
            org_with({"coder": {k: v for k, v in LOCAL.items() if k != "profile"}})
        with self.assertRaisesRegex(DurehError, "runtime local needs isolation worktree"):
            org_with({"coder": {**LOCAL, "isolation": "scratch"}})
        with self.assertRaisesRegex(DurehError, "runtime local needs write scopes"):
            org_with({"coder": {**LOCAL, "write_scopes": []}})
        with self.assertRaisesRegex(DurehError, "runtime local is only for a worker without the verify"):
            org_with({"checker": {"runtime": "local"}})
        with self.assertRaisesRegex(DurehError, "runtime local is only for a worker"):
            org_with({"pm-modules": {"kind": "manager", "tier": "pm", "runtime": "local", "profile": "p",
                                     "may_dispatch": ["py-writer", "checker"]}})

    def test_unknown_values_and_profile_without_local(self):
        with self.assertRaisesRegex(DurehError, "runtime must be one of claude-code, codex, local"):
            org_with({"coder": {**LOCAL, "runtime": "other"}})
        with self.assertRaisesRegex(DurehError, "runtime_fallback must be one of claude-code, none"):
            org_with({"coder": {**LOCAL, "runtime_fallback": "codex"}})
        with self.assertRaisesRegex(DurehError, "profile is read only by the local runtime"):
            org_with({"coder": {"kind": "worker", "tier": "worker", "profile": "p", "write_scopes": ["modules/*/src/**"]}})

    def test_the_profile_must_be_a_package_file(self):
        m = yamlsub.load_file(FIXTURE_PACKAGE / "startup.yaml")
        m["roles"]["coder"] = dict(LOCAL)
        with self.assertRaisesRegex(DurehError, "missing model profile of role coder"):
            pkg.check(m, FIXTURE_PACKAGE)
        m["roles"]["coder"]["profile"] = "../outside.Modelfile"
        with self.assertRaisesRegex(DurehError, "lies outside the package"):
            pkg.check(m, FIXTURE_PACKAGE)


class AgentFileAndSpawnHook(MemberCase):
    def test_agent_file_says_the_harness_runs_it(self):
        text = (self.site_root / ".claude" / "agents" / "py-local.md").read_text()
        self.assertIn("Run by the harness on the local runtime (`dureh member run`), not spawned", text)
        self.assertIn("member run --wi <WI> --fallback --reason", text)
        checker = (self.site_root / ".claude" / "agents" / "checker.md").read_text()
        self.assertIn("runs on the `codex` runtime", checker)
        writer = (self.site_root / ".claude" / "agents" / "py-writer.md").read_text()
        self.assertNotIn("member run --wi", writer)
        pm = (self.site_root / ".claude" / "agents" / "pm-modules.md").read_text()
        self.assertIn("Roles run by the harness, not spawned: checker (codex), py-local (local)", pm)
        desk = (self.site_root / "CLAUDE.md").read_text()
        self.assertIn("member run --wi <WI>", desk)

    def test_brief_names_the_runtime_and_the_allowlist(self):
        wi = self.item()
        b = self.wf.dispatch_brief(self.s, wi, "pm-modules")
        self.assertEqual(b["runtime"], "local")
        self.assertIn("Runtime: local (the harness runs this brief with `dureh member run`", b["brief"])
        self.assertIn("Write allowlist (exact files, relative to your work root): src/demo.py", b["brief"])
        from dureh_core.members import brief_path

        digest = b["header"].split("brief=")[1]
        self.assertEqual(brief_path(self.s, wi, digest).read_text(), b["brief"])

    def spawn(self, target, brief):
        return hook(self.s, hook_event_name="PreToolUse", tool_name="Agent", agent_type="pm-modules", agent_id="a-pm",
                    cwd=str(self.site_root), tool_input={"subagent_type": target, "prompt": brief, "description": "x"})

    def test_spawn_hook_refuses_a_member_role_without_a_recorded_fallback(self):
        wi = self.item()
        b = self.wf.dispatch_brief(self.s, wi, "pm-modules")
        out = self.spawn("py-local", b["brief"])
        self.assertTrue(denied(out))
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(f"run it with `dureh member run --wi {wi}`", reason)
        self.assertIn("--fallback", reason)
        self.assertEqual(self.events(wi, "wi.dispatched"), [])

    def test_fallback_is_a_separate_explicit_choice(self):
        from dureh_core import members

        wi = self.item()
        b = self.wf.dispatch_brief(self.s, wi, "pm-modules")
        with self.assertRaisesRegex(DurehError, "--fallback needs --reason"):
            members.run(self.s, "pm-modules", wi, fallback=True)
        r = members.run(self.s, "pm-modules", wi, fallback=True, reason="office hours closed")
        self.assertTrue(r["fallback"])
        self.assertIsNone(self.spawn("py-local", b["brief"]))
        d = self.events(wi, "wi.dispatched")[-1]["payload"]
        self.assertEqual(d["runtime"], "claude-code")
        self.assertEqual(d["runtime_fallback"]["from"], "local")
        self.assertEqual(d["runtime_fallback"]["reason"], "office hours closed")
        self.assertEqual(self.led.wi_status(wi)["status"], "dispatched")

    def test_no_fallback_when_the_role_declares_none(self):
        m = self.s.package_manifest()
        self.s.org.role("py-local").runtime_fallback = "none"
        from dureh_core import members

        wi = self.item()
        self.wf.dispatch_brief(self.s, wi, "pm-modules")
        with self.assertRaisesRegex(DurehError, "declares no fallback"):
            members.run(self.s, "pm-modules", wi, fallback=True, reason="x")
        self.assertTrue(m)

    def test_a_claude_role_is_not_a_member_run(self):
        from dureh_core import members

        wi = self.item(role="py-writer")
        self.wf.dispatch_brief(self.s, wi, "pm-modules")
        with self.assertRaisesRegex(DurehError, "runs as a Claude subagent"):
            members.run(self.s, "pm-modules", wi)


class Commands(MemberCase):
    def run_cli(self, *argv, actor="pm-modules"):
        import contextlib
        import io
        import os

        from dureh_core.cli import main

        out = io.StringIO()
        os.environ["DUREH_ACTOR"] = actor
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = main(["--site", str(self.site_root), *argv])
        finally:
            os.environ.pop("DUREH_ACTOR", None)
            os.environ.pop("DUREH_OBSERVE", None)
        return rc, out.getvalue()

    def test_cli_brief_member_status_and_office_hours_plan(self):
        wi = self.item()
        rc, out = self.run_cli("dispatch", "brief", "--wi", wi)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"# py-local runs on the local runtime: run `dureh member run --wi {wi}`", out)
        self.assertNotIn("# spawn subagent_type", out)
        rc, out = self.run_cli("member", "run", "--wi", wi)
        self.assertEqual(rc, 2)
        self.assertIn("NOT_RUN: no office-hours server is running", out)
        rc, out = self.run_cli("member", "status", "--wi", wi)
        self.assertEqual(rc, 0, out)
        self.assertIn("member.not-run", out)
        rc, out = self.run_cli("--observe", "member", "status", "--wi", wi)
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("--observe", "member", "run", "--wi", wi)
        self.assertIn("not a read-only command", out)
        rc, out = self.run_cli("site", "office-hours", "plan", actor="desk")
        self.assertEqual(rc, 0, out)
        self.assertIn("create coder-test -f", out)
        rc, out = self.run_cli("member", "exec", "--run", "run-x", actor="py-local")
        self.assertIn("never from an agent", out)
        rc, out = self.run_cli("ledger", "add", "event", "--type", "member.collected", "--wi", wi, actor="desk")
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
