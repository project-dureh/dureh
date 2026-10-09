"""Release 46, the boundary cut (proposals/boundary-design-2026-10-08.md 3, 4, 5, 8.1): startup.yaml schema 2 and its
validation, the closed capability set, package roles over the generic default org, protected paths, and the single
package source (`adapter sync` re-copies startup/ from a committed revision). Proved on the neutral example package."""
import contextlib
import io
import json
import os
import subprocess
import unittest
import unittest.mock
from pathlib import Path

from dureh_core import install, yamlsub
from dureh_core.cli import main
from dureh_core.org import HARNESS_ROOT, Org
from dureh_core.package import CAPABILITIES, check, globs_overlap
from dureh_core.paths import write_decision
from dureh_core.records import Ledger
from dureh_core.runtimes import claude_code as cc
from dureh_core.util import DurehError
from tests.helpers import EXAMPLE_PACKAGE, ExampleSiteCase, SiteCase, denied, hook


def example_manifest() -> dict:
    return yamlsub.load_file(EXAMPLE_PACKAGE / "startup.yaml")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class DefaultOrg(unittest.TestCase):
    def test_harness_holds_only_the_generic_four(self):
        org = Org.load(HARNESS_ROOT)
        self.assertEqual(set(org.roles), {"desk", "manager", "worker", "checker"})
        self.assertEqual(org.role("checker").capabilities, ["verify"])
        self.assertEqual(org.role("manager").may_dispatch, ["worker", "checker"])
        self.assertEqual(sorted(org.role("desk").capabilities), ["network", "submit_jobs", "verify"])
        self.assertEqual([r.name for r in org.with_capability("network")], [], "researcher left the default org (O3)")
        text = (HARNESS_ROOT / "org" / "roles.yaml").read_text()
        for gone in ("pipeline-runner", "data-steward", "interpreter:", "pm-projects", "researcher:", "pm-harness",
                     "tool-writer", "web-writer"):
            self.assertNotIn(gone, text)

    def test_promote_is_gone(self):
        # Director, 2026-10-09: the `dureh promote` data path left the harness; `wi promote` (Gate 2) stays
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            main(["--site", "/nonexistent", "promote", "--wi", "WI-0001"])
        self.assertIn("invalid choice", err.getvalue())
        self.assertFalse((HARNESS_ROOT / "dureh_core" / "promote.py").exists())
        m = example_manifest()
        m["roles"]["builder"]["capabilities"] = ["promote"]
        with self.assertRaisesRegex(DurehError, "unknown capability 'promote'"):
            Org.load(HARNESS_ROOT, manifest=m)
        from dureh_core.cli import parser

        self.assertIn("promote", parser()._subparsers._group_actions[0].choices["wi"]._subparsers._group_actions[0].choices)

    def test_capabilities_are_a_closed_set(self):
        self.assertEqual(CAPABILITIES, ("submit_jobs", "verify", "network"))

    def test_package_roles_add_and_reskin(self):
        org = Org.load(HARNESS_ROOT, manifest=example_manifest())
        self.assertTrue({"coordinator", "builder", "runner", "publisher", "scout"} <= set(org.roles))
        self.assertEqual(org.role("checker").skill, "skills/check/SKILL.md")
        self.assertEqual(org.role("checker").capabilities, ["verify"], "the re-skinned checker keeps verify")
        self.assertIn("coordinator", org.role("desk").may_dispatch, "every manager is dispatched by the desk")
        self.assertIn("scout", org.role("desk").may_dispatch, "desk: additions")
        self.assertIn("projects/*/README.md", org.role("desk").write_scopes)

    def test_package_may_replace_manager_and_worker(self):
        m = {"schema_version": 2, "package": {"id": "x", "name": "X"},
             "roles": {"worker": {"kind": "worker", "tier": "worker", "write_scopes": ["modules/*/lib/**"]}}}
        org = Org.load(HARNESS_ROOT, manifest=m)
        self.assertEqual(org.role("worker").write_scopes, ["modules/*/lib/**"])
        self.assertEqual(org.role("worker").source, "package")


