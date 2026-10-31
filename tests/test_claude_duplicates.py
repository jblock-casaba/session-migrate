import json
from pathlib import Path

import pytest

from session_migrate.cli import main
from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude, codex
from session_migrate.jsonl import encode_jsonl
from session_migrate.model import AgentFormat, EventKind, Role

SESSION_ID = "11111111-1111-4111-8111-111111111111"


def message(uid: str, parent: str | None, role: str, content: object) -> dict:
    return {
        "type": role,
        "uuid": uid,
        "parentUuid": parent,
        "sessionId": SESSION_ID,
        "timestamp": "2026-09-01T00:00:00Z",
        "message": {"role": role, "content": content},
    }


def write_records(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "claude.jsonl"
    path.write_bytes(encode_jsonl(records))
    return path


@pytest.mark.parametrize(
    "metadata", [{}, {"promptId": "resumed-prompt", "slug": "resumed-session"}]
)
@pytest.mark.parametrize("explicit_leaf", [False, True])
def test_duplicate_record_replays_once_without_changing_the_active_leaf(
    tmp_path: Path, metadata: dict, explicit_leaf: bool
) -> None:
    root = message("u1", None, "user", [{"type": "text", "text": "Read the file"}])
    root["promptId"] = "original-prompt"
    call = message(
        "a1", "u1", "assistant", [{"type": "tool_use", "id": "call-1", "name": "Read", "input": {}}]
    )
    result = message(
        "u2",
        "a1",
        "user",
        [{"type": "tool_result", "tool_use_id": "call-1", "content": "File text"}],
    )
    records = [
        root,
        call,
        result,
        message("a2", "u2", "assistant", "Finished"),
        {**root, **metadata},
        dict(call),
        dict(result),
    ]
    if explicit_leaf:
        records.append({"type": "last-prompt", "leafUuid": "a2"})
    path = write_records(tmp_path, records)
    original = path.read_bytes()

    session = claude.parse(path)
    artifact = convert_session(
        session, ConversionOptions(target_format=AgentFormat.CODEX, cwd=tmp_path)
    )
    target = tmp_path / "codex.jsonl"
    target.write_bytes(artifact.native_bytes)
    reparsed = codex.parse(target)

    assert [
        (event.role, event.text) for event in session.events if event.kind == EventKind.MESSAGE
    ] == [
        (Role.USER, "Read the file"),
        (Role.ASSISTANT, "Finished"),
    ]
    assert [
        (event.kind, event.tool_call_id)
        for event in reparsed.events
        if event.kind in {EventKind.TOOL_CALL, EventKind.TOOL_RESULT}
    ] == [(EventKind.TOOL_CALL, "call-1"), (EventKind.TOOL_RESULT, "call-1")]
    assert session.raw_record_count == len(records)
    assert artifact.dropped["opaque:duplicate_record_uuid"] == 3
    assert session.events[0].provenance.record_index == 0
    assert path.read_bytes() == original


def test_duplicate_handling_does_not_merge_distinct_messages_or_repeat_uuidless_records(
    tmp_path: Path,
) -> None:
    first = message("u1", None, "user", "Repeat")
    second = message("u2", "u1", "user", "Repeat")
    uuidless = message("u3", "u2", "assistant", "UUID-less fallback")
    del uuidless["uuid"]
    session = claude.parse(write_records(tmp_path, [first, dict(first), second, uuidless]))

    assert [event.text for event in session.events if event.kind == EventKind.MESSAGE] == [
        "Repeat",
        "Repeat",
        "UUID-less fallback",
    ]
    assert session.event_counts()["opaque"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"message": {"role": "user", "content": "Different text"}},
        {"message": {"role": "user", "content": "Original", "promptId": "nested-change"}},
        {"parentUuid": "different-parent"},
        {"logicalParentUuid": "different-parent"},
        {"sessionId": "22222222-2222-4222-8222-222222222222"},
        {"type": "assistant"},
        {"timestamp": "2026-09-01T00:00:01Z"},
        {"isMeta": True},
        {"isSidechain": True},
        {"isCompactSummary": True},
        {"origin": {"kind": "human"}},
        {"version": "new-version"},
        {"futureMetadata": "unknown-semantics"},
        {"promptId": {"not": "a string"}},
        {"slug": ["not", "a string"]},
    ],
)
def test_conflicting_duplicate_uuids_still_fail_closed(tmp_path: Path, changes: dict) -> None:
    root = message("u1", None, "user", "Original")
    path = write_records(tmp_path, [root, {**root, **changes}])

    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        claude.parse(path)


@pytest.mark.parametrize(
    ("parent", "error"),
    [("missing", "missing parent UUID"), ("a1", "ancestry cycle")],
)
def test_duplicate_handling_does_not_bypass_graph_validation(
    tmp_path: Path, parent: str, error: str
) -> None:
    root = message("u1", parent, "user", "Original")
    path = write_records(tmp_path, [root, dict(root), message("a1", "u1", "assistant", "Answer")])

    with pytest.raises(SessionMigrateError, match=error):
        claude.parse(path)


def test_import_by_id_with_duplicates_preserves_last_compaction_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = message("u1", None, "user", "Old question")
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": "boundary",
        "parentUuid": "u1",
        "sessionId": SESSION_ID,
        "timestamp": "2026-09-01T00:00:01Z",
    }
    summary = message("summary", "boundary", "user", "Plaintext summary")
    summary["isCompactSummary"] = True
    records = [
        root,
        boundary,
        summary,
        message("u2", "summary", "user", "Continue"),
        message("a1", "u2", "assistant", "Recent answer"),
        {**root, "slug": "resumed", "promptId": "new-prompt"},
        dict(summary),
        {"type": "last-prompt", "leafUuid": "a1"},
    ]
    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "synthetic-project"
    project.mkdir(parents=True)
    source_path = project / f"{SESSION_ID}.jsonl"
    source_path.write_bytes(encode_jsonl(records))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    target_home = tmp_path / "codex-home"
    args = [
        "import",
        "--format",
        "claude",
        "--to",
        "codex",
        "--since-last-compaction",
        "--home",
        str(target_home),
        "--cwd",
        str(tmp_path),
        SESSION_ID,
    ]

    assert main([*args, "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert not target_home.exists()
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    target = codex.parse(Path(result["output"]))

    assert [event.text for event in target.events if event.kind == EventKind.COMPACTION] == [
        "Plaintext summary"
    ]
    assert [event.text for event in target.events if event.kind == EventKind.MESSAGE] == [
        "Continue",
        "Recent answer",
    ]
    assert result["dropped_events"]["opaque:duplicate_record_uuid"] == 2
    assert source_path.read_bytes() == encode_jsonl(records)
