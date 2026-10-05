"""Site folder-name independence (proposals/site-rename-independence.md, step 2): with `upstream.mailbox` and
`session.name` pinned, renaming the site folder changes neither the mailbox, the message header, the session job
name, the pane log name nor the Remote Control name; without the keys both default to the folder name."""
import json
import os
import shutil

from helpers import SiteCase

from dureh_core import sessions
from dureh_core import upstream as up
from dureh_core.site import Site


class Rename(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.x = self.tmp / "exchange"
        self.x.mkdir()
        os.environ["DUREH_TEST_EXCHANGE"] = str(self.x)
        self.old = self.site_root.name

    def configure(self, root, **upstream_extra):
        cfg = json.loads((root / "config.json").read_text())
        cfg["upstream"] = {"exchange": "${DUREH_TEST_EXCHANGE}", **upstream_extra}
        (root / "config.json").write_text(json.dumps(cfg, indent=2))

    def pin(self, root, mailbox, session_name):
        self.configure(root, mailbox=mailbox)
        cfg = json.loads((root / "config.json").read_text())
        cfg["session"] = {**(cfg.get("session") or {}), "name": session_name}
        (root / "config.json").write_text(json.dumps(cfg, indent=2))

    def renamed(self):
        new = self.tmp / "renamed-company"
        shutil.copytree(self.site_root, new, symlinks=True)
        for f in (new / "notes" / "upstream").glob("FORWARD-*"):  # a send within the same second would collide
            f.unlink()
        return new

    def names(self, s: Site) -> dict:
        body = self.tmp / f"reply-{s.root.name}.md"
        body.write_text("hello\n")
        sent = up.send(s, "desk", "reply", str(body))
        script = sessions.plan(s)
        line = lambda key: next(l for l in script.splitlines() if l.startswith(key))
        return {"mailbox": up.exchange_dir(s), "sent_dir": os.path.dirname(os.path.dirname(sent["file"])),
                "header": next(l for l in open(sent["file"]).read().splitlines() if l.startswith("site: ")),
                "report": up.message(s).splitlines()[0].split(" at ")[-1].split(" (")[0],
                "collab": up.collaboration(s)["exchange"],
                "job": line("#SBATCH --job-name="), "pane_name": line("NAME="), "pane": line("PANE="),
                "rc": line("CMD=")}

    def test_pinned_names_survive_a_folder_rename(self):
        self.pin(self.site_root, self.old, self.old)
        before = self.names(Site(self.site_root))
        new = self.renamed()
        after = self.names(Site(new))
        self.assertEqual(after["mailbox"], self.x / self.old)
        self.assertEqual(after["mailbox"], before["mailbox"])
        self.assertEqual(after["sent_dir"], str(self.x / self.old))
        self.assertEqual(after["header"], f"site: {self.old}")
        self.assertEqual(after["report"], self.old)
        self.assertEqual(after["collab"], str(self.x / self.old))
        self.assertEqual(after["job"], f"#SBATCH --job-name=cc-{self.old}")
        for key in ("job", "pane_name", "rc", "header", "report", "collab"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["pane"], before["pane"].replace(str(self.site_root), str(new)))  # only the folder moves
        self.assertIn(f"--remote-control {self.old} ", after["rc"])

    def test_session_and_mailbox_names_may_differ_from_the_folder(self):
        self.pin(self.site_root, "UNC_Lab_MAILBOX", "short")
        n = self.names(Site(self.site_root))
        self.assertEqual(n["mailbox"], self.x / "UNC_Lab_MAILBOX")
        self.assertEqual(n["header"], "site: UNC_Lab_MAILBOX")
        self.assertEqual(n["job"], "#SBATCH --job-name=cc-short")
        self.assertEqual(n["pane_name"], "NAME=cc-short-\"${SLURM_JOB_ID:-local}\"")
        self.assertIn("--remote-control short ", n["rc"])
        self.assertIn("#SBATCH --job-name=cc-short-overtime", sessions.plan(Site(self.site_root), "overtime"))

    def test_defaults_follow_the_folder_name_without_the_keys(self):
        self.configure(self.site_root)
        new = self.renamed()
        n = self.names(Site(new))
        self.assertEqual(n["mailbox"], self.x / new.name)
        self.assertEqual(n["header"], f"site: {new.name}")
        self.assertEqual(n["report"], new.name)
        self.assertEqual(n["job"], f"#SBATCH --job-name=cc-{new.name}")
        self.assertIn(f"--remote-control {new.name} ", n["rc"])
