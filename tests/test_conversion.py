import json
from pathlib import Path

import pytest

from session_migrate import conversion
from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    load_session,
    target_import_paths,
    write_artifact,
)
from session_migrate.errors import JsonlError, SessionMigrateError
from session_migrate.formats import claude, codex
from session_migrate.model import AgentFormat, EventKind, Role, Session

TARGET_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def write_jsonl(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def claude_record(
    record_type: str,
    record_uuid: str,
    parent_uuid: str | None,
    content: object,
    *,
    cwd: str,
) -> dict[str, object]:
    return {
        "type": record_type,
        "uuid": record_uuid,
        "parentUuid": parent_uuid,
        "sessionId": "11111111-1111-4111-8111-111111111111",
        "timestamp": "2026-08-17T12:00:00Z",
        "cwd": cwd,
        "version": "2.1.209",
        "message": {"role": record_type, "content": content},
    }


def semantic_signature(session: Session, *, omit: set[EventKind] | None = None) -> list[tuple]:
    omitted = omit or set()
    signature: list[tuple] = []
    for event in session.events:
        if event.kind in omitted:
            continue
        portable_payload: object = None
        if event.kind == EventKind.TOOL_CALL:
            portable_payload = event.payload.get("input")
        elif event.kind == EventKind.TOOL_RESULT:
            portable_payload = event.payload.get("content_blocks")
        elif event.kind == EventKind.CONTEXT:
            portable_payload = {
                "block_type": event.payload.get("block_type"),
                "image_url": event.payload.get("image_url"),
            }
        signature.append(
            (
                event.kind,
                event.role,
                event.text,
                event.tool_name,
                event.tool_call_id,
                json.dumps(portable_payload, sort_keys=True),
            )
        )
    return signature


def test_claude_parser_uses_last_prompt_leaf(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    path = write_jsonl(
        tmp_path / "claude.jsonl",
        [
            claude_record("user", "u1", None, "root", cwd=cwd),
            claude_record("assistant", "a1", "u1", "answer", cwd=cwd),
            claude_record("user", "active", "a1", "active branch", cwd=cwd),
            claude_record("user", "abandoned", "a1", "abandoned branch", cwd=cwd),
            {"type": "last-prompt", "leafUuid": "active"},
        ],
    )

    session = claude.parse(path)

    messages = [event.text for event in session.events if event.kind == EventKind.MESSAGE]
    assert messages == ["root", "answer", "active branch"]
    assert session.event_counts()["opaque"] == 1


def test_claude_parser_orders_child_after_physical_parent(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    path = write_jsonl(
        tmp_path / "claude-out-of-order.jsonl",
        [
            claude_record("user", "u1", None, "run it", cwd=cwd),
            claude_record(
                "user",
                "u2",
                "a1",
                [{"type": "tool_result", "tool_use_id": "tool-1", "content": "done"}],
                cwd=cwd,
            ),
            claude_record(
                "assistant",
                "a1",
                "u1",
                [{"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}}],
                cwd=cwd,
            ),
            {"type": "last-prompt", "leafUuid": "u2"},
        ],
    )

    session = claude.parse(path)

    assert [event.kind for event in session.events] == [
        EventKind.MESSAGE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
    ]


def test_claude_active_meta_record_is_not_replayed_as_user_history(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    metadata = claude_record("user", "meta", "u1", "internal metadata", cwd=cwd)
    metadata["isMeta"] = True
    path = write_jsonl(
        tmp_path / "claude-meta-ancestor.jsonl",
        [
            claude_record("user", "u1", None, "actual request", cwd=cwd),
            metadata,
            claude_record("assistant", "a1", "meta", "actual answer", cwd=cwd),
            {"type": "last-prompt", "leafUuid": "a1"},
        ],
    )

    session = claude.parse(path)

    assert [event.text for event in session.events if event.kind == EventKind.MESSAGE] == [
        "actual request",
        "actual answer",
    ]
    artifact = convert_session(
        session,
        ConversionOptions(target_format=AgentFormat.CODEX, cwd=tmp_path),
    )
    assert artifact.dropped == {"opaque:active_graph_metadata_record": 1}


def test_claude_non_message_leaf_does_not_merge_abandoned_branch(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    path = write_jsonl(
        tmp_path / "claude-boundary-leaf.jsonl",
        [
            claude_record("user", "u1", None, "root", cwd=cwd),
            claude_record("assistant", "a1", "u1", "answer", cwd=cwd),
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "boundary",
                "parentUuid": None,
                "logicalParentUuid": "a1",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-08-17T12:00:02Z",
                "cwd": cwd,
                "version": "2.1.209",
            },
            claude_record("user", "abandoned", "a1", "not active", cwd=cwd),
            {"type": "last-prompt", "leafUuid": "boundary"},
        ],
    )

    session = claude.parse(path)

    assert [event.text for event in session.events if event.kind == EventKind.MESSAGE] == [
        "root",
        "answer",
    ]
    assert session.event_counts() == {"compaction": 1, "message": 2, "opaque": 1}


def test_claude_compaction_pair_maps_once_and_keeps_mainline(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    summary = claude_record("user", "summary", "boundary", "summary text", cwd=cwd)
    summary["isCompactSummary"] = True
    summary["isVisibleInTranscriptOnly"] = True
    path = write_jsonl(
        tmp_path / "claude-compaction.jsonl",
        [
            claude_record("user", "u1", None, "root", cwd=cwd),
            claude_record("assistant", "a1", "u1", "answer", cwd=cwd),
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "boundary",
                "parentUuid": None,
                "logicalParentUuid": "a1",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-08-17T12:00:02Z",
                "cwd": cwd,
                "version": "2.1.209",
            },
            summary,
            claude_record("assistant", "a2", "summary", "after summary", cwd=cwd),
            claude_record("user", "abandoned", "a1", "not active", cwd=cwd),
            {"type": "last-prompt", "leafUuid": "a2"},
        ],
    )

    source = claude.parse(path)
    assert [event.text for event in source.events if event.kind != EventKind.OPAQUE] == [
        "root",
        "answer",
        "summary text",
        "after summary",
    ]
    artifact = convert_session(
        source,
        ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    assert sum(record["type"] == "compacted" for record in records) == 1
    assert artifact.dropped == {"opaque:inactive_or_metadata_conversation_record": 1}


def test_claude_compaction_logical_back_edge_terminates_cycle(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    summary = claude_record("user", "summary", "boundary", "summary text", cwd=cwd)
    summary["isCompactSummary"] = True
    summary["isVisibleInTranscriptOnly"] = True
    path = write_jsonl(
        tmp_path / "claude-compaction-back-edge.jsonl",
        [
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "boundary",
                "parentUuid": None,
                "logicalParentUuid": "u2",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-08-17T12:00:00Z",
                "cwd": cwd,
                "version": "2.1.209",
                "compactMetadata": {
                    "preservedSegment": {
                        "anchorUuid": "summary",
                        "headUuid": "a1",
                        "tailUuid": "u2",
                    },
                    "preservedMessages": {
                        "uuids": ["a1", "u2"],
                        "allUuids": ["a1", "u2"],
                    },
                },
            },
            summary,
            claude_record("assistant", "a1", "summary", "after summary", cwd=cwd),
            claude_record("user", "u2", "a1", "continue", cwd=cwd),
            claude_record("assistant", "a2", "u2", "complete", cwd=cwd),
            {"type": "last-prompt", "leafUuid": "a2"},
        ],
    )

    session = claude.parse(path)

    assert [event.text for event in session.events] == [
        "summary text",
        "after summary",
        "continue",
        "complete",
    ]


def preserved_compaction(tmp_path: Path, layout: str, preserved: list[str] | None = None) -> Path:
    """Precomputed compaction that keeps recent turns where they were first written."""

    cwd = str(tmp_path)
    tail_after_summary = layout == "tail_after_summary"
    uuids = preserved or (["u1", "a1", "tail"] if tail_after_summary else ["u1", "a1"])
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": "boundary",
        "parentUuid": None,
        "logicalParentUuid": uuids[-1],
        "sessionId": "11111111-1111-4111-8111-111111111111",
        "timestamp": "2026-08-17T12:00:00Z",
        "cwd": cwd,
        "version": "2.1.304",
        "compactMetadata": {
            "trigger": "auto",
            "precomputed": True,
            "preservedSegment": {"anchorUuid": "summary", "headUuid": "u1", "tailUuid": uuids[-1]},
            "preservedMessages": {
                "anchorUuid": "summary",
                "uuids": uuids,
                "allUuids": [*uuids, "never-written"],
            },
        },
    }
    attachment = {**boundary, "type": "attachment", "uuid": "attachment", "parentUuid": "boundary"}
    del attachment["subtype"], attachment["logicalParentUuid"], attachment["compactMetadata"]
    summary = claude_record(
        "user", "summary", "attachment" if tail_after_summary else "boundary", "summary", cwd=cwd
    )
    summary["isCompactSummary"] = True
    records = [
        claude_record("user", "u0", None, "old question", cwd=cwd),
        claude_record("assistant", "a0", "u0", "old answer", cwd=cwd),
        claude_record("user", "u1", "a0", "recent question", cwd=cwd),
        claude_record("assistant", "a1", "u1", "recent answer", cwd=cwd),
        boundary,
        *([attachment] if tail_after_summary else []),
        summary,
    ]
    if tail_after_summary:
        records.append({**attachment, "uuid": "tail", "parentUuid": "summary"})
    records += [
        claude_record("assistant", "a2", records[-1]["uuid"], "after", cwd=cwd),
        {"type": "last-prompt", "leafUuid": "a2"},
    ]
    return write_jsonl(tmp_path / f"claude-{layout}.jsonl", records)


@pytest.mark.parametrize("layout", ["tail_after_summary", "logical_parent_is_tail"])
def test_claude_preserved_segment_is_relinked_after_its_summary(
    tmp_path: Path, layout: str
) -> None:
    """Claude Code chains preserved turns after the summary when it loads them."""

    session = claude.parse(preserved_compaction(tmp_path, layout))
    trimmed, stats = conversion.truncate_to_last_compaction(session)

    def texts(events: tuple) -> list[str]:
        return [event.text for event in events if event.text]

    assert texts(session.events) == [
        "old question",
        "old answer",
        "summary",
        "recent question",
        "recent answer",
        "after",
    ]
    assert texts(trimmed.events) == ["summary", "recent question", "recent answer", "after"]
    assert stats["compaction_summary_carried"] == 1


def test_claude_missing_preserved_message_skips_relink_like_claude(tmp_path: Path) -> None:
    path = preserved_compaction(tmp_path, "tail_after_summary", ["u1", "lost", "tail"])

    session = claude.parse(path)

    assert [event.text for event in session.events if event.text] == ["summary", "after"]


def test_claude_to_codex_preserves_messages_and_tools(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    source_path = write_jsonl(
        tmp_path / "claude.jsonl",
        [
            claude_record("user", "u1", None, "run it", cwd=cwd),
            claude_record(
                "assistant",
                "a1",
                "u1",
                [
                    {"type": "text", "text": "running"},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Shell",
                        "input": {"command": "pwd"},
                    },
                ],
                cwd=cwd,
            ),
            claude_record(
                "user",
                "u2",
                "a1",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "/work",
                    }
                ],
                cwd=cwd,
            ),
            claude_record("assistant", "a2", "u2", "done", cwd=cwd),
        ],
    )
    source = load_session(source_path)

    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=AgentFormat.CODEX,
            session_id=TARGET_ID,
            cwd=tmp_path,
        ),
    )
    target_path = tmp_path / "target.jsonl"
    target_path.write_bytes(artifact.native_bytes)
    reparsed = codex.parse(target_path)

    assert artifact.native_record_count == 9
    assert artifact.dropped == {}
    assert [event.kind for event in reparsed.events] == [
        EventKind.MESSAGE,
        EventKind.MESSAGE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.MESSAGE,
    ]
    call = reparsed.events[2]
    assert call.tool_name == "Shell"
    assert call.tool_call_id == "tool-1"
    assert call.payload["input"] == {"command": "pwd"}


def test_codex_to_claude_creates_linear_native_graph(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "codex.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "session_id": "22222222-2222-4222-8222-222222222222",
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path),
                    "originator": "codex",
                    "cli_version": "0.144.4",
                    "source": "cli",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hi"}],
                },
            },
        ],
    )
    source = codex.parse(source_path)

    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=AgentFormat.CLAUDE,
            session_id=TARGET_ID,
            cwd=tmp_path,
        ),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]

    assert [record["type"] for record in records] == ["user", "assistant"]
    assert records[0]["parentUuid"] is None
    assert records[1]["parentUuid"] == records[0]["uuid"]
    assert all(record["sessionId"] == TARGET_ID for record in records)


