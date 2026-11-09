import json
import uuid
from collections import Counter
from pathlib import Path

import pytest

from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    truncate_to_last_compaction,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import copilot
from session_migrate.jsonl import encode_jsonl
from session_migrate.model import AgentFormat, EventKind, Role

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "copilot-source-1.0.70"
EVENTS_FIXTURE = FIXTURE_ROOT / "copilot-source-native-events.jsonl"
WORKSPACE_FIXTURE = FIXTURE_ROOT / "copilot-source-workspace.yaml"
SESSION_ID = "33333333-3333-4333-8333-333333333333"
USER_IMAGE = "data:image/png;base64,c3ludGhldGljLWNvcGlsb3QtdXNlci1pbWFnZQ=="
TOOL_IMAGE = "data:image/png;base64,c3ludGhldGljLWNvcGlsb3QtdG9vbC1pbWFnZQ=="
DIAGNOSTIC_PAYLOADS = {
    "model.captured_assignment_context": {"assignmentContext": "DIAGNOSTIC_ONLY"},
    "model.message": {"message": {"role": "user", "content": "DIAGNOSTIC_ONLY"}},
    "model.messages_snapshot": {"messages": [{"role": "user", "content": "DIAGNOSTIC_ONLY"}]},
    "model.model_call_failure": {"requestMessages": "DIAGNOSTIC_ONLY", "turn": 1},
    "model.model_call_started": {"model": "fixture-model", "turn": 1},
    "model.model_call_success": {"requestMessages": "DIAGNOSTIC_ONLY", "turn": 1},
    "model.response": {"response": {"content": "DIAGNOSTIC_ONLY"}, "turn": 1},
    "model.telemetry": {"kind": "telemetry", "telemetry": {"event": "DIAGNOSTIC_ONLY"}},
    "model.turn_ended": {"model": "fixture-model", "turn": 1},
    "model.turn_failed": {"error": "DIAGNOSTIC_ONLY", "turn": 1},
    "model.turn_retry": {"kind": "turn_retry", "reason": "DIAGNOSTIC_ONLY", "turn": 1},
    "model.turn_started": {"model": "fixture-model", "turn": 1},
    "session.usage_checkpoint": {"totalNanoAiu": 1, "totalPremiumRequests": 1},
    "session.usage_record": {"usage": {"model": "DIAGNOSTIC_ONLY", "inputTokens": 1}},
    "subagent.configured": {"model": "fixture-model", "multiTurn": True},
}


def fixture_records() -> list[dict[str, object]]:
    return [json.loads(line) for line in EVENTS_FIXTURE.read_text().splitlines()]


def write_records(path: Path, records: list[dict[str, object]]) -> None:
    path.write_bytes(encode_jsonl(records))


def canonical_fixture(tmp_path: Path) -> Path:
    directory = tmp_path / SESSION_ID
    directory.mkdir()
    path = directory / "events.jsonl"
    path.write_bytes(EVENTS_FIXTURE.read_bytes())
    (directory / "workspace.yaml").write_bytes(WORKSPACE_FIXTURE.read_bytes())
    return path


