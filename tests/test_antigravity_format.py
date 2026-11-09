import json
import os
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import antigravity
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

FIXTURE = Path(__file__).parent / "fixtures" / "antigravity-1.1.16" / "basic.json"
TARGET_ID = "33333333-3333-4333-8333-333333333333"
TRAJECTORY_ID = "44444444-4444-4444-8444-444444444444"


def portable_session(tmp_path: Path) -> Session:
    events = (
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text="AGY_USER_ALPHA",
            timestamp="2026-08-20T12:00:00Z",
            provenance=Provenance(0, "user"),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.ASSISTANT,
            text="AGY_ASSISTANT_OMEGA",
            timestamp="2026-08-20T12:00:01Z",
            provenance=Provenance(1, "assistant"),
        ),
        Event(
            kind=EventKind.TOOL_CALL,
            role=Role.ASSISTANT,
            tool_name="echo_marker",
            tool_call_id="agy-call-1",
            timestamp="2026-08-20T12:00:02Z",
            payload={"input": {"text": "TOOL_INPUT_ALPHA", "count": 2}},
            provenance=Provenance(2, "tool_call"),
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            role=Role.TOOL,
            text="TOOL_RESULT_OMEGA",
            tool_name="echo_marker",
            tool_call_id="agy-call-1",
            timestamp="2026-08-20T12:00:03Z",
            payload={
                "is_error": False,
                "content_blocks": [{"type": "text", "text": "TOOL_RESULT_OMEGA"}],
            },
            provenance=Provenance(3, "tool_result"),
        ),
        Event(
            kind=EventKind.THINKING,
            role=Role.ASSISTANT,
            text="PRIVATE_THINKING_MUST_NOT_SURVIVE",
            provenance=Provenance(4, "thinking"),
        ),
    )
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "source.jsonl",
        source_sha256="0" * 64,
        session_id="11111111-1111-4111-8111-111111111111",
        cwd=tmp_path,
        started_at="2026-08-20T12:00:00Z",
        cli_version="2.1.209",
        model="fixture-model",
        title="Antigravity fixture",
        events=events,
        raw_record_count=len(events),
    )