def test_codex_developer_messages_are_not_converted_to_user_prompts(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "codex-roles.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path),
                    "originator": "codex",
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "privileged instructions"}],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "actual request"}],
                },
            },
        ],
    )

    source = codex.parse(source_path)
    assert [event.role for event in source.events] == [Role.SYSTEM, Role.USER]
    artifact = convert_session(
        source,
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    assert [record["message"]["content"] for record in records] == ["actual request"]
    assert artifact.dropped == {"message:privileged_role": 1}


def test_codex_developer_images_are_not_converted_to_user_images(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "codex-developer-image.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path),
                    "originator": "codex",
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "actual request"}],
                },
            },
        ],
    )

    artifact = convert_session(
        codex.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    assert [record["message"]["content"] for record in records] == ["actual request"]
    assert artifact.dropped == {"context:privileged_image": 1}


@pytest.mark.parametrize("field", ["thread_name", "name"])
def test_codex_parser_reads_current_and_legacy_thread_name_fields(
    tmp_path: Path, field: str
) -> None:
    source = write_jsonl(
        tmp_path / f"codex-title-{field}.jsonl",
        [
            {
                "timestamp": "2026-08-18T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "timestamp": "2026-08-18T12:00:00Z",
                    "cwd": str(tmp_path),
                    "cli_version": "0.147.0",
                    "history_mode": "legacy",
                },
            },
            {
                "timestamp": "2026-08-18T12:00:01Z",
                "type": "event_msg",
                "payload": {"type": "thread_name_updated", field: "Synthetic title"},
            },
            {
                "timestamp": "2026-08-18T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "question"}],
                },
            },
        ],
    )

    assert codex.parse(source).title == "Synthetic title"


