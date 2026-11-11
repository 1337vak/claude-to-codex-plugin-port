from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from .core import PortError, bind_adapter, build, inside, validate, write_json
from .scaffold import initialize, wire


def main():
    parser = argparse.ArgumentParser(description="Reproducible Claude-to-Codex adapters using Compound Engineering's converter")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a local adapter recipe and publishing workflow")
    init.add_argument("repository")
    init.add_argument("--output", type=Path, required=True)
    init.add_argument("--name")
    init.add_argument("--ref", default="HEAD")
    init.add_argument("--source-dir", default=".")
    for name in ("build", "verify", "wire", "publish", "restore-modes", "create-repository"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--adapter", type=Path, default=Path.cwd())
        if name in {"build", "verify", "publish", "restore-modes"}:
            cmd.add_argument("--plugin", type=Path)
        if name == "verify":
            cmd.add_argument("--native", action="store_true")
        if name == "publish":
            cmd.add_argument("--prepare", type=Path)
        if name == "create-repository":
            cmd.add_argument("--owner", required=True)
            cmd.add_argument("--private", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = initialize(args.repository, args.output.resolve(), name=args.name, ref=args.ref, source_dir=args.source_dir)
        else:
            adapter = args.adapter.resolve()
            plugin = getattr(args, "plugin", None) or adapter / "build/plugin"
            if args.command == "build":
                result = build(adapter, plugin)
            elif args.command == "wire":
                wire(adapter)
                result = {"status": "wired", "adapter": str(adapter)}
            elif args.command == "restore-modes":
                manifest = json.loads((plugin / "artifact-manifest.json").read_text())
                for name, entry in manifest.items():
                    inside(plugin, name).chmod(0o755 if entry["executable"] else 0o644)
                result = validate(plugin)
            elif args.command == "verify":
                result = {"artifact": validate(plugin)}
                bind_adapter(adapter, plugin)
                test = adapter / "tests/verify.py"
                if not test.exists():
                    raise PortError("Add tests/verify.py with observable adapter behavior assertions")
                subprocess.run([sys.executable, str(test), str(plugin)], check=True)
                result["behavior"] = {"status": "passed", "layer": "local-helper-and-content"}
                if args.native:
                    from .native import smoke
                    config = json.loads((adapter / "port.json").read_text())
                    expected = config.get("smoke_skills") or [path.parent.name for path in sorted((plugin / "skills").glob("*/SKILL.md"))]
                    native = smoke(plugin, config.get("marketplace", adapter.name), expected, test, hook_fixture=config.get("hook_fixture"))
                    result["native"] = native["native"]
                    result["installed_behavior"] = native["behavior"]
                    result["dispatch"] = native["dispatch"]
                result["artifact_manifest_sha256"] = hashlib.sha256((plugin / "artifact-manifest.json").read_bytes()).hexdigest()
                result["behavior_test_sha256"] = hashlib.sha256(test.read_bytes()).hexdigest()
                write_json(adapter / "build/verification.json", result)
            elif args.command == "publish":
                from .publish import publish
                result = publish(adapter, plugin, prepare=args.prepare)
            elif args.command == "create-repository":
                result = subprocess.run(["gh", "repo", "create", f"{args.owner}/{adapter.name}", "--private" if args.private else "--public", "--source", str(adapter), "--remote", "origin"], check=True)
                result = {"status": "created", "repository": f"{args.owner}/{adapter.name}"}
        print(json.dumps(result, indent=2))
    except (RuntimeError, subprocess.CalledProcessError, ValueError, OSError) as error:
        parser.exit(1, f"plugin-port: {error}\n")


if __name__ == "__main__":
    main()