class Validation(unittest.TestCase):
    def load(self, edit):
        m = example_manifest()
        edit(m)
        Org.load(HARNESS_ROOT, manifest=m)
        return check(m)

    def refused(self, edit, msg):
        with self.assertRaisesRegex(DurehError, msg):
            self.load(edit)

    def test_good_package_has_no_warnings(self):
        self.assertEqual(self.load(lambda m: None), [])

    def test_unknown_top_level_key_is_refused(self):
        self.refused(lambda m: m.update(iterators={"x": {}}), "unknown top-level key\\(s\\) iterators \\(refused since release 49")

    def test_organization_enable_list_is_refused(self):
        self.refused(lambda m: m.update(organization={"managers": ["coordinator"], "workers": ["builder"]}),
                     "organization .*no longer read.*`roles` is authoritative")

    def test_harness_min(self):
        self.refused(lambda m: m["package"].update(harness_min=999), "needs harness release 999")
        self.refused(lambda m: m["package"].update(harness_min="soon"), "harness_min must be a release number")

    def test_role_refusals(self):
        r = lambda m: m["roles"]  # noqa: E731
        self.refused(lambda m: r(m).update(Bad_Name={"kind": "worker", "tier": "worker"}), "must match")
        self.refused(lambda m: r(m).update(desk={"kind": "desk", "tier": "desk"}), "desk is reserved")
        self.refused(lambda m: r(m)["checker"].update(write_scopes=["modules/*/x/**"]), "checker is reserved")
        self.refused(lambda m: r(m)["checker"].update(capabilities=["network"]), "checker is reserved")
        self.refused(lambda m: r(m)["builder"].update(capabilities=["build_containers"]), "unknown capability 'build_containers'")
        self.refused(lambda m: r(m)["builder"].update(kind="desk"), "kind must be manager or worker")
        self.refused(lambda m: r(m)["builder"].update(tier="nope"), "unknown tier nope")
        self.refused(lambda m: r(m)["builder"].update(may_dispatch=["runner"]), "is a worker; only the desk and managers")
        self.refused(lambda m: r(m)["coordinator"].update(may_dispatch=["builder"]), "no role with the verify capability")
        self.refused(lambda m: r(m).update(boss={"kind": "manager", "tier": "pm", "may_dispatch": ["coordinator", "checker"]}),
                     "may dispatch only workers")
        self.refused(lambda m: r(m)["coordinator"].update(may_dispatch=["ghost", "checker"]), "unknown role ghost")
        self.refused(lambda m: r(m).update(auditor={"kind": "worker", "tier": "recount", "capabilities": ["verify"],
                                                    "write_scopes": ["modules/*/x/**"]}), "may not have write scopes")
        self.refused(lambda m: r(m)["builder"].update(isolation="vm"), "isolation must be worktree or scratch")
        self.refused(lambda m: r(m)["builder"].update(colour="red"), "unknown key")
        self.refused(lambda m: r(m)["builder"].update(write_scopes=["state/records/**"]), "write scope")
        self.refused(lambda m: r(m)["builder"].update(write_scopes=["site:notes/**"]), "write scope")
        self.refused(lambda m: m["desk"].update(write_scopes=["site:state/**"]), "site:\\* scopes are harness-owned")

    def test_label_protected_and_policy_refusals(self):
        self.refused(lambda m: m["labels"].update(levels=["part", "any"]), "reserved")
        self.refused(lambda m: m["labels"]["item_grammar"].update(other="^x$"), "not a level")
        self.refused(lambda m: m["labels"]["item_grammar"].update(sample="(unclosed"), "not a regular expression")
        self.refused(lambda m: m["labels"]["layout"].update(final="a/b"), "one directory name")
        self.refused(lambda m: m["protected"].append({"path": "x/**", "level": "secret"}), "no-read, read-only or none")
        self.refused(lambda m: m["protected"].append({"path": "/abs/**", "level": "no-read"}), "segment-aware glob")
        self.refused(lambda m: m["protected"].append({"path": "a/../b", "level": "no-read"}), "segment-aware glob")
        self.refused(lambda m: m["policies"].update(other={}), "only loop_rule")
        self.refused(lambda m: m.update(schema_version=3), "schema_version 3 is not supported")

    def test_scope_over_a_protected_path_is_refused(self):
        m = example_manifest()
        m["roles"]["publisher"]["write_scopes"] = ["projects/*/published/**"]
        org = Org.load(HARNESS_ROOT, manifest=m)
        from dureh_core.package import role_scope_conflicts

        self.assertTrue(role_scope_conflicts(m, org.roles.values()))
        self.assertTrue(globs_overlap("projects/*/published/**", "projects/p1/published/x/y"))
        self.assertTrue(globs_overlap("projects/*/**", "projects/p/inputs/**"))
        self.assertFalse(globs_overlap("projects/*/checkpoints/**", "projects/*/inputs/**"))
        self.assertFalse(globs_overlap("modules/*/src/**", "projects/*/inputs/**"))