def test_import_paths_are_native_and_manifest_is_private(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "codex.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path / "a.b_c-d e"),
                    "originator": "codex",
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            },
        ],
    )
    artifact = convert_session(
        codex.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID),
    )
    native_path, manifest_path = target_import_paths(artifact, tmp_path / "claude-home")

    assert native_path.name == f"{TARGET_ID}.jsonl"
    assert "a-b-c-d-e" in native_path.parent.name
    write_artifact(artifact, output_path=native_path, manifest_path=manifest_path)
    assert native_path.exists()
    assert manifest_path.exists()
    assert native_path.stat().st_mode & 0o777 == 0o600
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    manifest = json.loads(manifest_path.read_text())
    assert manifest["source"]["sha256"] == artifact.source.source_sha256
    assert manifest["target"]["sha256"] == artifact.target_sha256


def test_images_map_through_portable_data_urls(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    source_path = write_jsonl(
        tmp_path / "claude-image.jsonl",
        [
            claude_record(
                "user",
                "u1",
                None,
                [
                    {"type": "text", "text": "inspect this"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "c3ludGhldGlj",
                        },
                    },
                ],
                cwd=cwd,
            )
        ],
    )

    codex_artifact = convert_session(
        claude.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
    )
    codex_records = [json.loads(line) for line in codex_artifact.native_bytes.splitlines()]
    image_item = next(
        record
        for record in codex_records
        if record["type"] == "response_item"
        and record["payload"].get("content", [{}])[0].get("type") == "input_image"
    )
    assert image_item["payload"]["content"][0]["image_url"] == (
        "data:image/png;base64,c3ludGhldGlj"
    )
    assert codex_artifact.dropped == {}

    codex_path = tmp_path / "codex-image.jsonl"
    codex_path.write_bytes(codex_artifact.native_bytes)
    claude_artifact = convert_session(
        codex.parse(codex_path),
        ConversionOptions(
            target_format=AgentFormat.CLAUDE,
            session_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            cwd=tmp_path,
        ),
    )
    claude_records = [json.loads(line) for line in claude_artifact.native_bytes.splitlines()]
    image_block = next(
        block
        for record in claude_records
        for block in (
            record["message"]["content"] if isinstance(record["message"]["content"], list) else []
        )
        if block.get("type") == "image"
    )
    assert image_block["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "c3ludGhldGlj",
    }
    assert claude_artifact.dropped == {}


def test_structured_tool_results_preserve_text_and_images(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    source_path = write_jsonl(
        tmp_path / "claude-tool-result.jsonl",
        [
            claude_record(
                "assistant",
                "a1",
                None,
                [{"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}}],
                cwd=cwd,
            ),
            claude_record(
                "user",
                "u1",
                "a1",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": [
                            {"type": "text", "text": "result"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": "c3ludGhldGlj",
                                },
                            },
                            {"type": "tool_reference", "tool_name": "Read"},
                        ],
                    }
                ],
                cwd=cwd,
            ),
        ],
    )

    artifact = convert_session(
        claude.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    output = next(
        record["payload"]["output"]
        for record in records
        if record["type"] == "response_item"
        and record["payload"].get("type") == "function_call_output"
    )
    assert output == [
        {"type": "input_text", "text": "result"},
        {
            "type": "input_image",
            "image_url": "data:image/jpeg;base64,c3ludGhldGlj",
        },
    ]
    assert artifact.dropped == {"tool_result:tool_reference": 1}


def test_missing_tool_ids_are_linked_and_reported(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    source_path = write_jsonl(
        tmp_path / "claude-missing-tool-id.jsonl",
        [
            claude_record(
                "assistant",
                "a1",
                None,
                [{"type": "tool_use", "name": "Read", "input": {}}],
                cwd=cwd,
            ),
            claude_record(
                "user",
                "u1",
                "a1",
                [{"type": "tool_result", "content": "result"}],
                cwd=cwd,
            ),
        ],
    )

    artifact = convert_session(
        claude.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
    )
    response_items = [
        json.loads(line)["payload"]
        for line in artifact.native_bytes.splitlines()
        if json.loads(line)["type"] == "response_item"
    ]
    call = next(item for item in response_items if item["type"] == "function_call")
    output = next(item for item in response_items if item["type"] == "function_call_output")
    assert call["call_id"] == output["call_id"]
    assert artifact.dropped == {"tool_call:missing_id": 1, "tool_result:missing_id": 1}


def test_orphan_and_duplicate_tool_ids_are_reported_in_both_directions(
    tmp_path: Path,
) -> None:
    cwd = str(tmp_path)
    claude_source = write_jsonl(
        tmp_path / "claude-invalid-tool-links.jsonl",
        [
            claude_record("user", "u0", None, "start", cwd=cwd),
            claude_record(
                "assistant",
                "a1",
                "u0",
                [
                    {"type": "tool_use", "id": "duplicate", "name": "Read", "input": {}},
                    {"type": "tool_use", "id": "duplicate", "name": "Read", "input": {}},
                ],
                cwd=cwd,
            ),
            claude_record(
                "user",
                "u1",
                "a1",
                [
                    {"type": "tool_result", "tool_use_id": "orphan", "content": "one"},
                    {"type": "tool_result", "tool_use_id": "orphan", "content": "two"},
                ],
                cwd=cwd,
            ),
        ],
    )
    claude_artifact = convert_session(
        claude.parse(claude_source),
        ConversionOptions(target_format=AgentFormat.CODEX, cwd=tmp_path),
    )
    assert claude_artifact.dropped == {
        "tool_call:duplicate_id": 1,
        "tool_result:duplicate_id": 1,
        "tool_result:orphan_id": 2,
    }
    orphan_warning = next(
        warning
        for warning in claude_artifact.warnings
        if warning.get("event_kind") == "tool_result:orphan_id"
    )
    assert "record was retained" in orphan_warning["message"]

    codex_source = write_jsonl(
        tmp_path / "codex-invalid-tool-links.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "cwd": cwd,
                    "cli_version": "0.144.4",
                },
            },
            *[
                {
                    "timestamp": "2026-08-17T12:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "Read",
                        "arguments": "{}",
                        "call_id": "duplicate",
                    },
                }
                for _ in range(2)
            ],
            *[
                {
                    "timestamp": "2026-08-17T12:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "orphan",
                        "output": output,
                    },
                }
                for output in ("one", "two")
            ],
        ],
    )
    codex_artifact = convert_session(
        codex.parse(codex_source),
        ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path),
    )
    assert codex_artifact.dropped == {
        "tool_call:duplicate_id": 1,
        "tool_result:duplicate_id": 1,
        "tool_result:orphan_id": 2,
    }


