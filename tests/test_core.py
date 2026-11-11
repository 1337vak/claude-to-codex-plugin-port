import json
import os
from pathlib import Path
import tempfile
import unittest

from plugin_port import core
from support import packaging_fixture


class PackagingTests(unittest.TestCase):
    def test_preserves_opaque_resources_modes_and_origin_notices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, source, converted, config, source_files = packaging_fixture(root)
            output = root / "plugin"
            catalog = core.package(adapter, source, converted, output, config, source_files)
            self.assertEqual(core.validate(output)["status"], "passed")
            self.assertEqual((output / "skills/hello/assets/pixel.png").read_bytes(),
                             (source / "skills/hello/assets/pixel.png").read_bytes())
            self.assertTrue((output / "skills/hello/scripts/helper.py").stat().st_mode & 0o111)
            self.assertEqual((output / "assets/data.bin").read_bytes(), bytes(range(256)))
            self.assertEqual((output / "NOTICE.md").read_bytes(), (source / "NOTICE.md").read_bytes())
            self.assertEqual((output / "LICENSE").read_bytes(), (source / "LICENSE").read_bytes())
            self.assertEqual(catalog["roles"][0]["name"], "reviewer")
            self.assertIn("REVIEW_SENTINEL", (output / "skills/router/references/roles/reviewer.md").read_text())
            provenance = json.loads((output / "provenance.json").read_text())
            self.assertEqual(provenance["source"], config["source"])
            self.assertEqual(provenance["converter"], core.ENGINE)
            self.assertEqual(provenance["source_inventory"], source_files)
            self.assertEqual(provenance["source_inventory"]["agents/reviewer.md"]["sha256"],
                             source_files["agents/reviewer.md"]["sha256"])

    def test_long_workflow_body_is_retained_in_full(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = "BEGIN_FULL_WORKFLOW\n" + "Detailed fixture step.\n" * 600 + "END_FULL_WORKFLOW\n"
            args = packaging_fixture(root, body=body)
            output = root / "plugin"
            core.package(*args[:3], output, *args[3:])
            self.assertEqual((output / "skills/hello/WORKFLOW.md").read_text(), body)
            self.assertIn("WORKFLOW.md", (output / "skills/hello/SKILL.md").read_text())
            self.assertEqual(core.validate(output)["status"], "passed")

    def test_exact_counted_patch_and_drift_rejection(self):
        for count, succeeds in ((1, True), (2, False)):
            with self.subTest(expected_count=count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                adapter, source, converted, config, source_files = packaging_fixture(root)
                config["patches"] = [{"glob": "skills/hello/SKILL.md", "old": "Fixture workflow body.",
                                      "new": "Adapted workflow body.", "count": count}]
                (adapter / "port.json").write_text(json.dumps(config))
                if succeeds:
                    core.package(adapter, source, converted, root / "plugin", config, source_files)
                    provenance = json.loads((root / "plugin/provenance.json").read_text())
                    self.assertEqual(provenance["transformations"][0]["count"], 1)
                    self.assertIn("Adapted workflow body.", (root / "plugin/skills/hello/SKILL.md").read_text())
                else:
                    with self.assertRaisesRegex(core.PortError, "Patch drift"):
                        core.package(adapter, source, converted, root / "plugin", config, source_files)

    def test_artifact_validation_rejects_byte_mode_and_extra_file_drift(self):
        for alteration in ("bytes", "mode", "extra"):
            with self.subTest(alteration=alteration), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args = packaging_fixture(root)
                output = root / "plugin"
                core.package(*args[:3], output, *args[3:])
                if alteration == "bytes":
                    (output / "skills/hello/assets/pixel.png").write_bytes(b"changed")
                elif alteration == "mode":
                    (output / "skills/hello/scripts/helper.py").chmod(0o644)
                else:
                    (output / "unrecorded.txt").write_text("extra")
                with self.assertRaisesRegex(core.PortError, "differ"):
                    core.validate(output)

    def test_containment_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            package.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("untouched")
            (package / "link").symlink_to(outside, target_is_directory=True)
            for relative in ("../outside/keep.txt", str(outside / "keep.txt"), "link/keep.txt"):
                with self.subTest(relative=relative), self.assertRaises(core.PortError):
                    core.inside(package, relative)
            with self.assertRaisesRegex(core.PortError, "Symlink"):
                core.copy_tree(package / "link", root / "copied")
            self.assertEqual((outside / "keep.txt").read_text(), "untouched")

    def test_resource_destination_cannot_escape_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, source, converted, config, source_files = packaging_fixture(root)
            config["resources"] = [{"from": "assets/data.bin", "to": "../escaped.bin"}]
            with self.assertRaisesRegex(core.PortError, "escapes"):
                core.package(adapter, source, converted, root / "plugin", config, source_files)
            self.assertFalse((root / "escaped.bin").exists())

    def test_patch_glob_cannot_modify_sibling_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "package"
            output.mkdir()
            (root / "outside.txt").write_text("unchanged")
            with self.assertRaises(core.PortError):
                core.apply_patches(output, [{"glob": "../outside.txt", "old": "unchanged",
                                             "new": "changed", "count": 1}])
            self.assertEqual((root / "outside.txt").read_text(), "unchanged")


@unittest.skipUnless(os.environ.get("PLUGIN_PORT_ENGINE_TEST") == "1",
                     "Set PLUGIN_PORT_ENGINE_TEST=1 to exercise the pinned real converter")
class EngineRegressionTests(unittest.TestCase):
    def test_actual_converter_preserves_assets_and_allocates_collisions(self):
        with tempfile.TemporaryDirectory(prefix="plugin-port-engine-regression-") as directory:
            root = Path(directory)
            adapter, source, _, config, source_files = packaging_fixture(root)
            (source / "commands").mkdir()
            (source / "commands/hello.md").write_text(
                "---\ndescription: Authored hello command\n---\nCOMMAND_SENTINEL\n")
            engine, bun = core.prepare_engine(root / "engine", core.ENGINE, root / "cache")
            converted = root / "result"
            core.convert(source, converted, engine, config["name"], bun)
            skills = converted / "skills" / config["name"]
            self.assertEqual({path.name for path in skills.iterdir()}, {"hello", "hello-2"})
            self.assertIn("COMMAND_SENTINEL", (skills / "hello-2/SKILL.md").read_text())
            self.assertEqual((skills / "hello/assets/pixel.png").read_bytes(),
                             (source / "skills/hello/assets/pixel.png").read_bytes())
            self.assertTrue((skills / "hello/scripts/helper.py").stat().st_mode & 0o111)
            agent = converted / "agents" / config["name"] / "reviewer.toml"
            self.assertIn("REVIEW_SENTINEL", agent.read_text())
            metadata = json.loads((converted / "source-components.json").read_text())
            for kind in ("skills", "commands", "agents"):
                self.assertEqual([item["name"] for item in metadata[kind]],
                                 sorted(item["name"] for item in metadata[kind]))
            repeated = root / "repeated"
            core.convert(source, repeated, engine, config["name"], bun)
            self.assertEqual(core.inventory(converted), core.inventory(repeated))


if __name__ == "__main__":
    unittest.main()
