# Claude to Codex Plugin Port

Create reproducible Codex adapters for Claude Code plugins and instruction libraries. This project reuses [EveryInc's Compound Engineering converter](https://github.com/EveryInc/compound-engineering-plugin/tree/4ba48871919650d43a412e3faf0374272fdc209d) at a full commit SHA. It adds input locks, small adaptation recipes, native plugin packaging, behavioral verification, attribution, and Git marketplace publishing. It does not maintain a fork of the converter.

## Use the skill

```sh
codex plugin marketplace add 1337vak/claude-to-codex-plugin-port
codex plugin add claude-to-codex-plugin-port@claude-to-codex-plugin-port
```

Start a new thread and ask `$claude-to-codex-plugin-port` to port a GitHub repository. The skill reviews source-specific compatibility decisions and uses the scripts below. Repository creation and publication follow the user's authorization.

## CLI

Requires Git, Python 3.11+, Node/npm, and network access during builds. Bun 1.3.2 is used for the pinned converter; if Bun is absent, the tool installs the matching pinned platform binary into its own cache with npm lifecycle scripts disabled. `BUN_EXECUTABLE` selects an existing runtime. Install Codex CLI 0.167.0 for the verified native test baseline and GitHub CLI for repository creation/publication.

```sh
python3 -m plugin_port init https://github.com/owner/claude-example \
  --output ../codex-example-adapter
# Inspect source, edit port.json, add any necessary overrides and tests/verify.py.
python3 ../codex-example-adapter/scripts/port.py build
python3 ../codex-example-adapter/scripts/port.py verify --native
```

The CLI runs from this checkout without Python dependencies; `pip install .` also exposes `plugin-port`. For libraries under `.claude/`, add `--source-dir .claude` during initialization. Use `--ref` for a particular upstream ref; initialization resolves it to an immutable SHA. An adapter's bootstrap also pins this tool to a full commit. `PLUGIN_PORT_TOOL` is an explicit local development override, never used by generated CI.

## Adapter recipe

`port.json` contains:

- `source`: repository, full commit SHA, component directory, license and attribution.
- `converter` and `tool`: reviewed immutable revisions.
- `resources`: shared files/directories to package unchanged, with source and destination paths.
- `patches`: explicit literal replacements with a glob and exact total match `count`; source drift fails the build.
- `overrides/`: small authored files merged after converter output.
- `role_skill`: optional router skill holding converted specialist briefs and a catalog.
- `explicit_skills`: source workflow names that must remain explicit-only.
- `hooks`: reviewed native event definitions; hook scripts live in `overrides/` or preserved resources.
- `hook_fixture`: a local `path`, `content`, and `expected_feedback` for native PostToolUse verification; optional `skill` and `skill_contains` assert real skill-body injection.
- `compatibility`: decisions explaining preserved, adapted, advisory-only and omitted behavior.

Builds fetch fresh source and converter checkouts and install the converter's frozen dependency lock with lifecycle scripts disabled. Conversion happens in a temporary profile. The resulting skills retain their supporting resources and executable bits. Large instruction bodies move intact into adjacent `WORKFLOW.md` files. Native manifests, notices, conversion provenance, a source inventory and an artifact byte/executable-bit manifest are generated. A package digest contributes to its version so an adapter change invalidates cached installations even when upstream stays pinned.

The adapter's maintained branch contains the recipe and overrides. Generated code is kept in `build/` and the machine-generated `release` branch. Updates change the input pin deliberately, review transformation differences, and rerun verification. The converter performs some broad provider-specific rewrites; the adapter's compatibility review and exact patches remain necessary for semantic correctness.

## Verification

```sh
python3 -m unittest discover -s tests -v
PLUGIN_PORT_ENGINE_TEST=1 python3 -m unittest discover -s tests -v
```

Required CI enables the actual pinned-engine regression fixture. [The benchmark](benchmarks/README.md) records why EveryInc was selected over acplugin using the same executable fixtures.

Adapter checks have separate scopes. Package checks verify identities, bytes and executable bits. `tests/verify.py PLUGIN_DIRECTORY` checks source-specific helper/hook outcomes and preserved workflow semantics. Native checks use a clean temporary home with no inherited credentials, register/install the exact package, inspect skill discovery through Codex/app-server, rerun the adapter test against the installed cache, and uninstall. A hook fixture directs one real `apply_patch` through a deterministic loopback Responses provider, asserts native PostToolUse feedback in the following request, and optionally checks explicit skill instruction injection. Its one-invocation hook trust bypass applies only to this reviewed isolated fixture. Bundled hooks require a passing native fixture for publication. These checks evaluate transport and deterministic behavior; network crawls, paid integrations and model-driven judgments require separate appropriate fixtures and credentials.

## Publish

After source and tests are ready:

```sh
git -C ../codex-example-adapter init -b main
python3 ../codex-example-adapter/scripts/port.py create-repository --owner YOUR_ACCOUNT
# Review and commit source, then push main.
```

The generated GitHub workflow builds, tests with pinned Codex, then publishes a self-contained marketplace to `release` using the repository's scoped `GITHUB_TOKEN`. Pull requests validate without publishing. No additional publishing secret is needed. Repository Actions must be enabled and token policy must allow `contents: write`. The release push preserves branch ancestry and does not force-push.

```sh
codex plugin marketplace add YOUR_ACCOUNT/codex-example-adapter --ref release
codex plugin add PLUGIN@codex-example-adapter
```

`publish --prepare PATH` assembles a release tree for inspection. `publish` performs a credential-material audit before committing the generated branch. A source license still marked `REVIEW`, or unresolved compatibility decisions, blocks publication. Keep per-adapter tests meaningful; automation cannot establish equivalent behavior for arbitrary source workflows.

Public-directory submission is separate from Git marketplace distribution. Follow [OpenAI's submission flow](https://developers.openai.com/plugins/deploy/submission). Account verification and review may require manual intervention. Lifecycle hooks currently require manual distribution and explicit hook trust; they are ineligible for public-directory submission. Services may also need separate credentials or runtime dependencies.

## Attribution and license

MIT for this wrapper and skill. Conversion uses EveryInc's independently maintained MIT-licensed implementation at `4ba48871919650d43a412e3faf0374272fdc209d`; dependencies are installed from its frozen lock. [StackOne's adapter and native-test patterns](https://github.com/StackOneHQ/powerful-plugins) informed the design. [acplugin](https://github.com/TokenRollAI/acplugin) was evaluated as an alternative; its implementation is not bundled.

Each generated adapter must retain its origin's notices and any additional resource licenses. README attribution identifies the original project and pinned revision; `provenance.json` records source and converter inputs. These are unofficial adaptations with no implied endorsement by upstream authors or OpenAI.