def serialized(tmp_path: Path) -> bytes:
    data, _ = antigravity.serialize(
        portable_session(tmp_path),
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    return data


def fixture_database(tmp_path: Path) -> Path:
    value = json.loads(FIXTURE.read_text())
    rows = tuple(
        antigravity._StepRow(
            row["idx"],
            row["step_type"],
            row["status"],
            None,
            bytes.fromhex(row["step_payload_hex"]),
        )
        for row in value["steps"]
    )
    data = antigravity._build_database(
        rows,
        conversation_id=value["conversation_id"],
        trajectory_id=value["trajectory_id"],
        started_at=value["created_at"],
    )
    path = tmp_path / f"{value['conversation_id']}.db"
    path.write_bytes(data)
    return path


def event_signature(events: tuple[Event, ...]) -> list[tuple[object, ...]]:
    result = []
    for event in events:
        if event.kind == EventKind.THINKING:
            continue
        payload = event.payload.get("input") if event.kind == EventKind.TOOL_CALL else None
        result.append(
            (
                event.kind,
                event.role,
                event.text,
                event.tool_name,
                event.tool_call_id,
                json.dumps(payload, sort_keys=True),
            )
        )
    return result


def test_writer_parser_round_trip_preserves_messages_and_generic_tools(tmp_path: Path) -> None:
    source = portable_session(tmp_path)
    data, dropped = antigravity.serialize(
        source,
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(data)

    antigravity.validate_native_bytes(data, TARGET_ID)
    parsed = antigravity.parse(path)

    assert dropped == {"thinking:private": 1}
    assert b"PRIVATE_THINKING_MUST_NOT_SURVIVE" not in data
    assert antigravity.native_record_count(data) == 4
    assert event_signature(parsed.events) == event_signature(source.events)


def test_writer_preserves_out_of_order_and_duplicate_tool_linkage(tmp_path: Path) -> None:
    source = portable_session(tmp_path)
    user = source.events[0]
    call_one = replace(source.events[2], tool_name="one", tool_call_id="duplicate")
    call_two = replace(source.events[2], tool_name="two", tool_call_id="duplicate")
    result_two = replace(
        source.events[3],
        text="result two",
        tool_name="two",
        tool_call_id="duplicate",
        payload={"content_blocks": [{"type": "text", "text": "result two"}]},
    )
    result_one = replace(
        source.events[3],
        text="result one",
        tool_name="one",
        tool_call_id="duplicate",
        payload={"content_blocks": [{"type": "text", "text": "result one"}]},
    )
    source = replace(source, events=(user, call_one, call_two, result_two, result_one))

    data, dropped = antigravity.serialize(
        source,
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
    )
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(data)
    parsed = antigravity.parse(path)

    assert dropped == {"tool_call:duplicate_id": 1}
    calls = [event for event in parsed.events if event.kind == EventKind.TOOL_CALL]
    results = [event for event in parsed.events if event.kind == EventKind.TOOL_RESULT]
    assert [(event.tool_name, event.tool_call_id) for event in calls] == [
        ("one", "duplicate"),
        ("two", "duplicate-session-migrate-2"),
    ]
    assert [(event.text, event.tool_name, event.tool_call_id) for event in results] == [
        ("result two", "two", "duplicate-session-migrate-2"),
        ("result one", "one", "duplicate"),
    ]


def test_writer_counts_nonportable_tool_namespace(tmp_path: Path) -> None:
    source = portable_session(tmp_path)
    namespaced_call = replace(
        source.events[2],
        payload={"input": source.events[2].payload["input"], "namespace": "synthetic-mcp"},
    )
    source = replace(source, events=(source.events[0], namespaced_call, source.events[3]))

    data, dropped = antigravity.serialize(
        source,
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
    )

    antigravity.validate_native_bytes(data, TARGET_ID)
    assert dropped == {"tool_call:namespace": 1}


def test_sanitized_fixture_projects_content_free_thinking_and_tools(tmp_path: Path) -> None:
    parsed = antigravity.parse(fixture_database(tmp_path))

    assert parsed.raw_record_count == 3
    assert [(event.kind, event.text) for event in parsed.events] == [
        (EventKind.MESSAGE, "AGY_FIXTURE_USER_ALPHA"),
        (EventKind.THINKING, None),
        (EventKind.MESSAGE, "AGY_FIXTURE_ASSISTANT_OMEGA"),
        (EventKind.TOOL_CALL, None),
        (EventKind.TOOL_RESULT, "TOOL_RESULT_OMEGA"),
    ]
    assert parsed.events[3].tool_call_id == "fixture-call-1"
    assert parsed.events[3].payload["input"] == {"text": "TOOL_INPUT_ALPHA"}
    assert all(event.text != "AGY_PRIVATE_THINKING_NEVER_PROJECTED" for event in parsed.events)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "UPDATE trajectory_meta SET cascade_id='55555555-5555-4555-8555-555555555555'",
            "conversation ID does not match",
        ),
        ("UPDATE steps SET step_type=15 WHERE idx=0", "payload type disagrees"),
        ("UPDATE steps SET idx=9 WHERE idx=0", "indices are not contiguous"),
        ("CREATE TABLE unexpected(value TEXT)", "table set does not match"),
    ],
)
def test_validator_fails_closed_on_database_inconsistency(
    tmp_path: Path, statement: str, message: str
) -> None:
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(serialized(tmp_path))
    with sqlite3.connect(path) as db:
        db.execute(statement)

    with pytest.raises(SessionMigrateError, match=message):
        antigravity.validate_native_bytes(path.read_bytes(), TARGET_ID)


def test_validator_rejects_malformed_protobuf_and_id_mismatch(tmp_path: Path) -> None:
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(serialized(tmp_path))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE steps SET step_payload=x'80' WHERE idx=0")

    with pytest.raises(SessionMigrateError, match="varint is truncated"):
        antigravity.validate_native_bytes(path.read_bytes(), TARGET_ID)
    with pytest.raises(SessionMigrateError, match="does not match target"):
        antigravity.validate_native_bytes(
            serialized(tmp_path), "55555555-5555-4555-8555-555555555555"
        )


