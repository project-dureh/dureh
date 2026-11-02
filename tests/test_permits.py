"""Release 89 phase 2 (Director 2026-10-30, dureh-team#111 Approve 1; proposal site-permissions-2026-10-30 sections 2,
3 and 5, accepted with dureh-team#112): `dureh site permit`, the permit catalog at the site; `github item ask
--permit` binding a plan to a board Decision; the `site link-project` approval check. The gh subprocess is the fake
board of test_github; no test touches the network."""
import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest import mock

from helpers import SiteCase
from test_github import FAKE_TOKEN, ORG, FakeGh, node

from dureh_core import github, ops, permits, sitelinks
from dureh_core.cli import main
from dureh_core.records import Ledger
from dureh_core.site import Site
from dureh_core.util import DurehError

URL = f"git@github.com:{ORG}/tool.git"
THREE = ("--decide", "이 push 규칙을 넣을까요?", "--recommend", "예, 비공개 저장소만입니다.", "--otherwise",
         "기다립니다.", "--priority", "P2")
PRIVATE = {"id": "org-private", "match": {"owner": [ORG], "visibility": "private"},
           "allow": {"standing_ff": True}, "require": {"decision": "never"}}
DENY = {"id": "no-other", "match": {"owner": [ORG]}, "deny": True}


class PermitBase(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.token_file = self.tmp / "github-token"
        self.token_file.write_text(FAKE_TOKEN + "\n")
        self.token_file.chmod(0o600)
        self.conf(github={"enabled": True, "org": ORG, "project": 1, "token_file": str(self.token_file),
                          "director_login": "example-director"},
                  push={"allowed_remotes": [URL], "standing_ff": False})
        self.gh = FakeGh()
        p = mock.patch.object(github, "_exec", self.gh)
        p.start()
        self.addCleanup(p.stop)
        self.director()

    def conf(self, **keys):
        path = self.site_root / "config.json"
        cfg = json.loads(path.read_text())
        cfg.update(keys)
        path.write_text(json.dumps(cfg))

    def config(self):
        return json.loads((self.site_root / "config.json").read_text())

    def director(self):
        os.environ.pop("DUREH_ACTOR", None)
        os.environ.pop("CLAUDECODE", None)

    def desk(self):
        os.environ["DUREH_ACTOR"] = "desk"

    def site(self):
        return Site(self.site_root)

    def events(self, prefix="site.permit."):
        return [e for e in Ledger(self.site()).events() if e["type"].startswith(prefix)]

    def history(self):
        d = self.site_root / "state" / "config-history"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", err):
            code = main(["--site", str(self.site_root), *args])
        return code, out.getvalue(), err.getvalue()

    def unchanged(self, before):
        self.assertEqual(self.config(), before)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.history(), [])


class Catalog(PermitBase):
    def test_list_and_refused_keys(self):
        code, out, err = self.cli("site", "permit", "list")
        self.assertEqual(code, 0, err)
        self.assertIn("push.policy | C (B when it only tightens) | yes | yes | yes | tightening only", out)
        self.assertIn("github.director_login | D | yes | yes | no | no | \"example-director\"", out)
        self.assertIn("harness | - | - | no | no | no | -", out)
        for key, why in (("harness.path", "harness-owned"), ("slurm.account", "release 90"),
                         ("modules.demo.env_rebuild", "release 90"), ("project", "not in the permit catalog"),
                         ("push", "not in the permit catalog")):
            with self.assertRaisesRegex(DurehError, why):
                permits.set_key(self.site(), "desk", key, 1, "m")
        self.assertEqual(self.events(), [])

    def test_show_history(self):
        permits.set_key(self.site(), "desk", "push.standing_ff", True, "on for the session")
        out = permits.show(self.site(), "push.standing_ff", history=True)
        self.assertEqual(out["value"], True)
        self.assertEqual([h["by"] for h in out["history"]], ["Director"])


