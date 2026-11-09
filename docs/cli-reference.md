# CLI reference

This page documents `session-migrate` 0.10.0. `smigrate` is an exact shorthand
for the same executable.

## Commands

```text
session-migrate inspect SOURCE [--format FORMAT] [--json]
session-migrate convert SOURCE --to TARGET --output PATH [OPTIONS]
session-migrate import SOURCE --to TARGET [--home PATH] [--dry-run] [OPTIONS]
session-migrate transfer SOURCE_ID --from FORMAT --to TARGET [OPTIONS]
session-migrate transfer --catalog-id ID --to TARGET [OPTIONS]
session-migrate transfer --title TITLE [--from FORMAT] --to TARGET [OPTIONS]
session-migrate catalog refresh [ROOT OPTIONS] [--validate] [--json]
session-migrate catalog roots list|add|remove ...
session-migrate catalog list [FILTERS]
session-migrate catalog search QUERY [FILTERS]
session-migrate catalog show CATALOG_ID [--include-paths] [--json]
```

`SOURCE` is either a filesystem path (transcript, native session directory, or
supported export bundle) or a native session ID as the agent itself prints it.
An ID is resolved against the default home of each file-based agent, so
`import 01a0573d-…` finds `~/.codex/sessions/…/rollout-…-01a0573d-….jsonl`
without the caller translating it first. An existing path always wins. Pass
`--format` to scope the search to one agent when an ID is ambiguous, and use
`transfer` for OpenCode, Kilo, Hermes, MastraCode, and Devin, whose sessions are
reached through their own CLIs or shared databases rather than a predictable
file layout.

`FORMAT` and `TARGET` accept:

```text
claude  codex  pi  omp  opencode  copilot  antigravity  cursor
vibe  muse  qwen  kimi  grok  kilo  openhands  hermes  mastracode  devin
```

All eighteen formats are readable and writable. Cursor is an experimental,
text-only adapter pinned to one exact Cursor Agent build. Same-format migration
is supported as a portable rewrite into a new independent session.

## `inspect`

`inspect` reports structural metadata and counts without printing message text,
tool arguments/results, image bytes, or titles.

```bash
smigrate inspect ~/.codex/sessions/2026/08/20/rollout-...jsonl
smigrate inspect 01a0573d-078a-7580-b7ed-dd0228839f35
smigrate inspect ./store.db --format cursor --json
```

It prints the source path, CWD, UUID, timestamps, SHA-256, and structural
counts. Those fields can still be sensitive. Successful inspection means the
container is structurally recognizable; conversion applies stricter semantic
validation.

`--format` bypasses automatic format selection. It does not make an unsupported
schema or version safe.

Kilo and OpenCode official bundles intentionally share one schema and retain
stored metadata across cross-imports. A standalone bundle cannot be attributed
reliably, so automatic inspection fails closed; pass `--format kilo` or
`--format opencode` for a trusted bundle.

## `convert`

`convert` writes a standalone target artifact plus a sidecar manifest:

```bash
smigrate convert SOURCE --to codex --output ./rollout.jsonl
```

The manifest is `OUTPUT.session-migrate.json`. `convert` never installs into a
native agent home and never invokes a target CLI. For OpenCode and Kilo Code it
writes an official import bundle; for Antigravity and Cursor it writes a
complete SQLite database. Multi-file targets such as Vibe, Kimi, Grok, and
OpenHands use a validation bundle that `import` publishes into their complete
native layout. Hermes and Devin use bounded transaction bundles for their
shared databases; MastraCode uses a valid one-thread LibSQL database artifact.

## `import`

`import` converts and installs into a target store:

```bash
smigrate import SOURCE --to pi --cwd "$PWD" --dry-run
smigrate import SOURCE --to pi --cwd "$PWD"
smigrate import 01a0573d-078a-7580-b7ed-dd0228839f35 --to copilot
```

The target paths are collision checked and never overwritten. `--dry-run`
performs conversion, native validation, and collision checks but does not
install a session or manifest. OpenCode's official read/list preflight can
initialize its ordinary XDG cache/database metadata during a dry run.

OpenCode and Kilo import always use their official pinned CLIs and do not accept
`--home`; isolate or select them with normal `HOME`/XDG variables. Antigravity
and Cursor installs verify the exact pinned executable and its published
hashes. Muse and Qwen install one native JSONL. Kimi, Grok, and OpenHands
publish their validated multi-file native sessions together. Hermes delegates
the final transaction to the exact pinned `SessionDB.import_sessions` API;
MastraCode and Devin merge one validated identity transactionally into their
shared stores.

## `transfer`

Direct lookup uses a native source ID:

