"""Publish a reviewed, verified generated tree to a Git marketplace branch."""
from pathlib import Path
import hashlib
import json
import re
import shutil
import subprocess
import tempfile

from .core import PortError, bind_adapter, copy_tree, inventory, marketplace, run, validate, write_json


def audit(root: Path):
    findings = []
    for relative in inventory(root):
        path = root / relative
        if path.name in {"auth.json", "credentials.json", ".env", "id_rsa", "id_ed25519"}:
            findings.append(relative + ": credential file")
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|sk-(?:proj-)?[A-Za-z0-9_-]{40,}", text):
            findings.append(relative + ": possible credential material")
    if findings:
        raise PortError("Publication audit findings (values withheld):\n" + "\n".join(findings))
    return {"status": "passed", "layer": "publication-audit"}


def release_tree(adapter: Path, plugin: Path, destination: Path):
    validate(plugin)
    bind_adapter(adapter, plugin)
    evidence = adapter / "build/verification.json"
    if not evidence.exists():
        raise PortError("Run verify --native before publishing")
    report = json.loads(evidence.read_text())
    digest = hashlib.sha256((plugin / "artifact-manifest.json").read_bytes()).hexdigest()
    if report.get("artifact_manifest_sha256") != digest or report.get("native", {}).get("status") != "passed" or report.get("installed_behavior", {}).get("status") != "passed":
        raise PortError("Native verification does not match the artifact being published")
    if report.get("behavior_test_sha256") != hashlib.sha256((adapter / "tests/verify.py").read_bytes()).hexdigest():
        raise PortError("Adapter behavior tests changed after verification; rerun verify --native")
    config = json.loads((adapter / "port.json").read_text())
    if config.get("hooks") and report.get("native", {}).get("hook_dispatch") != "passed":
        raise PortError("Bundled hooks require a passing native dispatch fixture before publication")
    if config["source"].get("license") in {None, "REVIEW", "unknown"}:
        raise PortError("Review and record the upstream license before publication")
    if any(item["status"] in {"unreviewed", "unresolved"} for item in config["compatibility"]):
        raise PortError("Resolve compatibility decisions before publication")
    copy_tree(plugin, destination)
    for name in ("README.md",):
        if (adapter / name).exists():
            copy_tree(adapter / name, destination / name, overwrite=True)
    write_json(destination / ".agents/plugins/marketplace.json", marketplace(config, adapter))
    (destination / "artifact-manifest.json").unlink()
    write_json(destination / "artifact-manifest.json", inventory(destination))
    validate(destination)
    audit(destination)
    return config


def publish(adapter: Path, plugin: Path, *, prepare: Path | None = None):
    if prepare is not None:
        config = release_tree(adapter, plugin, prepare)
        return {"status": "prepared", "path": str(prepare), "marketplace": config.get("marketplace", adapter.name)}
    with tempfile.TemporaryDirectory(prefix="plugin-port-release-") as folder:
        tree = Path(folder) / "release"
        config = release_tree(adapter, plugin, tree)
        remote = run(["git", "remote", "get-url", "origin"], cwd=adapter).strip()
        run(["git", "init", "--quiet", "-b", "release", tree])
        run(["git", "config", "user.name", "github-actions[bot]"], cwd=tree)
        run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=tree)
        run(["git", "remote", "add", "origin", remote], cwd=tree)
        existing = run(["git", "ls-remote", remote, "refs/heads/release"]).split()
        if existing:
            run(["git", "fetch", "--quiet", "origin", "release"], cwd=tree)
            # Preserve branch ancestry; publication is a normal fast-forward push.
            run(["git", "reset", "--soft", "FETCH_HEAD"], cwd=tree)
        # This is an isolated, audited output tree. Upstream .gitignore files must not
        # hide tracked resources that were present during verification.
        run(["git", "add", "--force", "--all"], cwd=tree)
        audit(tree)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=tree).returncode
        if changed or not existing:
            run(["git", "commit", "--quiet", "-m", f"Publish {config['name']} {config['version']}"], cwd=tree)
            run(["git", "-c", "credential.helper=!gh auth git-credential", "push", "origin", "HEAD:refs/heads/release"], cwd=tree)
        return {"status": "published" if changed or not existing else "unchanged", "branch": "release", "remote": remote}