class Writes(PermitBase):
    def test_director_terminal_sets_a_class_c_key(self):
        before = self.config()
        out = permits.set_key(self.site(), "desk", "push.standing_ff", True, "standing pushes")
        self.assertEqual((out["class"], out["by"]), ("C", "Director"))
        self.assertTrue(self.config()["push"]["standing_ff"])
        self.assertEqual({k: v for k, v in self.config().items() if k != "push"},
                         {k: v for k, v in before.items() if k != "push"}, "other keys untouched")
        [ev] = self.events()
        p = ev["payload"]
        self.assertEqual((ev["type"], p["key"], p["old"], p["new"], p["by"], p["via"]),
                         ("site.permit.set", "push.standing_ff", False, True, "Director", "dureh site permit set"))
        self.assertGreater(p["generated"], 0)
        backup = self.site_root / p["backup"]
        self.assertEqual(json.loads(backup.read_text()), before)

    def test_desk_class_c_without_approval_is_refused(self):
        self.desk()
        before = self.config()
        with self.assertRaisesRegex(DurehError, "class C.*needs --approval.*--permit"):
            permits.set_key(self.site(), "desk", "push.standing_ff", True, "m")
        with self.assertRaisesRegex(DurehError, "class C"):
            permits.add_item(self.site(), "desk", "push.policy", PRIVATE, "m")
        with self.assertRaisesRegex(DurehError, "class D key.*Director's switch"):
            permits.set_key(self.site(), "desk", "github.director_login", "someone", "m")
        os.environ.pop("DUREH_ACTOR")
        os.environ["CLAUDECODE"] = "1"  # an agent session without an identity is the desk too
        with self.assertRaisesRegex(DurehError, "class D key"):
            permits.set_key(self.site(), "desk", "github.director_login", "someone", "m")
        self.unchanged(before)

    def test_desk_applies_tightening_alone(self):
        self.desk()
        permits.remove_item(self.site(), "desk", "push.allowed_remotes", URL, "drop the exact entry")
        self.assertEqual(self.config()["push"]["allowed_remotes"], [])
        self.director()
        permits.set_key(self.site(), "desk", "push.policy", [PRIVATE], "seed")
        self.desk()
        permits.add_item(self.site(), "desk", "push.policy", DENY, "deny first", at=0)
        self.assertEqual([r["id"] for r in self.config()["push"]["policy"]], ["no-other", "org-private"])
        tighter = {**PRIVATE, "allow": {"standing_ff": False}, "require": {"decision": "always"}}
        out = permits.set_key(self.site(), "desk", "push.policy", [DENY, tighter], "stricter")
        self.assertEqual((out["class"], out["by"]), ("B", "desk"))
        with self.assertRaisesRegex(DurehError, "class C"):  # removing the deny rule lets org-private cover it
            permits.remove_item(self.site(), "desk", "push.policy", "no-other", "m")
        with self.assertRaisesRegex(DurehError, "class C"):  # an exact entry back is a widening
            permits.add_item(self.site(), "desk", "push.allowed_remotes", URL, "m")
        self.assertEqual([(e["type"], e["payload"]["by"]) for e in self.events()],
                         [("site.permit.removed", "desk"), ("site.permit.set", "Director"),
                          ("site.permit.added", "desk"), ("site.permit.set", "desk")])

    def test_invalid_result_is_refused(self):
        before = self.config()
        with self.assertRaisesRegex(DurehError, "resulting config is invalid.*match.owner"):
            permits.add_item(self.site(), "desk", "push.policy", {"id": "x", "match": {"owner": ["a/*"]}}, "m")
        with self.assertRaisesRegex(DurehError, "has no rule 'nope'"):
            permits.remove_item(self.site(), "desk", "push.policy", "nope", "m")
        with self.assertRaisesRegex(DurehError, "--memo is required"):
            permits.set_key(self.site(), "desk", "push.standing_ff", False, " ")
        self.unchanged(before)

    def test_module_push(self):
        self.conf(modules=[{"id": "demo", "path": "modules/demo"}])
        self.desk()
        self.assertEqual(permits.set_key(self.site(), "desk", "modules.demo.push", False, "push-off")["class"], "B")
        self.assertIs(self.config()["modules"][0]["push"], False)
        with self.assertRaisesRegex(DurehError, "class C"):
            permits.unset_key(self.site(), "desk", "modules.demo.push", "m")
        with self.assertRaisesRegex(DurehError, "not a registered module"):
            permits.set_key(self.site(), "desk", "modules.other.push", False, "m")

    def test_dashboard_issue_is_checked_on_github(self):
        self.desk()
        states = {"141": "open\n", "142": "closed\n"}
        orig = self.gh.__call__

        def gh(argv, env, input_text, timeout):
            if argv[1] == "api" and "/issues/" in argv[2]:
                n = argv[2].rsplit("/", 1)[1]
                if n in states:
                    return subprocess.CompletedProcess(argv, 0, states[n], "")
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 404")
            return orig(argv, env, input_text, timeout)

        with mock.patch.object(github, "_exec", gh):
            out = permits.set_key(self.site(), "desk", "github.dashboard_issue", 141, "the dashboard")
            self.assertEqual((out["class"], self.config()["github"]["dashboard_issue"]), ("B", 141))
            with self.assertRaisesRegex(DurehError, "not an open issue"):
                permits.set_key(self.site(), "desk", "github.dashboard_issue", 142, "m")
            with self.assertRaisesRegex(DurehError, "could not be asked.*fail-closed"):
                permits.set_key(self.site(), "desk", "github.dashboard_issue", 143, "m")