class Schema1(SiteCase):
    """Release 49: schema 1 (it loaded in release 46 only) is refused at install, `adapter sync` and `verify --self`."""

    OLD = ("package:\n  id: old\n  name: Old\nvalues:\n  - id: v1\n    title: Modules\n    skill: skills/code/SKILL.md\n"
           "organization:\n  managers: [manager]\n  workers: [worker, checker]\n")

    def test_schema_1_is_refused_at_install(self):
        for head in ("schema_version: 1\n", ""):  # an explicit 1, or no schema_version at all
            (self.package / "startup.yaml").write_text(head + self.OLD)
            with self.assertRaisesRegex(DurehError, "schema 1 is no longer supported.*move the package to schema 2"):
                install.validate_package(self.package)
            self.assertEqual(install.package_warnings(self.package), [], "a package that does not load warns nothing")
            with self.assertRaisesRegex(DurehError, "schema 1 is no longer supported"):
                self.install()
        self.assertFalse((HARNESS_ROOT / "org" / "legacy").exists(), "the legacy schema-1 roles are gone")

    def test_schema_1_is_refused_by_sync_and_verify_self(self):
        self.install()
        s = self.site()
        (s.startup / "startup.yaml").chmod(0o644)
        (s.startup / "startup.yaml").write_text("schema_version: 1\n" + self.OLD)
        run_self = __import__("dureh_core.selftest", fromlist=["run_self"]).run_self
        with unittest.mock.patch.dict(os.environ, {"DUREH_OBSERVE": "1"}):  # `--observe verify --self`
            r = run_self(self.site(), unit=False)
        self.assertEqual(r["result"], "fail")
        self.assertTrue(any("schema 1 is no longer supported" in p for p in r["problems"]), r["problems"])
        for call in (lambda: run_self(self.site(), unit=False), lambda: self.site().org):  # recording needs the org
            with self.assertRaisesRegex(DurehError, "schema 1 is no longer supported"):
                call()
        (self.package / "startup.yaml").write_text("schema_version: 1\n" + self.OLD)  # the package source
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--site", str(self.site_root), "adapter", "sync"]), 2)
        self.assertIn("schema 1 is no longer supported", err.getvalue())