def test_copilot_source_projects_native_messages_tools_media_and_compaction(
    tmp_path: Path,
) -> None:
    path = canonical_fixture(tmp_path)

    session = copilot.parse_session(path)
    by_kind = Counter(event.kind for event in session.events)
    messages = [event for event in session.events if event.kind == EventKind.MESSAGE]
    thinking = [event for event in session.events if event.kind == EventKind.THINKING]
    call = next(event for event in session.events if event.kind == EventKind.TOOL_CALL)
    result = next(event for event in session.events if event.kind == EventKind.TOOL_RESULT)
    images = [
        event.payload["image_url"]
        for event in session.events
        if event.kind == EventKind.CONTEXT and event.payload.get("block_type") == "image"
    ]
    opaque_reasons = {
        event.payload.get("reason") for event in session.events if event.kind == EventKind.OPAQUE
    }

    assert session.source_format == AgentFormat.COPILOT
    assert session.session_id == SESSION_ID
    assert session.cwd == Path("/synthetic/copilot-source/project")
    assert session.started_at == "2026-08-20T08:52:08.825Z"
    assert session.cli_version == "1.0.70"
    assert session.model == "fixture-model"
    assert session.model_provider == "github-copilot"
    assert session.title == "SYNTHETIC_COPILOT_SOURCE_TITLE"
    assert session.raw_record_count == 16
    assert len(session.source_sha256) == 64
    assert [(event.role, event.text) for event in messages] == [
        (Role.USER, "SYNTHETIC_COPILOT_SOURCE_USER"),
        (Role.ASSISTANT, "SYNTHETIC_COPILOT_SOURCE_ASSISTANT"),
    ]
    assert [event.text for event in thinking] == ["SYNTHETIC_COPILOT_SOURCE_THINKING"]
    assert call.tool_call_id == "call_copilot_source_probe"
    assert call.tool_name == "view"
    assert call.payload == {
        "input": {"path": "fixture.txt"},
        "namespace": "fixture-server",
    }
    assert result.tool_call_id == call.tool_call_id
    assert result.tool_name == "view"
    assert result.text == "SYNTHETIC_COPILOT_SOURCE_TOOL_RESULT"
    assert result.payload["content_blocks"] == [
        {"type": "text", "text": "SYNTHETIC_COPILOT_SOURCE_TOOL_RESULT"},
        {"type": "image", "image_url": TOOL_IMAGE},
    ]
    assert images == [USER_IMAGE]
    assert by_kind[EventKind.COMPACTION] == 1
    assert "copilot_user_transformed_content" in opaque_reasons
    assert "copilot_reasoning_opaque" in opaque_reasons
    assert "copilot_privileged_system_message" in opaque_reasons
    assert "copilot_subagent_scoped_event" in opaque_reasons
    assert "copilot_tool_structured_content" in opaque_reasons
    assert "copilot_tool_detailed_content" in opaque_reasons
    assert "SYNTHETIC_COPILOT_SOURCE_SUBAGENT" not in {event.text for event in session.events}


def test_copilot_source_uses_workspace_title_when_no_title_event(tmp_path: Path) -> None:
    records = fixture_records()
    title_index = next(i for i, row in enumerate(records) if row["type"] == "session.title_changed")
    removed = records.pop(title_index)
    records[title_index]["parentId"] = removed["parentId"]
    directory = tmp_path / SESSION_ID
    directory.mkdir()
    path = directory / "events.jsonl"
    write_records(path, records)
    (directory / "workspace.yaml").write_bytes(WORKSPACE_FIXTURE.read_bytes())

    assert copilot.parse_session(path).title == "SYNTHETIC_COPILOT_SOURCE_WORKSPACE_TITLE"