def test_snapshot_includes_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(serialized(tmp_path))
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "INSERT INTO steps(idx,step_type,status,has_subtrajectory,step_payload,step_format) "
            "SELECT 4,step_type,status,has_subtrajectory,step_payload,step_format "
            "FROM steps WHERE idx=0"
        )
        connection.commit()
        parsed = antigravity.parse(path)
    finally:
        connection.close()

    assert parsed.raw_record_count == 5
    assert parsed.events[-1].text == "AGY_USER_ALPHA"


def test_install_is_private_transactional_and_collision_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = serialized(tmp_path)
    target_home = tmp_path / "agy-home" / ".gemini" / "antigravity-cli"
    monkeypatch.setattr(antigravity, "verify_pinned_cli", lambda *args, **kwargs: Path("agy"))

    dry = antigravity.install_database(
        data,
        session_id=TARGET_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
        title="Installed fixture",
        target_home=target_home,
        dry_run=True,
    )
    assert not target_home.exists()
    assert not dry.conversation_path.exists()

    installed = antigravity.install_database(
        data,
        session_id=TARGET_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
        title="Installed fixture",
        target_home=target_home,
    )
    assert stat.S_IMODE(installed.conversation_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(installed.summaries_path.stat().st_mode) == 0o600
    parsed = antigravity.parse(installed.conversation_path)
    assert parsed.session_id == TARGET_ID
    assert parsed.title == "Installed fixture"
    assert parsed.cwd == tmp_path
    with sqlite3.connect(installed.summaries_path) as db:
        summary = db.execute(
            "SELECT title,step_count,workspace_uris,last_user_input_step_index "
            "FROM conversation_summaries WHERE conversation_id=?",
            (TARGET_ID,),
        ).fetchone()
    assert summary == (
        "Installed fixture",
        4,
        json.dumps([tmp_path.as_uri()], separators=(",", ":")),
        0,
    )
    with pytest.raises(SessionMigrateError, match="already exists"):
        antigravity.install_database(
            data,
            session_id=TARGET_ID,
            cwd=tmp_path,
            timestamp="2026-08-20T12:00:00Z",
            title=None,
            target_home=target_home,
        )


def test_failed_summary_insert_removes_only_new_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = serialized(tmp_path)
    target_home = tmp_path / "target"
    monkeypatch.setattr(antigravity, "verify_pinned_cli", lambda *args, **kwargs: Path("agy"))
    original = antigravity._summary_values

    def invalid_summary(**kwargs: object) -> tuple[object, ...]:
        values = list(original(**kwargs))  # type: ignore[arg-type]
        values[3] = None
        return tuple(values)

    monkeypatch.setattr(antigravity, "_summary_values", invalid_summary)
    with pytest.raises(sqlite3.IntegrityError):
        antigravity.install_database(
            data,
            session_id=TARGET_ID,
            cwd=tmp_path,
            timestamp="2026-08-20T12:00:00Z",
            title=None,
            target_home=target_home,
        )

    assert not (target_home / antigravity.session_relative_path(TARGET_ID)).exists()
    with sqlite3.connect(target_home / "conversation_summaries.db") as db:
        assert db.execute("SELECT count(*) FROM conversation_summaries").fetchone() == (0,)


def test_install_does_not_delete_or_index_a_racing_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = serialized(tmp_path)
    target_home = tmp_path / "target"
    monkeypatch.setattr(antigravity, "verify_pinned_cli", lambda *args, **kwargs: Path("agy"))
    original_write = antigravity.write_private_atomic
    replacement = tmp_path / "racing-replacement"
    replacement.write_bytes(b"racing replacement")

    def racing_write(path: Path, value: bytes) -> tuple[int, int]:
        identity = original_write(path, value)
        if path.parent.name == "conversations":
            path.unlink()
            os.link(replacement, path)
            os.chmod(path, 0o600)
        return identity

    monkeypatch.setattr(antigravity, "write_private_atomic", racing_write)
    with pytest.raises(SessionMigrateError, match="changed before it could be guarded"):
        antigravity.install_database(
            data,
            session_id=TARGET_ID,
            cwd=tmp_path,
            timestamp="2026-08-20T12:00:00Z",
            title=None,
            target_home=target_home,
        )

    target = target_home / antigravity.session_relative_path(TARGET_ID)
    assert target.read_bytes() == b"racing replacement"
    with sqlite3.connect(target_home / "conversation_summaries.db") as db:
        assert db.execute("SELECT count(*) FROM conversation_summaries").fetchone() == (0,)


def test_exact_binary_gate_rejects_unpinned_executable(tmp_path: Path) -> None:
    executable = tmp_path / "agy"
    executable.write_bytes(b"not the pinned Antigravity binary")
    os.chmod(executable, 0o700)

    with pytest.raises(SessionMigrateError, match="binary mismatch"):
        antigravity.verify_pinned_cli(executable)


# The picker index exactly as Antigravity 1.4.12 creates it.
AGY_1412_SUMMARY_DDL = (
    "CREATE TABLE `conversation_summaries` (`conversation_id` text,`title` text NOT NULL "
    'DEFAULT "",`preview` text NOT NULL DEFAULT "",`step_count` integer NOT NULL DEFAULT 0,'
    "`last_modified_time` datetime NOT NULL,`workspace_uris` text NOT NULL,`status` text NOT "
    'NULL DEFAULT "",`source` text NOT NULL DEFAULT "",`project_id` text NOT NULL DEFAULT "",'
    '`agent_name` text NOT NULL DEFAULT "",`parent_conversation_id` text NOT NULL DEFAULT "",'
    '`nesting_depth` integer NOT NULL DEFAULT 0,`battle_id` text NOT NULL DEFAULT "",'
    '`winning_conversation_id` text NOT NULL DEFAULT "",`not_fully_idle` numeric NOT NULL '
    "DEFAULT false,`killed` numeric NOT NULL DEFAULT false,`last_user_input_time` datetime NOT "
    "NULL,`last_user_input_step_index` integer NOT NULL DEFAULT -1,`app_data_dir` text NOT "
    'NULL DEFAULT "",`raw_summary` blob,`group_id` text NOT NULL DEFAULT "",PRIMARY KEY '
    "(`conversation_id`));"
    "CREATE INDEX `idx_conversation_summaries_last_modified_time` ON "
    "`conversation_summaries`(`last_modified_time`);"
    "CREATE INDEX `idx_conversation_summaries_last_user_input_time` ON "
    "`conversation_summaries`(`last_user_input_time`);"
    "PRAGMA user_version=3;"
)


def serialized_1412(tmp_path: Path) -> bytes:
    data, _ = antigravity.serialize(
        portable_session(tmp_path),
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
        cli_version=antigravity.ANTIGRAVITY_1412_VERSION,
        timestamp="2026-08-20T12:00:00.250000Z",
    )
    return data


def database_header(data: bytes, tmp_path: Path) -> tuple[int, list[int]]:
    path = tmp_path / "inspect.db"
    path.write_bytes(data)
    with sqlite3.connect(path) as db:
        user_version = db.execute("PRAGMA user_version").fetchone()[0]
        blob = db.execute("SELECT data FROM trajectory_metadata_blob").fetchone()[0]
    path.unlink()
    return user_version, [field.number for field in antigravity._decode_message(blob)]


def test_default_writer_keeps_the_1116_layout(tmp_path: Path) -> None:
    assert database_header(serialized(tmp_path), tmp_path) == (0, [2, 6, 18])


def test_1412_writer_records_schema_version_and_workspace(tmp_path: Path) -> None:
    data = serialized_1412(tmp_path)
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(data)

    parsed = antigravity.parse(path)
    with sqlite3.connect(path) as db:
        blob = db.execute("SELECT data FROM trajectory_metadata_blob").fetchone()[0]
    fields = {field.number: field.value for field in antigravity._decode_message(blob)}
    workspace = {field.number: field.value for field in antigravity._decode_message(fields[1])}
    created = {field.number: field.value for field in antigravity._decode_message(fields[2])}

    # Antigravity 1.4.12 migrates user_version 0 by discarding this metadata.
    assert database_header(data, tmp_path)[0] == 1
    assert workspace == {1: tmp_path.as_uri().encode(), 3: b""}
    assert fields[7] == tmp_path.as_uri().encode()
    assert created[2] == 250_000_000
    assert parsed.started_at == "2026-08-20T12:00:00.250000Z"
    assert event_signature(parsed.events) == event_signature(portable_session(tmp_path).events)


def test_1412_install_writes_current_summary_and_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned: list[object] = []
    monkeypatch.setattr(
        antigravity,
        "verify_pinned_cli",
        lambda *args, **kwargs: pinned.append(kwargs.get("version")) or Path("agy"),
    )
    target_home = tmp_path / "agy-home" / ".gemini" / "antigravity-cli"
    target_home.mkdir(parents=True)
    with sqlite3.connect(target_home / "conversation_summaries.db") as db:
        db.executescript(AGY_1412_SUMMARY_DDL)

    installed = antigravity.install_database(
        serialized_1412(tmp_path),
        session_id=TARGET_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
        title='Quoted "title" ✓',
        target_home=target_home,
        cli_version=antigravity.ANTIGRAVITY_1412_VERSION,
    )
    with sqlite3.connect(installed.summaries_path) as db:
        summary = db.execute(
            "SELECT title,status,source,raw_summary,group_id,app_data_dir "
            "FROM conversation_summaries WHERE conversation_id=?",
            (TARGET_ID,),
        ).fetchone()

    assert pinned == [antigravity.ANTIGRAVITY_1412_VERSION]
    assert summary == (
        'Quoted "title" ✓',
        "CASCADE_RUN_STATUS_IDLE",
        "",
        None,
        "",
        "antigravity-cli",
    )
    assert installed.annotation_path == target_home / "annotations" / f"{TARGET_ID}.pbtxt"
    assert installed.annotation_path.read_bytes() == (b'title:"Quoted \\"title\\" \\342\\234\\223"')
    assert stat.S_IMODE(installed.annotation_path.stat().st_mode) == 0o600
    with pytest.raises(SessionMigrateError, match="already exists"):
        antigravity.install_database(
            serialized_1412(tmp_path),
            session_id=TARGET_ID,
            cwd=tmp_path,
            timestamp="2026-08-20T12:00:00Z",
            title=None,
            target_home=target_home,
            cli_version=antigravity.ANTIGRAVITY_1412_VERSION,
            dry_run=True,
        )


def test_1412_install_creates_a_current_picker_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(antigravity, "verify_pinned_cli", lambda *args, **kwargs: Path("agy"))
    target_home = tmp_path / "antigravity-cli"

    installed = antigravity.install_database(
        serialized_1412(tmp_path),
        session_id=TARGET_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
        title=None,
        target_home=target_home,
        cli_version=antigravity.ANTIGRAVITY_1412_VERSION,
    )
    with sqlite3.connect(installed.summaries_path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(conversation_summaries)")]
        user_version = db.execute("PRAGMA user_version").fetchone()[0]

    assert columns[-2:] == ["raw_summary", "group_id"]
    assert user_version == 3
    assert installed.annotation_path.read_bytes() == b'title:"Imported conversation"'


@pytest.mark.parametrize(
    ("index_version", "install_version"),
    [("1.1.16", "1.4.12"), ("1.4.12", "1.1.16")],
)
def test_install_refuses_a_picker_index_of_another_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index_version: str, install_version: str
) -> None:
    monkeypatch.setattr(antigravity, "verify_pinned_cli", lambda *args, **kwargs: Path("agy"))
    target_home = tmp_path / "antigravity-cli"
    target_home.mkdir()
    antigravity._ensure_summary_database(target_home / "conversation_summaries.db", index_version)
    data = serialized_1412(tmp_path) if install_version == "1.4.12" else serialized(tmp_path)

    for dry_run in (True, False):
        with pytest.raises(SessionMigrateError, match=f"does not match pinned {install_version}"):
            antigravity.install_database(
                data,
                session_id=TARGET_ID,
                cwd=tmp_path,
                timestamp="2026-08-20T12:00:00Z",
                title=None,
                target_home=target_home,
                cli_version=install_version,
                dry_run=dry_run,
            )
    assert not (target_home / "conversations").exists()
    assert not (target_home / "annotations").exists()


def test_binary_gate_names_every_pinned_build_and_reports_none_installed(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "agy"
    executable.write_bytes(b"not a pinned Antigravity binary")
    os.chmod(executable, 0o700)

    with pytest.raises(SessionMigrateError, match="Linux x86_64 1.1.16 or macOS arm64 1.4.12"):
        antigravity.verify_pinned_cli(executable)
    with pytest.raises(SessionMigrateError, match=r"exact macOS arm64 1\.4\.12 build$"):
        antigravity.verify_pinned_cli(executable, version="1.4.12")
    assert antigravity.installed_pinned_version(executable) is None


def native_generic_step(
    tmp_path: Path, status: int, result: bytes | None, error: bytes | None
) -> Path:
    """Rewrite the fixture's generic tool step the way 1.4.12 stores a cancelled or failed one."""

    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(serialized(tmp_path))
    with sqlite3.connect(path) as db:
        index, payload = db.execute(
            "SELECT idx, step_payload FROM steps WHERE step_type=?",
            (antigravity.STEP_TYPE_GENERIC,),
        ).fetchone()
        generic = next(
            field.value for field in antigravity._decode_message(payload) if field.number == 140
        )
        arguments = b"".join(
            antigravity._field_bytes(1, field.value)
            for field in antigravity._decode_message(generic)
            if field.number == 1
        )
        inner = arguments + (antigravity._field_bytes(2, result) if result is not None else b"")
        rebuilt = (
            antigravity._field_varint(1, antigravity.STEP_TYPE_GENERIC)
            + antigravity._field_varint(4, status)
            + antigravity._field_bytes(140, inner)
            + (antigravity._field_bytes(31, error) if error is not None else b"")
        )
        db.execute(
            "UPDATE steps SET status=?, step_payload=? WHERE idx=?", (status, rebuilt, index)
        )
    return path


def tool_results(events: tuple[Event, ...]) -> list[tuple[object, ...]]:
    return [
        (event.tool_call_id, event.text, event.payload["is_error"])
        for event in events
        if event.kind == EventKind.TOOL_RESULT
    ]


def test_cancelled_native_tool_step_without_result_still_answers_its_call(
    tmp_path: Path,
) -> None:
    path = native_generic_step(tmp_path, antigravity.STEP_STATUS_CANCELED, None, None)

    session = antigravity.parse_session(path)

    assert tool_results(session.events) == [("agy-call-1", antigravity.CANCELED_TOOL_RESULT, True)]
    assert [
        event.payload["reason"]
        for event in session.events
        if event.payload.get("reason") == "antigravity_tool_step_without_result"
    ] == ["antigravity_tool_step_without_result"]


def test_failed_native_tool_step_uses_its_error_message(tmp_path: Path) -> None:
    ui_only_result = antigravity._field_bytes(3, antigravity._field_text(5, "Analyzing"))
    error = antigravity._field_text(2, "file does not exist") + antigravity._field_text(
        3, "file does not exist"
    )
    path = native_generic_step(tmp_path, antigravity.STEP_STATUS_ERROR, ui_only_result, error)

    session = antigravity.parse_session(path)

    assert tool_results(session.events) == [("agy-call-1", "file does not exist", True)]


def test_generated_database_still_requires_generic_tool_results(tmp_path: Path) -> None:
    path = native_generic_step(tmp_path, antigravity.STEP_STATUS_DONE, None, None)

    with pytest.raises(SessionMigrateError, match="generic tool result is missing"):
        antigravity.validate_native_bytes(path.read_bytes(), TARGET_ID)


def test_empty_conversation_is_reported_as_empty(tmp_path: Path) -> None:
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(serialized(tmp_path))
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM steps")

    with pytest.raises(SessionMigrateError, match="conversation is empty"):
        antigravity.parse_session(path)


def test_checkpoint_summary_is_a_compaction_boundary_carried_into_claude(
    tmp_path: Path,
) -> None:
    from session_migrate.conversion import (
        ConversionOptions,
        convert_session,
        truncate_to_last_compaction,
    )
    from session_migrate.model import TargetFormat

    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(serialized(tmp_path))
    with sqlite3.connect(path) as db:
        count = db.execute("SELECT count(*) FROM steps").fetchone()[0]
        checkpoint = antigravity._field_text(5, "<summary>AGY_CHECKPOINT_SUMMARY</summary>")
        checkpoint += antigravity._field_varint(12, count)
        user = antigravity._field_text(2, "AGY_AFTER_CHECKPOINT")
        for index, step_type, oneof in (
            (count, antigravity.STEP_TYPE_CHECKPOINT, checkpoint),
            (count + 1, antigravity.STEP_TYPE_USER_INPUT, user),
        ):
            payload = (
                antigravity._field_varint(1, step_type)
                + antigravity._field_varint(4, antigravity.STEP_STATUS_DONE)
                + antigravity._field_bytes(antigravity._STEP_PAYLOAD_FIELDS[step_type], oneof)
            )
            db.execute(
                "INSERT INTO steps(idx,step_type,status,step_payload) VALUES(?,?,?,?)",
                (index, step_type, antigravity.STEP_STATUS_DONE, payload),
            )

    trimmed, stats = truncate_to_last_compaction(antigravity.parse_session(path))
    artifact = convert_session(
        trimmed, ConversionOptions(target_format=TargetFormat.CLAUDE, cwd=tmp_path)
    )
    messages = [
        json.loads(line)["message"]
        for line in artifact.native_bytes.splitlines()
        if "message" in json.loads(line)
    ]

    assert stats["compaction_summary_carried"] == 1
    assert [event.text for event in trimmed.events if event.text] == [
        "<summary>AGY_CHECKPOINT_SUMMARY</summary>",
        "AGY_AFTER_CHECKPOINT",
    ]
    assert messages == [
        {
            "role": "user",
            "content": "Summary of the earlier part of this conversation:\n\n"
            "<summary>AGY_CHECKPOINT_SUMMARY</summary>",
        },
        {"role": "user", "content": "AGY_AFTER_CHECKPOINT"},
    ]
    assert artifact.dropped["compaction:summary_as_user_message"] == 1


def rewrite_steps(path: Path, changes: dict[int, tuple[int, bytes | None]]) -> None:
    """Set a step's status and replace (or drop, with None) its oneof payload."""

    with sqlite3.connect(path) as db:
        for index, (status, oneof) in changes.items():
            step_type, payload = db.execute(
                "SELECT step_type, step_payload FROM steps WHERE idx=?", (index,)
            ).fetchone()
            field = antigravity._STEP_PAYLOAD_FIELDS[step_type]
            current = next(
                f.value for f in antigravity._decode_message(payload) if f.number == field
            )
            rebuilt = antigravity._field_varint(1, step_type) + antigravity._field_varint(4, status)
            replacement = current if oneof == b"keep" else oneof
            if replacement is not None:
                rebuilt += antigravity._field_bytes(field, replacement)
            db.execute(
                "UPDATE steps SET status=?, step_payload=? WHERE idx=?", (status, rebuilt, index)
            )


def append_live_user_step(path: Path, text: str) -> None:
    with sqlite3.connect(path) as db:
        index = db.execute("SELECT count(*) FROM steps").fetchone()[0]
        payload = (
            antigravity._field_varint(1, antigravity.STEP_TYPE_USER_INPUT)
            + antigravity._field_varint(4, antigravity.STEP_STATUS_DONE)
            + antigravity._field_bytes(19, antigravity._field_text(2, text))
        )
        db.execute(
            "INSERT INTO steps(idx,step_type,status,step_payload) VALUES(?,?,?,?)",
            (index, antigravity.STEP_TYPE_USER_INPUT, antigravity.STEP_STATUS_DONE, payload),
        )


def cleared_conversation(tmp_path: Path, changes: dict[int, tuple[int, bytes | None]]) -> Path:
    """The fixture's four steps (user, reply, tool call, tool result) after a context clear."""

    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(serialized(tmp_path))
    cleared = antigravity.STEP_STATUS_CLEARED
    rewrite_steps(path, {index: (cleared, b"keep") for index in range(4)} | changes)
    append_live_user_step(path, "AGY_AFTER_CLEAR")
    return path


def reasons(session: Session) -> list[object]:
    return [
        event.payload.get("reason") for event in session.events if event.kind == EventKind.OPAQUE
    ]


def test_emptied_cleared_tool_step_still_answers_its_call(tmp_path: Path) -> None:
    cleared = antigravity.STEP_STATUS_CLEARED
    path = cleared_conversation(tmp_path, {3: (cleared, None)})

    session = antigravity.parse_session(path)

    assert tool_results(session.events) == [("agy-call-1", antigravity.CLEARED_TOOL_RESULT, False)]
    assert "antigravity_cleared_step_without_payload" in reasons(session)


def test_tool_result_whose_call_was_cleared_away_is_not_left_unpaired(tmp_path: Path) -> None:
    cleared = antigravity.STEP_STATUS_CLEARED
    path = cleared_conversation(tmp_path, {2: (cleared, b"")})

    session = antigravity.parse_session(path)

    assert tool_results(session.events) == []
    assert not [event for event in session.events if event.kind == EventKind.TOOL_CALL]
    assert "antigravity_tool_result_without_call" in reasons(session)


def test_cleared_user_step_text_is_recovered_from_its_items(tmp_path: Path) -> None:
    cleared = antigravity.STEP_STATUS_CLEARED
    items = antigravity._field_bytes(3, antigravity._field_text(1, "AGY_ITEM_TEXT"))
    path = cleared_conversation(tmp_path, {0: (cleared, items)})

    session = antigravity.parse_session(path)

    assert [event.text for event in session.events if event.role == Role.USER and event.text] == [
        "AGY_ITEM_TEXT",
        "AGY_AFTER_CLEAR",
    ]


def test_context_clear_is_a_boundary_before_the_first_live_step(tmp_path: Path) -> None:
    from session_migrate.conversion import truncate_to_last_compaction

    cleared = antigravity.STEP_STATUS_CLEARED
    path = cleared_conversation(tmp_path, {0: (cleared, b"")})

    session = antigravity.parse_session(path)
    trimmed, stats = truncate_to_last_compaction(session)

    assert "antigravity_cleared_step_without_payload" in reasons(session)
    assert [event.payload.get("source_subtype") for event in trimmed.events[:1]] == [
        "context_cleared"
    ]
    assert [event.text for event in trimmed.events if event.text] == ["AGY_AFTER_CLEAR"]
    assert stats["events_before_boundary"] > 0


def test_only_cleared_steps_may_lose_their_payload(tmp_path: Path) -> None:
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(serialized(tmp_path))
    rewrite_steps(path, {3: (antigravity.STEP_STATUS_DONE, None)})

    with pytest.raises(SessionMigrateError, match="step oneof payload is missing"):
        antigravity.parse_session(path)


def test_antigravity_window_load_matches_a_full_parse_cut_at_the_same_compaction(
    tmp_path: Path,
) -> None:
    from session_migrate.conversion import truncate_to_last_compaction
    from session_migrate.errors import NoCompactionBoundaryError

    cleared = antigravity.STEP_STATUS_CLEARED
    path = cleared_conversation(tmp_path, {3: (cleared, None)})

    def signature(session: Session) -> list[tuple[object, ...]]:
        return [
            (event.kind, event.role, event.text, event.tool_call_id, event.payload.get("reason"))
            for event in truncate_to_last_compaction(session)[0].events
        ]

    window = antigravity.parse_session(path, since_last_compaction=True)

    assert signature(window) == signature(antigravity.parse_session(path))
    assert window.records_before_window == 4

    plain = tmp_path / "plain" / f"{TARGET_ID}.db"
    plain.parent.mkdir()
    plain.write_bytes(serialized(tmp_path))
    with pytest.raises(NoCompactionBoundaryError):
        antigravity.parse_session(plain, since_last_compaction=True)


def test_sub_agent_conversation_points_to_its_root(tmp_path: Path) -> None:
    root = "55555555-5555-4555-8555-555555555555"
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(serialized(tmp_path))
    with sqlite3.connect(path) as db:
        blob = db.execute("SELECT data FROM trajectory_metadata_blob").fetchone()[0]
        rebuilt = b"".join(
            antigravity._field_text(6, root)
            if field.number == 6
            else antigravity._field_bytes(field.number, field.value)
            for field in antigravity._decode_message(blob)
        )
        db.execute("UPDATE trajectory_metadata_blob SET data=?", (rebuilt,))

    with pytest.raises(SessionMigrateError, match=f"sub-agent conversations.*{root}"):
        antigravity.parse_session(path)
