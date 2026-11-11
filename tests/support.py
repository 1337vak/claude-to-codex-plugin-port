"""Small authored fixtures; no downloaded plugin content or user configuration."""
import json
from pathlib import Path
import struct
import zlib

from plugin_port.core import ENGINE, TOOL_REPOSITORY, inventory


def _pixel_png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


def packaging_fixture(root: Path, *, body: str = "Fixture workflow body.\n"):
    adapter, source, converted = (root / name for name in ("adapter", "source", "converted"))
    adapter.mkdir()
    source.mkdir()
    config = {
        "name": "fixture-plugin", "version": "1.0.0", "display_name": "Fixture Plugin",
        "description": "An authored packaging test fixture", "marketplace": "fixture-market",
        "source": {"repository": "https://github.com/example/fixture.git", "commit": "a" * 40,
                   "license": "MIT", "directory": "."},
        "converter": ENGINE,
        "tool": {"repository": TOOL_REPOSITORY, "commit": "b" * 40},
        "role_skill": "router", "resources": [{"from": "assets", "to": "assets"}],
        "compatibility": [{"component": "Agent personas", "status": "adapted",
                           "reason": "Loaded on demand as role references"}],
    }
    (adapter / "port.json").write_text(json.dumps(config))
    for parent in (source / "skills/hello", converted / "skills/fixture-plugin/hello"):
        (parent / "assets").mkdir(parents=True)
        (parent / "scripts").mkdir()
        (parent / "SKILL.md").write_text(
            "---\nname: hello\ndescription: Authored fixture workflow\n---\n" + body)
        (parent / "assets/pixel.png").write_bytes(_pixel_png())
        helper = parent / "scripts/helper.py"
        helper.write_text("#!/usr/bin/env python3\nprint('fixture helper')\n")
        helper.chmod(0o755)
    (source / "assets").mkdir()
    (source / "assets/data.bin").write_bytes(bytes(range(256)))
    (source / "LICENSE").write_text("MIT License\nCopyright (c) Fixture Author\n")
    (source / "NOTICE.md").write_text("Fixture attribution notice.\n")
    (source / "agents").mkdir()
    (source / "agents/reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Fixture reviewer\n---\nREVIEW_SENTINEL\n")
    (converted / "agents/fixture-plugin").mkdir(parents=True)
    (converted / "agents/fixture-plugin/reviewer.toml").write_text(
        'name = "reviewer"\ndescription = "Fixture reviewer"\n'
        'developer_instructions = "REVIEW_SENTINEL"\n')
    router = adapter / "overrides/skills/router"
    router.mkdir(parents=True)
    (router / "SKILL.md").write_text(
        "---\nname: router\ndescription: Select a fixture role\n---\nRead catalog.json.\n")
    return adapter, source, converted, config, inventory(source)