```bash
smigrate transfer SOURCE_UUID --from claude --to codex --cwd "$PWD"
smigrate transfer SOURCE_UUID --from omp --source-cwd "$PWD" --to codex
smigrate transfer SOURCE_UUID --from cursor --source-cwd "$PWD" --to claude
smigrate transfer SOURCE_UUID --from vibe --source-cwd "$PWD" --to codex
smigrate transfer SOURCE_UUID --from qwen --source-cwd "$PWD" --to kimi
smigrate transfer session_SOURCE_UUID --from kimi --source-cwd "$PWD" --to muse
smigrate transfer ses_... --from opencode --to pi --source-cli ~/.opencode/bin/opencode
smigrate transfer SESSION_UUID --from grok --source-cwd "$PWD" --to openhands
smigrate transfer ses_... --from kilo --to claude --source-cli /path/to/kilo
smigrate transfer SESSION_UUID --from openhands --to qwen
smigrate transfer 20260830_120000_a1b2c3 --from hermes --to codex
smigrate transfer SESSION_UUID --from mastracode --to pi
smigrate transfer fix-timeline-merging --from devin --to claude
```

Claude, Pi, OMP, Cursor, Vibe, Qwen, Kimi, and Grok can use `--source-cwd` to
select a workspace-specific store. OpenCode and Kilo are virtual: the pinned
official CLI exports the requested ID. Hermes, MastraCode, and Devin are also
virtual catalog sources, but their selected native ID is parsed directly from
the agent's shared database. All other sources are read from their native files.

Catalog transfer avoids ambiguous paths and duplicate UUIDs:

```bash
smigrate catalog search "parser refactor"
smigrate transfer --catalog-id CATALOG_ID --to copilot
smigrate transfer --title "parser refactor" --from claude --to copilot
```

`SOURCE_ID` selects the source. `--session-id` assigns the new target UUID;
they are deliberately different concepts.

`--title` searches the existing catalog and proceeds only when one session
matches. An exact case-insensitive title wins over partial keyword matches.
Refresh the catalog first; if the title is ambiguous, use `catalog search` and
pass the selected opaque ID with `--catalog-id`.

Without `--to`, only Claude→Codex and Codex→Claude retain their historical
default. Every other source requires an explicit target.

## Conversion options

| Option | Meaning |
| --- | --- |
| `--format FORMAT` | Override source detection for file-based `inspect`, `convert`, or `import` |
| `--session-id UUID` | Assign a new target UUID; generated by default |
| `--cwd PATH` | Target working directory; precedence is option, source CWD, process CWD |
| `--target-cli-version VERSION` | Change emitted metadata only; the writer architecture remains pinned. For Antigravity, `1.1.16` or `1.4.12` selects that pinned build's store layout |
| `--target-cli PATH` | Pinned OpenCode, Kilo, Antigravity, Cursor, or Hermes executable/source for native import |
| `--since-last-compaction` | Transfer only the window the source agent would itself resume with |
| `--model-provider ID` | Codex, Pi, OMP, OpenCode, or Muse target provider |
| `--model ID` | Target model label for formats that persist one, including Grok, Kilo, and OpenHands |
| `--home PATH` | Target native home, except OpenCode and Kilo |
| `--dry-run` | Validate and collision-check without installing migrator artifacts |

An irrelevant target-specific option may be accepted but has no effect. The
manifest records the target metadata version and warns when it differs from the
validated writer pin. Automatic OpenCode, Kilo, Antigravity, Cursor, and Hermes
installation still requires the exact pinned version. Antigravity `import` and
`transfer` without `--target-cli-version` write the layout of whichever pinned
`agy` build is installed (Linux x86_64 1.1.16 or macOS arm64 1.4.12); `convert`
never runs a target CLI and defaults to the 1.1.16 layout.

### `--since-last-compaction`

By default a migration replays the whole transcript, including history the source
agent had already compacted away. That is the faithful record, but a long session
can exceed the target model's context window several times over, which makes the
imported session unusable for an actual resume.

`--since-last-compaction` keeps what the source itself carried forward: the
plaintext messages from its last replacement history, followed by every later
event. Everything before that boundary is dropped, and the manifest reports a
`truncated_to_last_compaction` warning with the event counts on each side.

The compaction summary is carried over only when the source recorded it as
plaintext. Claude writes its summary as a readable `isCompactSummary` message, so
a truncated Claude migration keeps the summary that frames the remaining turns.
In every observed Codex rollout the `compacted.message` field is empty and the
replacement history ends in a provider-encrypted `compaction` item, so there the
summary survives only as a counted opaque placeholder and the carried anchor
messages, not the summary, give the resumed session its framing. Antigravity
checkpoints and Copilot compactions and context clears record plaintext
summaries and serve as boundaries too.

A Claude target has no portable compaction record, so a transcript that starts
from a summary replays it as the first user message under the heading
"Summary of the earlier part of this conversation:", counted as
`compaction:summary_as_user_message`. Summaries later in a full transcript are
still omitted because the turns they summarize are present.

The option fails closed when a session has no compaction boundary, because the
full transcript is already its effective window.

For a Claude source the transcript is not loaded whole. One streaming pass keeps
a small index (UUIDs, parent links, record types, byte offsets), the active
branch is found from it, and only the records from its last compaction boundary
on are read back, including preserved turns stored before the boundary. The
total-size safety limit applies to that loaded window instead of the file, so a
transcript far over 256 MiB can still be cut; per-record limits and the index
pass's own bounds still apply. The truncation warning's
`events_before_boundary` then counts only events inside the loaded window.
Other sources still load the whole session before cutting it.