def test_copilot_source_can_be_rewritten_with_explicit_losses(tmp_path: Path) -> None:
    source = copilot.parse_session(canonical_fixture(tmp_path))

    data, losses = copilot.serialize(
        source,
        session_id="55555555-5555-4555-8555-555555555555",
        cwd=tmp_path,
        timestamp="2026-08-20T09:00:00Z",
    )
    output = tmp_path / "rewritten.jsonl"
    output.write_bytes(data)
    copilot.validate_native_bytes(data, "55555555-5555-4555-8555-555555555555")
    reparsed = copilot.parse_session(output)

    assert any(event.text == "SYNTHETIC_COPILOT_SOURCE_USER" for event in reparsed.events)
    assert any(event.text == "SYNTHETIC_COPILOT_SOURCE_ASSISTANT" for event in reparsed.events)
    assert any(event.text == "SYNTHETIC_COPILOT_SOURCE_SUMMARY" for event in reparsed.events)
    assert any(event.payload.get("image_url") == USER_IMAGE for event in reparsed.events)
    assert losses["thinking"] == 1
    assert losses["tool_call:namespace"] == 1
    assert losses["opaque:copilot_privileged_system_message"] == 1
    assert losses["opaque:copilot_subagent_scoped_event"] == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda rows: rows[1].update(type="Future Unknown"), "event type is invalid"),
        (lambda rows: rows[1].update(type=None), "event type is invalid"),
        (lambda rows: rows[1].update(type="future.unknown", data=[]), "data must be an object"),
        (lambda rows: rows[1].update(type="future.unknown", ephemeral=True), "ephemeral event"),
        (lambda rows: rows[2].update(parentId=None), "parent chain"),
        (lambda rows: rows[0]["data"].update(version=2), "version is unsupported"),
        (lambda rows: rows[1].update(ephemeral=True), "ephemeral event"),
        (
            lambda rows: rows[7]["data"]["toolRequests"][0].update(name=""),
            "tool request is missing linkage",
        ),
        (
            lambda rows: rows[4]["data"].update(byteLength=999),
            "integrity check failed",
        ),
        (
            lambda rows: rows[5]["data"]["attachments"][0].update(byteLength=999),
            "metadata does not match",
        ),
        (
            lambda rows: rows[10]["data"]["result"]["contents"][1].update(data="!!!"),
            "tool content is not base64",
        ),
        (
            lambda rows: rows[2].update(
                type="session.context_cleared", data={"messagesCleared": True}
            ),
            "context clear count is invalid",
        ),
        (
            lambda rows: rows[2].update(
                type="session.context_cleared",
                data={"messagesCleared": 1, "initialMessage": 7},
            ),
            "context clear initial message is invalid",
        ),
        (
            lambda rows: rows[2].update(type="session.mode_notice_delivered", data={}),
            "mode notice is invalid",
        ),
        (
            lambda rows: rows[2].update(
                type="session.mode_notice_delivered", data={"mode": "plan", "content": 1}
            ),
            "mode notice content is invalid",
        ),
    ],
)
def test_copilot_source_fails_closed_on_malformed_native_data(
    tmp_path: Path, mutation: object, message: str
) -> None:
    records = fixture_records()
    mutation(records)  # type: ignore[operator]
    path = tmp_path / "copilot-source-malformed.jsonl"
    write_records(path, records)

    with pytest.raises(SessionMigrateError, match=message):
        copilot.parse_session(path)


def test_copilot_source_rejects_structural_depth_bomb(tmp_path: Path) -> None:
    records = fixture_records()
    value: dict[str, object] = {}
    for _ in range(70):
        value = {"next": value}
    records[-1]["data"]["nested"] = value
    path = tmp_path / "copilot-source-deep.jsonl"
    write_records(path, records)

    with pytest.raises(SessionMigrateError, match="structural safety"):
        copilot.parse_session(path)


def test_copilot_source_rejects_directory_identity_mismatch(tmp_path: Path) -> None:
    directory = tmp_path / "77777777-7777-4777-8777-777777777777"
    directory.mkdir()
    path = directory / "events.jsonl"
    path.write_bytes(EVENTS_FIXTURE.read_bytes())

    with pytest.raises(SessionMigrateError, match="directory does not match"):
        copilot.parse_session(path)