def test_metadata_only_session_has_specific_empty_history_error(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "metadata-only.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "cwd": str(tmp_path),
                    "cli_version": "0.144.4",
                },
            }
        ],
    )

    with pytest.raises(SessionMigrateError, match="no resumable conversation history"):
        convert_session(
            codex.parse(path),
            ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path),
        )


def test_invalid_event_timestamp_falls_back_and_is_reported(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    first = claude_record("user", "u1", None, "hello", cwd=cwd)
    second = claude_record("assistant", "a1", "u1", "hi", cwd=cwd)
    second["timestamp"] = "not-a-timestamp"
    source_path = write_jsonl(tmp_path / "claude-time.jsonl", [first, second])

    artifact = convert_session(
        claude.parse(source_path),
        ConversionOptions(
            target_format=AgentFormat.CODEX,
            session_id=TARGET_ID,
            cwd=tmp_path,
            target_cli_version="9.9.9",
        ),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    assert all(record["timestamp"] != "not-a-timestamp" for record in records)
    assert artifact.dropped == {"timestamp:invalid": 1}
    assert any(warning["code"] == "unvalidated_target_version" for warning in artifact.warnings)


def test_load_session_rejects_source_change_during_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_jsonl(
        tmp_path / "changing.jsonl",
        [claude_record("user", "u1", None, "hello", cwd=str(tmp_path))],
    )
    original_parse = claude.parse

    def parse_then_append(source_path: Path, **options: bool) -> Session:
        session = original_parse(source_path, **options)
        with source_path.open("a") as stream:
            stream.write("{}\n")
        return session

    monkeypatch.setattr(claude, "parse", parse_then_append)

    with pytest.raises(JsonlError, match="source session changed"):
        load_session(path, AgentFormat.CLAUDE)


def test_rejects_unknown_history_mode_and_expands_replacement_history(tmp_path: Path) -> None:
    base_meta = {
        "timestamp": "2026-08-17T12:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": "22222222-2222-4222-8222-222222222222",
            "timestamp": "2026-08-17T12:00:00Z",
            "cwd": str(tmp_path),
            "cli_version": "0.144.4",
            "model_provider": "openai",
        },
    }
    unknown = json.loads(json.dumps(base_meta))
    unknown["payload"]["history_mode"] = "sharded"
    unknown_path = write_jsonl(tmp_path / "unknown.jsonl", [unknown])
    with pytest.raises(SessionMigrateError, match="history mode"):
        codex.parse(unknown_path)

    lineage = json.loads(json.dumps(base_meta))
    lineage["payload"]["history_mode"] = "paginated"
    lineage["payload"]["history_base"] = {"thread_id": "33333333-3333-4333-8333-333333333333"}
    lineage_path = write_jsonl(tmp_path / "lineage.jsonl", [lineage])
    with pytest.raises(SessionMigrateError, match="history_base is malformed"):
        codex.parse(lineage_path)

    replacement_path = write_jsonl(
        tmp_path / "replacement.jsonl",
        [
            base_meta,
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "visible history"}],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "event_msg",
                "payload": {"type": "context_compacted"},
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "compacted",
                "payload": {
                    "message": "summary",
                    "replacement_history": [
                        {
                            "type": "compaction",
                            "encrypted_content": "opaque-provider-state",
                        }
                    ],
                },
            },
        ],
    )
    session = codex.parse(replacement_path)
    assert [event.kind for event in session.events] == [
        EventKind.MESSAGE,
        EventKind.COMPACTION,
    ]
    artifact = convert_session(
        session,
        ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path),
    )
    assert artifact.dropped == {"compaction:replacement_history_expanded": 1}
    assert "visible pre-compaction transcript" in artifact.warnings[0]["message"]


def test_legacy_filters_injected_context_using_ui_messages(tmp_path: Path) -> None:
    """Legacy rollouts inject the same environment state; `user_message` is the truth."""

    injected = "<environment_context>\n  <cwd>/work</cwd>\n</environment_context>"
    path = write_jsonl(
        tmp_path / "legacy.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "cwd": str(tmp_path),
                    "cli_version": "0.144.4",
                    "history_mode": "legacy",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": injected,
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:02Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "ship it"},
            },
            {
                "timestamp": "2026-08-17T12:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "ship it"}],
                },
            },
        ],
    )
    session = codex.parse(path)

    messages = [event for event in session.events if event.kind == EventKind.MESSAGE]
    assert [(event.role, event.text) for event in messages] == [(Role.USER, "ship it")]
    assert [
        event.payload["context_source"]
        for event in session.events
        if event.payload.get("block_type") == "codex_injected_user_context"
    ] == ["environment_context"]


def test_ui_confirmed_prompt_is_never_reclassified(tmp_path: Path) -> None:
    """A human who literally pastes a wrapper still gets their prompt imported."""

    pasted = "<environment_context>\n  <cwd>/work</cwd>\n</environment_context>"
    path = write_jsonl(
        tmp_path / "legacy.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "cwd": str(tmp_path),
                    "cli_version": "0.144.4",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": pasted},
            },
            {
                "timestamp": "2026-08-17T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": pasted}],
                },
            },
        ],
    )
    session = codex.parse(path)

    messages = [event for event in session.events if event.kind == EventKind.MESSAGE]
    assert [(event.role, event.text) for event in messages] == [(Role.USER, pasted)]


def paginated_record(
    ordinal: int,
    record_type: str,
    payload: dict[str, object],
    *,
    timestamp: str = "2026-08-17T12:00:00Z",
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "ordinal": ordinal,
        "type": record_type,
        "payload": payload,
    }


def paginated_meta(tmp_path: Path, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "22222222-2222-4222-8222-222222222222",
        "timestamp": "2026-08-17T12:00:00Z",
        "cwd": str(tmp_path),
        "cli_version": "0.151.0",
        "model_provider": "openai",
        "history_mode": "paginated",
    }
    payload.update(overrides)
    return paginated_record(0, "session_meta", payload)


def user_message_item(ordinal: int, text: str) -> dict[str, object]:
    return paginated_record(
        ordinal,
        "event_msg",
        {
            "type": "item_completed",
            "item": {"type": "UserMessage", "content": [{"type": "text", "text": text}]},
        },
    )


def user_response_item(ordinal: int, text: str) -> dict[str, object]:
    return paginated_record(
        ordinal,
        "response_item",
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    )


