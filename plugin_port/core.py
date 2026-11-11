"""Input locking and packaging; conversion belongs to Compound Engineering."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib

ENGINE = {
    "repository": "https://github.com/EveryInc/compound-engineering-plugin.git",
    "commit": "4ba48871919650d43a412e3faf0374272fdc209d",
}
TOOL_REPOSITORY = "https://github.com/1337vak/claude-to-codex-plugin-port.git"
TOOL_RELEASE = "v0.1.1"


class PortError(RuntimeError):
    pass


def run(argv, *, cwd=None, env=None, timeout=300):
    result = subprocess.run([str(x) for x in argv], cwd=cwd, env=env,
                            text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise PortError(f"Command failed ({result.returncode}): {argv[0]}\n{result.stderr[-4000:]}")
    return result.stdout


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def inside(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise PortError(f"Absolute package path: {relative}")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise PortError(f"Path escapes root: {relative}")
    return path


def github_url(value: str) -> str:
    value = value.removesuffix(".git").rstrip("/")
    if re.fullmatch(r"[\w.-]+/[\w.-]+", value):
        value = "https://github.com/" + value
    if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", value):
        raise PortError("Expected a GitHub repository URL or owner/repository")
    return value + ".git"


def resolve_ref(repository: str, ref: str):
    if re.fullmatch(r"[a-f0-9]{40}", ref):
        return ref
    rows = run(["git", "ls-remote", github_url(repository), ref, ref + "^{}"]).splitlines()
    entries = {row.split()[1]: row.split()[0] for row in rows if len(row.split()) == 2}
    peeled = {value for name, value in entries.items() if name.endswith("^{}")}
    direct = set(entries.values())
    choices = peeled or direct
    if len(choices) != 1:
        raise PortError(f"Git ref is missing or ambiguous: {ref}")
    return choices.pop()


def fetch(repository: str, commit: str, destination: Path):
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise PortError("An immutable full Git commit SHA is required")
    repository = github_url(repository)
    run(["git", "init", "--quiet", destination])
    run(["git", "-C", destination, "-c", "credential.helper=", "fetch", "--quiet", "--depth=1", repository, commit])
    run(["git", "-C", destination, "checkout", "--quiet", "--detach", "FETCH_HEAD"])
    if run(["git", "-C", destination, "rev-parse", "HEAD"]).strip() != commit:
        raise PortError("Fetched source does not match locked commit")
    if run(["git", "-C", destination, "status", "--porcelain", "--untracked-files=all", "--ignored"]).strip():
        raise PortError("Fetched source is not a clean checkout")
    if any(line.startswith("160000 ") for line in run(["git", "-C", destination, "ls-files", "--stage"]).splitlines()):
        raise PortError("Git submodules require separately locked resources and an explicit adaptation")


def copy_tree(source: Path, destination: Path, *, overwrite=False, authored=False):
    """Copy opaque bytes/modes. Links are rejected, never silently traversed."""
    if source.is_symlink():
        raise PortError(f"Symlink requires an explicit adaptation: {source}")
    if source.is_dir():
        for path in sorted(source.iterdir()):
            if path.name != ".git" and not (authored and (path.name == "__pycache__" or path.suffix == ".pyc" or path.name == ".DS_Store")):
                copy_tree(path, destination / path.name, overwrite=overwrite, authored=authored)
    elif source.is_file():
        if destination.exists() and not overwrite:
            raise PortError(f"Output collision: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    else:
        raise PortError(f"Missing source resource: {source}")


def inventory(root: Path):
    result = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.relative_to(root).parts:
            continue
        if path.is_symlink():
            raise PortError(f"Symlink in artifact: {path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "executable": bool(path.stat().st_mode & 0o111),
            }
    return result


def bun_runtime(cache: Path):
    configured = os.environ.get("BUN_EXECUTABLE") or shutil.which("bun")
    if configured:
        if run([configured, "--version"]).strip() != "1.3.2":
            raise PortError("This release requires Bun 1.3.2; set BUN_EXECUTABLE to that runtime")
        return configured
    system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    machine = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x64", "AMD64": "x64"}.get(platform.machine())
    if not system or not machine:
        raise PortError("Install Bun 1.3.2 and set BUN_EXECUTABLE on this platform")
    package = f"@oven/bun-{system}-{machine}"
    runtime = cache / "bun-1.3.2" / f"{system}-{machine}"
    binary = runtime / "node_modules" / package / "bin/bun"
    if not binary.exists():
        runtime.mkdir(parents=True, exist_ok=True)
        write_json(runtime / "package.json", {"private": True, "dependencies": {package: "1.3.2"}})
        run(["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=runtime)
    if run([binary, "--version"]).strip() != "1.3.2":
        raise PortError("Unexpected pinned Bun runtime version")
    return str(binary)


def prepare_engine(destination: Path, spec: dict, cache: Path):
    if spec != ENGINE:
        raise PortError("This release supports only its reviewed Compound Engineering engine lock")
    # A fresh checkout avoids persistent source-cache trust and concurrent build mutations.
    fetch(spec["repository"], spec["commit"], destination)
    bun = bun_runtime(cache)
    run([bun, "install", "--frozen-lockfile", "--ignore-scripts", "--production"], cwd=destination)
    return destination, bun


def convert(source: Path, output: Path, engine: Path, name: str, bun: str):
    """Stage a plugin view and invoke the existing converter unchanged."""
    with tempfile.TemporaryDirectory(prefix="plugin-port-view-") as temp:
        stage = Path(temp) / "source"
        copy_tree(source, stage)
        manifest_path = stage / ".claude-plugin/plugin.json"
        original_manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        manifest = {**original_manifest, "name": name, "version": "0.1.0"}
        withheld = []
        for key, surface, default in (("hooks", "hooks", "hooks/hooks.json"), ("mcpServers", "mcp", ".mcp.json")):
            if key in manifest or (source / default).exists():
                withheld.append(surface)
            manifest.pop(key, None)
            # Native activation is explicitly reviewed by the adapter recipe. The existing
            # converter's legacy global hook/config installation is not a packaging step.
            if (stage / default).is_file():
                (stage / default).unlink()
        if (source / "settings.json").is_file():
            settings = json.loads((source / "settings.json").read_text())
            if settings.get("hooks") and "hooks" not in withheld:
                withheld.append("hooks")
        manifest.pop("apps", None)
        write_json(manifest_path, manifest)
        inspector = Path(temp) / "inventory.ts"
        inspector.write_text(
            "import {loadClaudePlugin} from " + json.dumps(str(engine / "src/parsers/claude.ts")) + ";\n"
            "import {convertClaudeToCodex} from " + json.dumps(str(engine / "src/converters/claude-to-codex.ts")) + ";\n"
            "import {writeCodexBundle} from " + json.dumps(str(engine / "src/targets/codex.ts")) + ";\n"
            "const p = await loadClaudePlugin(" + json.dumps(str(stage)) + ");\n"
            "const ordered=(a,b)=>a.name<b.name?-1:a.name>b.name?1:0;\n"
            "p.skills.sort(ordered);p.commands.sort(ordered);p.agents.sort(ordered);\n"
            "await writeCodexBundle(" + json.dumps(str(output)) + ",convertClaudeToCodex(p,{codexIncludeSkills:true}),{outputIsCodexRoot:true});\n"
            "console.log(JSON.stringify({skills:p.skills.map(s=>({name:s.name,description:s.description,manual:s.disableModelInvocation,source:s.skillPath.replace(p.root+'/', '')})),commands:p.commands.map(c=>({name:c.name,manual:c.disableModelInvocation})),agents:p.agents.map(a=>({name:a.name,model:a.model,source:a.sourcePath.replace(p.root+'/', '')}))}));\n")
        metadata = json.loads(run([bun, inspector]))
        metadata["source_manifest_fields"] = sorted(original_manifest)
        metadata["withheld_capabilities"] = withheld
        write_json(output / "source-components.json", metadata)
        return "Converted through the pinned upstream parser, converter and writer with sorted component inputs."


def apply_patches(root: Path, patches: list):
    log = []
    for patch in patches:
        if Path(patch["glob"]).is_absolute() or ".." in Path(patch["glob"]).parts:
            raise PortError("Patch glob must stay inside the generated tree")
        paths = sorted(p for p in root.glob(patch["glob"]) if p.is_file())
        for path in paths:
            inside(root, path.relative_to(root).as_posix())
        changes = [(p, p.read_text()) for p in paths]
        count = sum(text.count(patch["old"]) for _, text in changes)
        if not patch["old"] or count != patch["count"]:
            raise PortError(f"Patch drift in {patch['glob']}: expected {patch['count']}, found {count}: {patch['old'][:80]}")
        for path, text in changes:
            if patch["old"] in text:
                path.write_text(text.replace(patch["old"], patch["new"]))
        log.append({"glob": patch["glob"], "old": patch["old"], "new": patch["new"], "count": count})
    return log


def frontmatter(text: str):
    match = re.match(r"\A---\r?\n.*?\r?\n---\r?\n", text, re.S)
    return (match[0], text[match.end():]) if match else ("", text)


def marketplace(config: dict, adapter: Path):
    return {"name": config.get("marketplace", adapter.name),
            "interface": {"displayName": config["display_name"]},
            "plugins": [{"name": config["name"], "source": {"source": "local", "path": "./"},
                         "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                         "category": config.get("category", "Productivity")}]}


def adapter_inputs(adapter: Path):
    overrides = adapter / "overrides"
    files = inventory(overrides) if overrides.exists() else {}
    files = {name: item for name, item in files.items() if "__pycache__" not in Path(name).parts
             and Path(name).suffix != ".pyc" and Path(name).name != ".DS_Store"}
    readme = adapter / "README.md"
    return {"overrides": files, "readme_sha256": hashlib.sha256(readme.read_bytes()).hexdigest() if readme.exists() else None}


def package(adapter: Path, source: Path, converted: Path, output: Path, config: dict, source_inventory: dict):
    """Assemble resources around converted instructions; don't reimplement converters."""
    output.mkdir(parents=True)
    converted_skills = converted / "skills" / config["name"]
    if not converted_skills.is_dir():
        raise PortError("Converter produced no skill tree")
    copy_tree(converted_skills, output / "skills")
    for resource in config.get("resources", []):
        copy_tree(inside(source, resource["from"]), inside(output, resource["to"]))
    role_skill = config.get("role_skill")
    metadata_file = converted / "source-components.json"
    metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
    descriptions = {item["name"]: item.get("description", "") for item in metadata.get("skills", [])}
    skill_sources = {item["name"]: item.get("source") for item in metadata.get("skills", [])}
    agent_sources = {item["name"]: item.get("source") for item in metadata.get("agents", [])}
    decisions = " ".join(item["component"] + " " + item["reason"] for item in config.get("compatibility", [])).lower()
    for capability in metadata.get("withheld_capabilities", []):
        if capability.rstrip("s") not in decisions:
            raise PortError(f"Record an explicit compatibility decision for source {capability}")
    if any(command.get("manual") for command in metadata.get("commands", [])) and "command" not in decisions:
        raise PortError("The converter omits manual-only commands; record an explicit adaptation/omission decision")
    roles = []
    if role_skill:
        for file in sorted((converted / "agents" / config["name"]).glob("*.toml")):
            data = tomllib.loads(file.read_text())
            relative = f"references/roles/{file.stem}.md"
            path = output / "skills" / role_skill / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data["developer_instructions"].rstrip() + "\n")
            original = inside(source, agent_sources.get(file.stem) or f"agents/{file.stem}.md")
            roles.append({"name": file.stem, "description": data.get("description", ""), "path": relative,
                          "source_sha256": hashlib.sha256(original.read_bytes()).hexdigest() if original.exists() else None})
    # Preserve upstream notices, including additional licenses carried inside resource trees.
    for name in ("LICENSE", "LICENSE.md", "NOTICE", "NOTICE.md"):
        if (source / name).is_file():
            copy_tree(source / name, output / name)
    overrides = adapter / "overrides"
    if overrides.exists():
        copy_tree(overrides, output, overwrite=True, authored=True)
    for path in sorted((output / "skills").glob("*/SKILL.md")):
        configured = any(fnmatch.fnmatch(path.parent.name, item) for item in config.get("explicit_skills", [])) and not (overrides / "skills" / path.parent.name / "SKILL.md").exists()
        source_manual = any(item["name"] == path.parent.name and item.get("manual") for item in metadata.get("skills", [])) and not (overrides / "skills" / path.parent.name / "agents/openai.yaml").exists()
        if configured or source_manual:
            policy = path.parent / "agents/openai.yaml"
            if policy.exists():
                if re.search(r"(?m)^\s+allow_implicit_invocation:\s*false\s*$", policy.read_text()):
                    continue
                raise PortError(f"Preserve existing metadata and set policy.allow_implicit_invocation=false in an override: {policy.relative_to(output)}")
            policy.parent.mkdir(parents=True, exist_ok=True)
            policy.write_text("policy:\n  allow_implicit_invocation: false\n")
    log = apply_patches(output, config.get("patches", []))
    if config.get("hooks"):
        write_json(output / "hooks/hooks.json", {"hooks": config["hooks"]})
    compat = "# Codex compatibility\n\n" + "\n\n".join(
        f"- **{item['component']} — {item['status']}**: {item['reason']}" for item in config.get("compatibility", []))
    compat += "\n\nUse the current user's authorization and actual host tools. Persona briefs do not enforce source tool allowlists or model choices. Never claim an automatic hook or external service ran without observed evidence.\n"
    (output / "COMPATIBILITY.md").write_text(compat)
    workflows = []
    for path in sorted((output / "skills").glob("*/SKILL.md")):
        fm, body = frontmatter(path.read_text())
        prelude = "Read [Codex compatibility](../../COMPATIBILITY.md) before following this workflow.\n\n"
        prelude += config.get("skill_preamble", "") + "\n\n"
        if len((prelude + body).encode()) > 6500:
            # Keep the full adapted body; never truncate it to fit a particular host loader.
            if (path.parent / "WORKFLOW.md").exists():
                raise PortError(f"Workflow extraction would overwrite a source resource: {path.parent}")
            (path.parent / "WORKFLOW.md").write_text(body)
            body = "Read [the complete workflow](WORKFLOW.md) and only the supporting references relevant to this request.\n"
        path.write_text(fm + "\n" + prelude + body)
        if path.parent.name != role_skill:
            original = inside(source, skill_sources.get(path.parent.name) or f"skills/{path.parent.name}/SKILL.md")
            workflows.append({"name": path.parent.name, "path": f"../{path.parent.name}/SKILL.md", "description": descriptions.get(path.parent.name, ""),
                              "source_sha256": hashlib.sha256(original.read_bytes()).hexdigest() if original.exists() else None})
    catalog = {"roles": roles, "workflows": workflows}
    if role_skill:
        write_json(output / "skills" / role_skill / "catalog.json", catalog)
    manifest = {"name": config["name"], "version": config["version"], "description": config["description"],
                "skills": "./skills/", "interface": {"displayName": config["display_name"], "shortDescription": config["description"][:100]}}
    if config.get("hooks"):
        manifest["hooks"] = "./hooks/hooks.json"
    write_json(output / ".codex-plugin/plugin.json", manifest)
    if (adapter / "README.md").is_file():
        copy_tree(adapter / "README.md", output / "README.md", overwrite=True)
    write_json(output / ".agents/plugins/marketplace.json", marketplace(config, adapter))
    provenance = {"source": config["source"], "converter": config["converter"], "tool": config["tool"],
                  "recipe_sha256": hashlib.sha256((adapter / "port.json").read_bytes()).hexdigest(),
                  "adapter_inputs": adapter_inputs(adapter),
                  "source_inventory": source_inventory, "source_components": metadata, "transformations": log,
                  "compatibility": config.get("compatibility", [])}
    write_json(output / "provenance.json", provenance)
    # Existing router helpers expect provenance/license alongside their entrypoint.
    if role_skill:
        write_json(output / "skills" / role_skill / "provenance.json", {"source": config["source"]["repository"], "commit": config["source"]["commit"], "license": config["source"].get("license"), "source_counts": {"roles": len(roles), "workflows": len(workflows)}})
        if (output / "LICENSE").exists():
            shutil.copy2(output / "LICENSE", output / "skills" / role_skill / "LICENSE")
    write_json(output / "artifact-manifest.json", inventory(output))
    return catalog


