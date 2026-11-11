---
name: claude-to-codex-plugin-port
description: Port a GitHub Claude Code plugin or instruction library into a reproducible Codex adapter using an existing converter, with compatibility decisions, behavior tests, origin attribution, and Git marketplace distribution.
---

# Claude to Codex plugin port

Use the scripts in this plugin to lock inputs, invoke the existing Compound Engineering converter, and package a native Codex plugin. Keep the adapter repository focused on its recipe, tests, and necessary overrides. The converter and source are fetched at full commit SHAs; do not copy their implementations into each adapter.

`<plugin-root>` is two directories above this skill. Invoke the CLI through `scripts/port.py` in this skill:

```sh
python3 <skill-root>/scripts/port.py init https://github.com/owner/claude-example --output /path/to/codex-example-adapter
```

Review the upstream license, notices, component inventory, runtime dependencies, and hook definitions. For a project template with resources under `.claude/`, use `--source-dir .claude`. Choose a concise `codex-…-adapter` repository name and preserve the source identity in the README and provenance.

Edit `port.json` to record the source commit, converter commit, compatibility decisions, required shared resources, and precise counted patches. The converter handles Claude parsing and component conversion; the wrapper handles reproducibility and native packaging. Unknown behavior requires a decision. Preserve domain procedures and meaningful decision gates when adapting tool names. Source allowlists and model tiers do not become enforced Codex permissions merely by being present in prose.

Put small authored changes in `overrides/`. Hooks belong inside the plugin and must use the actual Codex event payload. Retain upstream scripts where their behavior works; use a thin event shim when needed. Explain omitted events or automation in compatibility decisions. Preserve all applicable notices, including licenses in vendored resources. Keep credentials out of recipes and packages.

Add `tests/verify.py` accepting the installed plugin directory as its positional argument. Assert observable helper/hook outcomes and required workflow semantics using local fixtures. Then run:

```sh
python3 /path/to/adapter/scripts/port.py build
python3 /path/to/adapter/scripts/port.py verify --native
```

Native checks use a temporary Codex profile, install the exact artifact, verify discovery and bytes, run the adapter tests from another directory, and uninstall. Keep structural, helper, native-dispatch and model-behavior evidence distinct. If a check requires credentials or unavailable services, report it as untested; do not turn a skip into a pass. See the tool repository README for current supported versions and publishing behavior.

Create a GitHub repository and publish only within the user's authorization. `create-repository --adapter PATH --owner OWNER` creates the remote after local content is reviewable. Commit reviewed source, push `main`, and inspect the generated validation/publishing workflow. It publishes a generated `release` branch after tests pass. Test the remote marketplace:

```sh
codex plugin marketplace add OWNER/REPOSITORY --ref release
codex plugin add PLUGIN@MARKETPLACE
```

Public OpenAI directory submission is a separate reviewed process. Bundled lifecycle hooks are currently ineligible there. Tell the user about required account verification, hook trust, service credentials, or submission steps that remain manual. Existing publication authorization does not need to be requested again.
