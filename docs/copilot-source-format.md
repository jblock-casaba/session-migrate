# GitHub Copilot CLI source format

This note records the evidence and implementation contract for reading GitHub
Copilot CLI sessions as migration sources. All committed examples are synthetic.
No GitHub token, provider key, real prompt, repository path, or private session
identifier was copied into the fixture corpus.

## Pinned evidence

The reader was implemented and tested against these exact artifacts:

| Artifact | Pinned value |
| --- | --- |
| npm package | `@github/copilot@1.0.70` |
| package build metadata | `1a7a0a2e78` |
| public repository tag | `v1.0.70` (`758c2c9cc4bd5a83a8e48837ec62dc2305935ba4`) |
| session schema | version `1` |
| platform package | `@github/copilot-linux-x64@1.0.70` |

The platform package contains the readable distributed runtime (`app.js`) and
an auto-generated `copilot-sdk/generated/session-events.d.ts` whose header names
`session-events.schema.json` as its source. The TypeScript file defines the full
event-name union, required envelopes, attachment variants, tool calls/results,
binary assets, compaction, and reasoning fields. The implementation accepts the
complete pinned event-name union plus the explicitly listed newer diagnostic
events below, but projects only the portable subset. Other event names fail
closed.

The public repository tag and the npm package's build-metadata commit are both
recorded because they are not the same identifier. The distributed package and
its generated schema are authoritative for runtime behavior.

## Native storage

GitHub documents one local directory per session:

```text
$COPILOT_HOME/                       # defaults to ~/.copilot
  session-state/<session UUID>/
    events.jsonl                    # complete append-only event history
    workspace.yaml                  # picker name and workspace metadata
    session.db                      # per-session derived/runtime data
    checkpoints/
    files/
  session-store.db                  # derived cross-session index/search data
```

`events.jsonl` is the conversation source of truth. `workspace.yaml` supplies a
display name when no `session.title_changed` event exists. SQLite is not parsed
as conversation history: GitHub describes the global store as a structured
subset that can be reindexed from session files, and explicitly warns that it
is managed automatically.

When `events.jsonl` lives directly under a UUID-named directory, the reader
requires that name to match `session.start.data.sessionId`. This catches a common
class of copied or partially mixed session directories without preventing
file-oriented `convert` output from being inspected elsewhere.

## Event projection

| Native data | Portable IR | Notes |
| --- | --- | --- |
| `user.message.content` | user message | Uses display content; runtime-injected `transformedContent` is an explicit opaque loss so the destination can construct its own current wrapper. |
| Blob/file image attachment | user image context | Inline or content-addressed PNG, JPEG, GIF, and WebP survive exactly. Other attachment kinds remain opaque. |
| `assistant.message.content` | assistant message | Empty tool-only messages do not create empty text events. |
| `assistant.message.reasoningText` | thinking | Readable reasoning survives in the IR; target support determines replay. |
| `reasoningOpaque` / `encryptedContent` | opaque loss | The shipped schema describes these values as session-bound and stripped on resume, so they are never treated as portable thinking. |
| `assistant.message.toolRequests` | tool call | Preserves ID, name, arguments, and MCP server namespace. Arguments may be any JSON value, as the schema allows: custom tools such as `apply_patch` pass raw text, which targets that need an object receive as `{"input": ...}` and count as `tool_call:non_object_input`. |
| `task_complete` tool request | assistant message | Autopilot's final report exists only as this tool's `summary` argument, which Copilot displays as the answer, so it is replayed as assistant text. The call/result pair (whose result repeats the summary) is counted as `copilot_task_complete_tool`. |
| `tool.execution_start` | tool call fallback | Used only when no matching request was already projected; repeated IDs retain multiplicity. |
| `tool.execution_complete` | tool result | Preserves success/error, model-facing text, and portable binary images. |
| `session.compaction_complete` | compaction | Successful summaries survive. Failed or summary-less compactions are opaque. |
| `session.context_cleared` | compaction | The next context window starts from `initialMessage` alone, so a clear is the boundary `--since-last-compaction` cuts at; `initialMessage` is carried as its summary when present. |
| `session.title_changed` / `workspace.yaml` | title metadata | The most recent title event wins, then the sidecar name. |
| `session.model_change` | model metadata + opaque loss | The final model is retained; the temporal model-switch event is not replayed. |
| Root lifecycle, UI, permission, hook, usage, and shutdown events | opaque loss | Their type and reason are counted, but their potentially sensitive payload is not copied. |
| Recognized newer model diagnostics, usage checkpoints and records, and sub-agent configuration | opaque loss | Counted as `copilot_diagnostic_event`; model-input snapshots are not replayed as duplicate conversation turns. |
| Unrecognized well-formed event name | opaque loss | Counted as `copilot_unrecognized_event:<name>` with a warning; the payload is not copied. |
| Any event carrying `agentId` | opaque loss | Sub-agent timelines are not flattened into the main model conversation. |
| `system.message` | opaque loss | Privileged runtime prompts are not converted into user-controlled destination history. |
| `session.mode_notice_delivered` | opaque loss | A model-visible `content` notice is privileged runtime input and counted as `copilot_privileged_mode_notice`; a content-less notice is lifecycle metadata. |

Tool-result `result.content` is the model-facing text used by the pinned runtime.
`binaryResultsForLlm` is the model-facing media surface. Full detailed UI output,
structured MCP content, citations, and UI resources are counted separately when
they cannot be represented in the portable IR.

## Validation and security boundary

