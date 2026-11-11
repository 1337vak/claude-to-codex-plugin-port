"""Required native installation tests; a missing Codex executable is a failure."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from plugin_port.native import NativeError, smoke


class NativeSmokeTests(unittest.TestCase):
    def make_plugin(self, root: Path, *, explicit: bool = False) -> Path:
        plugin = root / "source"
        (plugin / ".codex-plugin").mkdir(parents=True)
        (plugin / ".codex-plugin/plugin.json").write_text(json.dumps({
            "name": "native-fixture", "version": "1.0.0",
            "description": "Synthetic plugin installation fixture", "skills": "./skills/"}))
        skill = plugin / "skills/hello"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: hello\ndescription: Say hello for the native test fixture\n---\n"
            "Use scripts/helper.py to produce the fixture greeting.\n")
        (skill / "scripts").mkdir()
        helper = skill / "scripts/helper.py"
        helper.write_text("#!/usr/bin/env python3\nprint('native fixture greeting')\n")
        helper.chmod(0o755)
        (skill / "assets").mkdir()
        (skill / "assets/binary.bin").write_bytes(bytes(range(256)))
        if explicit:
            (skill / "agents").mkdir()
            (skill / "agents/openai.yaml").write_text(
                "policy:\n  allow_implicit_invocation: false\n")
        return plugin

    def test_installs_discovers_and_tests_actual_cached_artifact(self):
        with tempfile.TemporaryDirectory(prefix="native-test-fixture-") as directory:
            root = Path(directory)
            plugin = self.make_plugin(root)
            behavior = root / "verify.py"
            behavior.write_text(
                "import os, pathlib, stat, subprocess, sys\n"
                "root = pathlib.Path(sys.argv[1])\n"
                "assert '/plugins/cache/' in root.as_posix()\n"
                "assert pathlib.Path.cwd() != root\n"
                "assert not os.environ.get('OPENAI_API_KEY')\n"
                "skill = root / 'skills/hello'\n"
                "assert (skill / 'assets/binary.bin').read_bytes() == bytes(range(256))\n"
                "helper = skill / 'scripts/helper.py'\n"
                "assert stat.S_IMODE(helper.stat().st_mode) & 0o111\n"
                "result = subprocess.run([sys.executable, str(helper)], check=True, "
                "capture_output=True, text=True)\n"
                "assert result.stdout.strip() == 'native fixture greeting'\n"
                "print('Installed artifact helper and binary verified')\n")
            result = smoke(plugin, "native-test-market", ["hello"], behavior)
            self.assertEqual(result["native"]["status"], "passed")
            self.assertEqual(result["native"]["automatic_skills"], ["native-fixture:hello"])
            self.assertEqual(result["native"]["uninstall"], "passed")
            self.assertEqual(result["native"]["hook_dispatch"], "not_exercised")
            self.assertEqual(result["behavior"]["status"], "passed")

    def test_explicit_skill_is_resolved_through_native_app_server(self):
        with tempfile.TemporaryDirectory(prefix="native-test-explicit-") as directory:
            plugin = self.make_plugin(Path(directory), explicit=True)
            result = smoke(plugin, "native-test-explicit", ["hello"])
            self.assertEqual(result["native"]["automatic_skills"], [])
            self.assertEqual(result["native"]["app_server_skills"], ["native-fixture:hello"])
            self.assertEqual(result["behavior"]["status"], "not_requested")

    def test_missing_expected_skill_fails_the_native_layer(self):
        with tempfile.TemporaryDirectory(prefix="native-test-missing-") as directory:
            plugin = self.make_plugin(Path(directory))
            with self.assertRaisesRegex(NativeError, "failed to discover.*missing"):
                smoke(plugin, "native-test-missing", ["missing"])

    def test_missing_codex_is_an_error(self):
        with mock.patch("plugin_port.native.shutil.which", return_value=None):
            with self.assertRaisesRegex(NativeError, "Codex CLI is required"):
                smoke(Path("unused"), "unused", [])

    def test_real_installed_hook_dispatch_with_loopback_provider(self):
        with tempfile.TemporaryDirectory(prefix="native-test-dispatch-") as directory:
            plugin = self.make_plugin(Path(directory), explicit=True)
            (plugin / "hooks").mkdir()
            (plugin / "hooks/hooks.json").write_text(json.dumps({"hooks": {"PostToolUse": [{
                "matcher": "^apply_patch$", "hooks": [{"type": "command",
                    "command": 'python3 "${PLUGIN_ROOT}/hooks/observe.py"'}]}]}}))
            (plugin / "hooks/observe.py").write_text(
                "import json, pathlib, sys\n"
                "event = json.load(sys.stdin)\n"
                "assert event['hook_event_name'] == 'PostToolUse'\n"
                "assert event['tool_name'] == 'apply_patch'\n"
                "assert '*** Add File: assets/broken.json' in event['tool_input']['command']\n"
                "assert (pathlib.Path(event['cwd']) / 'assets/broken.json').read_text() == '{broken\\n'\n"
                "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse', "
                "'additionalContext': 'FIXTURE_NATIVE_POSTTOOL'}}))\n")
            result = smoke(plugin, "native-test-dispatch", ["hello"], hook_fixture={
                "path": "assets/broken.json", "content": "{broken",
                "expected_feedback": "FIXTURE_NATIVE_POSTTOOL", "skill": "hello",
                "skill_contains": "Use scripts/helper.py to produce the fixture greeting.",
            })
            self.assertEqual(result["native"]["hook_dispatch"], "passed")
            self.assertEqual(result["dispatch"]["provider_requests"], 2)
            self.assertEqual(result["dispatch"]["explicit_skill_injection"], "passed")
            self.assertEqual(result["dispatch"]["model_semantics"], "not_evaluated")


if __name__ == "__main__":
    unittest.main()