@pytest.mark.parametrize("record_type", DIAGNOSTIC_PAYLOADS)
def test_copilot_source_counts_new_diagnostics_without_replaying_payloads(
    tmp_path: Path, record_type: str
) -> None:
    records = fixture_records()
    records[0]["data"]["copilotVersion"] = "1.0.83-0"
    records[2].update(type=record_type, data=DIAGNOSTIC_PAYLOADS[record_type])
    path = tmp_path / "diagnostics.jsonl"
    write_records(path, records)

    session = copilot.parse_session(path)
    diagnostic = next(event for event in session.events if event.provenance.record_index == 2)

    assert diagnostic.kind == EventKind.OPAQUE
    assert diagnostic.text is None
    assert diagnostic.payload == {
        "reason": "copilot_diagnostic_event",
        "source_event_type": record_type,
    }
    assert [
        (event.role, event.text) for event in session.events if event.kind == EventKind.MESSAGE
    ] == [
        (Role.USER, "SYNTHETIC_COPILOT_SOURCE_USER"),
        (Role.ASSISTANT, "SYNTHETIC_COPILOT_SOURCE_ASSISTANT"),
    ]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"type": "model/future_event"}, "event type is invalid"),
        ({"data": []}, "data must be an object"),
        ({"ephemeral": True}, "ephemeral event"),
        ({"parentId": None}, "parent chain"),
        ({"timestamp": "yesterday"}, "timestamp is invalid"),
    ],
)
def test_copilot_diagnostic_support_keeps_source_guards(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    records = fixture_records()
    records[2].update(type="model.turn_started", data={"model": "fixture-model", "turn": 1})
    records[2].update(changes)
    path = tmp_path / "invalid-diagnostic.jsonl"
    write_records(path, records)

    with pytest.raises(SessionMigrateError, match=message):
        copilot.parse_session(path)


def test_copilot_source_accepts_detached_model_trace_chains(tmp_path: Path) -> None:
    records = fixture_records()
    previous = records[5]
    trace_start = {
        "type": "model.turn_started",
        "data": {"model": "fixture-model", "turn": 1},
        "id": str(uuid.uuid4()),
        "parentId": None,
        "timestamp": previous["timestamp"],
    }
    trace_call = {
        **trace_start,
        "type": "model.model_call_started",
        "id": str(uuid.uuid4()),
        "parentId": str(uuid.uuid4()),
    }
    records[6:6] = [trace_start, trace_call]
    path = tmp_path / "detached-traces.jsonl"
    write_records(path, records)

    session = copilot.parse_session(path)

    assert [event.text for event in session.events if event.kind == EventKind.MESSAGE] == [
        "SYNTHETIC_COPILOT_SOURCE_USER",
        "SYNTHETIC_COPILOT_SOURCE_ASSISTANT",
    ]
    assert (
        sum(event.payload.get("reason") == "copilot_diagnostic_event" for event in session.events)
        == 2
    )


@pytest.mark.parametrize("invalid_parent", [None, "missing", "cycle", "fork"])
def test_copilot_source_validates_main_ancestry_across_interleaved_subagent_events(
    tmp_path: Path, invalid_parent: str | None
) -> None:
    records = fixture_records()
    checkpoint_id = str(uuid.uuid4())
    subagent = {
        **records[13],
        "type": "subagent.started",
        "data": {"agentName": "fixture-agent"},
        "parentId": checkpoint_id,
    }
    checkpoint = {
        **records[14],
        "type": "session.usage_checkpoint",
        "data": {"totalNanoAiu": 1},
        "id": checkpoint_id,
        "parentId": records[12]["id"],
        "timestamp": subagent["timestamp"],
    }
    configured = {
        **subagent,
        "id": str(uuid.uuid4()),
        "type": "subagent.configured",
        "data": {"model": "fixture-model"},
        "parentId": subagent["id"],
    }
    records[14]["parentId"] = configured["id"]
    if invalid_parent == "missing":
        subagent["parentId"] = str(uuid.uuid4())
    elif invalid_parent == "cycle":
        subagent["parentId"] = configured["id"]
    elif invalid_parent == "fork":
        records[14]["parentId"] = records[12]["id"]
    records[13:14] = [subagent, checkpoint, configured]
    path = tmp_path / "interleaved-subagent.jsonl"
    write_records(path, records)

    if invalid_parent:
        with pytest.raises(SessionMigrateError, match="parent chain"):
            copilot.parse_session(path)
    else:
        session = copilot.parse_session(path)
        assert [event.text for event in session.events if event.kind == EventKind.MESSAGE] == [
            "SYNTHETIC_COPILOT_SOURCE_USER",
            "SYNTHETIC_COPILOT_SOURCE_ASSISTANT",
        ]


def test_new_copilot_source_to_codex_keeps_last_successful_summary_and_later_history(
    tmp_path: Path,
) -> None:
    records = fixture_records()
    records[0]["data"]["copilotVersion"] = "1.0.83-0"

    def append(record_type: str, data: dict[str, object]) -> None:
        records.append(
            {
                "type": record_type,
                "data": data,
                "id": str(uuid.uuid4()),
                "parentId": records[-1]["id"],
                "timestamp": "2026-08-20T09:00:00Z",
            }
        )

    for record_type, data in DIAGNOSTIC_PAYLOADS.items():
        append(record_type, data)
    append("user.message", {"content": "Continue from the summary"})
    append(
        "assistant.message",
        {
            "messageId": "recent-message",
            "content": "Reading the current file",
            "toolRequests": [
                {"toolCallId": "recent-call", "name": "view", "arguments": {"path": "recent.txt"}}
            ],
        },
    )
    append(
        "tool.execution_start",
        {"toolCallId": "recent-call", "toolName": "view", "arguments": {"path": "recent.txt"}},
    )
    append(
        "tool.execution_complete",
        {"toolCallId": "recent-call", "success": True, "result": {"content": "Recent file text"}},
    )
    append("session.compaction_complete", {"success": False})
    path = tmp_path / "recent-copilot.jsonl"
    write_records(path, records)

    trimmed, stats = truncate_to_last_compaction(copilot.parse_session(path))
    artifact = convert_session(
        trimmed, ConversionOptions(target_format=AgentFormat.CODEX, cwd=tmp_path)
    )
    output = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    summaries = [row["payload"] for row in output if row["type"] == "compacted"]
    items = [row["payload"] for row in output if row["type"] == "response_item"]

    assert stats["compaction_summary_carried"] == 1
    assert summaries == [{"message": "SYNTHETIC_COPILOT_SOURCE_SUMMARY"}]
    assert [item["content"][0]["text"] for item in items if item["type"] == "message"] == [
        "Continue from the summary",
        "Reading the current file",
    ]
    calls = [item for item in items if item["type"] == "function_call"]
    results = [item for item in items if item["type"] == "function_call_output"]
    assert len(calls) == len(results) == 1
    assert calls[0]["call_id"] == results[0]["call_id"] == "recent-call"
    assert json.loads(calls[0]["arguments"]) == {"path": "recent.txt"}
    assert results[0]["output"] == "Recent file text"
    assert b"DIAGNOSTIC_ONLY" not in artifact.native_bytes
    assert artifact.dropped["opaque:copilot_diagnostic_event"] == len(DIAGNOSTIC_PAYLOADS)
    assert artifact.dropped["opaque:copilot_compaction_without_summary"] == 1


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ({"mode": "autopilot"}, "copilot_native_lifecycle_or_ui_event"),
        (
            {"mode": "autopilot", "content": "SYNTHETIC_MODE_NOTICE"},
            "copilot_privileged_mode_notice",
        ),
    ],
)
def test_copilot_mode_notice_is_counted_without_replaying_notice_text(
    tmp_path: Path, data: dict[str, object], reason: str
) -> None:
    records = fixture_records()
    records[2].update(type="session.mode_notice_delivered", data=data)
    path = tmp_path / "mode-notice.jsonl"
    write_records(path, records)

    session = copilot.parse_session(path)
    notice = next(event for event in session.events if event.provenance.record_index == 2)
    artifact = convert_session(
        session, ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path)
    )

    assert notice.kind == EventKind.OPAQUE
    assert notice.payload["reason"] == reason
    assert b"SYNTHETIC_MODE_NOTICE" not in artifact.native_bytes
    assert artifact.dropped[f"opaque:{reason}"] >= 1


