"""dureh-team#56 step 6 (release 95): with config github.push_transport "app" a company push to a repository of the
App's organization goes over HTTPS with the App installation token through git_credential.py; every other remote
keeps its URL and is listed as still on SSH; a missing or stale token refuses the push (no fallback)."""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import SiteCase

from dureh_core import github, github_app, ops, permits
from dureh_core.records import Ledger
from dureh_core.util import DurehError

TOKEN = "ghs_TESTtoken0123456789"
ORG_URL = "git@github.com:example-org/tool.git"
PERSONAL_URL = "git@github.com:someone/tool.git"
HELPER = [sys.executable, "-I", str(github_app.HELPER)]


class Base(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.key = self.tmp / "app.pem"
        self.key.write_text("not read in these tests\n")
        self.key.chmod(0o600)
        self.token_file = self.tmp / "github-token"
        self.write_token()

    def write_token(self, text=TOKEN, mode=0o600, age=0):
        self.token_file.write_text(text + "\n")
        self.token_file.chmod(mode)
        if age:
            t = time.time() - age
            os.utime(self.token_file, (t, t))

    def configure(self, transport="app", app=True, allowed=None, policy=None):
        path = self.site_root / "config.json"
        cfg = json.loads(path.read_text())
        block = {"enabled": True, "org": "example-org", "project": 1, "token_file": str(self.token_file)}
        if app:
            block["app"] = {"app_id": 7, "installation_id": 9, "key_file": str(self.key)}
        if transport is not None:
            block["push_transport"] = transport
        cfg["github"] = block
        cfg["push"] = {"allowed_remotes": allowed if allowed is not None else [ORG_URL], "standing_ff": True,
                       "standing_ff_new_branches": True, **({"policy": policy} if policy else {})}
        path.write_text(json.dumps(cfg))
        return self.site()


class Selection(Base):
    def test_org_repo_with_app_goes_https(self):
        t = github_app.push_transport(self.configure(), ORG_URL)
        self.assertEqual((t["via"], t["owner"], t["https_url"]),
                         ("app-https", "example-org", "https://github.com/example-org/tool.git"))
        for url in ("ssh://git@github.com/Example-Org/tool", "https://github.com/example-org/tool.git"):
            self.assertEqual(github_app.push_transport(self.site(), url)["via"], "app-https", url)

    def test_personal_repo_keeps_its_url(self):
        t = github_app.push_transport(self.configure(), PERSONAL_URL)
        self.assertEqual(t["via"], "remote as configured")
        self.assertIn("someone is not example-org", t["why"])

    def test_without_app_or_switch_nothing_changes(self):
        for kw in ({"app": False, "transport": None}, {"transport": None}, {"transport": "ssh"}):
            s = self.configure(**kw)
            t = github_app.push_transport(s, ORG_URL)
            self.assertEqual(t["via"], "remote as configured", kw)
            env, _ = github_app.push_git_env(s, ORG_URL)
            self.assertEqual(env.get("GIT_CONFIG_COUNT"), os.environ.get("GIT_CONFIG_COUNT"), kw)
        self.assertEqual(github_app.push_transport(self.site(), "/srv/git/tool.git")["why"], "not a GitHub remote")

    def test_config_values(self):
        self.assertEqual(github.config_errors(self.configure()), [])
        errs = " | ".join(github.config_errors(self.configure(transport="https")))
        self.assertIn('github.push_transport must be "ssh" or "app"', errs)
        errs = " | ".join(github.config_errors(self.configure(app=False)))
        self.assertIn("github.app is not set", errs)
        self.assertEqual(permits.catalog_key("github.push_transport"), "github.push_transport")
        row = next(r for r in permits.CATALOG if r["key"] == "github.push_transport")
        self.assertEqual((row["class"], row["Desk"], row["D+"]), ("D", False, False))

    def test_listing_names_what_stays_on_ssh(self):
        s = self.configure(allowed=["/srv/local/repo", ORG_URL, PERSONAL_URL,
                                    {"url": "git@github.com:example-org/*.git", "pattern": True}],
                           policy=[{"id": "someone-private", "match": {"owner": ["someone"], "name": ["lib-*"]}},
                                   {"id": "no", "match": {"owner": ["other"]}, "deny": True}])
        res = github_app.transports(s)
        via = {r["remote"]: r["via"] for r in res["remotes"]}
        self.assertEqual(via, {ORG_URL: "app-https", PERSONAL_URL: "remote as configured",
                               "git@github.com:example-org/*.git": "app-https",
                               "git@github.com:someone/lib-*.git": "remote as configured"})
        self.assertEqual(res["still_on_ssh"], [PERSONAL_URL, "git@github.com:someone/lib-*.git"])
        text = "\n".join(github_app.transport_lines(res))
        self.assertIn("still on SSH (2)", text)
        self.assertNotIn(TOKEN, json.dumps(res) + text)
        self.assertNotIn(str(self.token_file), json.dumps(res))


class Helper(Base):
    def run_helper(self, req, owner="example-org", op="get", max_age=3000):
        stdin = "".join(f"{k}={v}\n" for k, v in req.items()) + "\n"
        return subprocess.run([*HELPER, "--token-file", str(self.token_file), "--owner", owner, "--max-age",
                               str(max_age), op], input=stdin, capture_output=True, text=True, timeout=30)

    REQ = {"protocol": "https", "host": "github.com", "path": "example-org/tool.git"}

    def test_answers_in_the_credential_protocol(self):
        p = self.run_helper(self.REQ)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, f"username=x-access-token\npassword={TOKEN}\n", ""))
        self.assertEqual(self.run_helper(self.REQ, op="store").stdout, "")

    def test_refusals_never_carry_the_token(self):
        cases = [({**self.REQ, "path": "someone/tool.git"}, {}, "not of example-org"),
                 ({**self.REQ, "protocol": "http"}, {}, "https://github.com only"),
                 ({k: v for k, v in self.REQ.items() if k != "path"}, {}, "useHttpPath"),
                 (self.REQ, {"mode": 0o644}, "must be 600"),
                 (self.REQ, {"age": 3600}, "written 60 min ago"),
                 (self.REQ, {"text": ""}, "empty")]
        for req, tok, why in cases:
            self.write_token(**{"text": TOKEN, **tok})
            p = self.run_helper(req)
            self.assertEqual(p.stdout, "quit=1\n", why)
            self.assertIn(why, p.stderr)
            self.assertNotIn(TOKEN, p.stderr)
        self.token_file.unlink()
        self.assertIn("does not exist", self.run_helper(self.REQ).stderr)

    def git_env(self):
        env, t = github_app.push_git_env(self.configure(), ORG_URL)
        human = self.tmp / "global-gitconfig"  # a human helper in the global config must never be asked
        human.write_text('[credential]\n\thelper = "!f() { echo username=human; echo password=HUMAN; }; f"\n')
        env["GIT_CONFIG_GLOBAL"] = str(human)
        return env, t

    def test_git_uses_only_the_app_helper(self):
        env, t = self.git_env()
        repo = self.make_module()
        url = subprocess.run(["git", "ls-remote", "--get-url", ORG_URL], cwd=repo, env=env, capture_output=True,
                             text=True).stdout.strip()
        self.assertEqual(url, "https://github.com/example-org/tool.git")
        github_app.check_resolved(repo, ORG_URL, env, t)
        fill = subprocess.run(["git", "credential", "fill"], cwd=repo, env=env, capture_output=True, text=True,
                              input="url=https://github.com/example-org/tool.git\n\n", timeout=30)
        self.assertIn(f"password={TOKEN}", fill.stdout)
        self.assertIn("username=x-access-token", fill.stdout)
        other = subprocess.run(["git", "credential", "fill"], cwd=repo, env=env, capture_output=True, text=True,
                               input="url=https://github.com/someone/tool.git\n\n", timeout=30)
        self.assertNotEqual(other.returncode, 0)
        self.assertNotIn("HUMAN", other.stdout)
        self.assertNotIn(TOKEN, other.stdout + other.stderr)
        self.assertNotIn(TOKEN, json.dumps(env))  # the token never sits in the environment

    def test_a_checkout_rewrite_that_wins_refuses(self):
        env, t = self.git_env()
        repo = self.make_module()
        subprocess.run(["git", "config", "url./srv/elsewhere/.insteadOf", "git@github.com:example-org/"], cwd=repo,
                       check=True)
        with self.assertRaises(DurehError) as cm:
            github_app.check_resolved(repo, ORG_URL, env, t)
        self.assertIn("insteadOf", str(cm.exception))


