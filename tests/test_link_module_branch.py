"""Release 65 (UR-20261015-04): `site link-module --branch` and `--relink` (a linked module's rev and branch change
in place, refused for a dirty or unpushed checkout unless the module is push-off and --force is given)."""
import json
import subprocess
import unittest

from helpers import SiteCase

from dureh_core import sitelinks
from dureh_core.records import Ledger
from dureh_core.util import DurehError


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class LinkModuleBranch(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.src = self.tmp / "sources" / "demo"
        self.src.mkdir(parents=True)
        (self.src / "demo.py").write_text("x = 1\n")
        git(self.src, "init", "-q", "-b", "main")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "init")
        self.main1 = git(self.src, "rev-parse", "HEAD")
        git(self.src, "checkout", "-q", "-b", "feat/layer")
        for i in (2, 3):
            (self.src / "demo.py").write_text(f"x = {i}\n")
            git(self.src, "commit", "-qam", f"feat {i}")
        self.feat = git(self.src, "rev-parse", "HEAD")
        self.feat_first = git(self.src, "rev-parse", "HEAD~1")
        git(self.src, "checkout", "-q", "main")
        self.mod = self.site_root / "modules" / "demo"
        self.s = self.site()

    def entry(self):
        cfg = json.loads((self.site_root / "config.json").read_text())
        return next(e for e in cfg["modules"] if e["id"] == "demo")

    def events(self, kind):
        return [e for e in Ledger(self.site()).events() if e["type"] == kind]

    def test_branch_is_recorded_and_tracks_origin(self):
        out = sitelinks.link_module(self.s, "desk", "demo", str(self.src), self.feat_first, branch="feat/layer")
        self.assertEqual((out["branch"], out["rev"]), ("feat/layer", self.feat_first))
        self.assertEqual(self.entry()["branch"], "feat/layer")
        self.assertEqual(git(self.mod, "symbolic-ref", "--short", "HEAD"), "feat/layer")
        self.assertEqual(git(self.mod, "rev-parse", "HEAD"), self.feat_first)
        self.assertEqual(git(self.mod, "rev-parse", "--abbrev-ref", "feat/layer@{upstream}"), "origin/feat/layer")
        self.assertEqual(git(self.mod, "rev-parse", "main"), self.main1, "local main is not moved")
        ev = self.events("site.module-linked")[-1]["payload"]
        self.assertEqual((ev["branch"], ev["tracking"], ev["rev_on_branch"]), ("feat/layer", "origin/feat/layer", True))

    def test_branch_must_exist_at_the_source(self):
        with self.assertRaisesRegex(DurehError, "has no branch nope"):
            sitelinks.link_module(self.s, "desk", "demo", str(self.src), self.main1, branch="nope")
        self.assertFalse(self.mod.exists())
        with self.assertRaisesRegex(DurehError, "invalid branch name"):
            sitelinks.link_module(self.s, "desk", "demo", str(self.src), self.main1, branch="-x")

    def test_without_branch_it_is_main_as_before(self):
        out = sitelinks.link_module(self.s, "desk", "demo", str(self.src), self.main1)
        self.assertEqual(out["branch"], "main")
        with self.assertRaisesRegex(DurehError, "already linked.*--relink"):
            sitelinks.link_module(self.s, "desk", "demo", str(self.src), self.main1)

    def test_relink_moves_rev_and_branch_in_place(self):
        sitelinks.link_module(self.s, "desk", "demo", str(self.src), self.main1)
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["modules"][0]["env_rebuild"] = "true"
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        out = sitelinks.link_module(self.site(), "desk", "demo", None, self.feat, branch="feat/layer", relink=True)
        self.assertEqual(git(self.mod, "symbolic-ref", "--short", "HEAD"), "feat/layer")
        self.assertEqual(git(self.mod, "rev-parse", "HEAD"), self.feat)
        self.assertEqual(git(self.mod, "rev-parse", "main"), self.main1, "the previous branch is kept")
        e = self.entry()
        self.assertEqual((e["rev"], e["branch"], e["env_rebuild"]), (self.feat, "feat/layer", "true"))
        ev = self.events("site.module-relinked")[-1]["payload"]
        self.assertEqual((ev["from"]["rev"], ev["from"]["branch"], ev["to"]["rev"], ev["to"]["branch"]),
                         (self.main1, "main", self.feat, "feat/layer"))
        self.assertFalse(ev["forced"])
        self.assertEqual(ev["previous_branch_kept"], {"branch": "main", "head": self.main1})
        self.assertGreater(out["generated"], 0)
        # a rev change alone keeps the recorded branch
        sitelinks.link_module(self.site(), "desk", "demo", None, self.feat_first, relink=True)
        self.assertEqual((self.entry()["rev"], self.entry()["branch"]), (self.feat_first, "feat/layer"))
        self.assertEqual(git(self.mod, "rev-parse", "HEAD"), self.feat_first)

    def test_relink_refuses_dirty_and_unpushed_unless_push_off_and_force(self):
        sitelinks.link_module(self.s, "desk", "demo", str(self.src), self.main1)
        with self.assertRaisesRegex(DurehError, "not a registered module"):
            sitelinks.link_module(self.site(), "desk", "other", None, self.feat, relink=True)
        with self.assertRaisesRegex(DurehError, "keeps the source"):
            sitelinks.link_module(self.site(), "desk", "demo", "/elsewhere", self.feat, relink=True)
        (self.mod / "demo.py").write_text("x = 99\n")
        # push-off (the source is not in push.allowed_remotes) but no --force: refused
        with self.assertRaisesRegex(DurehError, "uncommitted changes.*pass --force"):
            sitelinks.link_module(self.site(), "desk", "demo", None, self.feat, relink=True)
        git(self.mod, "checkout", "--", "demo.py")
        (self.mod / "local.py").write_text("y = 1\n")
        git(self.mod, "add", "local.py")
        git(self.mod, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "local only")
        local = git(self.mod, "rev-parse", "HEAD")
        # a module that can be pushed: --force does not help
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg.setdefault("push", {})["allowed_remotes"] = [str(self.src)]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        with self.assertRaisesRegex(DurehError, "unpushed local commits.*only to a push-off module"):
            sitelinks.link_module(self.site(), "desk", "demo", None, self.feat, relink=True, force=True)
        # release 82: an object entry counts by its url
        cfg["push"]["allowed_remotes"] = [{"url": str(self.src), "branches": ["main"]}]
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        with self.assertRaisesRegex(DurehError, "unpushed local commits.*only to a push-off module"):
            sitelinks.link_module(self.site(), "desk", "demo", None, self.feat, relink=True, force=True)
        self.assertEqual(git(self.mod, "rev-parse", "HEAD"), local, "nothing changed")
        self.assertEqual(self.events("site.module-relinked"), [])
        # configured push-off (entry push: false) with --force: relinked, recorded as forced
        cfg["modules"][0]["push"] = False
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        out = sitelinks.link_module(self.site(), "desk", "demo", None, self.feat, branch="feat/layer", relink=True,
                                    force=True)
        self.assertTrue(out["forced"])
        self.assertTrue(out["push_off"])
        self.assertIn("unpushed", out["problems"][0])
        self.assertEqual(git(self.mod, "rev-parse", "HEAD"), self.feat)
        self.assertEqual(git(self.mod, "rev-parse", "main"), local, "the local commit stays on main")
        self.assertIs(self.entry()["push"], False)

    def test_relink_of_a_head_on_another_source_branch_is_not_unpushed(self):
        # the reported case: local main was moved to a feature commit; that commit is on origin/feat/layer
        sitelinks.link_module(self.s, "desk", "demo", str(self.src), self.feat_first)
        self.assertEqual(self.entry()["branch"], "main")
        out = sitelinks.link_module(self.site(), "desk", "demo", None, self.feat, branch="feat/layer", relink=True)
        self.assertFalse(out["forced"])
        self.assertIn("local branch main is kept", out["note"])

    def test_retire_compensation_names_the_branch(self):
        sitelinks.link_module(self.s, "desk", "demo", str(self.src), self.feat, branch="feat/layer")
        out = sitelinks.retire_module(self.site(), "desk", "demo", dry_run=True)
        self.assertTrue(out["compensation"].endswith("--branch feat/layer"), out["compensation"])


if __name__ == "__main__":
    unittest.main()