@pytest.mark.parametrize("initial_message", ["SYNTHETIC_ROLLOVER_PROMPT", None])
def test_copilot_context_clear_is_the_last_compaction_boundary(
    tmp_path: Path, initial_message: str | None
) -> None:
    records = fixture_records()

    def append(record_type: str, data: dict[str, object]) -> None:
        records.append(
            {
                "type": record_type,
                "data": data,
                "id": str(uuid.uuid4()),
                "parentId": records[-1]["id"],
                "timestamp": "2026-08-20T09:00:00Z",
            }
        )

    cleared: dict[str, object] = {"messagesCleared": 4}
    if initial_message:
        cleared["initialMessage"] = initial_message
    append("session.context_cleared", cleared)
    append(
        "user.message",
        {
            "content": "",
            "transformedContent": "SYNTHETIC_ROLLOVER_PROMPT",
            "source": "session-memory-rollover",
        },
    )
    append("assistant.message", {"messageId": "after-clear", "content": "Resuming after clear"})
    append("user.message", {"content": "What next?"})
    path = tmp_path / "context-cleared.jsonl"
    write_records(path, records)

    trimmed, stats = truncate_to_last_compaction(copilot.parse_session(path))
    artifact = convert_session(
        trimmed, ConversionOptions(target_format=AgentFormat.COPILOT, cwd=tmp_path)
    )
    output = [json.loads(line) for line in artifact.native_bytes.splitlines()]

    assert stats["events_before_boundary"] > 0
    assert stats.get("compaction_summary_carried") == (1 if initial_message else None)
    assert [event.text for event in trimmed.events if event.kind == EventKind.COMPACTION] == [
        initial_message
    ]
    assert [event.text for event in trimmed.events if event.kind == EventKind.MESSAGE] == [
        "Resuming after clear",
        "What next?",
    ]
    assert [
        row["data"]["summaryContent"]
        for row in output
        if row["type"] == "session.compaction_complete"
    ] == ([initial_message] if initial_message else [])