class Capabilities(ExampleSiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()

    def test_tools_come_from_kind_capabilities_and_scopes(self):
        org = self.s.org
        self.assertEqual(cc.role_tools(org.role("coordinator")), ["Agent", "Bash", "Read", "Grep", "Glob"])
        self.assertEqual(cc.role_tools(org.role("builder")), ["Bash", "Read", "Grep", "Glob", "Edit", "Write"])
        self.assertEqual(cc.role_tools(org.role("scout")), ["Bash", "Read", "Grep", "Glob", "WebSearch", "WebFetch"])
        self.assertEqual(cc.role_tools(org.role("checker")), ["Bash", "Read", "Grep", "Glob"])
        agents = self.site_root / ".claude" / "agents"
        self.assertIn("tools: Bash, Read, Grep, Glob, WebSearch, WebFetch", (agents / "scout.md").read_text())
        self.assertIn("slurm submit --wi <WI> --script", (agents / "runner.md").read_text())
        self.assertNotIn("slurm submit --wi <WI> --script", (agents / "builder.md").read_text())
        self.assertNotIn("dureh promote", (agents / "publisher.md").read_text())
        self.assertIn("verify --wi <WI>", (agents / "checker.md").read_text())
        self.assertIn("Report the number of checks that passed first.", (agents / "checker.md").read_text())
        self.assertIn("Name job scripts after their step", (agents / "runner.md").read_text())

    def test_event_authority_by_kind_and_capability(self):
        org = self.s.org
        self.assertTrue(org.may_append("runner", "run.status"))
        self.assertFalse(org.may_append("builder", "run.status"))
        self.assertTrue(org.may_append("checker", "verification.recorded"))
        self.assertFalse(org.may_append("runner", "verification.recorded"))
        self.assertTrue(org.may_append("coordinator", "dispatch.prepared"))
        self.assertFalse(org.may_append("builder", "container.built"))

    def test_network_needs_the_capability(self):
        def web(role):
            data = dict(hook_event_name="PreToolUse", tool_name="WebFetch", tool_input={"url": "https://example.org"},
                        cwd=str(self.site_root))
            if role != "desk":
                data.update(agent_type=role, agent_id="a1")
            return hook(self.s, **data)

        self.assertFalse(denied(web("desk")))
        self.assertFalse(denied(web("scout")))
        out = web("builder")
        self.assertTrue(denied(out))
        self.assertIn("network capability (desk, scout)", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_bootstrap_item_goes_to_the_values_manager(self):
        w = Ledger(self.s).work_items()[0]
        self.assertEqual(w["role"], "coordinator")

    def test_desk_text_names_capabilities_and_no_removed_words(self):
        text = (self.site_root / "CLAUDE.md").read_text()
        self.assertIn("submit_jobs: runner; verify: checker; network: scout", text)
        self.assertIn("Package loop rule: Example rule: every step loops over samples", text)
        for gone in ("container build", "pipeline-runner", "data-steward", "Verified checkpoints reach", "file level",
                     "Director 2026-10-06): one array task per file"):
            self.assertNotIn(gone, text)


class Protected(ExampleSiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        self.proj = self.site_root / "projects" / "p1"
        (self.proj / "inputs").mkdir(parents=True)
        (self.proj / "inputs" / "a.txt").write_text("secret\n")
        (self.proj / "published").mkdir()
        (self.proj / "checkpoints").mkdir()

    def pre(self, tool, role="desk", **ti):
        data = dict(hook_event_name="PreToolUse", tool_name=tool, tool_input=ti, cwd=str(self.site_root))
        if role != "desk":
            data.update(agent_type=role, agent_id="a1")
        return hook(self.s, **data)

    def test_writes_are_refused_for_every_role_including_the_desk(self):
        for role in self.s.org.roles:
            for rel in ("inputs/a.txt", "published/x.dat", "inputs/new/b.txt"):
                ok, why = write_decision(self.s, role, self.proj / rel)
                self.assertFalse(ok, (role, rel))
                self.assertIn("is protected", why)
        ok, _ = write_decision(self.s, "desk", self.proj / "README.md")
        self.assertTrue(ok, "outside the protected areas the desk scope stands")
        ok, _ = write_decision(self.s, "runner", self.proj / "checkpoints" / "x")
        self.assertTrue(ok)
        out = self.pre("Write", file_path=str(self.proj / "published" / "x.dat"), content="x")
        self.assertTrue(denied(out))

    def test_worktree_paths_map_to_the_target(self):
        from dureh_core import workflow as wf

        b = wf.wi_new(self.s, "desk", "B", "g", "coordinator", "projects/p1", ["ok"])["id"]
        w = wf.wi_new(self.s, "desk", "W", "g", "builder", "projects/p1", ["ok"], parent=b)["id"]
        wt = self.site_root / "worktrees" / w
        (wt / "inputs").mkdir(parents=True)
        ok, why = write_decision(self.s, "builder", wt / "inputs" / "x", agent_wi=w)
        self.assertFalse(ok)
        self.assertIn("projects/*/inputs/**", why)

    def test_no_read_refuses_reads_and_content_readers_not_listing(self):
        secret = str(self.proj / "inputs" / "a.txt")
        for role in ("desk", "builder", "checker"):
            self.assertTrue(denied(self.pre("Read", role, file_path=secret)), role)
            self.assertTrue(denied(self.pre("Grep", role, pattern="x", path=str(self.proj / "inputs"))), role)
            for cmd in (f"cat {secret}", "head -n 3 projects/p1/inputs/a.txt", f"grep x {secret}",
                        f"python3 {secret}", f"cp {secret} /tmp/x", f"wc -l < {secret}", "cat projects/p1/inputs/*.txt"):
                self.assertTrue(denied(self.pre("Bash", role, command=cmd)), (role, cmd))
            self.assertFalse(denied(self.pre("Bash", role, command="ls projects/p1/inputs")), role)
            self.assertFalse(denied(self.pre("Glob", role, pattern="projects/p1/inputs/*")), role)
            self.assertFalse(denied(self.pre("Read", role, file_path=str(self.proj / "published" / "x"))),
                             "read-only areas stay readable")
        evs = [e for e in Ledger(self.s).events() if e["type"] == "guard.denied"]
        self.assertTrue(any("no-read" in e["payload"]["reason"] and e["payload"]["tool"] == "Read" for e in evs))

    def test_settings_matcher_has_read_only_with_a_no_read_rule(self):
        settings = json.loads((self.site_root / ".claude" / "settings.json").read_text())
        self.assertIn("|Read|Grep", settings["hooks"]["PreToolUse"][0]["matcher"])

    def test_protected_list_cli_and_observe(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["--site", str(self.site_root), "--observe", "protected", "list"]), 0)
        self.assertIn("projects/*/inputs/**  no-read", buf.getvalue())
        self.assertIn("from package example-org 1.0.0", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["--site", str(self.site_root), "--observe", "protected", "list", "--json"]), 0)
        out = json.loads(buf.getvalue())
        self.assertEqual([r["level"] for r in out["rules"]], ["no-read", "read-only"])
        self.assertEqual(out["rules"][0]["source"]["package"], "example-org")
        self.assertEqual(len(Ledger(self.s).load("event")), len(Ledger(self.site()).load("event")))


class ProtectedNoneAndLinks(ExampleSiteCase):
    """Director, 2026-10-09: `level: none` lifts the package's own rules (most specific wins, later on a tie), never a
    harness zone; symlinks inside a protected area are resolved at sync and their real targets are guarded too."""

    def setUp(self):
        super().setUp()
        m = self.package / "startup.yaml"
        m.write_text(m.read_text().replace(
            "  - {path: projects/*/published/**, level: read-only, why: published results are never edited by an agent}\n",
            "  - {path: projects/*/published/**, level: read-only, why: published results are never edited by an agent}\n"
            "  - {path: projects/p-test/**, level: none, why: the test project is unprotected}\n"))
        self.outside = self.tmp / "lab-data"
        (self.outside / "raw").mkdir(parents=True)
        (self.outside / "raw" / "f.txt").write_text("raw\n")
        (self.outside / "other").mkdir()
        (self.outside / "other" / "g.txt").write_text("other\n")
        self.install()
        for name in ("p1", "p-test"):
            (self.site_root / "projects" / name / "inputs").mkdir(parents=True)
            (self.site_root / "projects" / name / "inputs" / "a.txt").write_text("x\n")
        (self.site_root / "projects" / "p1" / "inputs" / "raw").symlink_to(self.outside / "raw")
        (self.site_root / "projects" / "p-test" / "inputs" / "other").symlink_to(self.outside / "other")
        (self.site_root / "projects" / "p2").mkdir()
        (self.site_root / "projects" / "p2" / "inputs").symlink_to(self.outside)  # the area itself is a link
        cc.generate(self.site())
        self.s = self.site()

    def pre(self, tool, role="desk", **ti):
        data = dict(hook_event_name="PreToolUse", tool_name=tool, tool_input=ti, cwd=str(self.site_root))
        if role != "desk":
            data.update(agent_type=role, agent_id="a1")
        return hook(self.s, **data)

    def test_none_lifts_the_package_rules_only(self):
        lifted = self.site_root / "projects" / "p-test" / "inputs" / "a.txt"
        self.assertFalse(denied(self.pre("Read", file_path=str(lifted))))
        self.assertFalse(denied(self.pre("Bash", command=f"cat {lifted}")))
        self.assertTrue(denied(self.pre("Read", file_path=str(self.site_root / "projects" / "p1" / "inputs" / "a.txt"))))
        ok, why = write_decision(self.s, "runner", self.site_root / "projects" / "p-test" / "published" / "x")
        self.assertFalse(ok)
        self.assertNotIn("protected", why, "lifted: the role scope decides")
        ok, _ = write_decision(self.s, "runner", self.site_root / "projects" / "p-test" / "checkpoints" / "x")
        self.assertTrue(ok)
        ok, why = write_decision(self.s, "runner", self.site_root / "projects" / "p1" / "published" / "x")
        self.assertIn("is protected", why)

    def test_none_never_lifts_a_harness_zone(self):
        m = self.site_root / "startup" / "startup.yaml"
        m.chmod(0o644)
        m.write_text(m.read_text().replace("  - {path: projects/p-test/**, level: none",
                                           "  - {path: '**', level: none, why: all}\n  - {path: projects/p-test/**, level: none"))
        s = self.site()
        for rel, zone in (("state/records/event/x.json", "records"), ("config.json", None), (".claude/settings.json", "generated"),
                          ("startup/startup.yaml", "installed package")):
            ok, why = write_decision(s, "builder", self.site_root / rel)
            self.assertFalse(ok, rel)
        ok, why = write_decision(s, "desk", self.site_root / "state" / "records" / "x")
        self.assertIn("append-only", why)

    def test_most_specific_then_later_wins(self):
        from dureh_core.protect import best_rule

        rs = [{"path": "projects/*/inputs/**", "level": "no-read"}, {"path": "projects/p/inputs/**", "level": "read-only"},
              {"path": "projects/p/inputs/**", "level": "none"}]
        self.assertEqual(best_rule(rs, ["projects/p/inputs/a"])["level"], "none", "a tie: the later rule")
        self.assertEqual(best_rule(rs[:2], ["projects/p/inputs/a"])["level"], "read-only", "the more specific rule")
        self.assertEqual(best_rule(rs, ["projects/q/inputs/a"])["level"], "no-read")

    def test_none_outside_any_stricter_rule_warns(self):
        m = example_manifest()
        m["protected"].append({"path": "modules/*/scratch/**", "level": "none"})
        warns = check(m)
        self.assertTrue(any("modules/*/scratch/** none` lies under no stricter rule" in w for w in warns), warns)
        m["protected"].pop()
        m["protected"].append({"path": "projects/p-test/**", "level": "none"})
        self.assertEqual(check(m), [])

    def test_symlink_targets_are_resolved_and_guarded(self):
        data = json.loads((self.site_root / ".claude" / "dureh-protected-links.json").read_text())
        targets = {(x["link"], x["target"], x["level"]) for x in data["links"]}
        self.assertIn(("projects/p1/inputs/raw", str((self.outside / "raw").resolve()), "no-read"), targets)
        self.assertIn(("projects/p2/inputs", str(self.outside.resolve()), "no-read"), targets)
        self.assertFalse(any(x["link"].startswith("projects/p-test/") for x in data["links"]), "a lifted link is not stored")
        real = self.outside / "raw" / "f.txt"
        for role in ("desk", "builder"):
            self.assertTrue(denied(self.pre("Read", role, file_path=str(real))), role)
            self.assertTrue(denied(self.pre("Grep", role, pattern="x", path=str(self.outside / "raw"))), role)
            self.assertTrue(denied(self.pre("Bash", role, command=f"head {real}")), role)
        self.assertTrue(denied(self.pre("Read", file_path=str(self.outside / "other" / "g.txt"))),
                        "reached through p2's protected link too")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["--site", str(self.site_root), "--observe", "protected", "list"]), 0)
        self.assertIn(f"{(self.outside / 'raw').resolve()}  no-read  (link projects/p1/inputs/raw, rule projects/*/inputs/**)",
                      buf.getvalue())
        self.assertEqual(cc.drift(self.site()), [], "the links file is a tracked generated file")


