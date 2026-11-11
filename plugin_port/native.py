"""Exercise a package through Codex without hosted model calls or a user profile."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any


class NativeError(RuntimeError):
    """The installed Codex runtime rejected a package or failed a required check."""


_BAD_DIAGNOSTIC = re.compile(
    r"unsupported hook|unknown hook|failed to (?:load|parse) (?:plugin|manifest|hook|skill|config)"
    r"|invalid plugin|invalid manifest",
    re.I,
)


def _remove_native_tree(root: Path, *, timeout: float = 10) -> None:
    """Allow bounded time for Codex's background clone writes to finish."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            shutil.rmtree(root)
            return
        except OSError as error:
            if error.errno not in {errno.ENOTEMPTY, errno.EBUSY}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.1, remaining))


@contextmanager
def _native_workspace():
    root = Path(tempfile.mkdtemp(prefix="plugin-port-native-")).resolve()
    try:
        yield root
    finally:
        _remove_native_tree(root)


def _inventory(root: Path) -> dict[str, tuple[str, int]]:
    result = {}
    for file in sorted(root.rglob("*")):
        relative = file.relative_to(root).as_posix()
        if file.is_symlink():
            if not file.resolve().is_relative_to(root.resolve()):
                raise NativeError(f"Plugin symlink escapes its package: {relative}")
            result[relative] = ("symlink:" + os.readlink(file), 0)
        elif file.is_file():
            result[relative] = (
                hashlib.sha256(file.read_bytes()).hexdigest(),
                stat.S_IMODE(file.stat().st_mode) & 0o111,
            )
    return result


def _environment(root: Path) -> dict[str, str]:
    # No API keys, auth tokens, inherited CODEX settings, or user config paths.
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR"}}
    env.update({"CODEX_HOME": str(root / "codex"), "HOME": str(root / "user"),
                "XDG_CONFIG_HOME": str(root / "xdg"), "NO_COLOR": "1"})
    for name in ("codex", "user", "xdg"):
        (root / name).mkdir()
    return env


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NativeError(f"Native check could not finish: {error}") from error
    if result.returncode or _BAD_DIAGNOSTIC.search(result.stderr):
        detail = (result.stderr.strip() or result.stdout.strip())[-6000:]
        raise NativeError(f"Native check failed ({' '.join(command[:4])}): {detail}")
    return result


def _json(command: list[str], *, cwd: Path, env: dict[str, str]) -> Any:
    result = _run(command, cwd=cwd, env=env)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise NativeError(f"Codex returned invalid JSON: {error}") from error


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    return []


def _prompt_text(value: Any) -> str:
    # User context can name skills that Codex never discovered.
    return "\n".join(text for item in value
                     if not (isinstance(item, dict) and item.get("role") == "user")
                     for text in _strings(item))


def _app_server_skills(codex: str, *, cwd: Path, env: dict[str, str]) -> dict[str, dict]:
    requests = [
        {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "plugin-port-smoke", "version": "1"}}},
        {"method": "initialized"},
        {"id": 2, "method": "skills/list", "params": {
            "cwds": [str(cwd)], "forceReload": True}},
    ]
    with tempfile.TemporaryFile(mode="w+") as errors:
        process = subprocess.Popen([codex, "--disable", "hooks", "app-server"],
                                   cwd=cwd, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=errors, text=True)
        timer = threading.Timer(60, process.kill)
        timer.start()
        response = None
        try:
            assert process.stdin is not None and process.stdout is not None
            for request in requests:
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
                if "id" not in request:
                    continue
                response = None
                for line in process.stdout:
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if message.get("id") == request["id"]:
                        response = message
                        break
                if response is None or "error" in response:
                    raise NativeError(f"Codex app-server {request['method']} failed: {response}")
        except (OSError, BrokenPipeError) as error:
            raise NativeError(f"Codex app-server failed: {error}") from error
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            if process.stdin:
                process.stdin.close()
            if process.stdout:
                process.stdout.close()
        errors.seek(0)
        diagnostics = errors.read()
        if _BAD_DIAGNOSTIC.search(diagnostics):
            raise NativeError(f"Codex app-server rejected plugin: {diagnostics[-6000:]}")
    entries = response.get("result", {}).get("data") if isinstance(response, dict) else None
    if not isinstance(entries, list):
        raise NativeError("Codex app-server skills/list returned an invalid response")
    return {skill["name"]: skill for entry in entries if isinstance(entry, dict)
            for skill in entry.get("skills", []) if isinstance(skill, dict)
            and isinstance(skill.get("name"), str) and skill.get("enabled")}