def test_copilot_task_complete_summary_is_the_final_assistant_reply(tmp_path: Path) -> None:
    records = fixture_records()
    summary = "SYNTHETIC_TASK_COMPLETE_SUMMARY"

    def append(record_type: str, data: dict[str, object]) -> None:
        records.append(
            {
                "type": record_type,
                "data": data,
                "id": str(uuid.uuid4()),
                "parentId": records[-1]["id"],
                "timestamp": "2026-08-20T09:00:00Z",
            }
        )

    request = {
        "toolCallId": "done-call",
        "name": "task_complete",
        "arguments": {"summary": summary},
    }
    append("assistant.message", {"messageId": "done", "content": "", "toolRequests": [request]})
    append(
        "tool.execution_start",
        {"toolCallId": "done-call", "toolName": "task_complete", "arguments": {"summary": summary}},
    )
    append(
        "tool.execution_complete",
        {"toolCallId": "done-call", "success": True, "result": {"content": summary}},
    )
    append("session.task_complete", {"summary": summary, "success": True})
    path = tmp_path / "task-complete.jsonl"
    write_records(path, records)

    artifact = convert_session(
        copilot.parse_session(path),
        ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path),
    )
    output = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    messages = [row["message"] for row in output if "message" in row]
    blocks = [
        block
        for message in messages
        if isinstance(message["content"], list)
        for block in message["content"]
    ]

    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == [{"type": "text", "text": summary}]
    assert not any(block.get("name") == "task_complete" for block in blocks)
    assert not any(block.get("tool_use_id") == "done-call" for block in blocks)
    assert artifact.dropped["opaque:copilot_task_complete_tool"] == 1


def test_copilot_unrecognized_events_are_counted_by_name_without_replay(
    tmp_path: Path,
) -> None:
    records = fixture_records()
    records[2].update(type="instruction.update", data={"content": "UNRECOGNIZED_PAYLOAD"})
    detached = {
        **records[6],
        "type": "future.trace_event",
        "data": {"content": "UNRECOGNIZED_PAYLOAD"},
        "id": str(uuid.uuid4()),
        "parentId": str(uuid.uuid4()),
        "timestamp": records[5]["timestamp"],
    }
    records.insert(6, detached)
    path = tmp_path / "unrecognized.jsonl"
    write_records(path, records)

    session = copilot.parse_session(path)
    artifact = convert_session(
        session, ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path)
    )
    warnings = {
        warning["event_kind"]: warning["message"]
        for warning in artifact.warnings
        if warning["code"] == "dropped_event_kind"
    }

    assert [event.text for event in session.events if event.kind == EventKind.MESSAGE] == [
        "SYNTHETIC_COPILOT_SOURCE_USER",
        "SYNTHETIC_COPILOT_SOURCE_ASSISTANT",
    ]
    assert b"UNRECOGNIZED_PAYLOAD" not in artifact.native_bytes
    for name in ("instruction.update", "future.trace_event"):
        assert artifact.dropped[f"opaque:copilot_unrecognized_event:{name}"] == 1
        assert (
            "newer than session-migrate recognizes"
            in warnings[f"opaque:copilot_unrecognized_event:{name}"]
        )


