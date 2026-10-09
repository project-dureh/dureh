"""Release 46 removed `dureh container build|status|retire` (proposals/boundary-design-2026-10-08.md 8.1); the container
cache stays a write-guard zone: its location is config (`containers.cache`, else the env var named by
`containers.cache_env`), no role writes it, and container runtimes run only as scheduler jobs."""
import contextlib
import io
import json
import os

from dureh_core import containers
from dureh_core.cli import main
from dureh_core.paths import write_decision
from tests.helpers import ExampleSiteCase, denied, hook


class ContainerCache(ExampleSiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["env"] = {"vars": {"EXAMPLE_IMAGE_CACHE": "$DUREH_SITE/cache/containers"}}
        cfg["containers"] = {"cache_env": "EXAMPLE_IMAGE_CACHE"}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.cache = self.site_root / "cache" / "containers"
        self.cache.mkdir(parents=True)
        self.s = self.site()

    def test_location_comes_from_config(self):
        self.assertEqual(containers.cache_dir(self.s), self.cache)
        cfg = json.loads((self.site_root / "config.json").read_text())
        cfg["containers"]["cache"] = "images"  # the generic key wins over the env var
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(containers.cache_dir(self.site()), self.site_root / "images")
        cfg["containers"] = {"cache_env": None}
        cfg["env"] = {}
        (self.site_root / "config.json").write_text(json.dumps(cfg))
        os.environ.pop("EXAMPLE_IMAGE_CACHE", None)
        self.assertIsNone(containers.cache_dir(self.site()))

    def test_cache_is_written_by_no_role(self):
        target = self.cache / "x.sif"
        for role in self.s.org.roles:
            ok, why = write_decision(self.s, role, target)
            self.assertFalse(ok, role)
            self.assertIn("container cache", why)

    def test_bash_guard(self):
        def bash(command, role):
            data = dict(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": command},
                        cwd=str(self.site_root))
            if role != "desk":
                data.update(agent_type=role, agent_id="a1")
            return hook(self.s, **data)

        for role in ("runner", "desk"):
            for raw in ("apptainer build --fakeroot x.sif x.def", "singularity build x.sif x.def",
                        f"cp img.sif {self.cache}/x.sif", "echo x > cache/containers/a.sif"):
                self.assertTrue(denied(bash(raw, role)), (role, raw))

    def test_container_commands_are_gone(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            main(["--site", str(self.site_root), "container", "build", "--wi", "WI-0001"])
        self.assertIn("invalid choice", err.getvalue())
        text = (self.site_root / "CLAUDE.md").read_text() + "".join(
            p.read_text() for p in (self.site_root / ".claude" / "agents").glob("*.md"))
        self.assertNotIn("container build", text)
        self.assertNotIn("container retire", text)
