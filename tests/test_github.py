"""Release 69: the company's GitHub board (`dureh github`, docs/github.md). The gh subprocess is replaced by a fake
runner that answers from an in-memory board; no test touches the network or a real token."""
import io
import json
import os
import subprocess
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from helpers import SiteCase

from dureh_core import github
from dureh_core.cli import main
from dureh_core.ops import config_errors
from dureh_core.records import Ledger
from dureh_core.runtimes.claude_code import _claude_md
from dureh_core.util import DurehError

FAKE_TOKEN = "github_pat_test-not-a-real-token"
ORG = "example-org"
FIELDS = [
    {"id": "F_STATUS", "name": "Status", "dataType": "SINGLE_SELECT",
     "options": [{"id": f"O_S{i}", "name": n} for i, n in enumerate(github.STATUSES)]},
    {"id": "F_PROJECT", "name": "Project", "dataType": "SINGLE_SELECT",
     "options": [{"id": "O_P0", "name": "example-project"}]},
    {"id": "F_LAYER", "name": "Layer", "dataType": "SINGLE_SELECT",
     "options": [{"id": "O_L1", "name": "1 company"}, {"id": "O_L2", "name": "2 DUREH"}, {"id": "O_L3", "name": "3 Director"}]},
    {"id": "F_DECISION", "name": "Decision", "dataType": "SINGLE_SELECT",
     "options": [{"id": f"O_D{i}", "name": n} for i, n in enumerate(github.DECISIONS)]},
    {"id": "F_UNITS", "name": "Units", "dataType": "NUMBER"},
    {"id": "F_PRIORITY", "name": "Priority", "dataType": "SINGLE_SELECT",
     "options": [{"id": "O_PR0", "name": "P0 now"}, {"id": "O_PR1", "name": "P1 next"}, {"id": "O_PR2", "name": "P2 later"},
                 {"id": "O_PR3", "name": "P3 someday"}]},
    {"id": "F_START", "name": "Start date", "dataType": "DATE"},
    {"id": "F_TARGET", "name": "Target date", "dataType": "DATE"},
    {"id": "F_TITLE", "name": "Title", "dataType": "TITLE"},
]


def node(item_id, number, title, status=None, decision=None, layer=None, comments=(), labels=(), state="OPEN",
         repo=None):
    values = []
    for field, v in (("Status", status), ("Decision", decision), ("Layer", layer)):
        if v is not None:
            values.append({"name": v, "field": {"name": field}})
    return {"id": item_id, "fieldValues": {"nodes": values},
            "content": {"__typename": "Issue", "number": number, "title": title, "state": state,
                        "url": f"https://github.com/{repo or ORG + '/office'}/issues/{number}",
                        "repository": {"nameWithOwner": repo or f"{ORG}/office"}, "assignees": {"nodes": []},
                        "labels": {"nodes": [{"name": n} for n in labels]},
                        "comments": {"nodes": [{"id": cid, "url": f"https://github.com/{ORG}/office/issues/{number}#{cid}",
                                                "author": {"login": who}, "body": body}
                                               for cid, who, body in comments]}}}


def issue(iid, number, title, author="example-director", created="2026-10-16T12:00:00Z", body="", labels=(),
          repo="office"):
    return {"id": iid, "number": number, "title": title, "url": f"https://github.com/{ORG}/{repo}/issues/{number}",
            "createdAt": created, "body": body, "author": {"login": author},
            "labels": {"nodes": [{"name": n} for n in labels]}}


THREE = ("--decide", "A를 먼저 할까요?", "--recommend", "예, A가 B의 입력입니다.", "--otherwise",
         "30분 뒤 Option 1로 진행합니다.")


class FakeGh:
    """Answers the gh commands the harness uses, from an in-memory board and in-memory repositories."""

    def __init__(self):
        self.calls, self.items, self.next_issue, self.fail_next = [], [], 10, None
        self.repos = {"office": []}  # repository name -> open issues (newest first, as GitHub orders them)
        self.org_listable = True
        self.costs = {"board": 3, "office": 1, "org": 2}  # rateLimit.cost answered per query kind (release 79)
        self.remaining = 5000

    def _repo_node(self, name):
        newest = sorted(self.repos[name], key=lambda i: i["createdAt"], reverse=True)[:github.ISSUE_PAGE]
        return {"nameWithOwner": f"{ORG}/{name}", "isArchived": False, "issues": {"nodes": newest}}

    def _answer(self, argv, req, data, kind):
        if "rateLimit" in req["query"]:
            self.remaining -= self.costs[kind]
            data = dict(data or {}, rateLimit={"cost": self.costs[kind], "remaining": self.remaining,
                                               "resetAt": "2026-10-21T18:21:00Z"})
        return subprocess.CompletedProcess(argv, 0, json.dumps({"data": data}), "")

    def __call__(self, argv, env, input_text, timeout):
        self.calls.append({"argv": argv, "env": env, "input": input_text})
        if self.fail_next:
            msg, self.fail_next = self.fail_next, None
            return subprocess.CompletedProcess(argv, 1, "", msg)
        a = argv[1:]
        if a[:2] == ["api", "graphql"]:
            req = json.loads(input_text)
            if "repository(owner" in req["query"]:
                name = req["variables"]["repo"]
                repo = self._repo_node(name) if name in self.repos else None
                return self._answer(argv, req, {"repository": repo}, "office")
            if "repositories(" in req["query"]:
                if not self.org_listable:
                    return subprocess.CompletedProcess(argv, 0, json.dumps(
                        {"data": None, "errors": [{"message": "Resource not accessible by personal access token"}]}), "")
                start = int(req["variables"].get("after") or 0)
                names = list(self.repos)[start:start + 10]  # the query's page of 10 repositories
                more = start + 10 < len(self.repos)
                org = {"repositories": {"pageInfo": {"hasNextPage": more, "endCursor": str(start + 10) if more else None},
                                        "nodes": [self._repo_node(n) for n in names]}}
                return self._answer(argv, req, {"organization": org}, "org")
            proj = {"id": "PVT_1", "title": "Example board", "fields": {"nodes": FIELDS},
                    "items": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self.items}}
            if req["variables"]["org"] != ORG:
                return subprocess.CompletedProcess(argv, 0, '{"data": {}}', "")
            return self._answer(argv, req, {"organization": {"projectV2": proj}}, "board")
        if a[:2] == ["api", "rate_limit"]:
            res = {"graphql": {"limit": 5000, "used": 5000 - self.remaining, "remaining": self.remaining,
                               "reset": 1792606860},
                   "core": {"limit": 5000, "used": 42, "remaining": 4958, "reset": 1792606860}}
            return subprocess.CompletedProcess(argv, 0, json.dumps({"resources": res}), "")
        if a[:2] == ["issue", "create"]:
            self.next_issue += 1
            repo = a[a.index("--repo") + 1]
            return subprocess.CompletedProcess(argv, 0, f"https://github.com/{repo}/issues/{self.next_issue}\n", "")
        if a[:2] == ["project", "item-add"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"id": f"PVTI_{self.next_issue}"}), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def commands(self):
        return [c["argv"][1:3] for c in self.calls]


