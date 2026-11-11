"""Publication unit tests use synthetic receipts, never task-verification evidence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from plugin_port import core
import plugin_port.publish as publisher
from support import packaging_fixture


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _synthetic_receipt(adapter, plugin, *, dispatch="not_exercised"):
    core.write_json(adapter / "build/verification.json", {
        "artifact_manifest_sha256": _digest(plugin / "artifact-manifest.json"),
        "behavior_test_sha256": _digest(adapter / "tests/verify.py"),
        "native": {"status": "passed", "hook_dispatch": dispatch},
        "installed_behavior": {"status": "passed"},
    })


class PublicationUnitTests(unittest.TestCase):
    def fixture(self, root, *, hooks=False, ignored_binary=False):
        adapter, source, converted, config, source_files = packaging_fixture(root)
        (adapter / "tests").mkdir()
        (adapter / "tests/verify.py").write_text("print('Authored verification fixture')\n")
        (adapter / "README.md").write_text(
            "# Fixture adapter\n\nOrigin: https://github.com/example/fixture\n")
        if hooks:
            config["hooks"] = {"PostToolUse": [{"matcher": "apply_patch", "hooks": [{
                "type": "command", "command": 'python3 "${PLUGIN_ROOT}/hooks/fixture.py"'}]}]}
            scripts = adapter / "overrides/hooks"
            scripts.mkdir()
            (scripts / "fixture.py").write_text("print('{}')\n")
        if ignored_binary:
            (adapter / "overrides/.gitignore").write_text("*.bin\n*.png\n")
            (adapter / "overrides/obsolete.txt").write_text("Old release fixture\n")
        (adapter / "port.json").write_text(json.dumps(config))
        plugin = root / "plugin"
        core.package(adapter, source, converted, plugin, config, source_files)
        _synthetic_receipt(adapter, plugin)
        return adapter, source, converted, config, source_files, plugin

    def test_synthetic_receipt_prepares_valid_final_release_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, _, _, _, _, plugin = self.fixture(root)
            release = root / "release"
            publisher.release_tree(adapter, plugin, release)
            self.assertEqual(core.validate(release)["status"], "passed")
            manifest = json.loads((release / "artifact-manifest.json").read_text())
            self.assertIn("README.md", manifest)
            self.assertIn(".agents/plugins/marketplace.json", manifest)
            catalog = json.loads((release / ".agents/plugins/marketplace.json").read_text())
            self.assertEqual(catalog["plugins"][0]["source"]["path"], "./")

    def test_synthetic_receipt_rejects_changed_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, _, _, config, _, plugin = self.fixture(root)
            config["description"] = "Changed after build"
            (adapter / "port.json").write_text(json.dumps(config))
            with self.assertRaisesRegex(core.PortError, "recipe changed"):
                publisher.release_tree(adapter, plugin, root / "release")

    def test_synthetic_receipt_rejects_changed_behavior_test(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, _, _, _, _, plugin = self.fixture(root)
            (adapter / "tests/verify.py").write_text("print('Changed fixture test')\n")
            with self.assertRaisesRegex(core.PortError, "behavior tests changed"):
                publisher.release_tree(adapter, plugin, root / "release")

    def test_synthetic_receipt_rejects_different_valid_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, _, _, _, _, plugin = self.fixture(root)
            (plugin / "skills/hello/SKILL.md").write_text(
                (plugin / "skills/hello/SKILL.md").read_text() + "New generated content.\n")
            (plugin / "artifact-manifest.json").unlink()
            core.write_json(plugin / "artifact-manifest.json", core.inventory(plugin))
            self.assertEqual(core.validate(plugin)["status"], "passed")
            with self.assertRaisesRegex(core.PortError, "does not match the artifact"):
                publisher.release_tree(adapter, plugin, root / "release")

    def test_synthetic_hook_receipt_must_include_native_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, _, _, _, _, plugin = self.fixture(root, hooks=True)
            with self.assertRaisesRegex(core.PortError, "native dispatch"):
                publisher.release_tree(adapter, plugin, root / "rejected-release")
            _synthetic_receipt(adapter, plugin, dispatch="passed")
            publisher.release_tree(adapter, plugin, root / "release")
            self.assertEqual(core.validate(root / "release")["status"], "passed")

    def test_source_manual_flag_generates_explicit_invocation_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, source, converted, config, source_files = packaging_fixture(root)
            core.write_json(converted / "source-components.json", {"skills": [{
                "name": "hello", "manual": True, "source": "skills/hello/SKILL.md"}]})
            output = root / "plugin"
            core.package(adapter, source, converted, output, config, source_files)
            self.assertEqual((output / "skills/hello/agents/openai.yaml").read_text(),
                             "policy:\n  allow_implicit_invocation: false\n")
            self.assertEqual(core.validate(output)["status"], "passed")

    def test_local_bare_release_preserves_ignored_bytes_and_branch_ancestry(self):
        # Disable global/system Git config. Both remotes and all commits remain local.
        with tempfile.TemporaryDirectory(prefix="publish-unit-") as directory, mock.patch.dict(
                os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}):
            root = Path(directory)
            adapter, source, converted, config, source_files, plugin = self.fixture(root, ignored_binary=True)
            remote = root / "remote.git"
            core.run(["git", "init", "--quiet", "--bare", remote])
            core.run(["git", "init", "--quiet", adapter])
            core.run(["git", "config", "user.name", "Fixture Author"], cwd=adapter)
            core.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=adapter)
            core.run(["git", "remote", "add", "origin", remote], cwd=adapter)
            original_run = publisher.run
            audited_commits = []

            def audited_run(argv, **kwargs):
                if list(argv[:2]) == ["git", "commit"]:
                    tree = Path(kwargs["cwd"])
                    publisher.audit(tree)
                    # The authored fixture diff contains no credentials. Recheck exact
                    # staged additions with the session's scanner when it is installed.
                    scanner = Path.home() / ".codex/skills/security-before-commit/scripts/scan_staged.py"
                    if scanner.is_file():
                        subprocess.run([sys.executable, str(scanner)], cwd=tree,
                                       check=True, capture_output=True, text=True)
                    staged = core.run(["git", "diff", "--cached", "--name-only"], cwd=tree)
                    self.assertIn("artifact-manifest.json", staged)
                    audited_commits.append(core.inventory(tree))
                return original_run(argv, **kwargs)

            with mock.patch.object(publisher, "run", side_effect=audited_run):
                self.assertEqual(publisher.publish(adapter, plugin)["status"], "published")
                first = core.run(["git", "--git-dir", remote, "rev-parse", "release"]).strip()
                binary = subprocess.check_output(["git", "--git-dir", str(remote),
                                                  "show", "release:assets/data.bin"])
                self.assertEqual(binary, bytes(range(256)))
                pixel = subprocess.check_output(["git", "--git-dir", str(remote),
                                                 "show", "release:skills/hello/assets/pixel.png"])
                self.assertEqual(pixel, (plugin / "skills/hello/assets/pixel.png").read_bytes())
                config["version"] = "1.1.0"
                (adapter / "port.json").write_text(json.dumps(config))
                (adapter / "overrides/obsolete.txt").unlink()
                (adapter / "README.md").write_text("# Updated authored release fixture\n")
                next_plugin = root / "plugin-next"
                core.package(adapter, source, converted, next_plugin, config, source_files)
                _synthetic_receipt(adapter, next_plugin)
                self.assertEqual(publisher.publish(adapter, next_plugin)["status"], "published")
            parent = core.run(["git", "--git-dir", remote, "rev-parse", "release^"]).strip()
            self.assertEqual(parent, first)
            files = core.run(["git", "--git-dir", remote, "ls-tree", "-r", "--name-only", "release"])
            self.assertNotIn("obsolete.txt", files)
            self.assertIn(".gitignore", files)
            self.assertEqual(len(audited_commits), 2)


if __name__ == "__main__":
    unittest.main()