class Push(Base):
    def setUp(self):
        super().setUp()
        self.repo = self.make_module()
        self.calls = []

    def fake_git(self):
        real = ops.git

        def git(args, cwd, *, check=True, env=None):
            if args and args[0] in ("ls-remote", "fetch", "push"):
                self.calls.append((list(args), env))
                return ""  # an empty remote: the push creates the branch
            return real(args, cwd, check=check, env=env)
        return mock.patch.object(ops, "git", git)

    def pushes(self, s):
        return [e for e in Ledger(s).events() if e["type"].startswith("push.")]

    def test_org_push_goes_over_the_app(self):
        s = self.configure()
        with self.fake_git():
            ev = ops.push(s, "desk", str(self.repo), ORG_URL, "main", None)
        self.assertEqual(ev["type"], "push.completed")
        self.assertEqual(ev["payload"]["transport"], {"via": "app-https", "owner": "example-org"})
        self.assertEqual([c[0][0] for c in self.calls], ["ls-remote", "push"])
        for _args, env in self.calls:
            keys = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
            self.assertEqual(keys["url.https://github.com/example-org/.insteadOf"], "ssh://git@github.com/example-org/")
            self.assertIn("git_credential.py", keys["credential.https://github.com.helper"])
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn(TOKEN, json.dumps(Ledger(s).events()))

    def test_personal_push_is_unchanged(self):
        s = self.configure(allowed=[PERSONAL_URL])
        with self.fake_git():
            ev = ops.push(s, "desk", str(self.repo), PERSONAL_URL, "main", None)
        self.assertEqual(ev["payload"]["transport"]["via"], "remote as configured")
        self.assertTrue(all(env.get("GIT_CONFIG_COUNT") == os.environ.get("GIT_CONFIG_COUNT") for _a, env in self.calls))

    def test_missing_or_stale_token_refuses_before_any_network_call(self):
        for prep, why in ((lambda: self.write_token(age=4000), "min ago"), (self.token_file.unlink, "does not exist")):
            prep()
            s = self.configure()
            with self.fake_git(), self.assertRaises(DurehError) as cm:
                ops.push(s, "desk", str(self.repo), ORG_URL, "main", None)
            self.assertIn(why, str(cm.exception))
            self.assertIn("no fallback to SSH", str(cm.exception))
            self.assertEqual(self.calls, [])
            self.assertEqual(self.pushes(s), [])


if __name__ == "__main__":
    unittest.main()