def test_paginated_filters_injected_context_and_keeps_real_prompts(tmp_path: Path) -> None:
    """Codex injects environment state as user-role items; they must not become prompts."""

    path = write_jsonl(
        tmp_path / "paginated.jsonl",
        [
            paginated_meta(tmp_path),
            user_response_item(1, "<recommended_plugins>\nAirtable\n</recommended_plugins>"),
            user_response_item(
                2, "<environment_context>\n  <cwd>/work</cwd>\n</environment_context>"
            ),
            user_response_item(3, "# AGENTS.md instructions for /work\n\n<INSTRUCTIONS>\n"),
            user_response_item(4, "Return exactly OK."),
            user_message_item(5, "Return exactly OK."),
            paginated_record(
                6,
                "response_item",
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "OK"}],
                },
            ),
            paginated_record(
                7,
                "event_msg",
                {
                    "type": "item_completed",
                    "item": {"type": "AgentMessage", "content": [{"type": "Text", "text": "OK"}]},
                },
            ),
        ],
    )
    session = codex.parse(path)

    messages = [event for event in session.events if event.kind == EventKind.MESSAGE]
    assert [(event.role, event.text) for event in messages] == [
        (Role.USER, "Return exactly OK."),
        (Role.ASSISTANT, "OK"),
    ]
    injected = [
        event.payload["context_source"]
        for event in session.events
        if event.payload.get("block_type") == "codex_injected_user_context"
    ]
    assert injected == ["recommended_plugins", "environment_context", "agents_md_instructions"]


def test_paginated_keeps_unrecognized_and_image_prefixed_user_text(tmp_path: Path) -> None:
    """Sub-agent instructions have no UI item, and images prefix genuine prompts."""

    attachment = '<image name=[Image #1] path="/tmp/shot.png"></image>Why is this broken?'
    path = write_jsonl(
        tmp_path / "paginated.jsonl",
        [
            paginated_meta(tmp_path, thread_source="subagent"),
            user_response_item(1, "Investigate the failing parser and report back."),
            user_response_item(2, attachment),
            user_message_item(3, "Why is this broken?"),
        ],
    )
    session = codex.parse(path)

    messages = [event for event in session.events if event.kind == EventKind.MESSAGE]
    assert [(event.role, event.text) for event in messages] == [
        (Role.USER, "Investigate the failing parser and report back."),
        (Role.USER, attachment),
    ]
    assert not [
        event for event in session.events if event.payload.get("ui_only_projection") is True
    ]


def test_paginated_maps_context_compaction_items(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "paginated.jsonl",
        [
            paginated_meta(tmp_path),
            user_response_item(1, "hello"),
            user_message_item(2, "hello"),
            paginated_record(
                3,
                "event_msg",
                {"type": "item_completed", "item": {"type": "ContextCompaction", "id": "c1"}},
            ),
            paginated_record(4, "compacted", {"message": "summary"}),
        ],
    )
    session = codex.parse(path)

    assert [event.kind for event in session.events] == [
        EventKind.MESSAGE,
        EventKind.COMPACTION,
    ]


@pytest.mark.parametrize(
    ("records", "match"),
    [
        ("missing", "missing an integer 'ordinal'"),
        ("truncated", "starts at ordinal 392"),
        ("decreasing", "ordinals decrease"),
    ],
)
def test_paginated_ordinals_fail_closed(tmp_path: Path, records: str, match: str) -> None:
    """A complete, ordered ordinal chain is the only proof this file is the whole thread."""

    written: list[dict[str, object]] = [paginated_meta(tmp_path)]
    if records == "missing":
        record = user_response_item(1, "hi")
        record.pop("ordinal")
        written.append(record)
    elif records == "truncated":
        # Real history_base continuations start mid-stream, so earlier turns are absent.
        written[0]["ordinal"] = 392
        written.append(user_response_item(393, "hi"))
    else:
        written.append(user_response_item(2, "hi"))
        written.append(user_response_item(1, "out of order"))
    path = write_jsonl(tmp_path / "paginated.jsonl", written)
    with pytest.raises(SessionMigrateError, match=match):
        codex.parse(path)


def test_codex_mixed_ui_messages_recover_only_unmatched_fallback(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "partial-rollout.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "cwd": str(tmp_path),
                    "cli_version": "0.144.4",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "canonical"},
            },
            {
                "timestamp": "2026-08-17T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "canonical"}],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:03Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "partial only"},
            },
        ],
    )

    session = codex.parse(path)

    assert [event.kind for event in session.events] == [EventKind.MESSAGE, EventKind.MESSAGE]
    assert [event.text for event in session.events] == ["canonical", "partial only"]
    assert session.events[-1].payload == {"ui_only_projection": True}
    artifact = convert_session(
        session,
        ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path),
    )
    assert artifact.dropped == {"message:ui_only_projection": 1}
    assert "retained as visible conversation history" in artifact.warnings[0]["message"]


def test_rejects_invalid_claude_graphs(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    duplicate_path = write_jsonl(
        tmp_path / "duplicate.jsonl",
        [
            claude_record("user", "same", None, "one", cwd=cwd),
            claude_record("assistant", "same", None, "two", cwd=cwd),
        ],
    )
    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        claude.parse(duplicate_path)

    mixed = claude_record("assistant", "a1", "u1", "two", cwd=cwd)
    mixed["sessionId"] = "99999999-9999-4999-8999-999999999999"
    mixed_path = write_jsonl(
        tmp_path / "mixed-session.jsonl",
        [claude_record("user", "u1", None, "one", cwd=cwd), mixed],
    )
    with pytest.raises(SessionMigrateError, match="mixed sessionId"):
        claude.parse(mixed_path)

    broken_path = write_jsonl(
        tmp_path / "broken-parent.jsonl",
        [claude_record("assistant", "a1", "missing", "answer", cwd=cwd)],
    )
    repaired = claude.parse(broken_path)
    assert [event.text for event in repaired.events if event.text] == ["answer"]
    assert "claude_parent_link_repaired" in {
        event.payload.get("reason") for event in repaired.events
    }


def test_claude_lost_parent_links_to_the_previous_turn_end(tmp_path: Path) -> None:
    """A prompt Claude never wrote leaves its reply pointing at a missing UUID."""

    cwd = str(tmp_path)
    records = [
        claude_record("user", "u1", None, "first prompt", cwd=cwd),
        claude_record("assistant", "a1", "u1", "first answer", cwd=cwd),
        {**claude_record("system", "s1", "a1", "", cwd=cwd), "subtype": "turn_duration"},
        claude_record("assistant", "a2", "never-written", "second answer", cwd=cwd),
        {"type": "last-prompt", "leafUuid": "a2"},
    ]
    del records[2]["message"]
    records[3]["timestamp"] = "2026-08-17T13:30:00Z"
    session = claude.parse(write_jsonl(tmp_path / "lost-parent.jsonl", records))

    assert [event.text for event in session.events if event.kind == EventKind.MESSAGE] == [
        "first prompt",
        "first answer",
        "second answer",
    ]


def test_claude_lost_parent_prefers_claude_codes_timestamp_rule(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    records = [
        claude_record("user", "u1", None, "first prompt", cwd=cwd),
        claude_record("assistant", "a1", "u1", "first answer", cwd=cwd),
        claude_record("assistant", "dead-end", "u1", "abandoned branch", cwd=cwd),
        claude_record("assistant", "a2", "never-written", "second answer", cwd=cwd),
        {"type": "last-prompt", "leafUuid": "a2"},
    ]
    records[1]["timestamp"] = "2026-08-17T12:00:01Z"
    records[2]["timestamp"] = "2026-08-17T11:00:00Z"
    records[3]["timestamp"] = "2026-08-17T12:00:03Z"
    session = claude.parse(write_jsonl(tmp_path / "timestamp-parent.jsonl", records))

    # a1 was written two seconds before a2; the later-written dead end is older.
    assert [event.text for event in session.events if event.kind == EventKind.MESSAGE] == [
        "first prompt",
        "first answer",
        "second answer",
    ]


def test_rejects_standalone_claude_sidechain_with_precise_error(tmp_path: Path) -> None:
    record = claude_record("user", "u1", None, "subagent prompt", cwd=str(tmp_path))
    record["isSidechain"] = True
    path = write_jsonl(tmp_path / "sidechain.jsonl", [record])

    with pytest.raises(SessionMigrateError, match="sidechain/subagent"):
        claude.parse(path)


def test_custom_tool_input_is_wrapped_and_reported(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "codex-custom-tool.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path),
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "freeform",
                    "input": "raw input",
                    "call_id": "call-1",
                },
            },
        ],
    )

    artifact = convert_session(
        codex.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    assert records[0]["message"]["content"][0]["input"] == {"input": "raw input"}
    assert artifact.dropped == {"tool_call:non_object_input": 1}


def test_invalid_image_media_is_omitted_and_reported(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    source_path = write_jsonl(
        tmp_path / "invalid-image.jsonl",
        [
            claude_record(
                "user",
                "u1",
                None,
                [
                    {"type": "text", "text": "safe text remains"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "text/html",
                            "data": "PGgxPk5PUEU8L2gxPg==",
                        },
                    },
                ],
                cwd=cwd,
            )
        ],
    )

    artifact = convert_session(
        claude.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
    )
    assert artifact.dropped == {"context:image": 1}
    assert b"text/html" not in artifact.native_bytes


def test_rejects_conversion_without_resumable_history(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "title-only.jsonl",
        [
            {
                "type": "custom-title",
                "customTitle": "Synthetic title",
                "sessionId": "11111111-1111-4111-8111-111111111111",
            }
        ],
    )

    with pytest.raises(SessionMigrateError, match="no resumable conversation history"):
        convert_session(
            claude.parse(source_path),
            ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
        )


def test_unknown_structured_tool_output_is_counted(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "codex-encrypted-tool-output.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path),
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "tool",
                    "arguments": "{}",
                    "call_id": "call-1",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": [
                        {"type": "encrypted_content", "data": "opaque"},
                        17,
                    ],
                },
            },
        ],
    )

    artifact = convert_session(
        codex.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path),
    )
    assert artifact.dropped == {"tool_result:opaque": 2}