class GithubBase(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.token_file = self.tmp / "github-token"
        self.token_file.write_text(FAKE_TOKEN + "\n")
        self.token_file.chmod(0o600)
        self.set_config({"enabled": True, "org": ORG, "project": 1, "token_file": str(self.token_file),
                         "director_login": "example-director"})
        self.gh = FakeGh()
        p = mock.patch.object(github, "_exec", self.gh)
        p.start()
        self.addCleanup(p.stop)

    def set_config(self, value, key="github"):
        path = self.site_root / "config.json"
        cfg = json.loads(path.read_text())
        if value is None:
            cfg.pop(key, None)
        else:
            cfg[key] = value
        path.write_text(json.dumps(cfg))

    def run_cli(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(out), mock.patch("sys.stderr", err):
            code = main(["--site", str(self.site_root), "github", *args])
        return code, out.getvalue(), err.getvalue()

    def events(self, prefix="github."):
        return [e for e in Ledger(self.site()).load("event") if e["type"].startswith(prefix)]


class Config(GithubBase):
    def test_valid_config_has_no_errors(self):
        self.assertEqual(github.config_errors(self.site()), [])
        self.assertEqual([e for e in config_errors(self.site()) if "github" in e], [])

    def test_enabled_without_token_file_or_org_is_refused(self):
        self.set_config({"enabled": True, "project": 1})
        errs = [e for e in config_errors(self.site()) if "github" in e]
        self.assertEqual(len(errs), 1)
        self.assertIn("github.org, github.token_file are missing", errs[0])

    def test_missing_token_file_is_refused(self):
        self.set_config({"enabled": True, "org": ORG, "project": 1, "token_file": str(self.tmp / "nope")})
        errs = github.config_errors(self.site())
        self.assertTrue(any("does not exist" in e for e in errs), errs)

    def test_wrong_mode_is_refused_and_no_gh_runs(self):
        self.token_file.chmod(0o644)
        errs = github.config_errors(self.site())
        self.assertTrue(any("mode 644" in e and "chmod 600" in e for e in errs), errs)
        code, out, err = self.run_cli("status")
        self.assertEqual(code, 2)
        self.assertIn("mode 644", err)
        self.assertEqual(self.gh.calls, [])

    def test_disabled_block_is_only_shape_checked(self):
        self.set_config({"enabled": False})
        self.assertEqual(github.config_errors(self.site()), [])
        code, out, _ = self.run_cli("status")
        self.assertEqual((code, out.strip()), (0, github.OFF_LINE))
        self.set_config({"enabled": "yes", "colour": 1, "project": 0})
        errs = github.config_errors(self.site())
        self.assertTrue(any("unknown key" in e for e in errs))
        self.assertTrue(any("enabled must be true or false" in e for e in errs))
        self.assertTrue(any("github.project" in e for e in errs))

    def test_no_block_means_off(self):
        self.set_config(None)
        self.assertFalse(github.enabled(self.site()))
        self.assertEqual(github.config_errors(self.site()), [])


class Runner(GithubBase):
    def test_token_goes_to_the_subprocess_environment_only(self):
        os.environ["GITHUB_TOKEN"] = "other-token-from-the-environment"
        code, out, err = self.run_cli("status", "--json")
        self.assertEqual(code, 0, err)
        call = self.gh.calls[0]
        self.assertEqual(call["env"]["GH_TOKEN"], FAKE_TOKEN)
        self.assertNotIn("GITHUB_TOKEN", call["env"])
        self.assertNotIn(FAKE_TOKEN, " ".join(call["argv"]))
        self.assertNotIn(FAKE_TOKEN, call["input"])
        self.assertNotIn(FAKE_TOKEN, out + err)
        self.assertNotIn(FAKE_TOKEN, os.environ.get("GH_TOKEN", ""))

    def test_failure_text_is_redacted(self):
        self.gh.fail_next = f"HTTP 401: Bad credentials for {FAKE_TOKEN}"
        code, out, err = self.run_cli("status")
        self.assertEqual(code, 2)
        self.assertIn("exit 1", err)
        self.assertIn("***", err)
        self.assertNotIn(FAKE_TOKEN, out + err)

    def test_secret_text_is_refused(self):
        body = self.tmp / "ask.md"
        body.write_text("please use ghp_abcdef")
        code, _, err = self.run_cli("item", "ask", "--title", "Q", "--body-file", str(body))
        self.assertEqual(code, 2)
        self.assertIn("token prefix", err)
        self.assertEqual(self.gh.calls, [])


class Status(GithubBase):
    def test_groups_by_status(self):
        self.gh.items = [node("I1", 1, "first", "Today", None, "2 DUREH"),
                         node("I2", 2, "second", "Waiting for Director", "Option 1", "3 Director"),
                         node("I3", 3, "third")]
        res = github.status(self.site())
        self.assertEqual(list(res["groups"]), ["Today", "Waiting for Director", "(no status)"])
        row = res["groups"]["Waiting for Director"][0]
        self.assertEqual((row["decision"], row["layer"], row["number"]), ("Approve 1", "3 Director", 2))  # old name
        code, out, _ = self.run_cli("status")
        self.assertIn("office#2 second [decision: Approve 1] [layer: 3 Director]", out)
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(json.loads(out)["count"], 3)

    def test_status_is_allowed_with_observe_and_records_nothing(self):
        before = len(Ledger(self.site()).load("event"))
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()):
            code = main(["--site", str(self.site_root), "--observe", "github", "status"])
        self.assertEqual(code, 0)
        self.assertEqual(len(Ledger(self.site()).load("event")), before)
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["--site", str(self.site_root), "--observe", "github", "watch", "--once"]), 2)