def _dispatch_fixture(codex: str, fixture: dict, plugin_name: str, *,
                      cwd: Path, env: dict[str, str]) -> dict:
    """A loopback responder directs one real tool call, never a hosted model."""
    relative = fixture.get("path")
    content = fixture.get("content")
    feedback = fixture.get("expected_feedback")
    if (not isinstance(relative, str) or not relative or any(c in relative for c in "\r\n\0")
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts or "\\" in relative
            or not isinstance(content, str) or not isinstance(feedback, str) or not feedback):
        raise NativeError("Hook fixture requires a contained path, content, and expected_feedback")
    work = cwd / "hook-fixture"
    work.mkdir()
    target = work / relative
    if not target.resolve().is_relative_to(work.resolve()):
        raise NativeError("Hook fixture path escapes its temporary workspace")
    patch = "*** Begin Patch\n*** Add File: " + relative + "\n"
    patch += "\n".join("+" + line for line in content.splitlines()) + "\n*** End Patch"
    # Without explicit metadata, an isolated custom provider omits apply_patch.
    # Shape follows codex-rs/protocol/src/openai_models.rs at rust-v0.167.0.
    catalog = work / "fixture-model.json"
    catalog.write_text(json.dumps({"models": [{
        "slug": "plugin-port-fixture", "display_name": "Deterministic fixture",
        "description": "Local transport test", "supported_reasoning_levels": [],
        "shell_type": "unified_exec", "visibility": "list", "supported_in_api": True,
        "priority": 0, "base_instructions": "Execute the supplied deterministic tool fixture.",
        "support_verbosity": False, "default_verbosity": None,
        "apply_patch_tool_type": "freeform", "truncation_policy": {"mode": "bytes", "limit": 10000},
        "experimental_supported_tools": [], "tool_mode": "direct"}]}))
    inputs = []
    protocol_errors = []

    class Responder(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                if self.path != "/v1/responses" or len(inputs) >= 2:
                    raise ValueError("Unexpected or excessive fixture provider request")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length < 20_000_000:
                    raise ValueError("Invalid fixture provider request size")
                request = json.loads(self.rfile.read(length))
                inputs.append(request.get("input", []))
                if len(inputs) == 1:
                    item = {"type": "custom_tool_call", "id": "fixture_patch", "call_id": "fixture_call",
                            "name": "apply_patch", "input": patch}
                else:
                    item = {"type": "message", "id": "fixture_done", "role": "assistant",
                            "content": [{"type": "output_text", "text": "Deterministic fixture completed."}]}
                response_id = f"fixture_response_{len(inputs)}"
                events = [
                    {"type": "response.created", "response": {"id": response_id}},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {"id": response_id, "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
                ]
                body = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                               for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, OSError) as error:
                protocol_errors.append(str(error))
                self.send_error(400)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Responder)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    skill = fixture.get("skill")
    prompt = "Run the deterministic local fixture tool call and finish."
    if skill:
        mention = skill if skill.startswith(plugin_name + ":") else f"{plugin_name}:{skill}"
        prompt = "$" + mention + " " + prompt
    arguments = [codex, "--dangerously-bypass-hook-trust", "-a", "never",
        "-c", 'model_provider="plugin_port_fixture"', "-c", 'model="plugin-port-fixture"',
        "-c", f"model_catalog_json={json.dumps(str(catalog))}",
        "-c", 'model_providers.plugin_port_fixture.name="Deterministic loopback fixture"',
        "-c", f'model_providers.plugin_port_fixture.base_url="http://127.0.0.1:{server.server_port}/v1"',
        "-c", 'model_providers.plugin_port_fixture.wire_api="responses"',
        "-c", "model_providers.plugin_port_fixture.requires_openai_auth=false",
        "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", prompt]
    try:
        result = _run(arguments, cwd=work, env=env)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    if protocol_errors or len(inputs) != 2:
        raise NativeError(f"Deterministic hook fixture transport failed: {protocol_errors}; requests={len(inputs)}")
    if not target.is_file() or target.read_text() != content.rstrip("\n") + "\n":
        raise NativeError("Codex did not apply the deterministic fixture patch")
    original_items = {json.dumps(item, sort_keys=True) for item in inputs[0]}
    new_items = [item for item in inputs[1] if json.dumps(item, sort_keys=True) not in original_items]
    visible_feedback = "\n".join(_strings(new_items))
    if feedback not in visible_feedback or "hook: PostToolUse" not in result.stderr:
        raise NativeError("Installed native PostToolUse hook did not deliver expected model-visible feedback: "
                          + feedback)
    if fixture.get("skill_contains") and fixture["skill_contains"] not in "\n".join(_strings(inputs[0])):
        raise NativeError("Explicit skill invocation did not inject its expected instructions")
    return {"status": "passed", "scope": "native_dispatch_with_deterministic_provider",
            "model_semantics": "not_evaluated", "event": "PostToolUse", "tool": "apply_patch",
            "provider_requests": len(inputs), "path": relative, "feedback": feedback,
            "explicit_skill_injection": "passed" if fixture.get("skill_contains") else "not_requested"}