def test_malformed_known_tool_result_blocks_are_counted_in_both_directions(
    tmp_path: Path,
) -> None:
    cwd = str(tmp_path)
    claude_source = write_jsonl(
        tmp_path / "claude-malformed-tool-result.jsonl",
        [
            claude_record("user", "u1", None, "start", cwd=cwd),
            claude_record(
                "assistant",
                "a1",
                "u1",
                [{"type": "tool_use", "id": "call-1", "name": "Read", "input": {}}],
                cwd=cwd,
            ),
            claude_record(
                "user",
                "u2",
                "a1",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": [
                            {"type": "image", "source": {"type": "base64"}},
                            {"type": "tool_reference"},
                        ],
                    }
                ],
                cwd=cwd,
            ),
        ],
    )
    claude_artifact = convert_session(
        claude.parse(claude_source),
        ConversionOptions(target_format=AgentFormat.CODEX, cwd=tmp_path),
    )
    assert claude_artifact.dropped == {"tool_result:opaque": 2}

    codex_source = write_jsonl(
        tmp_path / "codex-malformed-tool-result.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": cwd,
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "Read",
                    "arguments": "{}",
                    "call_id": "call-1",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": [
                        {"type": "input_image"},
                        {"type": "tool_reference"},
                    ],
                },
            },
        ],
    )
    codex_artifact = convert_session(
        codex.parse(codex_source),
        ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path),
    )
    assert codex_artifact.dropped == {"tool_result:opaque": 2}


def test_manifest_failure_does_not_delete_replaced_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = write_jsonl(
        tmp_path / "codex.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path),
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            },
        ],
    )
    artifact = convert_session(
        codex.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path),
    )
    output_path = tmp_path / "output.jsonl"
    manifest_path = tmp_path / "manifest.json"
    original_writer = conversion.write_private_atomic
    calls = 0

    def fail_after_replacement(path: Path, data: bytes) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_writer(path, data)
        output_path.unlink()
        output_path.write_bytes(b"replacement owned by another process")
        raise JsonlError("synthetic manifest failure")

    monkeypatch.setattr(conversion, "write_private_atomic", fail_after_replacement)

    with pytest.raises(JsonlError, match="synthetic manifest failure"):
        write_artifact(
            artifact,
            output_path=output_path,
            manifest_path=manifest_path,
        )

    assert output_path.read_bytes() == b"replacement owned by another process"


def test_claude_fixture_semantics_survive_codex_round_trip(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl"
    source = claude.parse(fixture)
    artifact = convert_session(
        source,
        ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
    )
    converted = tmp_path / "converted-codex.jsonl"
    converted.write_bytes(artifact.native_bytes)
    reparsed = codex.parse(converted)

    assert semantic_signature(source, omit={EventKind.OPAQUE}) == semantic_signature(reparsed)


def test_codex_fixture_semantics_survive_claude_round_trip(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "codex-0.144.4" / "basic.jsonl"
    source = codex.parse(fixture)
    artifact = convert_session(
        source,
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path),
    )
    converted = tmp_path / "converted-claude.jsonl"
    converted.write_bytes(artifact.native_bytes)
    reparsed = claude.parse(converted)

    assert semantic_signature(source, omit={EventKind.COMPACTION}) == semantic_signature(reparsed)


def compacted_session(tmp_path: Path, *, carried: bool = True) -> Path:
    """A rollout with two compaction boundaries and turns after each."""

    replacement = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "original goal"}],
        },
        {"type": "compaction", "encrypted_content": "opaque-provider-state"},
    ]
    return write_jsonl(
        tmp_path / "compacted.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "cwd": str(tmp_path),
                    "cli_version": "0.144.4",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "ancient history"}],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:02Z",
                "type": "compacted",
                "payload": {"message": "", "replacement_history": replacement},
            },
            {
                "timestamp": "2026-08-17T12:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "middle history"}],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:04Z",
                "type": "compacted",
                "payload": (
                    {"message": "", "replacement_history": replacement}
                    if carried
                    else {"message": ""}
                ),
            },
            {
                "timestamp": "2026-08-17T12:00:05Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "recent answer"}],
                },
            },
        ],
    )


def test_truncation_keeps_the_last_window_the_agent_would_resume_with(tmp_path: Path) -> None:
    session = codex.parse(compacted_session(tmp_path))
    assert [event.text for event in session.events if event.kind == EventKind.MESSAGE] == [
        "ancient history",
        "middle history",
        "recent answer",
    ]

    trimmed, stats = conversion.truncate_to_last_compaction(session)
    # The carried anchor replaces every turn the agent had already compacted away.
    assert [event.text for event in trimmed.events if event.kind == EventKind.MESSAGE] == [
        "original goal",
        "recent answer",
    ]
    # The encrypted summary survives only as a counted placeholder, never as text.
    assert [
        event.payload.get("source_item_type")
        for event in trimmed.events
        if event.kind == EventKind.OPAQUE
    ] == ["compaction"]
    assert stats["events_carried_forward"] == 2
    assert stats["events_after_boundary"] == 1
    assert stats["events_before_boundary"] > 0