The source reader applies the shared JSONL limits (256 MiB total, 64 MiB per
record, 100,000 records) plus a per-record structural limit of 64 levels and
100,000 JSON nodes. It also checks:

- exactly one leading `session.start` with schema version `1`;
- a UUID session ID and matching canonical directory name;
- unique UUIDv4 event IDs and linear main-conversation `parentId` ancestry;
- valid RFC 3339 timestamps and object-valued event data;
- well-formed event names (unrecognized ones are skipped as counted losses) and
  non-persistence of ephemeral events;
- required message, tool-linkage, result, model-change, title, and system fields;
- bounded attachment, tool-request, content, and binary-result arrays;
- base64 validity, decoded lengths, SHA-256 asset IDs, duplicate assets, and
  reference MIME/length agreement.

The parser never reads Copilot configuration, GitHub authentication, provider
keys, environment secrets, logs, or either SQLite database.

## Native trajectory evidence

Two isolated, credential-free trajectories were run with the exact 1.0.70
binary and an in-process OpenAI-compatible loopback provider:

1. A plain one-turn run produced the native start/model/system/user/turn/message/
   shutdown sequence.
2. A tool trajectory made the model request Copilot's real built-in `view` tool,
   read a synthetic file, completed the tool result, and produced a final answer.

The second native log contained 13 records. `parse_session` recovered two
messages, one linked call/result pair, model and workspace-title metadata, and
explicit opaque events for the remaining native lifecycle/privileged records.
The parsed session was then serialized to a new UUID, installed using only
`events.jsonl` and `workspace.yaml`, cold-resumed by the exact binary, and
continued with a follow-up turn. The binary preserved the generated byte prefix,
appended its normal native events, and sent the original user text, real tool
result, assistant answer, and follow-up to the loopback provider.

Run the reusable test with an exact binary:

```console
SESSION_MIGRATE_COPILOT_BIN=/path/to/copilot-1.0.70 \
  uv run pytest -q tests/test_copilot_source_native.py
```

The test constructs a minimal environment from scratch and does not inherit
`GH_TOKEN`, `GITHUB_TOKEN`, `COPILOT_GITHUB_TOKEN`, or provider credentials.

Unit fixtures and malformed/security cases are covered by:

```console
uv run pytest -q tests/test_copilot_format.py tests/test_copilot_source.py
```

## Compatibility policy

The recorded CLI version is metadata, not the schema discriminator. A session
from another Copilot release is accepted when it still uses event schema version
`1` and every recognized event fits the validated payload subset.

Copilot adds event names in nearly every release (the 1.0.104 schema defines
more than 50 that this reader does not list), so an unrecognized but well-formed
name is not an error. After the same identity, timestamp, size, ephemeral, and
object-payload checks, it becomes an opaque loss counted as
`copilot_unrecognized_event:<name>`, and conversion warns that it was skipped.
Its payload is never copied, and like a model trace it may sit outside the main
ancestry chain. A new event that does carry conversation content therefore
surfaces as a named warning instead of blocking the migration; recognize it
explicitly to project it. Malformed names still fail closed.

Version-1 captures reporting `1.0.83-0` also contain these recognized diagnostic
records: `model.captured_assignment_context`, `model.message`,
`model.messages_snapshot`, `model.model_call_failure`,
`model.model_call_started`, `model.model_call_success`, `model.response`,
`model.turn_ended`, `model.turn_failed`, `model.turn_started`,
`session.usage_checkpoint`, and `subagent.configured`. Later version-1 captures
(observed through `1.0.94-3`) add `model.telemetry`, `model.turn_retry`, and
`session.usage_record`, recognized the same way, plus the schema-defined
`session.context_cleared` and `session.mode_notice_delivered`, which are
validated and projected as listed above. Their envelopes still
undergo identity, timestamp, size, and object-payload checks.
Their bodies are not projected, since request/response snapshots would
duplicate canonical messages and may include privileged runtime input.
Other `model.*` names follow the unrecognized-event policy above.

Model traces may have independent roots and links to trace events not persisted
in the conversation log. Sub-agent events may be appended out of parent order.
Main-conversation ancestry is checked through skipped trace/sub-agent nodes,
and sub-agent chains must attach to an existing mainline ancestor without
cycles. Missing mainline ancestors, mainline forks, invalid parent IDs, and
out-of-order mainline ancestry still fail closed.

Concurrent Copilot writers can log an event a few milliseconds earlier than the
one before it (observed after `subagent.started` and `subagent.completed`). File
order is authoritative and no records are reordered: such an event takes the
preceding timestamp, so every target still sees non-decreasing times, and is
counted as `copilot_timestamp_out_of_order`.

For `--since-last-compaction`, the last successful compaction with readable
`summaryContent` is retained along with subsequent history. A later failed
compaction does not replace that boundary. Codex receives the summary as a
plaintext `compacted.message` record.

Unrecognized event names, schema versions, or incompatible portable payloads
are rejected rather than guessed. The writer and native-validation pin remain
1.0.70; newer source-version warnings are still emitted. Updating that pin
requires a new distributed-schema diff, sanitized native fixture, and
exact-binary test.

## Primary references

- [About GitHub Copilot CLI session data](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/chronicle)
- [Copilot CLI configuration directory](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference)
- [Using GitHub Copilot CLI session data](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/chronicle)
- [Copilot CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)
- [Copilot CLI v1.0.70 tag](https://github.com/github/copilot-cli/releases/tag/v1.0.70)