def validate(plugin: Path):
    manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
    expected = json.loads((plugin / "artifact-manifest.json").read_text())
    actual = inventory(plugin)
    actual.pop("artifact-manifest.json", None)
    if actual != expected:
        raise PortError("Artifact bytes/modes differ from their manifest")
    skills = sorted((plugin / "skills").glob("*/SKILL.md"))
    if not skills:
        raise PortError("Plugin contains no skills")
    names = set()
    for skill in skills:
        fm, _ = frontmatter(skill.read_text())
        match = re.search(r"(?m)^name:\s*[\"']?([^\s\"']+)", fm)
        if not match or match[1] != skill.parent.name or match[1] in names:
            raise PortError(f"Invalid/duplicate skill identity: {skill}")
        names.add(match[1])
    if "hooks" in manifest:
        hooks = json.loads(inside(plugin, manifest["hooks"]).read_text())["hooks"]
        for entries in hooks.values():
            for entry in entries:
                for hook in entry["hooks"]:
                    if hook["type"] != "command" or "args" in hook:
                        raise PortError("Hooks require reviewed command handlers; args arrays are not supported")
    return {"status": "passed", "layer": "artifact", "skills": len(skills), "files": len(actual)}


def bind_adapter(adapter: Path, plugin: Path):
    provenance = json.loads((plugin / "provenance.json").read_text())
    current = hashlib.sha256((adapter / "port.json").read_bytes()).hexdigest()
    if provenance.get("recipe_sha256") != current:
        raise PortError("Adapter recipe changed after build; rebuild before verification/publication")
    if provenance.get("adapter_inputs") != adapter_inputs(adapter):
        raise PortError("Adapter overrides or README changed after build; rebuild before verification/publication")