class Widening(unittest.TestCase):
    def test_table(self):
        w = permits.push_widening
        self.assertEqual(w({"standing_ff": True}, {"standing_ff": False}), [])
        self.assertTrue(w({}, {"standing_ff": True}))
        self.assertTrue(w({}, {"allowed_remotes": [URL]}))
        self.assertEqual(w({"allowed_remotes": [URL]}, {"allowed_remotes": []}), [])
        obj = {"url": URL, "branches": ["main", "dev"], "standing_ff": True}
        self.assertEqual(w({"allowed_remotes": [obj]}, {"allowed_remotes": [{**obj, "branches": ["main"]}]}), [])
        self.assertTrue(w({"allowed_remotes": [obj]}, {"allowed_remotes": [{**obj, "branches": ["main", "x"]}]}))
        self.assertTrue(w({"allowed_remotes": [obj]}, {"allowed_remotes": [], "policy": [PRIVATE]}))
        self.assertTrue(w({"policy": [PRIVATE]}, {"policy": [{**PRIVATE, "match": {"owner": [ORG]}}]}))
        self.assertEqual(w({"policy": [PRIVATE]}, {"policy": [PRIVATE, DENY]}), [])
        self.assertTrue(w({"policy": [DENY, PRIVATE]}, {"policy": [PRIVATE, DENY]}), "order")
        self.assertEqual(w({"policy": [DENY, PRIVATE]}, {"policy": []}), [], "removing every rule only narrows")
        loose = {**PRIVATE, "require": {"decision": "first"}}
        self.assertTrue(w({"policy": [PRIVATE]}, {"policy": [loose]}) == [])
        self.assertTrue(w({"policy": [loose]}, {"policy": [{**loose, "allow": {"standing_ff": True, "squash": True}}]}))


class LinkProject(PermitBase):
    def test_desk_needs_a_bound_decision(self):
        self.desk()
        with mock.patch("subprocess.run") as run:
            with self.assertRaisesRegex(DurehError, "data project link is fail-closed.*--approval"):
                sitelinks.link_project(self.site(), "desk", "ds", "/data/ds")
            self.decide_unbound()
            with self.assertRaisesRegex(DurehError, "was not asked with `github item ask --permit"):
                sitelinks.link_project(self.site(), "desk", "ds", "/data/ds", approval=self.url)
        run.assert_not_called()
        self.assertFalse((self.site_root / "projects" / "ds").exists())
        code, _, err = self.cli("site", "link-project", "--name", "ds", "--source", "/data/ds")
        self.assertEqual(code, 2)
        self.assertIn("fail-closed", err)

    def decide_unbound(self):
        self.url = f"https://github.com/{ORG}/office/issues/5"
        Ledger(self.site()).event(github.DECIDED, "desk", None, {"item_id": "I5", "url": self.url, "from": None,
                                                                 "to": "Approve 1", "director_comments": []})

    def test_plan_class(self):
        pl = permits.plan(self.site(), [{"op": "link-project", "name": "ds", "source": "/data/ds"},
                                        {"op": "new-module", "id": "m1", "package": "m1"}])
        self.assertEqual((pl["class"], [r["class"] for r in pl["steps"]]), ("C", ["C", "B"]))


class PlanApply(PermitBase):
    def source(self):
        src = self.tmp / "src-module"
        subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
        (src / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=src, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=src, check=True)
        return src

    def test_plan_shows_diff_and_binding(self):
        steps = [{"op": "set", "key": "push.standing_ff", "value": True},
                 {"op": "add", "key": "push.policy", "value": DENY}]
        f = self.tmp / "p.json"
        f.write_text(json.dumps(steps))
        code, out, err = self.cli("site", "permit", "plan", "--file", str(f))
        self.assertEqual(code, 0, err)
        self.assertIn("plan class C (fail-closed)", out)
        self.assertIn(f"binding sha256 {permits.binding(steps)}", out)
        self.assertIn("1. set push.standing_ff [C]: false -> true", out)
        self.assertIn("widens: push.standing_ff turns on", out)
        self.assertIn("2. add push.policy [B]", out)
        self.assertEqual(self.events(), [])

    def test_accept_a_module_in_one_plan(self):
        src = self.source()
        steps = [{"op": "link-module", "id": "tool", "source": str(src), "branch": "main"},
                 {"op": "set", "key": "modules.tool.push", "value": False}]
        out = permits.apply(self.site(), "desk", steps, "accept tool")
        self.assertEqual([s["op"] for s in out["steps"]], ["link-module", "set"])
        [mod] = self.config()["modules"]
        self.assertEqual((mod["id"], mod["push"]), ("tool", False))
        [ev] = self.events()
        self.assertEqual(ev["type"], "site.permit.applied")
        linked = [e for e in Ledger(self.site()).events() if e["type"] == "site.module-linked"]
        self.assertEqual(ev["refs"], [linked[0]["id"]])

    def test_failed_step_stops_and_records_how_to_undo(self):
        steps = [{"op": "add", "key": "push.policy", "value": DENY},
                 {"op": "link-module", "id": "tool", "source": str(self.tmp / "missing"), "rev": "main"},
                 {"op": "set", "key": "push.standing_ff", "value": True}]
        with self.assertRaisesRegex(DurehError, "stopped at step 2.*restore config.json from state/config-history/"):
            permits.apply(self.site(), "desk", steps, "m")
        [ev] = self.events()
        self.assertEqual((ev["type"], ev["payload"]["failed_step"]), ("site.permit.failed", 2))
        self.assertEqual(self.config()["push"]["policy"], [DENY])
        self.assertFalse(self.config()["push"]["standing_ff"])


