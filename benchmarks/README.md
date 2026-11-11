# Converter baseline comparison

The initial implementation reuses Compound Engineering's converter unchanged.
The selection was based on real CLI runs against small, authored fixtures on
2026-11-11, followed by source inspection. No source hook or helper was executed
during conversion. These checks measure conversion and packaging behavior;
they do not establish model-level workflow equivalence.

## Locked sources

| Engine | Examined commit |
| --- | --- |
| [TokenRollAI/acplugin](https://github.com/TokenRollAI/acplugin) | [`49d9ba2c5fe5523073610dfa99d9224fd3775813`](https://github.com/TokenRollAI/acplugin/tree/49d9ba2c5fe5523073610dfa99d9224fd3775813) |
| [EveryInc/compound-engineering-plugin](https://github.com/EveryInc/compound-engineering-plugin) | [`4ba48871919650d43a412e3faf0374272fdc209d`](https://github.com/EveryInc/compound-engineering-plugin/tree/4ba48871919650d43a412e3faf0374272fdc209d) |

Acplugin was installed using `npm ci --ignore-scripts`, then compiled with
`npm run build`. Compound Engineering used its production dependencies with
lifecycle scripts disabled and Bun 1.3.2. Each conversion received a fresh
output directory; no personal Codex directory or credentials were used.

```sh
node acplugin/dist/index.js convert FIXTURE --to codex --all --output OUTPUT
bun compound-engineering/src/index.ts convert FIXTURE --to codex \
  --include-skills --codex-home OUTPUT
```

Compound Engineering's `--output` does not select the Codex output directory.
An explicit `--codex-home` is essential. Its default conversion is agents-only;
`--include-skills` enables the standalone skill and command conversion used here.

## Observed results

| Fixture or property | Acplugin | Compound Engineering |
| --- | --- | --- |
| Skill with a PNG resource | Changed binary bytes during UTF-8 conversion | Preserved bytes |
| Executable skill helper | Changed mode from 0755 to 0644 | Preserved executable mode |
| Commands-only plugin | Created a skill, but manifest omitted its skill directory | Created parsed command skill and legacy prompt |
| Skill `cmd-deploy` plus command `deploy` | Overwrote the original skill | Preserved both |
| Skill `deploy` plus command `deploy` | Preserved as `deploy` and `cmd-deploy` | Preserved as `deploy` and `deploy-2` |
| Hook JSON plus standalone hook script | Copied hook JSON, omitted script | Wrote hook JSON, omitted script |
| Two-plugin marketplace root | Converted both, but emitted directories differed from marketplace references | Required a single-plugin manifest |
| Project root with `.claude/skills` | Discovered the skill | Required a single-plugin manifest |

Acplugin exited successfully in all cases, including the packaging failures.
This is why a successful converter exit is only one layer of validation.

Source examples explaining the results include Acplugin's
[text-only file helpers](https://github.com/TokenRollAI/acplugin/blob/49d9ba2c5fe5523073610dfa99d9224fd3775813/src/utils/fs.ts),
[writer](https://github.com/TokenRollAI/acplugin/blob/49d9ba2c5fe5523073610dfa99d9224fd3775813/src/writer/codex.ts),
and [marketplace layout](https://github.com/TokenRollAI/acplugin/blob/49d9ba2c5fe5523073610dfa99d9224fd3775813/src/converter/pluginManifest.ts),
and Compound Engineering's
[converter](https://github.com/EveryInc/compound-engineering-plugin/blob/4ba48871919650d43a412e3faf0374272fdc209d/src/converters/claude-to-codex.ts)
and [Codex writer](https://github.com/EveryInc/compound-engineering-plugin/blob/4ba48871919650d43a412e3faf0374272fdc209d/src/targets/codex.ts).

## Wrapper responsibilities

The selected engine preserves resource bytes and executable bits, parses command
frontmatter, adapts Codex invocations, and allocates conflicting skill names. The
wrapper supplies immutable source locking, a normalized single-plugin input view,
reviewed hook scripts, attribution, compatibility decisions, native plugin and
marketplace packaging, and artifact integrity checks. Generated engine files are
intermediate build artifacts, never a maintained fork.

Run the maintained engine regression and native installation tests with:

```sh
PLUGIN_PORT_ENGINE_TEST=1 python3 -m unittest discover -s tests -v
```

The engine regression tests actual conversion of binary resources, executable
helpers, a command/skill name collision, and role instructions. Native tests use
an isolated Codex home and an unrelated working directory, exercise actual
installation/discovery/uninstallation, and test the installed cached artifact.
An installed synthetic hook is also dispatched through real Codex `apply_patch`
using a bounded loopback Responses fixture. Its feedback is verified in the next
request. The responder is deterministic; no hosted model or billable API is used.
Missing native prerequisites fail explicitly. Ordinary fast local tests can omit
the engine opt-in; CI enables it.