class Watch(GithubBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(github, "ORG_SCAN_INTERVAL", 0)  # every poll scans the organization here; see WatchCost
        p.start()
        self.addCleanup(p.stop)

    def lines(self, polls=1, sleep=None):
        got = []
        github.watch(self.site(), "desk", 5, emit=got.append, sleep=sleep or (lambda s: None), polls=polls)
        return got

    def test_decision_changes_and_comments(self):
        self.gh.items = [node("I1", 1, "first", "Waiting for Director", None, "3 Director",
                              [("C1", "example-director", "old opinion")]),
                         node("I2", 2, "second", "Today", "Approved")]
        first = self.lines()
        self.assertEqual(len(first), 1)  # the Decision already set; old comments taken as seen
        self.assertTrue(first[0].startswith("decision: office#2 second -> Approve 1 (was none)"), first)
        self.assertEqual(self.lines(), [])
        self.gh.items[0] = node("I1", 1, "first", "Waiting for Director", "Option 2", "3 Director",
                                [("C1", "example-director", "old opinion"),
                                 ("C2", "desk-bot", f"answer\n{github.DESK_MARK}"),
                                 ("C3", "example-director", "one more\nthought")])
        got = self.lines()
        self.assertEqual(len(got), 2, got)
        self.assertTrue(got[0].startswith("decision: office#1 first -> Approve 2 (was none) [layer 3 Director]"))
        self.assertTrue(got[0].endswith(f"| Director: one more https://github.com/{ORG}/office/issues/1#C3 "
                                        "(+1 earlier)"), got[0])
        self.assertEqual(got[1], f"comment: office#1 by example-director: one more thought "
                                 f"https://github.com/{ORG}/office/issues/1#C3")
        decided = [e for e in self.events() if e["type"] == github.DECIDED]
        self.assertEqual([(e["payload"]["from"], e["payload"]["to"]) for e in decided],
                         [(None, "Approve 1"), (None, "Approve 2")])

    def test_a_decision_moves_the_item(self):
        """Release 85 (Director 2026-10-29): the desk's watch moves an item to Today when the Director decides;
        Discuss and items already In progress or Waiting for Director stay."""
        self.gh.items = [node("I1", 1, "first", "Needs approval", None, "3 Director"),
                         node("I2", 2, "second", "Needs approval", None, "3 Director"),
                         node("I3", 3, "third", "In progress", None, "3 Director")]
        self.lines()
        self.gh.calls.clear()
        self.gh.items = [node("I1", 1, "first", "Needs approval", "Approve 1", "3 Director"),
                         node("I2", 2, "second", "Needs approval", "Discuss", "3 Director"),
                         node("I3", 3, "third", "In progress", "Approve 1", "3 Director")]
        got = self.lines()
        self.assertIn("status: office#1 -> Today (Decision Approve 1)", got)
        self.assertFalse(any(x.startswith("status: office#2") or x.startswith("status: office#3") for x in got), got)
        today = "O_S" + str(list(github.STATUSES).index("Today"))
        edits = [c["argv"] for c in self.gh.calls if c["argv"][1:3] == ["project", "item-edit"]]
        self.assertEqual([e[-1] for e in edits], [today])
        self.assertEqual(github.decision_status("Rejected", "Needs approval"), "Today")
        self.assertIsNone(github.decision_status("Approve 2", "Waiting for Director"))
        self.assertIsNone(github.decision_status("Approve 1", "Done"))

    def test_board_status_rules(self):
        """Release 86 (dureh-team#91 item 1): the one Status rule the team tools call too."""
        bs = github.board_status
        self.assertEqual(bs(state="CLOSED", decision="Approve 1"), ("Done", "closed", None))
        self.assertEqual(bs(merged=True)[0], "Done")
        self.assertEqual(bs(labels=["waiting-director"], decision="Approve 1")[0], "Waiting for Director")
        want, _, conflict = bs(labels=["waiting-director", "needs-review"])
        self.assertIsNone(want)
        self.assertIn("waiting-director and needs-review", conflict)
        self.assertEqual(bs(labels=["Needs-Approval"], decision="Discuss")[0], "Needs approval")
        self.assertEqual(bs(labels=["on-hold"], status="In progress")[0], "Today")
        self.assertEqual(bs(director_merges_pr=True, decision="Approve 1")[0], "Waiting for Director")
        self.assertEqual(bs(decision="Later", status="Needs approval"), (None, "", None))  # Later = Discuss, a hold
        self.assertEqual(bs(decision="Option 2", status="Needs approval"), ("Today", "Decision Approve 2", None))
        self.assertEqual(bs(decision="Rejected", status="In progress")[0], "In progress")
        self.assertEqual(bs(status="Backlog"), (None, "", None))

    def test_state_survives_a_restart(self):
        self.gh.items = [node("I1", 1, "first", "Today", "Later")]
        self.assertEqual(len(self.lines()), 1)
        state = json.loads(github.state_path(self.site()).read_text())
        self.assertEqual(state["decisions"], {"I1": "Discuss"})  # Later is read as Discuss
        self.assertTrue(str(github.state_path(self.site())).startswith(str(self.site_root / "cache")))
        code, out, _ = self.run_cli("watch", "--once")  # a new process: nothing new
        self.assertEqual((code, out), (0, ""))
        self.gh.items = [node("I1", 1, "first", "Today", "Rejected")]
        code, out, _ = self.run_cli("watch", "--once")
        self.assertIn("-> Rejected (was Discuss)", out)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_followup_after_twenty_minutes_without_a_desk_comment(self):
        """Release 80 (dureh-team#63 part 3): an Approved item still open 20 min later with no desk comment."""
        clock = [1000.0]
        p = mock.patch.object(github, "_clock", lambda: clock[0])
        p.start()
        self.addCleanup(p.stop)
        self.gh.items = [node("I1", 1, "push", "Needs approval", None, "3 Director"),
                         node("I2", 2, "order", "Needs approval", None, "3 Director")]
        self.lines()
        self.gh.items = [node("I1", 1, "push", "Today", "Approved", "3 Director"),
                         node("I2", 2, "order", "Today", "Option 1", "3 Director")]
        self.assertEqual(len(self.lines()), 2)  # the two decisions
        clock[0] += 10 * 60
        self.assertEqual(self.lines(), [])
        self.gh.items[1] = node("I2", 2, "order", "Today", "Option 1", "3 Director",
                                [("C9", "desk-bot", f"started WI-0002\n{github.DESK_MARK}")])
        clock[0] += 11 * 60
        got = self.lines()
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0].startswith("followup: office#1 push -> Approve 1 21 min ago, still open"), got)
        self.assertIn("대표님 조치 필요", got[0])
        clock[0] += 60 * 60
        self.assertEqual(self.lines(), [], "once")

    def test_migrated_value_is_no_change(self):
        """Director 2026-10-27: the boards' options are renamed (Approved -> Approve 1 ...); a stored old value read as
        its new name is no Decision change: no line, no record."""
        self.gh.items = [node("I1", 1, "first", "Today", "Approved"), node("I2", 2, "second", "Today", "Later")]
        self.assertEqual(len(self.lines()), 2)
        state = json.loads(github.state_path(self.site()).read_text())
        state["decisions"] = {"I1": "Approved", "I2": "Later"}  # a state saved by an older release
        github.state_path(self.site()).write_text(json.dumps(state))
        self.gh.items = [node("I1", 1, "first", "Today", "Approve 1"), node("I2", 2, "second", "Today", "Discuss")]
        self.assertEqual(self.lines(), [])
        self.assertEqual(len([e for e in self.events() if e["type"] == github.DECIDED]), 2)

    def test_discuss_is_a_hold_with_the_directors_comments(self):
        clock = [1000.0]
        p = mock.patch.object(github, "_clock", lambda: clock[0])
        p.start()
        self.addCleanup(p.stop)
        self.gh.items = [node("I1", 1, "push", "Needs approval", None, "3 Director")]
        self.lines()
        said = [("C1", "example-director", "before the last answer"),
                ("C2", "example-director", "ANSWER: Approve 1 (Director, Decision column) <!-- dureh -->"),
                ("C3", "someone-else", "drive-by"),
                ("C4", "example-director", "<!-- dureh --> DUREH note"),
                ("C5", "example-director", "\n비용이 얼마인가요?\n두 번째 줄")]
        self.gh.items = [node("I1", 1, "push", "Needs approval", "Discuss", "3 Director", said)]
        got = self.lines()
        self.assertEqual(got[0], "decision: office#1 push -> Discuss (was none) [layer 3 Director] "
                                 f"https://github.com/{ORG}/office/issues/1 | Discuss: hold, the work does not proceed; "
                                 "answer the Director in the item's comments | Director: 비용이 얼마인가요? "
                                 f"https://github.com/{ORG}/office/issues/1#C5")
        ev = [e for e in self.events() if e["type"] == github.DECIDED][-1]
        self.assertEqual(ev["payload"]["to"], "Discuss")
        self.assertEqual(ev["payload"]["director_comments"], [f"https://github.com/{ORG}/office/issues/1#C5"])
        self.assertNotIn("비용", json.dumps(ev))  # no text in the record
        clock[0] += 21 * 60
        got = self.lines()
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0].startswith("followup: office#1 push -> Discuss 21 min ago"), got)
        self.assertIn("answer the Director's comments on the item", got[0])
        code, out, _ = self.run_cli("status")
        self.assertIn("    Discuss: hold; the work does not proceed; answer the Director in the item's comments", out)
        self.assertIn(f"    Director: 비용이 얼마인가요? https://github.com/{ORG}/office/issues/1#C5", out)
        code, out, _ = self.run_cli("status", "--json")
        rows = json.loads(out)["groups"]["Needs approval"]
        self.assertEqual(rows[0]["director_comments"], [{"text": "비용이 얼마인가요?",
                                                         "url": f"https://github.com/{ORG}/office/issues/1#C5"}])

    def test_followup_skips_closed_and_first_poll(self):
        clock = [1000.0]
        p = mock.patch.object(github, "_clock", lambda: clock[0])
        p.start()
        self.addCleanup(p.stop)
        self.gh.items = [node("I1", 1, "old", "Today", "Approved", "3 Director")]
        self.assertEqual(len(self.lines()), 1)  # first poll reports, does not track
        self.gh.items.append(node("I2", 2, "done", "Today", None, "3 Director"))
        self.lines()
        self.gh.items[1] = node("I2", 2, "done", "Today", "Approved", "3 Director")
        self.lines()
        self.gh.items[1] = node("I2", 2, "done", "Done", "Approved", "3 Director", state="CLOSED")
        clock[0] += 30 * 60
        self.assertEqual(self.lines(), [])

    def test_empty_board_answer_keeps_the_state(self):
        """2026-10-22: one poll saw the board with no items; the next replayed every Decision and comment as new."""
        self.gh.items = [node("I1", 1, "first", "Today", "Approved", None, [("C1", "example-director", "hi")])]
        self.assertEqual(len(self.lines()), 1)
        full, self.gh.items = self.gh.items, []
        self.assertEqual(self.lines(), [])
        self.gh.items = full
        self.assertEqual(self.lines(), [])
        decided = [e for e in self.events() if e["type"] == github.DECIDED]
        self.assertEqual(len(decided), 1)

    def test_transient_failure_is_skipped(self):
        self.gh.items = [node("I1", 1, "first", "Today", "Approved")]
        self.gh.fail_next = "HTTP 502: Bad Gateway"
        slept = []
        got = self.lines(polls=2, sleep=slept.append)
        self.assertEqual(len(got), 1)
        self.assertEqual(slept, [5.0])

    def test_rate_limit_backs_off_once_and_resumes(self):
        self.gh.items = [node("I1", 1, "first", "Today", "Approved")]
        fails = ["GraphQL: API rate limit already exceeded for user ID 7221078.",
                 "You have exceeded a secondary rate limit. Please wait a few minutes before you try again.",
                 "API rate limit already exceeded", "API rate limit already exceeded",
                 "API rate limit already exceeded"]
        orig = self.gh.__call__

        def runner(argv, env, input_text, timeout):
            if fails and argv[1:3] == ["api", "graphql"]:  # `gh api rate_limit` (M2) is not counted by GitHub
                self.gh.fail_next = fails.pop(0)
            return orig(argv, env, input_text, timeout)

        with mock.patch.object(github, "_exec", runner):
            slept, got = [], []
            github.watch(self.site(), "desk", 120, emit=got.append, sleep=slept.append, polls=7)
        self.assertEqual(slept, [120.0, 240.0, 480.0, 900.0, 900.0, 120.0])
        self.assertEqual(len([g for g in got if "rate limited" in g]), 1, got)
        self.assertTrue(got[0].startswith("github watch: rate limited by GitHub; backing off (next poll in 120 s"), got)
        self.assertEqual(got[1], "github watch: resumed after 5 rate-limited polls")
        self.assertTrue(got[2].startswith("decision: office#1 first -> Approve 1"), got)
        self.assertEqual(len(got), 3, got)

    def test_rate_limit_honours_retry_after(self):
        self.gh.fail_next = "HTTP 403: secondary rate limit (Retry-After: 600)"
        slept, got = [], []
        github.watch(self.site(), "desk", 120, emit=got.append, sleep=slept.append, polls=2)
        self.assertEqual(slept, [600.0])
        self.assertEqual(got[-1], "github watch: resumed after 1 rate-limited poll")

    def test_other_errors_stay_silent_without_backoff(self):
        self.gh.fail_next = "HTTP 502: Bad Gateway"
        slept, got = [], []
        github.watch(self.site(), "desk", 120, emit=got.append, sleep=slept.append, polls=2)
        self.assertEqual((slept, got), ([120.0], []))

    def test_default_interval_is_120(self):
        self.assertEqual(github.WATCH_INTERVAL, 120)
        with mock.patch.object(github, "watch") as w:
            code, _, _ = self.run_cli("watch", "--once")
        self.assertEqual(code, 0)
        self.assertEqual(w.call_args.args[2], 120)

    def test_new_issues_are_reported_once(self):
        self.gh.repos["office"] = [issue("IS1", 1, "old issue", created="2026-01-01T00:00:00Z")]
        self.gh.repos["example-project"] = [issue("IS2", 4, "old project issue", created="2026-01-01T00:00:00Z",
                                                  repo="example-project")]
        self.assertEqual(self.lines(), [])  # the first poll takes existing open issues as seen
        self.gh.repos["office"].append(issue("IS3", 2, "please run the\nnew analysis", labels=["kind:request"]))
        self.gh.repos["office"].append(issue("IS4", 3, "desk issue", author="desk-bot", body=f"x\n{github.DESK_MARK}"))
        self.gh.repos["example-project"].append(issue("IS5", 5, "a bug", author="someone", repo="example-project"))
        got = self.lines()
        self.assertEqual(got, [f"work request office#2 by example-director: please run the new analysis | "
                               f"https://github.com/{ORG}/office/issues/2",
                               f"issue example-project#5 by someone: a bug | "
                               f"https://github.com/{ORG}/example-project/issues/5"])
        opened = self.events(github.OPENED)
        self.assertEqual([(e["payload"]["number"], e["payload"]["request"], e["payload"]["repo"]) for e in opened],
                         [(2, True, f"{ORG}/office"), (5, False, f"{ORG}/example-project")])
        self.assertTrue(all("title" not in e["payload"] and "body" not in e["payload"] for e in opened))
        self.assertEqual(self.lines(), [])
        code, out, _ = self.run_cli("watch", "--once")  # a restart: nothing again
        self.assertEqual((code, out), (0, ""))
        state = json.loads(github.state_path(self.site()).read_text())
        self.assertEqual(sorted(state["issues"]), ["IS1", "IS2", "IS3", "IS4", "IS5"])

    def test_issues_opened_while_no_watch_ran_are_reported(self):
        self.lines()
        state_file = github.state_path(self.site())
        state = json.loads(state_file.read_text())
        for k in ("issues", "issue_repos", "polled_utc"):  # a state saved before release 71
            state.pop(k, None)
        state["polled"] = "2026-10-16T10:00:00"
        state_file.write_text(json.dumps(state))
        self.gh.repos["office"] = [issue("IS1", 1, "before", created="2026-10-16T09:00:00Z"),
                                   issue("IS2", 2, "after", created="2026-10-16T11:00:00Z")]
        with mock.patch.dict(os.environ, {"TZ": "UTC"}):
            time.tzset()
            got = self.lines()
        time.tzset()
        self.assertEqual(got, [f"issue office#2 by example-director: after | https://github.com/{ORG}/office/issues/2"])

    def test_office_only_when_the_org_cannot_be_listed(self):
        self.gh.org_listable = False
        self.gh.repos["example-project"] = []
        self.lines()
        self.gh.repos["office"].append(issue("IS1", 1, "new"))
        self.gh.repos["example-project"].append(issue("IS2", 2, "hidden", created="2026-01-01T00:00:00Z",
                                                      repo="example-project"))
        self.assertEqual(self.lines(), [f"issue office#1 by example-director: new | https://github.com/{ORG}/office/issues/1"])
        self.gh.org_listable = True  # listed later: issues it already had count as seen, newer ones are reported
        self.assertEqual(self.lines(), [])

    def test_only_the_desk_watches(self):
        with self.assertRaises(DurehError):
            github.watch(self.site(), "py-writer", polls=1)