## Home resolution

| Format | Default source/target root |
| --- | --- |
| Claude | `$CLAUDE_CONFIG_DIR`, otherwise `~/.claude` |
| Codex | `$CODEX_HOME`, otherwise `~/.codex` |
| Pi | `$PI_CODING_AGENT_DIR`, otherwise `~/.pi/agent` |
| Oh My Pi | `$PI_CODING_AGENT_DIR`, otherwise `~/.omp/agent` |
| OpenCode | official CLI under its normal XDG data root |
| Copilot | `$COPILOT_HOME`, otherwise `~/.copilot` |
| Antigravity | `~/.gemini/antigravity-cli` |
| Cursor | `$CURSOR_CONFIG_DIR`, `$XDG_CONFIG_HOME/cursor`, otherwise `~/.cursor` |
| Vibe | `$VIBE_HOME`, otherwise `~/.vibe` |
| Muse | `$XDG_DATA_HOME/muse`, otherwise `~/.local/share/muse` |
| Qwen | `$QWEN_HOME`, otherwise `~/.qwen` |
| Kimi | `$KIMI_CODE_HOME`, otherwise `~/.kimi-code` |
| Grok | `$GROK_HOME`, otherwise `~/.grok` |
| Kilo Code | official CLI under its normal XDG data root |
| OpenHands | `$OPENHANDS_CONVERSATIONS_DIR`, otherwise `~/.openhands/conversations` |
| Hermes Agent | `$HERMES_HOME`, otherwise `~/.hermes` |
| MastraCode | `$MASTRA_DB_PATH`; otherwise `$MASTRA_APP_DATA_DIR/mastra.db` or the platform data directory |
| Devin CLI | `$XDG_DATA_HOME/devin/cli`, otherwise `~/.local/share/devin/cli` on Linux |

Explicit `--home` or `--source-home` wins where supported. All CLI path options
expand `~` consistently.

## Catalog

The private SQLite catalog is at
`$SESSION_MIGRATE_CATALOG`, otherwise
`$XDG_STATE_HOME/session-migrate/catalog.sqlite3`, otherwise
`~/.local/state/session-migrate/catalog.sqlite3`.

Refresh auto-registers existing default, environment-selected, and ancestor
project roots:

```bash
smigrate catalog refresh
smigrate catalog refresh --discover-under ~/dev --validate
```

Additional roots are repeatable:

```text
--claude-root PATH       --codex-root PATH
--pi-root PATH           --omp-root PATH
--opencode-root PATH
--copilot-root PATH      --antigravity-root PATH
--cursor-root PATH       --vibe-root PATH
--muse-root PATH         --qwen-root PATH
--kimi-root PATH
--grok-root PATH         --kilo-root PATH
--openhands-root PATH
--hermes-root PATH       --mastracode-root PATH
--devin-root PATH
```

`--discover-under` is bounded to the supplied directory, never follows
symlinked directories, and recognizes conventional hidden stores. Arbitrary
custom root names must be registered explicitly. “All sessions” means all
recognized entries in these configured/discovered roots—not a whole-disk scan.

Search is case-insensitive across native title/name metadata and IDs. Multiple
keywords are ANDed and may occur in any order:

```bash
smigrate catalog search "database migration" --format codex
smigrate catalog search "timeout postgres" --lifecycle archived
smigrate catalog list --status candidate --since 2026-08-01T00:00:00Z
```

Paths and CWDs are neither searched nor printed unless `--include-paths` is
passed. The catalog never stores conversation bodies or tool payloads. See the
[catalog guide](session-catalog.md) for statuses and schema behavior.

## Successful JSON output

`convert`, `import`, and `transfer` print one content-free JSON object:

```json
{
  "cwd": "/target/workspace",
  "dropped_events": {"thinking:unsupported": 2},
  "dry_run": true,
  "manifest": "/target/state/session-migrate/manifests/UUID.json",
  "output": "/target/native/session/path",
  "records": 12,
  "session_id": "TARGET_ID",
  "sha256": "TARGET_SHA256",
  "source_format": "claude",
  "target_format": "cursor",
  "warnings": []
}
```

The historical field name `dropped_events` includes omissions,
transformations, and retained inconsistencies that need operator attention.
Non-empty warnings do not mean the command failed. Review them before resume.

Manifests use schema version 2 and include migration version, source/target
identity and hashes, structural counts, warning objects, and the same loss
counters. They never include message/tool/media bodies, but their paths, IDs,
CWDs, timestamps, and hashes are operationally sensitive.

A dry-run conversion is regenerated on apply. A fixed `--session-id` usually
pins native paths, but generated structural IDs/timestamps can change, so do not
expect identical SHA-256 values.

## Exit behavior

- `0`: success, help, or version
- `2`: argument error or expected migration failure

Expected failures are printed to stderr as:

```text
session-migrate: error: MESSAGE
```

There is no overwrite/force mode. Source files are always left untouched. If
manifest finalization fails after an external/native install, the error states
that the target session may already exist so the operator can inspect it before
retrying.