class FollowUps(ExampleSiteCase):
    """Fixes of 2026-10-09 found while drafting a company package on this release."""

    def test_scope_conflicts_follow_the_runtime_precedence(self):
        from dureh_core.package import effective_rule, role_scope_conflicts
        from dureh_core.protect import best_rule

        m = example_manifest()
        m["protected"] = [{"path": "projects/*/published/**", "level": "read-only"},
                          {"path": "projects/*/**/.git/**", "level": "read-only"},
                          {"path": "projects/p-test/**", "level": "none"}]
        m["roles"]["runner"]["write_scopes"] = ["projects/p-test/checkpoints/**", "projects/p-test/published/**"]
        org = Org.load(HARNESS_ROOT, manifest=m)
        self.assertEqual(role_scope_conflicts(m, org.roles.values()), [], "inside the lifted area nothing conflicts")
        w = "projects/p-test/checkpoints/.git/x"
        self.assertEqual(effective_rule(m["protected"], w), best_rule(m["protected"], [w]))
        self.assertEqual(effective_rule(m["protected"], w)["level"], "none",
                         "`.git` inside the lifted area: the more specific none rule decides, as at run time")
        m["roles"]["runner"]["write_scopes"] = ["projects/*/checkpoints/**"]
        org = Org.load(HARNESS_ROOT, manifest=m)
        out = role_scope_conflicts(m, org.roles.values())
        self.assertEqual(len(out), 1, out)
        self.assertIn("projects/*/**/.git/**", out[0])
        m["roles"]["runner"]["write_scopes"] = ["projects/*/published/**"]
        org = Org.load(HARNESS_ROOT, manifest=m)
        self.assertTrue(role_scope_conflicts(m, org.roles.values()))
        # the same package installs; the runtime agrees with the validation
        pm = self.package / "startup.yaml"
        pm.write_text(pm.read_text().replace("      - projects/*/checkpoints/**\n", "      - projects/p-test/checkpoints/**\n")
                      .replace("  - {path: projects/*/inputs/**, level: no-read, why: example raw inputs are never read by agents}\n",
                               "  - {path: projects/*/**/.git/**, level: read-only, why: repository internals}\n"
                               "  - {path: projects/p-test/**, level: none, why: test project}\n"))
        self.install()
        s = self.site()
        ok, why = write_decision(s, "runner", self.site_root / "projects" / "p-test" / "checkpoints" / ".git" / "x")
        self.assertTrue(ok, why)
        ok, why = write_decision(s, "desk", self.site_root / "projects" / "p1" / "a" / ".git" / "x")
        self.assertIn("is protected", why)

    def test_package_roles_replace_the_generic_manager_and_worker(self):
        org = Org.load(HARNESS_ROOT, manifest=example_manifest())
        self.assertNotIn("manager", org.roles)
        self.assertNotIn("worker", org.roles)
        self.assertEqual({"desk", "checker"} <= set(org.roles), True)
        self.assertNotIn("manager", org.role("desk").may_dispatch)
        self.assertFalse(any(org.scope_allows(r, "modules/m/src/x.py") for r in org.roles if r != "builder"),
                         "only the package's own role writes module sources")
        only_workers = {"schema_version": 2, "package": {"id": "x", "name": "X"},
                        "roles": {"doer": {"kind": "worker", "tier": "worker", "write_scopes": ["modules/*/lib/**"]}}}
        org = Org.load(HARNESS_ROOT, manifest=only_workers)
        self.assertIn("manager", org.roles, "no package manager: the generic one stays")
        self.assertNotIn("worker", org.roles)
        self.assertEqual(org.role("manager").may_dispatch, ["checker"])
        explicit = {**only_workers, "roles": {**only_workers["roles"],
                                              "worker": {"kind": "worker", "tier": "worker", "write_scopes": []}}}
        self.assertEqual(Org.load(HARNESS_ROOT, manifest=explicit).role("worker").source, "package",
                         "a package may still declare `worker` itself")
        self.install()
        agents = {p.stem for p in (self.site_root / ".claude" / "agents").glob("*.md")}
        self.assertEqual(agents, {"coordinator", "builder", "runner", "publisher", "scout", "checker"})

    def test_desk_text_lists_every_protected_rule(self):
        pm = self.package / "startup.yaml"
        extra = "".join(f"  - {{path: projects/*/area{i}/**, level: read-only, why: r{i}}}\n" for i in range(7))
        pm.write_text(pm.read_text().replace("templates:\n", "") .replace("  project: templates/project\n",
                      "") .replace("guards:\n", extra + "  - {path: projects/p-test/**, level: none, why: t}\ntemplates:\n"
                                   "  project: templates/project\nguards:\n", 1))
        self.install()
        text = (self.site_root / "CLAUDE.md").read_text()
        for i in range(7):
            self.assertIn(f"`projects/*/area{i}/**` read-only", text)
        self.assertIn("`projects/p-test/**` none", text)
        self.assertIn("`projects/*/inputs/**` no-read", text)
        self.assertNotIn("; ...", text)


