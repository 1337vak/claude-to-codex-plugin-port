"""Regressions found by using the documented skill from an installed package."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from plugin_port import core, scaffold


SOURCE_SHA = "1" * 40
TOOL_SHA = "2" * 40


class InstalledSkillInitializationTests(unittest.TestCase):
    def initialize_installed(self, folder, repository, *, name=None, enclosing_checkout=False):
        installed = Path(folder) / "installed-plugin"
        adapter = Path(folder) / "adapter"

        def fake_run(arguments, **kwargs):
            args = list(map(str, arguments))
            if args == ["git", "rev-parse", "--show-toplevel"]:
                if enclosing_checkout:
                    return str(installed.parent) + "\n"
                raise core.PortError("Installed plugins do not contain .git")
            if args == ["git", "rev-parse", "HEAD"]:
                self.fail("An installed package must not pin an enclosing checkout's HEAD")
            if "ls-remote" in args and scaffold.TOOL_REPOSITORY in args:
                selector = next((arg for arg in reversed(args) if arg.startswith("refs/tags/")), "refs/tags/v0.1.0")
                return TOOL_SHA + "\t" + selector + "\n"
            self.fail(f"Unexpected external operation: {args}")

        with patch.object(scaffold, "__file__", str(installed / "plugin_port/scaffold.py")), \
                patch.object(scaffold, "run", side_effect=fake_run), \
                patch.object(core, "run", side_effect=fake_run):
            scaffold.initialize(repository, adapter, name=name, ref=SOURCE_SHA)
        return json.loads((adapter / "port.json").read_text()), adapter

    def test_installed_skill_pins_released_tool_without_git_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            config, adapter = self.initialize_installed(folder, "owner/claude-tiny")
            self.assertEqual(config["source"]["commit"], SOURCE_SHA)
            self.assertEqual(config["tool"]["commit"], TOOL_SHA)
            self.assertEqual(config["tool"]["repository"], scaffold.TOOL_REPOSITORY)
            self.assertTrue((adapter / "scripts/port.py").is_file())
            self.assertTrue((adapter / ".github/workflows/publish.yml").is_file())

    def test_installed_skill_does_not_pin_a_parent_repository(self):
        with tempfile.TemporaryDirectory() as folder:
            config, _ = self.initialize_installed(folder, "owner/claude-tiny", enclosing_checkout=True)
            self.assertEqual(config["tool"]["commit"], TOOL_SHA)

    def test_claude_code_prefix_has_concise_codex_name(self):
        with tempfile.TemporaryDirectory() as folder:
            config, _ = self.initialize_installed(folder, "owner/claude-code-game-studio")
            self.assertEqual(config["name"], "codex-game-studio")
            self.assertEqual(config["source"]["repository"], "https://github.com/owner/claude-code-game-studio.git")

    def test_explicit_plugin_name_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            config, _ = self.initialize_installed(folder, "owner/claude-tiny", name="codex-custom")
            self.assertEqual(config["name"], "codex-custom")


if __name__ == "__main__":
    unittest.main()