def test_truncation_without_replacement_history_keeps_later_events(tmp_path: Path) -> None:
    session = codex.parse(compacted_session(tmp_path, carried=False))
    trimmed, stats = conversion.truncate_to_last_compaction(session)

    assert [event.text for event in trimmed.events if event.kind == EventKind.MESSAGE] == [
        "recent answer"
    ]
    assert stats["events_carried_forward"] == 0


def test_truncation_is_a_no_op_without_compaction(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "plain.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {"id": "22222222-2222-4222-8222-222222222222", "cwd": str(tmp_path)},
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "only turn"}],
                },
            },
        ],
    )
    session = codex.parse(path)
    trimmed, stats = conversion.truncate_to_last_compaction(session)

    assert stats == {}
    assert trimmed.events == session.events


def test_truncated_conversion_carries_the_anchor_into_the_target(tmp_path: Path) -> None:
    session = codex.parse(compacted_session(tmp_path))
    trimmed, _ = conversion.truncate_to_last_compaction(session)
    artifact = convert_session(
        trimmed,
        ConversionOptions(target_format=AgentFormat.COPILOT, cwd=tmp_path),
    )
    records = [json.loads(line) for line in artifact.native_bytes.decode().splitlines() if line]

    assert [
        record["data"]["content"] for record in records if record["type"] == "user.message"
    ] == ["original goal"]


def test_truncation_carries_a_plaintext_claude_summary(tmp_path: Path) -> None:
    """Claude records its summary as readable text, unlike Codex's encrypted state."""

    cwd = str(tmp_path)
    summary = "Summary: the earlier work established the parser contract."
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "parentUuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
        "sessionId": "11111111-1111-4111-8111-111111111111",
        "timestamp": "2026-08-17T12:00:02Z",
        "cwd": cwd,
        "version": "2.1.209",
        "compactMetadata": {"trigger": "auto"},
    }
    summary_record = claude_record(
        "user",
        "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        summary,
        cwd=cwd,
    )
    summary_record["isCompactSummary"] = True
    path = write_jsonl(
        tmp_path / "claude.jsonl",
        [
            claude_record(
                "user", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", None, "ancient history", cwd=cwd
            ),
            boundary,
            summary_record,
            claude_record(
                "user",
                "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "what next?",
                cwd=cwd,
            ),
            claude_record(
                "assistant",
                "ffffffff-ffff-4fff-8fff-ffffffffffff",
                "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                [{"type": "text", "text": "continuing"}],
                cwd=cwd,
            ),
        ],
    )
    session = claude.parse(path)
    trimmed, stats = conversion.truncate_to_last_compaction(session)

    assert stats["compaction_summary_carried"] == 1
    assert [event.text for event in trimmed.events if event.kind == EventKind.COMPACTION] == [
        summary
    ]
    assert [event.text for event in trimmed.events if event.kind == EventKind.MESSAGE] == [
        "what next?",
        "continuing",
    ]

    artifact = convert_session(
        trimmed,
        ConversionOptions(target_format=AgentFormat.COPILOT, cwd=tmp_path),
    )
    records = [json.loads(line) for line in artifact.native_bytes.decode().splitlines() if line]
    assert [
        record["data"]["summaryContent"]
        for record in records
        if record["type"] == "session.compaction_complete"
    ] == [summary]


def test_claude_filters_slash_commands_and_task_notifications(tmp_path: Path) -> None:
    """Claude logs its own command echoes and notices as user-role turns."""

    cwd = str(tmp_path)
    records = []
    parent = None
    for uid, text, origin in (
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", "<command-name>/compact</command-name>", None),
        (
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
            "<local-command-stdout>Compacted</local-command-stdout>",
            None,
        ),
        (
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3",
            "<task-notification>\n<task-id>x</task-id>\n</task-notification>",
            None,
        ),
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4", "please fix the parser", {"kind": "human"}),
    ):
        record = claude_record("user", uid, parent, text, cwd=cwd)
        if origin:
            record["origin"] = origin
        records.append(record)
        parent = uid
    session = claude.parse(write_jsonl(tmp_path / "claude.jsonl", records))

    assert [event.text for event in session.events if event.kind == EventKind.MESSAGE] == [
        "please fix the parser"
    ]
    assert [
        event.payload["context_source"]
        for event in session.events
        if event.payload.get("block_type") == "claude_injected_user_context"
    ] == ["command-name", "local-command-stdout", "task-notification"]


