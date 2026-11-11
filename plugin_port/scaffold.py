"""Small generated adapter entrypoint and GitHub release workflow."""
from pathlib import Path
import json
import re

from .core import ENGINE, TOOL_RELEASE, TOOL_REPOSITORY, PortError, github_url, resolve_ref, run, write_json


BOOTSTRAP = '''#!/usr/bin/env python3
"""Generated bootstrap: execute the port tool at the repository's locked commit."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
lock = json.loads((root / "port.json").read_text())["tool"]
local = os.environ.get("PLUGIN_PORT_TOOL")  # Explicit development override only.
with tempfile.TemporaryDirectory(prefix="plugin-port-tool-") as folder:
    tool = Path(local).resolve() if local else Path(folder)
    if not local:
        subprocess.run(["git", "init", "--quiet", str(tool)], check=True)
        subprocess.run(["git", "-C", str(tool), "fetch", "--quiet", "--depth=1", lock["repository"], lock["commit"]], check=True)
        subprocess.run(["git", "-C", str(tool), "checkout", "--quiet", "--detach", "FETCH_HEAD"], check=True)
        actual = subprocess.check_output(["git", "-C", str(tool), "rev-parse", "HEAD"], text=True).strip()
        if actual != lock["commit"]:
            raise SystemExit("Tool checkout does not match lock")
    env = {**os.environ, "PYTHONPATH": str(tool)}
    subprocess.run([sys.executable, "-m", "plugin_port", *sys.argv[1:], "--adapter", str(root)], env=env, check=True)
'''

WORKFLOW = '''name: Validate and publish adapter
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: adapter-${{ github.ref }}
  cancel-in-progress: true
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - uses: actions/setup-node@v4
        with:
          node-version: '22'
      - uses: oven-sh/setup-bun@v2
        with:
          bun-version: '1.3.2'
      - run: npm install --global @openai/codex@0.167.0 --ignore-scripts
      - run: python3 scripts/port.py build
      - run: python3 scripts/port.py verify --native
      - uses: actions/upload-artifact@v4
        with:
          name: plugin
          path: build/
          include-hidden-files: true
  publish:
    if: github.event_name != 'pull_request'
    needs: verify
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - uses: actions/download-artifact@v4
        with:
          name: plugin
          path: build
      # Artifact transport does not preserve Unix executable bits; restore from the manifest.
      - run: python3 scripts/port.py restore-modes
      - run: python3 scripts/port.py publish
        env:
          GH_TOKEN: ${{ github.token }}
'''


def wire(adapter: Path):
    (adapter / "scripts").mkdir(parents=True, exist_ok=True)
    (adapter / "scripts/port.py").write_text(BOOTSTRAP)
    (adapter / ".github/workflows").mkdir(parents=True, exist_ok=True)
    (adapter / ".github/workflows/publish.yml").write_text(WORKFLOW)


def initialize(repository: str, adapter: Path, *, name=None, ref="HEAD", source_dir="."):
    repository = github_url(repository)
    origin_name = repository.removesuffix(".git").split("/")[-1].lower()
    default = re.sub(r"^claude(?:-code)?-", "codex-", origin_name)
    if default == origin_name:
        default = "codex-" + default
    name = name or default.removesuffix("-adapter")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise PortError("Plugin name must use lowercase words separated by hyphens")
    if adapter.exists() and any(adapter.iterdir()):
        raise PortError("Initialize requires a new or empty adapter directory")
    commit = resolve_ref(repository, ref)
    tool_root = Path(__file__).resolve().parents[1]
    try:
        checkout = run(["git", "rev-parse", "--show-toplevel"], cwd=tool_root).strip()
        if Path(checkout).resolve() != tool_root.resolve():
            raise PortError("Installed plugin is not a source checkout")
        tool_commit = run(["git", "rev-parse", "HEAD"], cwd=tool_root).strip()
    except PortError:
        # Installed plugins omit .git; resolve this packaged release once and lock its SHA.
        tool_commit = resolve_ref(TOOL_REPOSITORY, "refs/tags/" + TOOL_RELEASE)
    adapter.mkdir(parents=True, exist_ok=True)
    write_json(adapter / "port.json", {
        "schema_version": 1, "name": name, "display_name": name.replace("-", " ").title(),
        "marketplace": adapter.name, "version": "0.1.0",
        "description": f"Codex adaptation of {origin_name}",
        "source": {"repository": repository, "commit": commit, "directory": source_dir,
                   "license": "REVIEW", "attribution": "Review the upstream license and notices"},
        "converter": ENGINE, "tool": {"repository": TOOL_REPOSITORY, "commit": tool_commit},
        "resources": [], "patches": [],
        "compatibility": [{"component": "source behavior", "status": "unreviewed",
                           "reason": "Review source capabilities, resource dependencies and runtime behavior before publishing."}],
    })
    (adapter / ".gitignore").write_text("/build/\n__pycache__/\n*.pyc\n")
    (adapter / "README.md").write_text(f"# {name}\n\nUnofficial adaptation of [{origin_name}]({repository.removesuffix('.git')}), pinned to `{commit}`.\n\nReview `port.json`, add necessary resources and adaptations, then run `python3 scripts/port.py build` and `python3 scripts/port.py verify --native`.\n")
    wire(adapter)
    return {"adapter": str(adapter), "source_commit": commit, "next": "Review source capabilities, write recipe and tests/verify.py, then build and verify."}
