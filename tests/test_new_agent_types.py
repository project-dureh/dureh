"""Release 86 (dureh-team#91 item 7): `adapter sync` names agent types it adds, because Claude Code reads agent files
when a session starts (10-29: a new planner role was briefed but could not be spawned in the running session)."""
import contextlib
import io

from helpers import SiteCase

from dureh_core.cli import main


class NewAgentTypes(SiteCase):
    def sync(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["--site", str(self.site_root), "adapter", "sync"]), 0)
        return out.getvalue()

    def test_sync_names_added_agent_types_only(self):
        self.install()
        self.assertNotIn("new agent type", self.sync())
        agents = sorted((self.site_root / ".claude" / "agents").glob("*.md"))
        gone = agents[0]
        gone.unlink()  # as if the package had just added this role
        text = self.sync()
        self.assertIn(f"new agent type(s): {gone.stem}; a running desk session cannot spawn them", text)
        self.assertNotIn("new agent type", self.sync())