class NoProtection(SiteCase):
    def test_none_declared_and_reads_unhooked(self):
        self.install()
        s = self.site()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["--site", str(self.site_root), "--observe", "protected", "list"]), 0)
        self.assertIn("none declared: interim default (workspace rule 2)", buf.getvalue())
        settings = json.loads((self.site_root / ".claude" / "settings.json").read_text())
        self.assertNotIn("Read", settings["hooks"]["PreToolUse"][0]["matcher"])
        self.assertIsNone(hook(s, hook_event_name="PreToolUse", tool_name="Read",
                               tool_input={"file_path": str(self.site_root / "config.json")}, cwd=str(self.site_root)))


class PackageSource(ExampleSiteCase):
    """Option A (section 4): the site runs a copy of a committed package revision; `adapter sync` re-copies it."""

    def setUp(self):
        super().setUp()
        git(self.package, "init", "-q", "-b", "main")
        git(self.package, "add", "-A")
        git(self.package, "commit", "-qm", "package 1.0.0")
        self.head = git(self.package, "rev-parse", "HEAD")

    def sync(self, *extra):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--site", str(self.site_root), "adapter", "sync", *extra])
        return code, buf.getvalue()

    def test_install_pins_the_committed_head(self):
        self.install()
        cfg = json.loads((self.site_root / "config.json").read_text())
        self.assertEqual(cfg["startup"]["rev"], self.head)
        dep = json.loads((self.site_root / "deployment.json").read_text())
        self.assertEqual(dep["package"]["rev"], self.head)
        self.assertFalse((self.site_root / "startup" / ".git").exists())

    def test_install_refuses_an_uncommitted_source(self):
        (self.package / "CORE-VALUES.md").write_text("edited\n")
        with self.assertRaisesRegex(DurehError, "uncommitted changes"):
            self.install()

    def test_sync_copies_the_new_revision_and_records_it(self):
        self.install()
        (self.package / "skills" / "build" / "SKILL.md").write_text("# Build v2\n")
        m = self.package / "startup.yaml"
        m.write_text(m.read_text().replace("version: 1.0.0", "version: 1.1.0"))
        code, out = self.sync()
        self.assertNotEqual(code, 0, "uncommitted source is refused")
        self.assertEqual((self.site_root / "startup" / "skills" / "build" / "SKILL.md").read_text(),
                         "# Build small tools\n\nKeep changes small and add a regression test.\n")
        git(self.package, "commit", "-qam", "1.1.0")
        new = git(self.package, "rev-parse", "HEAD")
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertIn(f"package example-org: {self.head} -> {new} (2 file(s) changed; package.synced recorded)", out)
        self.assertEqual((self.site_root / "startup" / "skills" / "build" / "SKILL.md").read_text(), "# Build v2\n")
        s = self.site()
        self.assertEqual((s.config["startup"]["rev"], s.config["startup"]["version"]), (new, "1.1.0"))
        ev = [e for e in Ledger(s).events() if e["type"] == "package.synced"]
        self.assertEqual(ev[-1]["payload"]["changed"], ["skills/build/SKILL.md", "startup.yaml"])
        self.assertEqual((ev[-1]["payload"]["from"], ev[-1]["payload"]["to"]), (self.head, new))
        self.assertEqual(json.loads((self.site_root / "deployment.json").read_text())["package"]["rev"], new)
        self.assertFalse((self.site_root / "startup.new").exists() or (self.site_root / "startup.old").exists())
        code, out = self.sync()
        self.assertIn("(unchanged)", out)
        self.assertEqual(len([e for e in Ledger(self.site()).events() if e["type"] == "package.synced"]), 1)
        code, out = self.sync("--package-rev", self.head)  # roll back to the earlier revision
        self.assertEqual(code, 0, out)
        self.assertEqual(self.site().config["startup"]["rev"], self.head)
        self.assertIn("Keep changes small", (self.site_root / "startup" / "skills" / "build" / "SKILL.md").read_text())

    def test_an_invalid_revision_leaves_the_installed_package(self):
        self.install()
        m = self.package / "startup.yaml"
        m.write_text(m.read_text().replace("capabilities: [submit_jobs]", "capabilities: [teleport]"))
        git(self.package, "commit", "-qam", "broken")
        code, _ = self.sync()
        self.assertNotEqual(code, 0)
        self.assertIn("capabilities: [submit_jobs]", (self.site_root / "startup" / "startup.yaml").read_text())
        self.assertEqual(self.site().config["startup"]["rev"], self.head)
        self.assertFalse((self.site_root / "startup.new").exists())
        code, _ = self.sync("--package-rev", "no-such-rev")
        self.assertNotEqual(code, 0)

    def test_without_a_source_startup_is_kept(self):
        self.install()
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["startup"]["source"] = str(self.tmp / "moved-away")
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertIn("does not exist; startup/ kept as installed", out)
        code, _ = self.sync("--package-rev", "HEAD")
        self.assertNotEqual(code, 0)