def test_claude_human_origin_wins_for_notices_but_not_command_envelopes(
    tmp_path: Path,
) -> None:
    """A quoted notice is a prompt; a slash-command envelope never is."""

    cwd = str(tmp_path)
    invoked = claude_record(
        "user",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
        None,
        "<command-name>/doctor</command-name>",
        cwd=cwd,
    )
    invoked["origin"] = {"kind": "human"}
    invoked_session = claude.parse(write_jsonl(tmp_path / "invoked.jsonl", [invoked]))
    # Claude builds this envelope itself, so human origin means "ran a command".
    assert not [e for e in invoked_session.events if e.kind == EventKind.MESSAGE]

    pasted = "<task-notification>look at this notification I got</task-notification>"
    record = claude_record("user", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", None, pasted, cwd=cwd)
    record["origin"] = {"kind": "human"}
    session = claude.parse(write_jsonl(tmp_path / "claude.jsonl", [record]))

    assert [event.text for event in session.events if event.kind == EventKind.MESSAGE] == [pasted]


@pytest.mark.parametrize(
    ("is_error", "expected"),
    [(True, claude.EMPTY_ERROR_RESULT), (False, "")],
)
def test_claude_writer_never_emits_an_empty_error_tool_result(
    tmp_path: Path, is_error: bool, expected: str
) -> None:
    """The Messages API rejects an error tool_result whose content is empty."""

    from session_migrate.model import Event, Provenance

    events = (
        Event(kind=EventKind.MESSAGE, role=Role.USER, text="run it", provenance=Provenance(0, "u")),
        Event(
            kind=EventKind.TOOL_CALL,
            role=Role.ASSISTANT,
            tool_name="run_command",
            tool_call_id="call-1",
            payload={"input": {}},
            provenance=Provenance(1, "a"),
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            role=Role.TOOL,
            text=None,
            tool_call_id="call-1",
            payload={"is_error": is_error, "content_blocks": [{"type": "text", "text": ""}]},
            provenance=Provenance(2, "t"),
        ),
    )
    session = Session(
        source_format=AgentFormat.ANTIGRAVITY,
        source_path=tmp_path / "source.db",
        source_sha256="0" * 64,
        session_id=None,
        cwd=tmp_path,
        started_at="2026-11-01T00:00:00Z",
        cli_version=None,
        model=None,
        title=None,
        events=events,
        raw_record_count=len(events),
    )
    artifact = convert_session(
        session, ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path)
    )
    results = [
        block
        for line in artifact.native_bytes.splitlines()
        for block in (json.loads(line).get("message", {}).get("content") or [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]

    assert [(block["content"], block["is_error"]) for block in results] == [(expected, is_error)]
    assert artifact.dropped.get("tool_result:empty_error_output", 0) == int(is_error)


CODEX_THREAD = "22222222-2222-4222-8222-222222222222"


def assistant_response_item(ordinal: int, text: str) -> dict[str, object]:
    return paginated_record(
        ordinal,
        "response_item",
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    )


def codex_continued_thread(tmp_path: Path, *, offset_shift: int = 0) -> tuple[Path, Path]:
    """A base rollout whose last turn was aborted, and the segment that replaced it."""

    sessions = tmp_path / "codex" / "sessions"
    base_records = [
        paginated_meta(tmp_path),
        user_response_item(1, "first prompt"),
        assistant_response_item(2, "first answer"),
        user_response_item(3, "aborted prompt"),
    ]
    base = sessions / "2026" / "08" / "17" / f"rollout-2026-08-17T12-00-00-{CODEX_THREAD}.jsonl"
    base.parent.mkdir(parents=True)
    write_jsonl(base, base_records)
    cut = sum(len(json.dumps(record).encode()) + 1 for record in base_records[:3])
    meta = paginated_meta(
        tmp_path,
        history_base={
            "thread_id": CODEX_THREAD,
            "end_ordinal_exclusive": 3,
            "end_byte_offset": cut + offset_shift,
        },
    )
    meta["ordinal"] = 3
    continuation = (
        sessions
        / "2026"
        / "08"
        / "18"
        / f"rollout-2026-08-18T09-00-00-{CODEX_THREAD}_33333333-3333-4333-8333-333333333333.jsonl"
    )
    continuation.parent.mkdir(parents=True)
    write_jsonl(
        continuation,
        [meta, user_response_item(4, "second prompt"), assistant_response_item(5, "second answer")],
    )
    return base, continuation


def test_codex_continuation_reads_its_history_base_without_the_aborted_tail(
    tmp_path: Path,
) -> None:
    """Codex 0.166 continues a thread in a new segment instead of copying its history."""

    _, continuation = codex_continued_thread(tmp_path)

    session = codex.parse(continuation)

    assert [
        (event.role, event.text) for event in session.events if event.kind == EventKind.MESSAGE
    ] == [
        (Role.USER, "first prompt"),
        (Role.ASSISTANT, "first answer"),
        (Role.USER, "second prompt"),
        (Role.ASSISTANT, "second answer"),
    ]
    assert session.session_id == CODEX_THREAD


@pytest.mark.parametrize("damage", ["missing_base", "wrong_offset"])
def test_codex_continuation_fails_without_its_exact_base(tmp_path: Path, damage: str) -> None:
    base, continuation = codex_continued_thread(
        tmp_path, offset_shift=1 if damage == "wrong_offset" else 0
    )
    if damage == "missing_base":
        base.unlink()

    with pytest.raises(SessionMigrateError, match="was not found up to ordinal 3"):
        codex.parse(continuation)


def test_codex_session_id_locates_the_latest_segment(tmp_path: Path) -> None:
    from session_migrate.discovery import locate_session

    _, continuation = codex_continued_thread(tmp_path)

    assert locate_session(AgentFormat.CODEX, CODEX_THREAD, tmp_path / "codex") == continuation


@pytest.mark.parametrize("layout", ["tail_after_summary", "logical_parent_is_tail"])
def test_claude_window_load_matches_a_full_parse_cut_at_the_same_compaction(
    tmp_path: Path, layout: str
) -> None:
    path = preserved_compaction(tmp_path, layout)

    def signature(session: Session) -> list[tuple[object, ...]]:
        return [
            (event.kind, event.role, event.text, event.payload.get("reason"))
            for event in conversion.truncate_to_last_compaction(session)[0].events
        ]

    assert signature(claude.parse(path, since_last_compaction=True)) == signature(
        claude.parse(path)
    )


def test_claude_window_load_rereads_only_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old history is indexed but never loaded, so the file's total size does not matter."""

    path = preserved_compaction(tmp_path, "tail_after_summary")
    loaded: list[object] = []
    original = claude.reread_jsonl

    def recording(source: Path, locations: object, **limits: int) -> list[object]:
        records = original(source, locations, **limits)  # type: ignore[arg-type]
        loaded.extend(record.value.get("uuid") for record in records)
        return records

    monkeypatch.setattr(claude, "reread_jsonl", recording)
    claude.parse(path, since_last_compaction=True)

    assert "u0" not in loaded and "a0" not in loaded
    assert {"summary", "u1", "a1", "a2"} <= set(loaded)


def test_claude_window_load_without_compaction_fails_early(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    path = write_jsonl(
        tmp_path / "plain.jsonl",
        [
            claude_record("user", "u1", None, "hello", cwd=cwd),
            claude_record("assistant", "a1", "u1", "hi", cwd=cwd),
        ],
    )

    with pytest.raises(SessionMigrateError, match="found no compaction boundary"):
        claude.parse(path, since_last_compaction=True)


def test_reread_window_is_bounded_like_one_file(tmp_path: Path) -> None:
    from session_migrate.jsonl import iter_jsonl, reread_jsonl

    path = write_jsonl(tmp_path / "records.jsonl", [{"n": index} for index in range(3)])
    located = list(iter_jsonl(path))

    assert [record.value for record in reread_jsonl(path, located[1:])] == [{"n": 1}, {"n": 2}]
    with pytest.raises(JsonlError, match="window exceeds"):
        reread_jsonl(path, located, max_total_bytes=located[0].byte_length)


def test_codex_window_load_matches_a_full_parse_across_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, continuation = codex_continued_thread(tmp_path)
    records = [json.loads(line) for line in continuation.read_text().splitlines()]
    compacted = paginated_record(
        4,
        "compacted",
        {"message": "", "replacement_history": [records[1]["payload"]]},
    )
    for record in records[1:]:
        record["ordinal"] += 1
    write_jsonl(continuation, [records[0], compacted, *records[1:]])
    reread: list[object] = []
    original = codex.reread_jsonl

    def recording(source: Path, locations: object, **limits: int) -> list[object]:
        loaded = original(source, locations, **limits)  # type: ignore[arg-type]
        reread.extend(record.value.get("ordinal") for record in loaded)
        return loaded

    def signature(session: Session) -> list[tuple[object, ...]]:
        return [
            (event.kind, event.role, event.text, event.payload.get("source_record_type"))
            for event in conversion.truncate_to_last_compaction(session)[0].events
        ]

    full = signature(codex.parse(continuation))
    monkeypatch.setattr(codex, "reread_jsonl", recording)
    window = codex.parse(continuation, since_last_compaction=True)

    assert signature(window) == full
    assert reread == [0, 4, 5, 6]
    assert window.records_before_window == 3
