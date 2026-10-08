"""Release 44: the persona's conversation language reaches Claude Code as `language` in the generated settings.json;
generated documents stay English."""
import json

from helpers import SiteCase

from dureh_core.runtimes import claude_code


class SettingsLanguage(SiteCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.s = self.site()
        self.persona = self.s.state / "persona" / "persona.json"

    def settings(self):
        claude_code.generate(self.s)
        return json.loads((self.site_root / ".claude" / "settings.json").read_text())

    def set_persona(self, **values):
        self.persona.parent.mkdir(parents=True, exist_ok=True)
        cur = json.loads(self.persona.read_text()) if self.persona.is_file() else {}
        self.persona.write_text(json.dumps({**cur, **values}))

    def test_no_language_key_without_a_recorded_language(self):
        self.set_persona(language="")
        self.assertNotIn("language", self.settings())

    def test_persona_language_is_written(self):
        self.set_persona(language="Korean")
        settings = self.settings()
        self.assertEqual(settings["language"], "Korean")
        md = (self.site_root / "CLAUDE.md").read_text()
        self.assertIn("- language: Korean.", md)
        self.assertIn(claude_code.ENGLISH_RULE, md, "documents stay English")