def build(adapter: Path, output: Path | None = None, cache: Path | None = None):
    adapter = adapter.resolve()
    config = json.loads((adapter / "port.json").read_text())
    requested_output = output or adapter / "build/plugin"
    if requested_output.is_symlink():
        raise PortError("Build output must not be a symlink")
    output = requested_output.resolve()
    if output == adapter or adapter.is_relative_to(output):
        raise PortError("Build output must not replace the adapter repository")
    if output.exists() and not (output / "artifact-manifest.json").is_file():
        raise PortError("Existing output is not an owned generated plugin")
    cache = cache or Path(os.environ.get("PLUGIN_PORT_CACHE", Path.home() / ".cache/claude-to-codex-plugin-port"))
    with tempfile.TemporaryDirectory(prefix="plugin-port-build-") as temp:
        stage = Path(temp)
        engine, bun = prepare_engine(stage / "engine", config["converter"], cache)
        fetch(config["source"]["repository"], config["source"]["commit"], stage / "upstream")
        source = inside(stage / "upstream", config["source"].get("directory", "."))
        source_inventory = inventory(source)
        source_inventory = {key: value for key, value in source_inventory.items() if not key.startswith(".git/")}
        convert(source, stage / "converted", engine, config["name"], bun)
        catalog = package(adapter, source, stage / "converted", stage / "plugin", config, source_inventory)
        # The license can be at the repository root when source.directory points inside it.
        for notice in ("LICENSE", "LICENSE.md", "NOTICE", "NOTICE.md"):
            if not (stage / "plugin" / notice).exists() and (stage / "upstream" / notice).exists():
                shutil.copy2(stage / "upstream" / notice, stage / "plugin" / notice)
        if config.get("role_skill") and (stage / "plugin/LICENSE").exists():
            shutil.copy2(stage / "plugin/LICENSE", stage / "plugin/skills" / config["role_skill"] / "LICENSE")
        (stage / "plugin/artifact-manifest.json").unlink()
        manifest_path = stage / "plugin/.codex-plugin/plugin.json"
        payload = inventory(stage / "plugin")
        payload.pop(".codex-plugin/plugin.json", None)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = config["version"] + "+" + digest
        write_json(manifest_path, manifest)
        write_json(stage / "plugin/artifact-manifest.json", inventory(stage / "plugin"))
        result = validate(stage / "plugin")
        output.parent.mkdir(parents=True, exist_ok=True)
        pending = Path(tempfile.mkdtemp(prefix=".plugin-port-", dir=output.parent))
        backup = None
        try:
            shutil.copytree(stage / "plugin", pending, dirs_exist_ok=True)
            if output.exists():
                backup = Path(tempfile.mkdtemp(prefix=".plugin-port-old-", dir=output.parent))
                backup.rmdir()
                os.replace(output, backup)
            os.replace(pending, output)
        except Exception:
            if backup and backup.exists() and not output.exists():
                os.replace(backup, output)
            raise
        finally:
            if pending.exists():
                shutil.rmtree(pending)
            if backup and backup.exists():
                shutil.rmtree(backup)
    return {**result, "output": str(output), "roles": len(catalog["roles"]), "upstream_commit": config["source"]["commit"]}