class WatchCost(GithubBase):
    """Release 79 (v0.4 bundle 1 step 3): small pages (G1), the organization scan every ORG_SCAN_INTERVAL (G2), a
    lighter board read (G3), and the measured cost (M1) and the time the limit ran out (M2) in cache/github/usage.jsonl."""

    def lines(self, polls=1, sleep=None):
        got = []
        github.watch(self.site(), "desk", 5, emit=got.append, sleep=sleep or (lambda s: None), polls=polls)
        return got

    def queries(self):
        return [json.loads(c["input"])["query"] for c in self.gh.calls if c["argv"][1:3] == ["api", "graphql"]]

    def usage(self):
        path = github.usage_path(self.site())
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def test_watch_queries_ask_small_pages_and_their_cost(self):
        q = github.ORG_ISSUES_QUERY
        for part in ("repositories(first: 10", "issues(first: 20", "labels(first: 10", "rateLimit { cost remaining"):
            self.assertIn(part, q)
        self.assertNotIn("first: 100", q)
        self.assertNotIn("first: 50", q)
        self.assertIn("issues(first: 20", github.OFFICE_ISSUES_QUERY)
        w = github.WATCH_BOARD_QUERY
        self.assertIn("rateLimit { cost remaining resetAt }", w)
        self.assertIn("comments(last: 30)", w)
        self.assertNotIn("assignees(", w)
        self.assertNotIn("labels(", w)
        self.assertIn("assignees(first: 5)", github.BOARD_QUERY)  # status and sweep keep the full read
        self.assertNotIn("rateLimit", github.BOARD_QUERY)

    def test_org_scan_runs_every_interval_and_misses_nothing(self):
        self.gh.repos["example-project"] = []
        self.lines()
        self.assertEqual(sum("repositories(" in q for q in self.queries()), 1)
        self.gh.repos["example-project"].append(issue("IS1", 1, "a bug", author="someone", repo="example-project"))
        self.gh.repos["office"].append(issue("IS2", 2, "office news"))
        got = self.lines()  # within the interval: the office repository only
        self.assertEqual(got, [f"issue office#2 by example-director: office news | https://github.com/{ORG}/office/issues/2"])
        self.assertEqual(sum("repositories(" in q for q in self.queries()), 1)
        state_file = github.state_path(self.site())
        state = json.loads(state_file.read_text())
        state["org_scanned_at"] -= github.ORG_SCAN_INTERVAL + 1
        state_file.write_text(json.dumps(state))
        got = self.lines()
        self.assertEqual(got, [f"issue example-project#1 by someone: a bug | "
                               f"https://github.com/{ORG}/example-project/issues/1"])
        self.assertEqual(sum("repositories(" in q for q in self.queries()), 2)

    def test_repositories_are_paged_by_ten(self):
        for i in range(12):
            self.gh.repos[f"project-{i:02d}"] = []
        self.lines()
        self.gh.repos["project-11"].append(issue("IS1", 1, "late page", repo="project-11"))
        state_file = github.state_path(self.site())
        state = json.loads(state_file.read_text())
        state["org_scanned_at"] = 0
        state_file.write_text(json.dumps(state))
        self.assertEqual(self.lines(), [f"issue project-11#1 by example-director: late page | "
                                        f"https://github.com/{ORG}/project-11/issues/1"])

    def test_an_older_issue_sliding_into_the_page_is_not_reported(self):
        self.gh.repos["office"] = [issue(f"IS{n}", n, f"old {n}", created=f"2026-01-{n:02d}T00:00:00Z")
                                   for n in range(1, github.ISSUE_PAGE + 3)]
        self.assertEqual(self.lines(), [])
        self.gh.repos["office"] = [i for i in self.gh.repos["office"] if i["number"] < github.ISSUE_PAGE]  # newest closed
        self.assertEqual(self.lines(), [])
        self.gh.repos["office"].append(issue("ISN", 99, "really new"))
        self.assertEqual(self.lines(), [f"issue office#99 by example-director: really new | "
                                        f"https://github.com/{ORG}/office/issues/99"])

    def test_usage_records_the_cost_of_every_query(self):
        self.lines(polls=2)
        rows = self.usage()
        self.assertEqual([r["query"] for r in rows], ["board", "office_issues", "org_issues", "board", "office_issues"])
        self.assertEqual([r["cost"] for r in rows], [3, 1, 2, 3, 1])
        self.assertEqual(rows[-1]["remaining"], 5000 - 10)
        self.assertTrue(all(r["caller"] == "watch" and r["reset_at"] == "2026-10-21T18:21:00Z" and r["ts"].endswith("Z")
                            for r in rows))
        self.assertTrue(str(github.usage_path(self.site())).startswith(str(self.site_root / "cache")))
        self.assertEqual(self.events("github.usage"), [])  # a cache, not a record

    def test_the_time_the_limit_ran_out_is_recorded_once(self):
        self.gh.remaining = 0
        fails = ["GraphQL: API rate limit already exceeded for user ID 7.", "API rate limit already exceeded"]
        orig = self.gh.__call__

        def runner(argv, env, input_text, timeout):
            if fails and argv[1:3] == ["api", "graphql"]:
                self.gh.fail_next = fails.pop(0)
            return orig(argv, env, input_text, timeout)

        with mock.patch.object(github, "_exec", runner):
            github.watch(self.site(), "desk", 120, emit=lambda line: None, sleep=lambda s: None, polls=3)
        out = [r for r in self.usage() if r.get("event") == "exhausted"]
        self.assertEqual(len(out), 1, self.usage())
        self.assertIn("rate limit", out[0]["error"])
        self.assertEqual(out[0]["graphql"]["remaining"], 0)
        self.assertTrue(out[0]["graphql"]["reset_at"].endswith("Z"))
        self.assertEqual(out[0]["core"]["remaining"], 4958)
        self.assertNotIn(FAKE_TOKEN, github.usage_path(self.site()).read_text())

    def test_a_rate_limited_org_scan_backs_off(self):
        self.gh.repos["example-project"] = []
        self.lines()
        orig = self.gh.__call__

        def runner(argv, env, input_text, timeout):
            if argv[1:3] == ["api", "graphql"] and "repositories(" in (input_text or ""):
                return subprocess.CompletedProcess(argv, 1, "", "GraphQL: API rate limit already exceeded")
            return orig(argv, env, input_text, timeout)

        with mock.patch.object(github, "ORG_SCAN_INTERVAL", 0), mock.patch.object(github, "_exec", runner):
            got, slept = [], []
            github.watch(self.site(), "desk", 120, emit=got.append, sleep=slept.append, polls=2)
        self.assertEqual(slept, [120.0])
        self.assertTrue(got and got[0].startswith("github watch: rate limited"), got)


