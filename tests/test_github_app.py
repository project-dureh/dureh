"""dureh-team#56 step 5 (release 84): with config github.app the harness mints the company App's installation token
into github.token_file (mode 600), and the session job keeps it fresh; without it nothing changes."""
import json
import os
import shutil
import stat
import subprocess
import unittest
from unittest import mock

from helpers import SiteCase

from dureh_core import github, github_app, sessions
from dureh_core.util import DurehError


@unittest.skipUnless(shutil.which("openssl"), "openssl signs the JWT when `cryptography` is missing")
class GithubApp(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.key = self.tmp / "app.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(self.key), "2048"], check=True, capture_output=True)
        self.key.chmod(0o600)
        self.token_file = self.tmp / "github-token"
        self.set_github({"enabled": True, "org": "example-org", "project": 1, "token_file": str(self.token_file),
                         "app": {"app_id": 7, "installation_id": 9, "key_file": str(self.key)}})

    def set_github(self, block):
        path = self.site_root / "config.json"
        cfg = json.loads(path.read_text())
        cfg["github"] = block
        path.write_text(json.dumps(cfg))

    def test_refresh_writes_the_token_mode_600_and_never_prints_it(self):
        calls = []

        def answer(method, path, bearer):
            calls.append((method, path, bearer.count(".")))
            return {"token": "ghs_secret", "expires_at": "2026-10-28T18:00:00Z"}

        with mock.patch.object(github_app, "http_json", answer):
            out = github_app.refresh(self.site())
        self.assertEqual(calls, [("POST", "/app/installations/9/access_tokens", 2)])
        self.assertEqual(self.token_file.read_text(), "ghs_secret\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.token_file).st_mode), 0o600)
        self.assertNotIn("ghs_secret", json.dumps(out))
        self.assertEqual(github.config_errors(self.site()), [])

    def test_serve_reports_a_failed_turn_and_keeps_going(self):
        turns = []

        def answer(method, path, bearer):
            turns.append(1)
            if len(turns) == 1:
                github_app.fail("POST x: HTTP 502")
            return {"token": "t2", "expires_at": "later"}

        slept = []

        def sleep(s):
            slept.append(s)
            if len(slept) == 2:
                raise KeyboardInterrupt

        with mock.patch.object(github_app, "http_json", answer), self.assertRaises(KeyboardInterrupt):
            github_app.serve(self.site(), every=1800, sleep=sleep)
        self.assertEqual(len(turns), 2)
        self.assertEqual(self.token_file.read_text(), "t2\n")

    def test_session_job_runs_the_refresher(self):
        text = sessions.plan(self.site())
        self.assertIn("github token --every 1800", text)
        self.assertIn('trap \'kill "$TOKEN_PID"', text)

    def test_bad_app_blocks_are_named(self):
        self.key.chmod(0o644)
        self.set_github({"enabled": True, "org": "example-org", "project": 1, "token_file": str(self.token_file),
                         "app": {"app_id": "x", "key_file": str(self.key), "extra": 1}})
        errs = " | ".join(github.config_errors(self.site()))
        for part in ("app.extra: unknown key", "app.app_id must be a positive integer",
                     "app.installation_id must be a positive integer", "key file", "mode 644"):
            self.assertIn(part, errs)


class NoApp(SiteCase):
    def test_without_an_app_nothing_changes(self):
        self.install()
        self.assertNotIn("github token", sessions.plan(self.site()))
        with self.assertRaisesRegex(DurehError, "github.app is not set"):
            github_app.refresh(self.site())


if __name__ == "__main__":
    unittest.main()