def smoke(plugin: Path, marketplace_name: str, expected_skills: list[str],
          behavior_test: Path | None = None, *, hook_fixture: dict | None = None) -> dict:
    """Install, discover, test installed bytes, and uninstall in an isolated profile.

    Discovery disables hooks. Optional behavior tests run against the installed
    artifact. An optional reviewed hook fixture uses real Codex dispatch with a
    deterministic loopback provider and one-invocation hook trust bypass. No
    hosted model requests, login, or persistent trust changes are performed.
    """
    codex = shutil.which("codex")
    if not codex:
        raise NativeError("Codex CLI is required for native checks; install it and retry")
    plugin = plugin.resolve(strict=True)
    manifest_path = plugin / "plugin.json"
    if not manifest_path.is_file():
        manifest_path = plugin / ".codex-plugin/plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise NativeError(f"Cannot read plugin manifest: {error}") from error
    name = manifest.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise NativeError("Plugin manifest requires a kebab-case name")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", marketplace_name):
        raise NativeError("Marketplace requires a kebab-case name")
    expected = [skill if skill.startswith(name + ":") else f"{name}:{skill}"
                for skill in expected_skills]
    original_files = _inventory(plugin)
    selector = f"{name}@{marketplace_name}"
    with _native_workspace() as root:
        env = _environment(root)
        cwd = root / "work"
        cwd.mkdir()
        market = root / "market"
        copied = market / "plugins" / name
        shutil.copytree(plugin, copied, symlinks=True)
        catalog = market / ".agents/plugins/marketplace.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({"name": marketplace_name, "plugins": [{
            "name": name, "source": {"source": "local", "path": f"./plugins/{name}"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity"}]}))
        run = lambda args: _json([codex, *args], cwd=cwd, env=env)
        version = _run([codex, "--version"], cwd=cwd, env=env).stdout.strip()
        run(["plugin", "marketplace", "add", str(market), "--json"])
        markets = run(["plugin", "marketplace", "list", "--json"])
        if marketplace_name not in {entry.get("name") for entry in markets.get("marketplaces", [])}:
            raise NativeError("Codex did not register the temporary marketplace")
        available = run(["plugin", "list", "--marketplace", marketplace_name, "--available", "--json"])
        if selector not in {entry.get("pluginId") for entry in available.get("available", [])}:
            raise NativeError("Codex did not discover the packaged plugin in its marketplace")
        installation = run(["plugin", "add", selector, "--json"])
        installed = Path(installation.get("installedPath", "")).resolve()
        cache = (root / "codex/plugins/cache").resolve()
        if not installed.is_dir() or not installed.is_relative_to(cache):
            raise NativeError("Codex did not report an installed artifact inside the isolated cache")
        behavior = {"status": "not_requested"}
        dispatch = {"status": "not_requested"}
        try:
            installed_list = run(["plugin", "list", "--marketplace", marketplace_name, "--json"])
            if not any(entry.get("pluginId") == selector and entry.get("enabled")
                       for entry in installed_list.get("installed", [])):
                raise NativeError("Codex did not enable the installed plugin")
            if _inventory(installed) != original_files:
                raise NativeError("Installed plugin bytes or executable bits differ from the package")
            prompt = run(["--disable", "hooks", "debug", "prompt-input"])
            text = _prompt_text(prompt)
            automatic = [skill for skill in expected if skill in text]
            missing = [skill for skill in expected if skill not in automatic]
            resolved = _app_server_skills(codex, cwd=cwd, env=env) if missing else {}
            unresolved = [skill for skill in missing if skill not in resolved]
            if unresolved:
                raise NativeError("Codex failed to discover expected skills: " + ", ".join(unresolved))
            for skill in missing:
                path = resolved[skill].get("path")
                if not isinstance(path, str) or not Path(path).resolve().is_relative_to(installed):
                    raise NativeError(f"Codex resolved {skill} outside the installed package")
            if behavior_test is not None:
                result = _run([sys.executable, str(behavior_test.resolve(strict=True)), str(installed)],
                              cwd=cwd, env=env)
                behavior = {"status": "passed", "stdout": result.stdout.strip()[-6000:],
                            "scope": "installed_artifact_handlers_and_helpers"}
            if hook_fixture is not None:
                dispatch = _dispatch_fixture(codex, hook_fixture, name, cwd=cwd, env=env)
        finally:
            run(["plugin", "remove", selector, "--json"])
            remaining = run(["plugin", "list", "--marketplace", marketplace_name, "--json"])
            if any(entry.get("pluginId") == selector for entry in remaining.get("installed", [])):
                raise NativeError("Codex retained the uninstalled plugin")
            configuration = tomllib.loads((root / "codex/config.toml").read_text())
            if selector in configuration.get("plugins", {}) or installed.exists():
                raise NativeError("Codex uninstall retained plugin configuration or cache")
        return {"native": {"status": "passed", "codex_version": version,
                           "plugin": selector, "automatic_skills": automatic,
                           "app_server_skills": missing, "installed_files_verified": len(original_files),
                           "uninstall": "passed", "hook_dispatch": dispatch["status"] if hook_fixture else "not_exercised"},
                "behavior": behavior, "dispatch": dispatch}