class Items(GithubBase):
    def test_item_add_creates_links_and_records(self):
        code, out, err = self.run_cli("item", "add", "--wi", "WI-0001", "--status", "today", "--layer", "2",
                                      "--project", "example-project", "--units", "3")
        self.assertEqual(code, 0, err)
        cmds = self.gh.commands()
        self.assertEqual(cmds[0], ["api", "graphql"])
        self.assertIn(["issue", "create"], cmds)
        self.assertIn(["project", "item-add"], cmds)
        create = next(c for c in self.gh.calls if c["argv"][1:3] == ["issue", "create"])
        self.assertIn(f"{ORG}/office", create["argv"])
        self.assertIn("WI-0001 A bootstrap proposal", create["argv"])
        self.assertIn(github.DESK_MARK, create["input"])
        edits = [c["argv"] for c in self.gh.calls if c["argv"][1:3] == ["project", "item-edit"]]
        opts = {e[e.index("--field-id") + 1]: e[-1] for e in edits}
        self.assertEqual(opts, {"F_STATUS": "O_S1", "F_LAYER": "O_L2", "F_PROJECT": "O_P0", "F_UNITS": "3"})
        self.assertTrue(all(e[e.index("--id") + 1] == "PVTI_11" and "PVT_1" in e for e in edits))
        ev = [e for e in self.events() if e["type"] == github.ADDED]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["wi"], "WI-0001")
        self.assertEqual(ev[0]["payload"]["url"], f"https://github.com/{ORG}/office/issues/11")
        self.assertEqual(ev[0]["payload"]["item_id"], "PVTI_11")
        code, _, err = self.run_cli("item", "add", "--wi", "WI-0001")
        self.assertEqual(code, 2)
        self.assertIn("already has a board item", err)

    def test_start_item_moves_the_wis_item_to_in_progress(self):
        """Release 86 (dureh-team#91 item 2): dispatching a WI moves its board item to In progress."""
        self.assertIsNone(github.start_item(self.site(), "desk", "WI-0001"))  # no board item: nothing
        self.run_cli("item", "add", "--wi", "WI-0001")
        self.gh.calls.clear()
        line = github.start_item(self.site(), "desk", "WI-0001")
        self.assertEqual(line, f"board: https://github.com/{ORG}/office/issues/11 -> In progress")
        prog = "O_S" + str(list(github.STATUSES).index("In progress"))
        edits = [c["argv"] for c in self.gh.calls if c["argv"][1:3] == ["project", "item-edit"]]
        self.assertEqual([e[-1] for e in edits], [prog])

    def test_unknown_option_fails_before_anything_is_created(self):
        code, _, err = self.run_cli("item", "add", "--wi", "WI-0001", "--project", "no-such-project")
        self.assertEqual(code, 2)
        self.assertIn("no option 'no-such-project'", err)
        self.assertNotIn(["issue", "create"], self.gh.commands())

    def test_decision_values_both_sets(self):
        """Director 2026-10-27: new names, old names read as new; a board not yet migrated gets its old option."""
        dv = github.decision_value
        for old, new in (("Approved", "Approve 1"), ("Option 1", "Approve 1"), ("option 2", "Approve 2"),
                         ("Option 3", "Approve 3"), ("Rejected", "Rejected"), ("Reject", "Rejected"), ("Later", "Discuss"),
                         ("Discuss", "Discuss")):
            self.assertEqual(dv(old), new)
        self.assertIsNone(dv(""))
        self.assertTrue(github.is_approval("Option 2"))
        self.assertFalse(github.is_approval("Later"))
        self.assertEqual(github._fields(decision="option 3"), {"Decision": "Approve 3"})
        with self.assertRaises(DurehError):
            github._fields(decision="Maybe")
        old = {"fields": {"Decision": {"id": "F_D", "options": {n: f"O_{n}" for n in github.DECISION_ALIASES}}}}
        self.assertEqual(github._option(old, "Decision", "Approve 2"), ("F_D", "O_Option 2"))
        self.assertEqual(github._option(old, "Decision", "Discuss"), ("F_D", "O_Later"))
        self.assertIn(github._option(old, "Decision", "Approve 1")[1], ("O_Approved", "O_Option 1"))
        new = {"fields": {"Decision": {"id": "F_D", "options": {n: f"O_{n}" for n in github.DECISIONS}}}}
        self.assertEqual(github._option(new, "Decision", "Reject"), ("F_D", "O_Rejected"))

    def test_item_set_status_and_decision_by_layer(self):
        self.run_cli("item", "add", "--wi", "WI-0001", "--layer", "3")
        self.gh.calls.clear()
        code, out, err = self.run_cli("item", "set", "--wi", "WI-0001", "--status", "Done")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["fields"], {"Status": "Done"})
        code, _, err = self.run_cli("item", "set", "--wi", "WI-0001", "--decision", "Approved")
        self.assertEqual(code, 2)
        self.assertIn("only the Director decides", err)
        code, out, err = self.run_cli("item", "set", "--wi", "WI-0001", "--layer", "2", "--decision", "approved")
        self.assertEqual(code, 0, err)
        self.assertEqual([e["type"] for e in self.events()].count(github.UPDATED), 2)

    def test_wip_limit_on_in_progress_and_on_hold(self):
        """Release 82 (dureh-team#74): a third open In progress item of the company is refused; --on-hold moves an
        item back to Today with the label on-hold, which a later In progress removes."""
        from dureh_core import workflow as wf

        self.run_cli("item", "add", "--wi", "WI-0001")
        wf.wi_new(self.site(), "desk", "second", "g", "doc-writer", "modules/demo", ["ok"])
        mine = f"https://github.com/{ORG}/office/issues/11"
        self.gh.items = [node("I1", 1, "first run", status="In progress"), node("I2", 2, "second run", status="In progress"),
                         node("I3", 3, "closed run", status="In progress", state="CLOSED"),
                         node("I4", 4, "other company", status="In progress", repo="other-org/x"),
                         node("PVTI_11", 11, "mine", status="Today", labels=["on-hold"])]
        self.gh.calls.clear()
        code, _, err = self.run_cli("item", "set", "--wi", "WI-0001", "--status", "In progress")
        self.assertEqual(code, 2)
        self.assertIn("2 open items In progress", err)
        self.assertIn("office#1 first run", err)
        self.assertIn("office#2 second run", err)
        self.assertNotIn("office#3", err)
        self.assertIn("--status Today --on-hold", err)
        self.assertNotIn(["project", "item-edit"], self.gh.commands())
        code, _, err = self.run_cli("item", "add", "--wi", "WI-0002", "--status", "In progress")
        self.assertEqual(code, 2)
        self.assertIn("2 open items In progress", err)
        # move one back: Today + on-hold
        self.gh.next_issue = 20  # the fake names the board item after the last issue number
        self.run_cli("item", "add", "--wi", "WI-0002", "--url", f"https://github.com/{ORG}/office/issues/2")
        self.gh.calls.clear()
        code, out, err = self.run_cli("item", "set", "--url", f"https://github.com/{ORG}/office/issues/2", "--on-hold")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["fields"], {"Status": "Today"})
        edit = [c["argv"][1:] for c in self.gh.calls if c["argv"][1:3] == ["issue", "edit"]]
        self.assertEqual(edit, [["issue", "edit", f"https://github.com/{ORG}/office/issues/2", "--add-label", "on-hold"]])
        self.assertIn(["label", "create"], self.gh.commands())
        code, _, err = self.run_cli("item", "set", "--wi", "WI-0001", "--on-hold", "--status", "Done")
        self.assertEqual(code, 2)
        self.assertIn("--on-hold sets Status Today", err)
        # one slot free: In progress goes through and drops on-hold
        self.gh.items[1] = node("I2", 2, "second run", status="Today", labels=["on-hold"])
        self.gh.calls.clear()
        code, out, err = self.run_cli("item", "set", "--wi", "WI-0001", "--status", "in progress")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["labels"], {"remove": ["on-hold"]})
        edit = [c["argv"][1:] for c in self.gh.calls if c["argv"][1:3] == ["issue", "edit"]]
        self.assertEqual(edit, [["issue", "edit", mine, "--remove-label", "on-hold"]])
        self.assertEqual(self.events()[-1]["payload"]["labels"], {"remove": ["on-hold"]})
        # an item already In progress may be set again (it is not counted against itself)
        self.gh.items[1] = node("I2", 2, "second run", status="In progress")
        self.gh.items[4] = node("PVTI_11", 11, "mine", status="In progress")
        code, _, err = self.run_cli("item", "set", "--wi", "WI-0001", "--status", "In progress")
        self.assertEqual(code, 2, "two others are running")
        self.gh.items[0] = node("I1", 1, "first run", status="Done")
        code, _, err = self.run_cli("item", "set", "--wi", "WI-0001", "--status", "In progress")
        self.assertEqual(code, 0, err)

    def test_layer_1_2_ask_keeps_the_wip_limit(self):
        """Release 83 (the release-82 WIP limit, dureh-team#74): a layer 1-2 ask opens In progress, so it is refused
        while two open items of the company are In progress, before anything is created; --on-hold opens it Today
        with the label on-hold; a layer-3 ask (Needs approval) is not counted and takes no --on-hold."""
        body = self.tmp / "ask.md"
        body.write_text("Which order?")
        ask = lambda *extra: self.run_cli("item", "ask", "--title", "Order", "--body-file", str(body), *extra)  # noqa: E731
        self.gh.items = [node("I1", 1, "first run", status="In progress"), node("I2", 2, "second run", status="In progress"),
                         node("I4", 4, "other company", status="In progress", repo="other-org/x")]
        self.gh.calls.clear()
        for layer in ("1", "2"):
            code, _, err = ask("--layer", layer)
            self.assertEqual(code, 2, layer)
            self.assertIn("2 open items In progress", err)
            self.assertIn("office#1 first run", err)
            self.assertIn("office#2 second run", err)
            self.assertIn("--on-hold", err)
        self.assertNotIn(["issue", "create"], self.gh.commands())
        self.assertNotIn(["project", "item-add"], self.gh.commands())
        self.assertFalse([e for e in self.events() if e["type"] == github.ASKED])
        # --on-hold: Status Today and the label, the limit untouched
        self.gh.calls.clear()
        code, out, err = ask("--layer", "2", "--on-hold")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(res["fields"]["Status"], "Today")
        self.assertEqual(res["labels"], {"add": ["on-hold"]})
        argv = next(c["argv"] for c in self.gh.calls if c["argv"][1:3] == ["issue", "create"])
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--label"], ["kind:decision", "on-hold"])
        self.assertEqual([e for e in self.events() if e["type"] == github.ASKED][-1]["payload"]["labels"],
                         {"add": ["on-hold"]})
        # layer 3 opens Needs approval: not refused by the limit, and --on-hold does not apply
        code, _, err = ask("--layer", "3", *THREE)
        self.assertEqual(code, 0, err)
        code, _, err = ask("--layer", "3", "--on-hold", *THREE)
        self.assertEqual(code, 2)
        self.assertIn("--on-hold is for a layer 1-2 ask", err)
        # one slot free: the layer-2 ask opens In progress
        self.gh.items[0] = node("I1", 1, "first run", status="Done")
        code, out, err = ask("--layer", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["fields"]["Status"], "In progress")

    def test_item_priority_and_roadmap_dates(self):
        """#31: the desk sets Priority (P0-P3) and the roadmap's Start/Target dates through the harness."""
        code, _, err = self.run_cli("item", "add", "--wi", "WI-0001", "--priority", "p2")
        self.assertEqual(code, 0, err)
        self.gh.calls.clear()
        code, out, err = self.run_cli("item", "set", "--wi", "WI-0001", "--priority", "P1", "--start", "2026-10-21",
                                      "--target", "2026-10-23")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["fields"], {"Priority": "P1", "Start date": "2026-10-21",
                                                     "Target date": "2026-10-23"})
        edits = [c["argv"] for c in self.gh.calls if c["argv"][1:3] == ["project", "item-edit"]]
        got = {e[e.index("--field-id") + 1]: e[-2:] for e in edits}
        self.assertEqual(got, {"F_PRIORITY": ["--single-select-option-id", "O_PR1"], "F_START": ["--date", "2026-10-21"],
                               "F_TARGET": ["--date", "2026-10-23"]})
        for args, msg in ((["--priority", "P5"], "--priority must be one of"), (["--start", "21/10"], "--start must be a date")):
            code, _, err = self.run_cli("item", "set", "--wi", "WI-0001", *args)
            self.assertEqual(code, 2)
            self.assertIn(msg, err)

    def test_item_close_and_sweep(self):
        """#34: the desk closes an item at promote, and the sweep closes Done items that wait for nobody."""
        self.run_cli("item", "add", "--wi", "WI-0001")
        self.gh.calls.clear()
        code, out, err = self.run_cli("item", "close", "--wi", "WI-0001", "--comment", "promoted")
        self.assertEqual(code, 0, err)
        close = [c["argv"] for c in self.gh.calls if c["argv"][1:3] == ["issue", "close"]]
        self.assertEqual(close[0][3], f"https://github.com/{ORG}/office/issues/11")
        self.assertIn(github.DESK_MARK, close[0][close[0].index("--comment") + 1])
        self.assertEqual([e["type"] for e in self.events()][-1], github.CLOSED)
        self.gh.items = [node("I1", 1, "done", status="Done"), node("I2", 2, "review", status="Done", labels=["needs-review"]),
                         node("I3", 3, "open work", status="In progress"), node("I4", 4, "closed", status="Done", state="CLOSED"),
                         node("I5", 5, "elsewhere", status="Done", repo="other-org/x")]
        self.gh.calls.clear()
        code, out, err = self.run_cli("sweep", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["would_close"], [f"https://github.com/{ORG}/office/issues/1"])
        self.assertNotIn(["issue", "close"], self.gh.commands())
        code, out, err = self.run_cli("sweep")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual((res["closed"], res["kept"]), ([f"https://github.com/{ORG}/office/issues/1"],
                                                        [f"https://github.com/{ORG}/office/issues/2"]))
        self.assertEqual(self.gh.commands().count(["issue", "close"]), 1)
        self.assertEqual([e["type"] for e in self.events()][-1], github.SWEPT)
        os.environ["DUREH_ACTOR"] = "py-writer"
        code, _, err = self.run_cli("sweep")
        os.environ.pop("DUREH_ACTOR")
        self.assertEqual(code, 2)
        self.assertIn("only the desk", err)

    def ask(self, *extra, wi="WI-0001"):
        body = self.tmp / "ask.md"
        body.write_text("Which order?")
        code, out, err = self.run_cli("item", "ask", "--title", "Order", "--body-file", str(body), "--options", "A",
                                      "B", *(["--wi", wi] if wi else []), *THREE, *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_an_ask_about_a_wi_does_not_take_its_board_link(self):
        """Release 81 (finding cc81a566): ask --wi, then item set --wi to Done and item close --wi: the WI's own item
        moves and closes, the ask item is untouched (it went In progress, then Done at promote, before)."""
        self.run_cli("item", "add", "--wi", "WI-0001")  # issue 11, item PVTI_11
        ask = self.ask()  # issue 12, item PVTI_12
        self.assertEqual(ask["item_id"], "PVTI_12")
        self.gh.calls.clear()
        code, out, err = self.run_cli("item", "set", "--wi", "WI-0001", "--status", "Done")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["item_id"], "PVTI_11")
        code, _, err = self.run_cli("item", "close", "--wi", "WI-0001")
        self.assertEqual(code, 0, err)
        code, _, err = self.run_cli("item", "comment", "--wi", "WI-0001", stdin="promoted")
        self.assertEqual(code, 0, err)
        touched = [c["argv"] for c in self.gh.calls if c["argv"][1:3] in (["project", "item-edit"], ["issue", "close"],
                                                                         ["issue", "comment"])]
        self.assertEqual(len(touched), 3)
        self.assertFalse([a for a in touched if "PVTI_12" in a or ask["url"] in a], "the ask item is untouched")
        self.assertTrue(all("PVTI_11" in a or f"https://github.com/{ORG}/office/issues/11" in a for a in touched), touched)
        code, out, err = self.run_cli("item", "set", "--url", ask["url"], "--status", "In progress")
        self.assertEqual((code, json.loads(out)["item_id"]), (0, "PVTI_12"), err)  # the ask is named by --url

    def test_a_wi_with_only_an_ask_has_no_board_link(self):
        ask = self.ask()
        self.gh.calls.clear()
        code, _, err = self.run_cli("item", "set", "--wi", "WI-0001", "--status", "Done")
        self.assertEqual(code, 2)
        self.assertIn(f"the decision ask {ask['url']} is not the WI's item; name it with --url", err)
        self.assertEqual(self.gh.calls, [], "nothing on the board changed")

    def test_sweep_keeps_undecided_and_fail_closed_asks(self):
        """Release 81 (finding cc81a566): the sweep closed a fail-closed decision ask with an empty Decision."""
        plain = self.ask(wi=None)["number"]  # 11: recorded ask
        closed_ask = self.ask("--fail-closed", "--otherwise", "답이 올 때까지 기다립니다.", wi=None)["number"]  # 12: recorded fail-closed ask
        u = lambda n: f"https://github.com/{ORG}/office/issues/{n}"
        self.gh.items = [
            node("I11", plain, "recorded ask, no Decision, label gone", status="Done"),
            node("I12", closed_ask, "recorded fail-closed, decided, label gone", status="Done", decision="Approved"),
            node("I20", 20, "decision ask, empty Decision", status="Done", labels=["kind:decision", "fail-closed"]),
            node("I21", 21, "decision ask, empty Decision", status="Done", labels=["kind:decision"]),
            node("I22", 22, "decision ask, decided", status="Done", labels=["kind:decision"], decision="Option 1"),
            node("I23", 23, "fail-closed, decided", status="Done", labels=["kind:decision", "fail-closed"],
                 decision="Approved"),
            node("I24", 24, "plain work", status="Done")]
        self.gh.calls.clear()
        code, out, err = self.run_cli("sweep")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(res["closed"], [u(22), u(24)])
        self.assertEqual(res["kept"], [u(plain), u(closed_ask), u(20), u(21), u(23)])
        closed = [c["argv"][3] for c in self.gh.calls if c["argv"][1:3] == ["issue", "close"]]
        self.assertEqual(closed, [u(22), u(24)])

    def test_desk_cannot_finish_an_undecided_fail_closed_ask_by_hand(self):
        """Release 81 (DUREH's provisional choice): `item set --url <ask> --status Done` and `item close --url <ask>`
        are refused on a fail-closed ask (record or label) whose Decision is empty; other undecided asks stay allowed."""
        recorded = self.ask("--fail-closed", "--otherwise", "답이 올 때까지 기다립니다.", wi=None)  # issue 11
        plain = self.ask(wi=None)  # issue 12, not fail-closed
        self.gh.items = [node("I11", 11, "fail-closed ask", status="Needs approval", labels=["kind:decision"]),
                         node("I12", 12, "plain ask", status="Needs approval", labels=["kind:decision"]),
                         node("I30", 30, "labelled only", status="Needs approval",
                              labels=["kind:decision", "fail-closed"])]
        labelled = f"https://github.com/{ORG}/office/issues/30"
        self.gh.calls.clear()
        # `item set` takes recorded items only; `item close --url` any issue, so the label alone counts there
        for url, args in ((recorded["url"], ["set", "--url", recorded["url"], "--status", "Done"]),
                          (recorded["url"], ["close", "--url", recorded["url"]]), (labelled, ["close", "--url", labelled])):
            code, _, err = self.run_cli("item", *args)
            self.assertEqual(code, 2, args)
            self.assertIn(f"refused: {url} is a fail-closed ask and its Decision column is empty", err)
            self.assertIn("the board's Decision column", err)
        self.assertFalse([c for c in self.gh.calls if c["argv"][1:3] in (["project", "item-edit"], ["issue", "close"])])
        code, _, err = self.run_cli("item", "set", "--url", recorded["url"], "--status", "In progress")
        self.assertEqual(code, 0, err)  # only Done (and a close) is refused
        for args in (["set", "--url", plain["url"], "--status", "Done"], ["close", "--url", plain["url"]]):
            code, _, err = self.run_cli("item", *args)
            self.assertEqual(code, 0, err)  # an undecided ask that is not fail-closed: allowed by hand
        self.gh.items[0] = node("I11", 11, "fail-closed ask", status="Needs approval", decision="Option 2",
                                labels=["kind:decision", "fail-closed"])
        for args in (["set", "--url", recorded["url"], "--status", "Done"], ["close", "--url", recorded["url"]]):
            code, _, err = self.run_cli("item", *args)
            self.assertEqual(code, 0, err)  # once decided, the desk finishes it
        self.assertEqual(self.gh.commands().count(["issue", "close"]), 2)

    def test_ask_assigns_the_director_and_marks(self):
        body = self.tmp / "ask.md"
        body.write_text("Which order for the next two items?")
        code, out, err = self.run_cli("item", "ask", "--title", "Order of work", "--body-file", str(body),
                                      "--options", "first A", "first B", "--layer", "3", "--wi", "WI-0001", *THREE)
        self.assertEqual(code, 0, err)
        create = next(c for c in self.gh.calls if c["argv"][1:3] == ["issue", "create"])
        argv = create["argv"]
        self.assertEqual(argv[argv.index("--assignee") + 1], "example-director")
        self.assertEqual(argv[argv.index("--label") + 1], "kind:decision")
        self.assertTrue(create["input"].rstrip().endswith(github.DESK_MARK))
        self.assertIn("- **Approve 2**: first B", create["input"])
        self.assertIn("- **Rejected**:", create["input"])
        self.assertIn("- **Discuss**: hold; nothing proceeds, the desk answers your comments", create["input"])
        self.assertIn("Decision** column: Approve 1, Approve 2, Rejected, Discuss.", create["input"])
        self.assertIn("your comments on this item are read with it", create["input"])
        self.assertNotIn("Option 1", create["input"].split("**배경 · Background**")[1])
        self.assertIn(["label", "create"], self.gh.commands())
        edits = [c["argv"] for c in self.gh.calls if c["argv"][1:3] == ["project", "item-edit"]]
        self.assertIn("O_S3", [e[-1] for e in edits])  # Needs approval
        self.assertIn("O_L3", [e[-1] for e in edits])
        ev = [e for e in self.events() if e["type"] == github.ASKED]
        self.assertEqual(len(ev), 1)
        p = ev[0]["payload"]
        self.assertEqual((p["layer"], p["options"], p["assignee"], ev[0]["wi"]), (3, 2, "example-director", "WI-0001"))
        self.assertNotIn("Which order", json.dumps(ev[0]))  # no text in the record
        res = json.loads(out)
        self.gh.calls.clear()
        code, _, err = self.run_cli("item", "set", "--url", res["url"], "--decision", "Option 1")
        self.assertEqual(code, 2)
        self.assertIn("only the Director decides", err)

    def test_layer3_ask_opens_with_three_lines(self):
        """Release 80 (dureh-team#30 Option 1): 결정할 것 / 추천 / 답이 없으면 first, the body as background."""
        body = self.tmp / "ask.md"
        body.write_text("Two items are ready; the order matters for the release.")
        code, _, err = self.run_cli("item", "ask", "--title", "Order", "--body-file", str(body), "--options", "A", "B")
        self.assertEqual(code, 2)
        self.assertIn("give --decide, --recommend, --otherwise", err)
        self.assertEqual(self.gh.calls, [])
        code, _, err = self.run_cli("item", "ask", "--title", "Order", "--body-file", str(body), "--options", "A", "B",
                                    *THREE)
        self.assertEqual(code, 0, err)
        text = next(c for c in self.gh.calls if c["argv"][1:3] == ["issue", "create"])["input"]
        self.assertTrue(text.startswith("**결정할 것:** A를 먼저 할까요?\n**추천:** 예, A가 B의 입력입니다.\n"
                                        "**답이 없으면:** 30분 뒤 Option 1로 진행합니다."), text)
        self.assertLess(text.index("**배경 · Background**"), text.index("Two items are ready"))
        self.gh.calls.clear()
        code, _, err = self.run_cli("item", "ask", "--title", "Order", "--body-file", str(body), "--layer", "2")
        self.assertEqual(code, 0, err)  # a layer-2 ask (DUREH) needs no three lines

    def test_class_d_ask_is_a_director_step_with_the_command(self):
        """Release 85 (approval classes): --class D opens Waiting for Director with the exact command, no options."""
        body = self.tmp / "step.md"
        body.write_text("The push was refused to the desk after the approval on item 7.")
        code, _, err = self.run_cli("item", "ask", "--title", "Push", "--body-file", str(body), "--class", "D")
        self.assertEqual(code, 2)
        self.assertIn("--class D needs --command", err)
        code, out, err = self.run_cli("item", "ask", "--title", "Push", "--body-file", str(body), "--class", "D",
                                      "--command", "git push origin main")
        self.assertEqual(code, 0, err)
        create = next(c for c in self.gh.calls if c["argv"][1:3] == ["issue", "create"])
        argv, text = create["argv"], create["input"]
        self.assertEqual(argv[argv.index("--label") + 1], "waiting-director")
        self.assertEqual(argv[argv.index("--assignee") + 1], "example-director")
        self.assertTrue(text.startswith("## 현재 상태\n\nThe push was refused"))
        self.assertIn("## 대표님이 하실 일\n\n```\ngit push origin main\n```", text)
        self.assertNotIn("Approve 1", text)
        wait = "O_S" + str(list(github.STATUSES).index("Waiting for Director"))
        edits = [c["argv"] for c in self.gh.calls if c["argv"][1:3] == ["project", "item-edit"]]
        self.assertIn(wait, [e[-1] for e in edits])
        self.assertEqual([e["payload"]["class"] for e in self.events() if e["type"] == github.DIRECTOR_STEP], ["D"])
        code, _, err = self.run_cli("item", "ask", "--title", "Q", "--body-file", str(body), "--command", "x", *THREE)
        self.assertEqual(code, 2)
        self.assertIn("--command goes with --class D", err)

    def test_ask_refuses_more_than_three_options(self):
        body = self.tmp / "ask.md"
        body.write_text("q")
        code, _, err = self.run_cli("item", "ask", "--title", "Q", "--body-file", str(body),
                                    "--options", "a", "b", "c", "d")
        self.assertEqual(code, 2)
        self.assertIn("at most 3 options", err)

    def test_comment_carries_the_marker(self):
        self.run_cli("item", "add", "--wi", "WI-0001")
        code, out, err = self.run_cli("item", "comment", "--wi", "WI-0001", stdin="thanks, noted")
        self.assertEqual(code, 0, err)
        call = self.gh.calls[-1]
        self.assertEqual(call["argv"][1:3], ["issue", "comment"])
        self.assertIn(github.DESK_MARK, call["input"])
        self.assertEqual([e["type"] for e in self.events()][-1], github.COMMENTED)

    def test_only_the_desk_writes(self):
        os.environ["DUREH_ACTOR"] = "py-writer"
        code, _, err = self.run_cli("item", "add", "--wi", "WI-0001")
        os.environ.pop("DUREH_ACTOR")
        self.assertEqual(code, 2)
        self.assertIn("only the desk", err)

    def test_event_add_refuses_github_events(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()) as err:
            code = main(["--site", str(self.site_root), "event", "add", "--kind", "github.item.added", "--wi",
                         "WI-0001", "--payload", "{}", "--actor", "desk"])
        self.assertEqual(code, 2)


