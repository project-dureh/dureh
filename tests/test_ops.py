"""Operational tools: path-refs, purge, push, slurm, usage, exams, adapter drift, YAML subset."""
import json
import os
import shutil
import stat
import subprocess
import unittest
from pathlib import Path

from helpers import HARNESS, SiteCase, copy_writable

from dureh_core import ops
from dureh_core import workflow as wf
from dureh_core.records import Ledger
from dureh_core.util import DurehError


class Ops(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()

    def test_path_refs(self):
        (self.site_root / "notes" / "a.md").write_text("see modules/demo/src/demo.py for details\n")
        r = ops.path_refs(self.s, "modules/demo/src/demo.py", roots=[self.site_root])
        self.assertTrue(any("notes/a.md:1" in h for h in r["hits"]))

    def test_purge_removes_links_not_targets(self):
        self.make_module()
        b = wf.wi_new(self.s, "desk", "b", "g", "pm-modules", "modules/demo", ["a"])["id"]
        w = wf.wi_new(self.s, "desk", "t", "g", "py-writer", "modules/demo", ["a"], parent=b)["id"]
        wf.wi_approve(self.s, "desk", w, "ok")
        # the bundle is never dispatched here; create the worktree directly
        wf._create_worktree(self.s, w, "modules/demo")
        precious = self.tmp / "precious"
        precious.mkdir()
        (precious / "keep.txt").write_text("keep")
        with self.assertRaisesRegex(DurehError, "purge only"):
            wf.purge(self.s, "desk", w)
        wf.dispatch_stop(self.s, "desk", w, "test")
        link = self.site_root / "worktrees" / w / "linked"
        link.symlink_to(precious)
        plan = wf.purge(self.s, "desk", w, dry_run=True)
        self.assertTrue(plan["planned"] and not plan["removed"])
        wf.purge(self.s, "desk", w)
        self.assertTrue((precious / "keep.txt").is_file(), "symlink target survives purge")
        self.assertFalse((self.site_root / "worktrees" / w).exists())
        with self.assertRaisesRegex(DurehError, "only the desk"):
            wf.purge(self.s, "py-writer", w)

    def test_purge_removes_a_worktree_git_cannot_delete(self):
        """Release 57 (UR-20261012-01): `git worktree remove --force` deregisters the worktree even when it
        fails to delete the directory; purge then removes the directory itself and prunes."""
        self.make_module()
        b = wf.wi_new(self.s, "desk", "b", "g", "pm-modules", "modules/demo", ["a"])["id"]
        w = wf.wi_new(self.s, "desk", "t", "g", "py-writer", "modules/demo", ["a"], parent=b)["id"]
        wf.wi_approve(self.s, "desk", w, "ok")
        wf._create_worktree(self.s, w, "modules/demo")
        wf.dispatch_stop(self.s, "desk", w, "test")
        wt = self.site_root / "worktrees" / w
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt, capture_output=True,
                                text=True).stdout.strip()
        outside = self.tmp / "main-env"
        outside.mkdir()
        (outside / "python").write_text("#!/bin/sh\n")
        venv = wt / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").symlink_to(outside / "python")
        (venv / "lib" / "pkg").mkdir(parents=True)
        (venv / "lib" / "pkg" / "f.py").write_text("x\n")
        (venv / "lib64").symlink_to("lib")
        (venv / "lib" / "pkg").chmod(stat.S_IRUSR | stat.S_IXUSR)  # git cannot unlink f.py
        mod = self.site_root / "modules" / "demo"
        out = wf.purge(self.s, "desk", w)
        self.assertFalse(wt.exists())
        self.assertTrue((outside / "python").is_file(), "symlink target untouched")
        self.assertIn(str(wt), out["removed"])
        self.assertNotIn(str(wt), subprocess.run(["git", "worktree", "list"], cwd=mod, capture_output=True, text=True).stdout)
        self.assertEqual(subprocess.run(["git", "branch", "--list", branch], cwd=mod, capture_output=True,
                                        text=True).stdout.strip(), "", "the stopped WI's own branch dropped")
        self.assertEqual([e["type"] for e in Ledger(self.s).events(w) if e["type"] == "purged"], ["purged"])

    def _stopped_project_wi(self, s=None):
        s = s or self.s
        (self.site_root / "projects" / "lna").mkdir(parents=True, exist_ok=True)
        w = wf.wi_new(s, "desk", "run", "g", "researcher", "projects/lna", ["a"])["id"]
        wf.wi_approve(s, "desk", w, "ok")
        return w

    def test_purge_removes_old_job_logs_in_project_root_only(self):
        """UR-20261004-01: a record from before release 19 (no log_out/log_err, no chdir); the
        site's shape was projects/<P>/dureh-WI-0052-3612221.{out,err} in the project root."""
        w = self._stopped_project_wi()
        Ledger(self.s).append("run", "slurm", "desk", w, {
            "run_type": "slurm", "status": "submitted", "job_id": "3612221",
            "command": ["sbatch", "--parsable", f"--job-name=dureh-{w}", "job.sh"], "script": "job.sh"})
        proj = self.site_root / "projects" / "lna"
        mine = [proj / f"dureh-{w}-3612221.out", proj / f"dureh-{w}-3612221.err"]
        keep = [proj / f"dureh-{w}-9999999.out",  # a job id this WI never recorded
                proj / "dureh-WI-0999-3612221.out",  # another WI's name
                proj / "notes.out"]
        for f in mine + keep:
            f.write_text("log\n")
        outside = self.tmp / "outside.out"
        outside.write_text("not the site's")
        (self.site_root / f"dureh-{w}-3612221.out").symlink_to(outside)  # a link, never a regular file
        Ledger(self.s).event("wi.dispatched", "desk", w, {"role": "researcher"})
        wf.dispatch_stop(self.s, "desk", w, "test")
        plan = wf.purge(self.s, "desk", w, dry_run=True)
        self.assertEqual(sorted(plan["planned_logs"]), sorted(str(f) for f in mine))
        self.assertTrue(all(f.is_file() for f in mine), "dry run removes nothing")
        out = wf.purge(self.s, "desk", w)
        self.assertEqual(sorted(out["removed_logs"]), sorted(str(f) for f in mine))
        self.assertFalse(any(f.exists() for f in mine))
        self.assertTrue(all(f.is_file() for f in keep))
        self.assertTrue((self.site_root / f"dureh-{w}-3612221.out").is_symlink() and outside.is_file())
        rec = [e for e in Ledger(self.s).events(w) if e["type"] == "purged"][-1]
        self.assertEqual(sorted(rec["payload"]["removed_logs"]), sorted(str(f) for f in mine))

    def test_purge_removes_old_job_logs_in_dir_named_by_script(self):
        """The site's second shape: <site>/checkpoints/phase-d/dureh-WI-0077-3639768.{out,err}, a site-root
        subdirectory named by the recorded script's #SBATCH -o/-e."""
        w = self._stopped_project_wi()
        script = self.site_root / "projects" / "lna" / "code" / "phase_d.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n#SBATCH -o checkpoints/phase-d/%x-%j.out\n"
                          "#SBATCH --error=checkpoints/phase-d/%x-%j.err\necho hi\n")
        Ledger(self.s).append("run", "slurm", "desk", w, {
            "run_type": "slurm", "status": "submitted", "job_id": "3639768",
            "command": ["sbatch", "--parsable", f"--job-name=dureh-{w}", str(script)], "script": str(script)})
        d = self.site_root / "checkpoints" / "phase-d"
        d.mkdir(parents=True)
        mine = [d / f"dureh-{w}-3639768.out", d / f"dureh-{w}-3639768.err"]
        keep = d / "summary.json"
        for f in mine + [keep]:
            f.write_text("x")
        wf.dispatch_stop(self.s, "desk", w, "test")
        out = wf.purge(self.s, "desk", w)
        self.assertEqual(sorted(out["removed_logs"]), sorted(str(f) for f in mine))
        self.assertTrue(keep.is_file() and d.is_dir())

    def test_purge_removes_recorded_job_logs_inside_the_site_only(self):
        fake = self.tmp / "fakebin"
        fake.mkdir()
        (fake / "sbatch").write_text("#!/bin/sh\necho 4244\n")
        (fake / "sbatch").chmod(0o755)
        cfg = json.loads(self.s.config_path.read_text())
        cfg["env"] = {"path_prepend": [str(fake)]}
        self.s.config_path.write_text(json.dumps(cfg))
        from dureh_core.site import Site
        s = Site(self.site_root)
        w = self._stopped_project_wi(s)
        Ledger(s).event("wi.dispatched", "desk", w, {"role": "researcher"})
        script = self.tmp / "job.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        run = ops.slurm_submit(s, "desk", w, str(script))
        self.assertTrue(run["payload"]["log_out"].endswith("%x-%j.out"))
        logs = self.site_root / "logs" / "slurm" / w
        mine = [logs / f"{s.session_name}-{w}-4244.out", logs / f"{s.session_name}-{w}-4244.err"]
        for f in mine:
            f.write_text("x")
        away = self.tmp / "away"
        away.mkdir()
        own = self.tmp / "own.sh"
        # a record from before release 32 whose script named a log outside the site
        Ledger(s).append("run", "slurm", "desk", w, {
            "run_type": "slurm", "status": "submitted", "job_id": "4244", "script": str(own),
            "command": ["sbatch", "--parsable", f"--job-name=dureh-{w}", str(own)],
            "log_out": f"{away}/%x-%j.out", "log_err": f"{away}/%x-%j.out"})
        (away / f"dureh-{w}-4244.out").write_text("outside the site")
        wf.dispatch_stop(s, "desk", w, "test")
        out = wf.purge(s, "desk", w)
        self.assertEqual(sorted(out["removed_logs"]), sorted(str(f) for f in mine))
        self.assertTrue((away / f"dureh-{w}-4244.out").is_file(), "never outside the site tree")
        self.assertTrue(any(x["reason"] == "outside the site tree" for x in out["skipped_logs"]))

    def test_push_requires_allowlist_and_bound_approval(self):
        repo = self.make_module()
        bare = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
        with self.assertRaisesRegex(DurehError, "allowed_remotes"):
            ops.push(self.s, "desk", str(repo), "origin", "main", "dec-x")
        cfg = json.loads(self.s.config_path.read_text())
        cfg["push"] = {"allowed_remotes": [str(bare)]}
        self.s.config_path.write_text(json.dumps(cfg))
        from dureh_core.site import Site
        s = Site(self.site_root)
        dec = ops.push_approve(s, "desk", str(repo), "origin", "main", "Director approved in test")
        (repo / "src" / "demo.py").write_text("def f():\n    return 3\n")
        subprocess.run(["git", "commit", "-qam", "later change"], cwd=repo, check=True)
        with self.assertRaisesRegex(DurehError, "does not approve"):
            ops.push(s, "desk", str(repo), "origin", "main", dec["id"])
        dec2 = ops.push_approve(s, "desk", str(repo), "origin", "main", "approved again")
        with self.assertRaisesRegex(DurehError, "only the desk"):
            ops.push(s, "py-writer", str(repo), "origin", "main", dec2["id"])
        ev = ops.push(s, "desk", str(repo), "origin", "main", dec2["id"])
        remote_head = subprocess.run(["git", "rev-parse", "main"], cwd=bare, capture_output=True, text=True).stdout.strip()
        self.assertEqual(remote_head, ev["payload"]["revision"])

    def _push_fixture(self, **push_cfg):
        """A module checkout with an allowlisted bare `origin` holding its `main`; config push = allowed + push_cfg."""
        repo = self.make_module()
        bare = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, check=True)
        cfg = json.loads(self.s.config_path.read_text())
        cfg["push"] = {"allowed_remotes": [str(bare)], **push_cfg}
        self.s.config_path.write_text(json.dumps(cfg))
        from dureh_core.site import Site
        return Site(self.site_root), repo, bare

    @staticmethod
    def _commit(repo, text):
        (repo / "src" / "demo.py").write_text(text)
        subprocess.run(["git", "commit", "-qam", "later change"], cwd=repo, check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()

    @staticmethod
    def _remote_tip(bare, branch="main"):
        return subprocess.run(["git", "rev-parse", branch], cwd=bare, capture_output=True, text=True).stdout.strip()

    def test_standing_ff_push_fast_forwards_an_existing_branch(self):
        """Release 58: with push.standing_ff a fast-forward to an existing branch needs no --approval."""
        s, repo, bare = self._push_fixture(standing_ff=True)
        rev = self._commit(repo, "def f():\n    return 2\n")
        ev = ops.push(s, "desk", str(repo), "origin", "main", None)
        self.assertEqual(ev["type"], "push.completed")
        self.assertEqual(self._remote_tip(bare), rev)
        self.assertEqual(ev["payload"]["approval"], "standing-ff")
        self.assertTrue(ev["payload"]["fast_forward"])
        self.assertFalse(ev["payload"]["new_branch"])
        self.assertEqual(ev["payload"]["ref"], "refs/heads/main")
        self.assertNotIn("decision", ev["payload"])
        self.assertEqual(ev["refs"], [])
        self.assertEqual(ops.config_errors(s), [])
        # the CLI accepts the omission
        from dureh_core.cli import main
        import io
        from contextlib import redirect_stdout
        rev2 = self._commit(repo, "def f():\n    return 3\n")
        with redirect_stdout(io.StringIO()):
            code = main(["--site", str(self.site_root), "push", "--repo", str(repo), "--remote", "origin", "--ref", "main"])
        self.assertEqual(code, 0)
        self.assertEqual(self._remote_tip(bare), rev2)

    def test_standing_ff_push_refuses_a_non_fast_forward(self):
        s, repo, bare = self._push_fixture(standing_ff=True)
        remote_tip = self._remote_tip(bare)
        # diverge: amend the pushed commit so the remote tip is no ancestor of HEAD
        subprocess.run(["git", "commit", "-q", "--amend", "-m", "rewritten"], cwd=repo, check=True)
        with self.assertRaisesRegex(DurehError, "not a fast-forward.*push --approve"):
            ops.push(s, "desk", str(repo), "origin", "main", None)
        self.assertEqual(self._remote_tip(bare), remote_tip, "nothing pushed, nothing forced")
        self.assertEqual([e for e in Ledger(s).events() if e["type"].startswith("push.")], [])
        # a dry run reports the same refusal
        with self.assertRaisesRegex(DurehError, "not a fast-forward"):
            ops.push(s, "desk", str(repo), "origin", "main", None, dry_run=True)

    def test_standing_ff_off_still_needs_an_approval(self):
        """Without push.standing_ff (absent or false) a push without --approval is refused, naming the approval path."""
        from dureh_core.site import Site
        s, repo, bare = self._push_fixture()
        self._commit(repo, "def f():\n    return 2\n")
        before = self._remote_tip(bare)
        with self.assertRaisesRegex(DurehError, "--approval DECISION is required .*push.standing_ff is off.*push --approve"):
            ops.push(s, "desk", str(repo), "origin", "main", None)
        cfg = json.loads(s.config_path.read_text())
        cfg["push"]["standing_ff"] = False
        s.config_path.write_text(json.dumps(cfg))
        with self.assertRaisesRegex(DurehError, "push.standing_ff is off"):
            ops.push(Site(self.site_root), "desk", str(repo), "origin", "main", None)
        self.assertEqual(self._remote_tip(bare), before)
        self.assertEqual([e for e in Ledger(s).events() if e["type"].startswith("push.")], [])

    def test_standing_ff_new_branch_needs_its_own_switch(self):
        from dureh_core.site import Site
        s, repo, bare = self._push_fixture(standing_ff=True)
        subprocess.run(["git", "branch", "-q", "feature"], cwd=repo, check=True)
        with self.assertRaisesRegex(DurehError, "new branch.*standing_ff_new_branches is off"):
            ops.push(s, "desk", str(repo), "origin", "feature", None)
        proc = subprocess.run(["git", "rev-parse", "--verify", "-q", "refs/heads/feature"], cwd=bare, capture_output=True)
        self.assertNotEqual(proc.returncode, 0, "feature must not exist on the remote")
        cfg = json.loads(s.config_path.read_text())
        cfg["push"]["standing_ff_new_branches"] = True
        s.config_path.write_text(json.dumps(cfg))
        ev = ops.push(Site(self.site_root), "desk", str(repo), "origin", "feature", None)
        self.assertTrue(ev["payload"]["new_branch"])
        self.assertEqual(ev["payload"]["approval"], "standing-ff")
        self.assertEqual(self._remote_tip(bare, "feature"), ev["payload"]["revision"])

    def test_approval_path_unchanged_with_standing_ff(self):
        """A bound decision still works and still refuses a stale binding, standing_ff or not; non-ff with approval pushes."""
        s, repo, bare = self._push_fixture(standing_ff=True)
        dec = ops.push_approve(s, "desk", str(repo), "origin", "main", "approved")
        self._commit(repo, "def f():\n    return 2\n")
        with self.assertRaisesRegex(DurehError, "does not approve"):
            ops.push(s, "desk", str(repo), "origin", "main", dec["id"])
        dec2 = ops.push_approve(s, "desk", str(repo), "origin", "main", "approved again")
        ev = ops.push(s, "desk", str(repo), "origin", "main", dec2["id"])
        self.assertEqual(ev["payload"]["decision"], dec2["id"])
        self.assertNotIn("fast_forward", ev["payload"])
        self.assertEqual(ev["refs"], [dec2["id"]])
        self.assertEqual(self._remote_tip(bare), ev["payload"]["revision"])

    def test_push_config_shape_is_checked(self):
        s, _repo, _bare = self._push_fixture(standing_ff="yes", standing_ff_new_branches=1)
        errs = ops.config_errors(s)
        self.assertTrue(any("push.standing_ff must be true or false (release 58" in e for e in errs), errs)
        self.assertTrue(any("push.standing_ff_new_branches must be true or false" in e for e in errs), errs)

    def _rule_fixture(self, site_wide=None, **rule):
        """Release 82: `origin` allowlisted as an object entry {url: bare, **rule} (plus site-wide push keys)."""
        from dureh_core.site import Site
        _s, repo, bare = self._push_fixture()
        cfg = json.loads(self.s.config_path.read_text())
        cfg["push"] = {"allowed_remotes": [{"url": str(bare), **rule}], **(site_wide or {})}
        self.s.config_path.write_text(json.dumps(cfg))
        return Site(self.site_root), repo, bare

    def test_object_rule_refuses_a_branch_outside_its_list_even_with_a_decision(self):
        s, repo, bare = self._rule_fixture(branches=["golden", "integrate/*"], standing_ff=True)
        before = self._remote_tip(bare)
        self._commit(repo, "def f():\n    return 2\n")
        with self.assertRaisesRegex(DurehError, "branch main is outside config push.allowed_remotes rule for .*"
                                                "pull request the Director merges, or a config change"):
            ops.push(s, "desk", str(repo), "origin", "main", None)
        with self.assertRaisesRegex(DurehError, "branch main is outside.*nothing was recorded"):
            ops.push_approve(s, "desk", str(repo), "origin", "main", "approved")
        # a decision recorded before the limit existed does not open the branch either
        cfg = json.loads(s.config_path.read_text())
        rules = cfg["push"]["allowed_remotes"]
        cfg["push"]["allowed_remotes"] = [str(bare)]
        s.config_path.write_text(json.dumps(cfg))
        from dureh_core.site import Site
        dec = ops.push_approve(Site(self.site_root), "desk", str(repo), "origin", "main", "approved")
        cfg["push"]["allowed_remotes"] = rules
        s.config_path.write_text(json.dumps(cfg))
        with self.assertRaisesRegex(DurehError, "even with a Director decision"):
            ops.push(Site(self.site_root), "desk", str(repo), "origin", "main", dec["id"])
        self.assertEqual(self._remote_tip(bare), before)
        self.assertEqual([e for e in Ledger(s).events() if e["type"].startswith("push.")], [])

    def test_object_rule_standing_ff_inside_branches_and_records_the_rule(self):
        s, repo, bare = self._rule_fixture(branches=["main"], standing_ff=True)
        rev = self._commit(repo, "def f():\n    return 2\n")
        ev = ops.push(s, "desk", str(repo), "origin", "main", None)
        self.assertEqual(self._remote_tip(bare), rev)
        self.assertEqual(ev["payload"]["approval"], "standing-ff")
        self.assertEqual(ev["payload"]["rule"], {"url": str(bare), "form": "object", "branches": ["main"],
                                                 "standing_ff": True, "new_branches": [], "tags": False})
        self.assertEqual(ops.config_errors(s), [])
        st = ops.status(s)
        self.assertEqual(st["push"], [f"{bare}: branches main; standing fast-forward on; new branches none; tags refused"])
        self.assertIn(f"Push: {bare}: branches main", ops.render_status(st))

    def test_object_rule_standing_ff_defaults_off_whatever_the_site_says(self):
        s, repo, bare = self._rule_fixture(site_wide={"standing_ff": True, "standing_ff_new_branches": True})
        self._commit(repo, "def f():\n    return 2\n")
        with self.assertRaisesRegex(DurehError, "--approval DECISION is required .*rule for .* has standing_ff off"):
            ops.push(s, "desk", str(repo), "origin", "main", None)
        dec = ops.push_approve(s, "desk", str(repo), "origin", "main", "approved")
        ev = ops.push(s, "desk", str(repo), "origin", "main", dec["id"])  # no branches: every branch with a decision
        self.assertEqual(ev["payload"]["rule"]["branches"], None)
        self.assertEqual(self._remote_tip(bare), ev["payload"]["revision"])

    def test_object_rule_new_branches(self):
        s, repo, bare = self._rule_fixture(branches=["main", "integrate/*", "feature"], standing_ff=True,
                                           new_branches=["integrate/*"])
        subprocess.run(["git", "branch", "-q", "feature"], cwd=repo, check=True)
        subprocess.run(["git", "branch", "-q", "integrate/a"], cwd=repo, check=True)
        with self.assertRaisesRegex(DurehError, "new branch.*has new_branches integrate/\\*"):
            ops.push(s, "desk", str(repo), "origin", "feature", None)
        ev = ops.push(s, "desk", str(repo), "origin", "integrate/a", None)
        self.assertTrue(ev["payload"]["new_branch"])
        self.assertEqual(self._remote_tip(bare, "integrate/a"), ev["payload"]["revision"])
        # a decision creates a listed branch the standing push may not
        dec = ops.push_approve(s, "desk", str(repo), "origin", "feature", "approved")
        ops.push(s, "desk", str(repo), "origin", "feature", dec["id"])
        self.assertEqual(self._remote_tip(bare, "feature"), ev["payload"]["revision"])

    def test_object_rule_tags(self):
        s, repo, bare = self._rule_fixture(standing_ff=True)
        subprocess.run(["git", "tag", "v1"], cwd=repo, check=True)
        with self.assertRaisesRegex(DurehError, "refs/tags/v1 is a tag and .* has tags false"):
            ops.push(s, "desk", str(repo), "origin", "refs/tags/v1", None)
        with self.assertRaisesRegex(DurehError, "tags false"):
            ops.push_approve(s, "desk", str(repo), "origin", "refs/tags/v1", "approved")
        cfg = json.loads(s.config_path.read_text())
        cfg["push"]["allowed_remotes"][0]["tags"] = True
        s.config_path.write_text(json.dumps(cfg))
        from dureh_core.site import Site
        s = Site(self.site_root)
        with self.assertRaisesRegex(DurehError, "a tag push needs a Director decision"):
            ops.push(s, "desk", str(repo), "origin", "refs/tags/v1", None)
        dec = ops.push_approve(s, "desk", str(repo), "origin", "refs/tags/v1", "approved")
        ev = ops.push(s, "desk", str(repo), "origin", "refs/tags/v1", dec["id"])
        self.assertEqual(ev["payload"]["ref"], "refs/tags/v1")
        self.assertEqual(self._remote_tip(bare, "v1"), ev["payload"]["revision"])

    def test_string_entries_keep_their_meaning_next_to_object_entries(self):
        s, repo, bare = self._push_fixture(standing_ff=True)
        cfg = json.loads(s.config_path.read_text())
        cfg["push"]["allowed_remotes"] = [{"url": "git@example.org:other.git", "branches": ["x"]}, str(bare)]
        s.config_path.write_text(json.dumps(cfg))
        from dureh_core.site import Site
        s = Site(self.site_root)
        rev = self._commit(repo, "def f():\n    return 2\n")
        ev = ops.push(s, "desk", str(repo), "origin", "main", None)
        self.assertEqual(self._remote_tip(bare), rev)
        self.assertEqual(ev["payload"]["rule"]["form"], "string")
        self.assertIsNone(ev["payload"]["rule"]["branches"])
        self.assertIn(f"{bare}: all branches; standing fast-forward on (site-wide)", ops.status(s)["push"])

    def test_push_object_entry_shape_is_checked(self):
        s, _repo, _bare = self._push_fixture()
        cfg = json.loads(s.config_path.read_text())
        cfg["push"]["allowed_remotes"] = [{"branches": "main"}, {"url": "u", "standing_ff": "yes", "tags": 1,
                                                                 "new_branches": [3], "deny": []}, 7]
        s.config_path.write_text(json.dumps(cfg))
        from dureh_core.site import Site
        errs = ops.config_errors(Site(self.site_root))
        for want in ("allowed_remotes[0].url is required", "allowed_remotes[0].branches must be a list",
                     "allowed_remotes[1].standing_ff must be true or false", "allowed_remotes[1].tags must be true",
                     "allowed_remotes[1].new_branches must be a list", "allowed_remotes[1] has unknown key(s) deny",
                     "allowed_remotes[2] must be a remote URL string or an object"):
            self.assertTrue(any(want in e for e in errs), (want, errs))
        cfg["push"]["allowed_remotes"] = "u"
        s.config_path.write_text(json.dumps(cfg))
        self.assertTrue(any("allowed_remotes must be a list" in e for e in ops.config_errors(Site(self.site_root))))

    def test_slurm_submit_records_run_with_fake_scheduler(self):
        fake = self.tmp / "fakebin"
        fake.mkdir()
        (fake / "sbatch").write_text("#!/bin/sh\necho \"$@\" > \"$(dirname \"$0\")/args\"\necho 4242\n")
        (fake / "sbatch").chmod(0o755)
        cfg = json.loads(self.s.config_path.read_text())
        cfg["env"] = {"path_prepend": [str(fake)]}
        self.s.config_path.write_text(json.dumps(cfg))
        from dureh_core.site import Site
        s = Site(self.site_root)
        w = wf.wi_new(s, "desk", "run", "g", "researcher", "site", ["a"])["id"]
        script = self.tmp / "job.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        with self.assertRaisesRegex(DurehError, "may not submit scheduler jobs: that needs the submit_jobs capability"):
            ops.slurm_submit(s, "py-writer", w, str(script))
        with self.assertRaisesRegex(DurehError, "not dispatched"):
            ops.slurm_submit(s, "desk", w, str(script))
        wf.wi_approve(s, "desk", w, "ok")
        Ledger(s).event("wi.dispatched", "desk", w, {"role": "researcher"})
        with self.assertRaisesRegex(DurehError, "missing"):
            ops.slurm_submit(s, "desk", w, str(self.tmp / "nope.sh"))
        run = ops.slurm_submit(s, "desk", w, str(script))
        self.assertEqual(run["payload"]["job_id"], "4242")
        self.assertIn("--account=acct-x", (fake / "args").read_text())
        dry = ops.slurm_submit(s, "desk", w, str(script), dry_run=True)
        self.assertEqual(dry["payload"]["status"], "not-run")
        # logs go to the site, not into the submitting (project) directory
        args = (fake / "args").read_text()
        self.assertIn(f"--output={self.site_root}/logs/slurm/{w}/%x-%j.out", args)
        self.assertIn(f"--error={self.site_root}/logs/slurm/{w}/%x-%j.err", args)
        self.assertTrue((self.site_root / "logs" / "slurm" / w).is_dir())
        own = self.tmp / "own.sh"
        own.write_text("#!/bin/sh\n#SBATCH --output=/tmp/x.out\necho hi\n")
        logs = f"{self.site_root}/logs/slurm/{w}"
        p = ops.slurm_submit(s, "desk", w, str(own), dry_run=True)["payload"]
        self.assertEqual((p["logs"], p["log_out"]), (logs, f"{logs}/x.out"))
        p = ops.slurm_submit(s, "desk", w, str(script), dry_run=True, extra=["-o", "y.out"])["payload"]
        self.assertEqual(p["log_out"], f"{logs}/y.out")
        self.assertEqual(p["command"][-2:], [f"--output={logs}/y.out", str(script)])

    def test_slurm_wait_records_only_the_final_state(self):
        fake = self.tmp / "fakebin"
        fake.mkdir()
        counter = self.tmp / "n"
        (fake / "sbatch").write_text("#!/bin/sh\necho 4243\n")
        (fake / "sacct").write_text(f"#!/bin/sh\nn=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}\n"
                                    "if [ $n -lt 3 ]; then echo 'RUNNING|0:0|00:01'; else echo 'COMPLETED|0:0|00:02'; fi\n")
        for f in ("sbatch", "sacct"):
            (fake / f).chmod(0o755)
        cfg = json.loads(self.s.config_path.read_text())
        cfg["env"] = {"path_prepend": [str(fake)]}
        self.s.config_path.write_text(json.dumps(cfg))
        from dureh_core.site import Site
        s = Site(self.site_root)
        w = wf.wi_new(s, "desk", "run", "g", "researcher", "site", ["a"])["id"]
        wf.wi_approve(s, "desk", w, "ok")
        Ledger(s).event("wi.dispatched", "desk", w, {"role": "researcher"})
        script = self.tmp / "job.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        run = ops.slurm_submit(s, "desk", w, str(script))
        ev = ops.slurm_wait(s, "desk", run["id"], timeout=30, interval=1)
        self.assertEqual(ev["payload"]["scheduler_state"], "COMPLETED")
        self.assertTrue(ev["payload"]["final"])
        states = [e for e in Ledger(s).events(w) if e["type"] == "run.status"]
        self.assertEqual(len(states), 1, "only the final state is recorded")

    def test_wi_show_several_with_summary(self):
        import contextlib
        import io
        from dureh_core import cli
        a = wf.wi_new(self.s, "desk", "A", "g", "researcher", "site", ["a"])["id"]
        b = wf.wi_new(self.s, "desk", "B", "g", "researcher", "site", ["a"])["id"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["--site", str(self.site_root), "wi", "show", "--wi", a, "--wi", b, "--summary"]), 0)
        out = json.loads(buf.getvalue())
        self.assertEqual([o["wi"] for o in out], [a, b])
        self.assertIn("last_events", out[0])

    def test_usage_reports_undefined_without_promotions(self):
        t = self.tmp / "t.jsonl"
        t.write_text(json.dumps({"message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5}}}) + "\n" +
                     json.dumps({"message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5}}}) + "\n")
        u = ops.transcript_usage(t)
        self.assertEqual((u["input_tokens"], u["output_tokens"], u["messages"]), (10, 5, 1))
        Ledger(self.s).append("run", "agent", "desk", "WI-0001", {"run_type": "agent", "status": "completed", "usage": u})
        r = ops.usage(self.s)
        self.assertEqual(r["tokens_total"], 15)
        self.assertTrue(str(r["tokens_per_promoted_wi"]).startswith("undefined"))

    def test_exam_scoring(self):
        from dureh_core.exams import tasks

        task = [t for t in tasks("py-writer") if t.name == "pyw-001-missing-input"][0]
        ws = self.tmp / "ws"
        copy_writable(task / "fixture", ws)
        from dureh_core.exams import score
        r = score(self.s, "desk", "py-writer", str(ws), None)
        self.assertEqual(r["result"], "fail", "unfixed fixture must fail the oracle")
        (ws / "src/demo/__init__.py").write_text(
            "import json\nimport os\n\n\ndef load_manifest(path):\n    if not os.path.exists(path):\n"
            "        raise FileNotFoundError(f'manifest is missing: {path}')\n"
            "    with open(path, encoding='utf-8') as stream:\n        return json.load(stream)\n")
        (ws / "tests/test_missing.py").write_text(
            "import os, sys, tempfile, unittest\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
            "import demo\n\nclass M(unittest.TestCase):\n    def test_missing(self):\n"
            "        with self.assertRaises(FileNotFoundError):\n            demo.load_manifest(os.path.join(tempfile.mkdtemp(), 'x.json'))\n")
        r = score(self.s, "desk", "py-writer", str(ws), None)
        self.assertEqual(r["result"], "pass", r)
        (ws / "data").mkdir()
        (ws / "data" / "x.dat").write_text("forbidden")
        r = score(self.s, "desk", "py-writer", str(ws), None)
        self.assertTrue(r["hard_failure"])

    def test_adapter_drift_and_tool_coverage(self):
        from dureh_core.runtimes.claude_code import drift, generate
        from dureh_core.selftest import tool_coverage

        self.assertEqual(drift(self.s), [])
        self.assertEqual(tool_coverage(self.s), [])
        agent = self.site_root / ".claude" / "agents" / "py-writer.md"
        agent.write_text(agent.read_text() + "\nextra hand edit\n")
        self.assertTrue(any("edited by hand" in d for d in drift(self.s)))
        generate(self.s)
        self.assertEqual(drift(self.s), [])
        settings = json.loads((self.site_root / ".claude" / "settings.json").read_text())
        self.assertIn("PreToolUse", settings["hooks"])
        self.assertFalse(settings["autoMemoryEnabled"])
        front = agent.read_text().split("---")[1]
        self.assertIn("model: claude-sonnet-5", front)
        self.assertIn("name: py-writer", front)


class SiteLinks(SiteCase):
    def test_link_module_pins_revision(self):
        self.install()
        s = self.site()
        src = self.tmp / "upstream"
        (src / "src").mkdir(parents=True)
        (src / "src" / "m.py").write_text("x = 1\n")
        subprocess.run(["git", "init", "-q", "-b", "dev", str(src)], check=True)
        subprocess.run(["git", "add", "-A"], cwd=src, check=True)
        subprocess.run(["git", "commit", "-qm", "one"], cwd=src, check=True)
        first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True).stdout.strip()
        (src / "src" / "m.py").write_text("x = 2\n")
        subprocess.run(["git", "commit", "-qam", "two"], cwd=src, check=True)
        from dureh_core.sitelinks import link_module
        e = link_module(s, "desk", "demo", str(src), first)
        self.assertEqual(e["rev"], first)
        self.assertEqual((self.site_root / "modules/demo/src/m.py").read_text(), "x = 1\n")
        with self.assertRaisesRegex(DurehError, "only the desk"):
            link_module(s, "py-writer", "demo2", str(src), first)
        with self.assertRaisesRegex(DurehError, "already linked"):
            link_module(s, "desk", "demo", str(src), first)
        cfg = json.loads(s.config_path.read_text())
        self.assertEqual(cfg["modules"][0]["id"], "demo")


class YamlSubset(unittest.TestCase):
    def test_matches_pyyaml_on_harness_files(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed; subset parser checked by the other tests only")
        from dureh_core import yamlsub

        files = list((HARNESS / "org").rglob("*.yaml")) + list((HARNESS / "tests" / "fixtures").rglob("*.yaml"))
        self.assertTrue(files)
        for f in files:
            with self.subTest(f=f.name):
                self.assertEqual(yamlsub.load_file(f), yaml.safe_load(f.read_text()))

    def test_rejects_unsupported(self):
        from dureh_core import yamlsub

        for bad in ("a: {b: {c: 1}}", "a:\n\tb: 1", "a: 1\na: 2", "- a\nb: 1"):
            with self.subTest(bad=bad):
                with self.assertRaises(DurehError):
                    yamlsub.loads(bad)


if __name__ == "__main__":
    unittest.main()