def test_copilot_custom_tool_text_arguments_are_kept_as_tool_input(tmp_path: Path) -> None:
    patch = "*** Begin Patch\n*** Update File: fixture.txt\n@@\n-old\n+new\n*** End Patch"
    records = fixture_records()
    records[7]["data"]["toolRequests"][0].update(name="apply_patch", arguments=patch)
    records[8]["data"].update(toolName="apply_patch", arguments=patch)
    path = tmp_path / "custom-tool.jsonl"
    write_records(path, records)

    session = copilot.parse_session(path)
    call = next(event for event in session.events if event.kind == EventKind.TOOL_CALL)
    artifact = convert_session(
        session, ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path)
    )
    output = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    tool_uses = [
        block
        for row in output
        if isinstance(row.get("message", {}).get("content"), list)
        for block in row["message"]["content"]
        if block.get("type") == "tool_use"
    ]

    assert (call.tool_name, call.payload["input"]) == ("apply_patch", patch)
    assert [block["input"] for block in tool_uses] == [{"input": patch}]
    assert artifact.dropped["tool_call:non_object_input"] == 1


@pytest.mark.parametrize("target", [AgentFormat.CLAUDE, AgentFormat.COPILOT, AgentFormat.HERMES])
def test_copilot_out_of_order_timestamp_keeps_file_order_and_holds_clock(
    tmp_path: Path, target: AgentFormat
) -> None:
    records = fixture_records()
    # Copilot's concurrent writers (e.g. sub-agent lifecycle) log a few ms early.
    records[11]["timestamp"] = "2026-08-20T08:52:08.830Z"
    path = tmp_path / "out-of-order.jsonl"
    write_records(path, records)

    session = copilot.parse_session(path)
    final = next(
        event
        for event in session.events
        if event.provenance.record_index == 11 and event.kind == EventKind.MESSAGE
    )
    artifact = convert_session(session, ConversionOptions(target_format=target, cwd=tmp_path))

    assert final.text == "SYNTHETIC_COPILOT_SOURCE_ASSISTANT"
    assert final.timestamp == records[10]["timestamp"]
    assert [
        event.provenance.record_index
        for event in session.events
        if event.payload.get("reason") == "copilot_timestamp_out_of_order"
    ] == [11]
    assert artifact.native_bytes


@pytest.mark.parametrize("boundary", ["fixture", "between_call_and_result"])
def test_copilot_window_load_matches_a_full_parse_cut_at_the_same_compaction(
    tmp_path: Path, boundary: str
) -> None:
    records = fixture_records()
    if boundary == "between_call_and_result":
        compaction = {
            "type": "session.compaction_complete",
            "data": {"success": True, "summaryContent": "SYNTHETIC_EARLY_SUMMARY"},
            "id": str(uuid.uuid4()),
            "parentId": records[8]["id"],
            "timestamp": records[8]["timestamp"],
        }
        records[9]["parentId"] = compaction["id"]
        records.insert(9, compaction)
        # Disable the fixture's later compaction so the window starts at this one.
        records[13]["data"]["success"] = False
    path = tmp_path / "events.jsonl"
    write_records(path, records)

    def signature(session: object) -> list[tuple[object, ...]]:
        return [
            (
                event.kind,
                event.role,
                event.text,
                event.tool_call_id,
                event.tool_name,
                event.timestamp,
            )
            for event in truncate_to_last_compaction(session)[0].events  # type: ignore[arg-type]
        ]

    full = signature(copilot.parse_session(path))
    window = copilot.parse_session(path, since_last_compaction=True)

    assert signature(window) == full
    assert window.records_before_window > 0
    if boundary == "between_call_and_result":
        assert ("view", "call_copilot_source_probe") in {
            (event.tool_name, event.tool_call_id)
            for event in window.events
            if event.kind == EventKind.TOOL_RESULT
        }


def test_copilot_window_load_still_validates_the_whole_log(tmp_path: Path) -> None:
    records = fixture_records()
    records[3]["parentId"] = str(uuid.uuid4())
    path = tmp_path / "events.jsonl"
    write_records(path, records)

    with pytest.raises(SessionMigrateError, match="parent chain"):
        copilot.parse_session(path, since_last_compaction=True)