class DeskText(GithubBase):
    def test_rule_when_enabled(self):
        text = _claude_md(self.site())
        self.assertIn("12a. GitHub is the company's coordination board (release 69)", text)
        self.assertIn("github item add --wi <WI>", text)
        self.assertIn("github watch` as a Monitor", text)
        self.assertIn("every 120 s by default", text)
        self.assertIn("Decision column", text)
        self.assertIn("A `work request` line", text)
        self.assertIn("github item add --wi <WI> --url <issue url>", text)

    def test_rule_when_off(self):
        self.set_config({"enabled": False})
        text = _claude_md(self.site())
        self.assertIn("12a. GitHub (`", text)
        self.assertIn("is off", text)
        self.assertNotIn("github item ask", text)

    def test_slack_too_prefers_github_and_drops_opinions(self):
        token = self.tmp / "slack-token"
        token.write_text("xoxb-test\n")
        slack = {"channel": "C00EXAMPLE1", "username": "desk (example-org)", "director_user": "U00DIRECTOR",
                 "agents": {"U00EXAMPLE1": "Helper"}, "consult_agent": "Helper", "token_file": str(token)}
        self.set_config(slack, key="slack")
        text = _claude_md(self.site())
        self.assertIn("GitHub is preferred for asks", text)
        self.assertNotIn("slack opinions --watch", text)
        self.assertNotIn("Helper is consulted first", text)
        self.set_config({"enabled": False})
        text = _claude_md(self.site())
        self.assertIn("slack opinions --watch", text)
        self.assertNotIn("GitHub is preferred for asks", text)