class Migrate(PermitBase):
    PATTERN = {"url": f"git@github.com:{ORG}/*.git", "pattern": True, "standing_ff": True}

    def setUp(self):
        super().setUp()
        self.conf(push={"allowed_remotes": [URL, self.PATTERN], "standing_ff": True})
        self.repo = self.make_module()
        remotes = self.tmp / "gh" / ORG
        remotes.mkdir(parents=True)
        self.bare = remotes / "other.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        subprocess.run(["git", "config", f"url.{remotes}/.insteadOf", f"git@github.com:{ORG}/"], cwd=self.repo,
                       check=True)
        subprocess.run(["git", "push", "-q", str(self.bare), "main"], cwd=self.repo, check=True)

    def push(self, visibility):
        def answer(_cfg, args, **_kw):
            return (visibility if args[-1] == ".visibility" else str(visibility == "private").lower()) + "\n"
        self.n = getattr(self, "n", 1) + 1
        (self.repo / "src" / "demo.py").write_text(f"def f():\n    return {self.n}\n")
        subprocess.run(["git", "commit", "-qam", "change"], cwd=self.repo, check=True)
        with mock.patch.object(github, "config", return_value={}), mock.patch.object(github, "gh", side_effect=answer):
            return ops.push(self.site(), "desk", str(self.repo), f"git@github.com:{ORG}/other.git", "main", None)

    def test_same_behaviour_after_migration(self):
        os.environ["DUREH_ACTOR"] = "desk"
        ev = self.push("private")
        self.assertEqual(ev["payload"]["policy"], {"source": "pattern"})
        with self.assertRaisesRegex(DurehError, "is not private"):
            self.push("public")
        dry = permits.migrate_push(self.site(), "desk", dry_run=True)
        self.assertEqual(self.events(), [])
        self.assertEqual(dry["to"], [{"id": f"{ORG}-private", "match": {"owner": [ORG], "name": ["*"],
                                                                       "visibility": "private"},
                                      "allow": {"standing_ff": True, "new_branches": [], "tags": False,
                                                "squash": False}, "require": {"decision": "never"}}])
        out = permits.migrate_push(self.site(), "desk")
        cfg = self.config()["push"]
        self.assertEqual((cfg["allowed_remotes"], cfg["standing_ff"]), ([URL], True))
        self.assertEqual(cfg["policy"], dry["to"])
        self.assertEqual(ops.config_errors(self.site()), [])
        self.assertEqual(self.events()[0]["payload"]["by"], "desk")
        self.assertEqual(self.events()[0]["id"], out["record"])
        ev = self.push("private")
        self.assertEqual((ev["payload"]["policy"]["source"], ev["payload"]["policy"]["rule_id"]),
                         ("policy", f"{ORG}-private"))
        with self.assertRaisesRegex(DurehError, "no push.policy rule covers"):
            self.push("public")
        self.assertFalse(permits.migrate_push(self.site(), "desk")["migrated"])

    def test_existing_policy_keeps_the_pattern_refusals(self):
        self.conf(push={"allowed_remotes": [self.PATTERN], "policy": [{**PRIVATE, "id": "later"}]})
        out = permits.migrate_push(self.site(), "desk", dry_run=True)
        self.assertEqual([r["id"] for r in out["to"]], [f"{ORG}-private", f"{ORG}-rest-private"])
        self.assertTrue(out["to"][1]["deny"])


class Reserved(PermitBase):
    def test_event_add_cannot_forge_a_permit_record(self):
        code, _, err = self.cli("ledger", "add", "event", "--type", "site.permit.set", "--payload", "{}")
        self.assertEqual(code, 2)
        self.assertIn("events are written only by", err)
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