class NonGitSource(SiteCase):
    def test_a_source_without_git_is_copied_and_hashed(self):
        self.install()
        rev = self.site().config["startup"]["rev"]
        self.assertTrue(rev.startswith("sha256:"))
        self.assertEqual(rev, install.tree_digest(self.site_root / "startup"))
        (self.package / "CORE-VALUES.md").write_text("# Test values\n\n1. Changed.\n")
        rec = install.sync_package(self.site())
        self.assertEqual(rec["changed"], ["CORE-VALUES.md"])
        self.assertNotEqual(self.site().config["startup"]["rev"], rev)


class WatchDefault(SiteCase):
    def test_new_sites_watch_on_demand_existing_keep_monitor(self):
        from dureh_core.ops import watch_mode

        self.install()
        s = self.site()
        self.assertEqual(s.config["slurm"]["watch"], "on-demand")
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["slurm"].pop("watch")
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(watch_mode(self.site()), "monitor", "an existing site without the key keeps monitor")


class ConfigWarnings(SiteCase):
    def test_keys_this_release_ignores_or_refuses_are_named(self):
        from dureh_core.ops import config_errors, config_warnings

        self.install()
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["project"] = {"manifest": "m.yaml", "pipeline_version_key": "v", "derivative_entry": "{x}"}
        cfg["containers"] = {"cache_env": "X", "manifest_globs": ["*.yaml"], "mem": "4G"}
        cfg["slurm"]["external_ignore"] = ["cc-*"]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        w = config_warnings(self.site())
        self.assertEqual(len(w), 2, w)
        self.assertIn("project.pipeline_version_key, project.derivative_entry", w[0])
        self.assertIn("containers.manifest_globs, containers.mem", w[1])
        e = config_errors(self.site())
        self.assertEqual(len(e), 1, e)
        self.assertIn("slurm.external_ignore is refused since release 49", e[0])
        r = __import__("dureh_core.selftest", fromlist=["run_self"]).run_self(self.site(), unit=False)
        self.assertEqual(r["result"], "fail", r)
        self.assertEqual(len(r["counts"]["warnings"]), 2)
        self.assertTrue(any("external_ignore" in p for p in r["problems"]), r["problems"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--site", str(self.site_root), "adapter", "sync"]), 2)
        self.assertIn("slurm.external_ignore is refused", err.getvalue())
        cfg["slurm"].pop("external_ignore")
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        r = __import__("dureh_core.selftest", fromlist=["run_self"]).run_self(self.site(), unit=False)
        self.assertEqual(r["result"], "pass", r)


class RemovedRoles(SiteCase):
    def test_status_warns_about_open_items_of_a_removed_role(self):
        from dureh_core.ops import render_status, status

        self.install()
        s = self.site()
        led = Ledger(s)
        rec = led.work_items()[0]
        # an item recorded before release 46 for a role this organization no longer defines
        path = s.record_dir("work-item") / f"{rec['id']}.json"
        data = json.loads(path.read_text())
        data["payload"]["role"] = "gone-runner"
        path.chmod(0o644)
        path.write_text(json.dumps(data))
        text = render_status(status(self.site()))
        self.assertIn(f"{rec['id']} (gone-runner)", text)
        self.assertIn("role is not in this organization", text)


if __name__ == "__main__":
    unittest.main()