class Digest(GithubBase):
    """Release 86 (dureh-team#91 item 5): the company dashboard in the #31 format."""

    ASK = ("## 현재 상태\n\n배경.\n\n## 결정할 것\n\n**A를 먼저 할까요?**\n\n- **Approve 1 (추천):** A 먼저\n"
           "- **Approve 2:** B 먼저\n\n답이 없으면: 기다립니다.\n")
    STEP = "## 현재 상태\n\n준비됨.\n\n## 대표님이 하실 일\n\n**`git push origin main` 한 번**\n"

    def board(self):
        def body(n, text):
            n["content"]["body"] = text
            return n
        prio = lambda n, v: (n["fieldValues"]["nodes"].append({"name": v, "field": {"name": "Priority"}}), n)[1]  # noqa: E731
        self.gh.items = [body(node("I1", 1, "Decision: order", "Needs approval"), self.ASK),
                         prio(body(node("I2", 2, "Decision: fail", "Needs approval", labels=["fail-closed"]),
                                   "결정할 것: 공개 push?"), "P0 now"),
                         body(node("I3", 3, "Push it", "Waiting for Director"), self.STEP),
                         body(node("I4", 4, "Closed ask", "Needs approval", state="CLOSED"), self.ASK),
                         node("I5", 5, "Working", "In progress")]

    def test_print(self):
        self.board()
        code, out, _ = self.run_cli("digest")
        self.assertEqual(code, 0)
        self.assertIn("# example-org 대시보드", out)
        first, second = out.index("office#2"), out.index("office#1")
        self.assertLess(first, second)  # Priority first
        self.assertIn("- **결정할 것:** A를 먼저 할까요?", out)
        self.assertIn("- **데스크 추천:** Approve 1: A 먼저", out)
        self.assertIn("- **안 하면 / 답이 없으면:** 기다립니다.", out)
        self.assertIn("- **결정할 것:** 공개 push?", out)
        self.assertIn("- **데스크 추천:** <채워 주세요>", out)
        self.assertIn("없음 (fail-closed), 답을 기다립니다", out)
        self.assertIn("- `git push origin main` 한 번", out)
        self.assertNotIn("office#4", out)
        self.assertIn(github.DESK_MARK, out)  # not read as the Director's own issue
        self.assertNotIn("office#5", out)
        self.assertTrue(any("body" in c["input"] for c in self.gh.calls if c["input"]))
        self.assertEqual(self.events(), [])

    def test_write_needs_the_desk_and_the_issue(self):
        self.board()
        code, _, err = self.run_cli("digest", "--write", "--actor", "desk")
        self.assertEqual(code, 2)
        self.assertIn("dashboard_issue is not set", err)
        cfg = json.loads((self.site_root / "config.json").read_text())["github"]
        self.set_config({**cfg, "dashboard_issue": 7})
        code, _, err = self.run_cli("digest", "--write", "--actor", "nobody")
        self.assertEqual(code, 2)
        self.assertFalse(any(c["argv"][1:3] == ["issue", "edit"] for c in self.gh.calls))
        code, out, _ = self.run_cli("digest", "--write", "--actor", "desk")
        self.assertEqual(code, 0, out)
        self.assertIn("issues/7 written (2 to decide, 1 Director steps)", out)
        edit = [c for c in self.gh.calls if c["argv"][1:3] == ["issue", "edit"]]
        self.assertEqual(len(edit), 1)
        self.assertIn("## 1. 정하실 것", edit[0]["input"])
        self.assertEqual([e["type"] for e in self.events()], [github.DIGESTED])

    def test_write_unchanged_is_skipped(self):
        self.board()
        cfg = json.loads((self.site_root / "config.json").read_text())["github"]
        self.set_config({**cfg, "dashboard_issue": 7})
        body = github.digest(self.site())["text"].replace("갱신: ", "갱신: 어제 ", 1)
        real = self.gh.__call__

        def answer(argv, env, input_text, timeout):
            if argv[1:3] == ["issue", "view"]:
                self.gh.calls.append({"argv": argv, "env": env, "input": input_text})
                return subprocess.CompletedProcess(argv, 0, body, "")
            return real(argv, env, input_text, timeout)
        with mock.patch.object(github, "_exec", answer):
            code, out, _ = self.run_cli("digest", "--write", "--actor", "desk")
        self.assertIn("unchanged", out)
        self.assertFalse(any(c["argv"][1:3] == ["issue", "edit"] for c in self.gh.calls))
        self.assertEqual(self.events(), [])

    def test_bad_dashboard_issue_is_refused(self):
        cfg = json.loads((self.site_root / "config.json").read_text())["github"]
        self.set_config({**cfg, "dashboard_issue": "seven"})
        self.assertTrue(any("dashboard_issue must be an issue number" in e for e in github.config_errors(self.site())))


if __name__ == "__main__":
    unittest.main()
