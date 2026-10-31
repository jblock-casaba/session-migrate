"""Codex rollout JSONL reader and conservative native writer."""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter, deque
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.common import content_text, object_value, string, valid_rfc3339
from session_migrate.jsonl import encode_jsonl, file_sha256, iter_jsonl
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_CODEX_VERSION = "0.144.4"

SUPPORTED_HISTORY_MODES = frozenset({"legacy", "paginated"})

_MESSAGE_TEXT_BLOCK_TYPES = frozenset({"input_text", "output_text", "text"})

# Paginated rollouts carry the UI transcript as `item_completed` envelopes instead of
# the legacy `user_message`/`agent_message`/`context_compacted` events.
_ITEM_TEXT_BLOCK_TYPES = frozenset({"text", "Text", "input_text", "output_text"})

# Codex injects environment state as user-role response items. They are model input but
# never a prompt the human typed, so importing them verbatim fabricates user turns.
# Every wrapper below was confirmed against Codex's own UI transcript to never carry a
# real prompt; `<image ...>` is deliberately excluded because it prefixes genuine text.
CONTEXTUAL_USER_WRAPPERS: tuple[str, ...] = (
    "<environment_context>",
    "<recommended_plugins>",
    "<codex_internal_context",
    "<user_shell_command>",
    "<subagent_notification>",
    "<turn_aborted>",
    "<user_action>",
    "<hook_prompt",
    "<skill>",
)
AGENTS_INSTRUCTIONS_PREFIX = "# AGENTS.md instructions for "

_IMAGE_ATTACHMENT_PREFIX = re.compile(r"\A(?:\s*<image\b[^>]*>\s*(?:</image>\s*)?)+", re.IGNORECASE)


def parse(path: Path) -> Session:
    records = list(iter_jsonl(path))
    history_mode = _canonical_history_mode(records)
    if history_mode == "paginated":
        _validate_paginated_ordinals(records)
    genuine_user_texts = _ui_user_texts(records)
    events: list[Event] = []
    fallback_events: list[Event] = []
    context_compacted_events: list[Event] = []
    session_id = None
    cwd = None
    started_at = None
    cli_version = None
    model = None
    model_provider = None
    response_message_count = 0
    response_messages: Counter[tuple[Role | None, str]] = Counter()
    title = None

    for record in records:
        value = record.value
        record_type = string(value.get("type"))
        timestamp = string(value.get("timestamp"))
        payload = object_value(value.get("payload"))
        provenance = Provenance(record.index, record_type)
        if record_type == "session_meta":
            session_id = (
                session_id or string(payload.get("id")) or string(payload.get("session_id"))
            )
            cwd_value = string(payload.get("cwd"))
            cwd = cwd or (Path(cwd_value) if cwd_value else None)
            started_at = started_at or string(payload.get("timestamp")) or timestamp
            cli_version = cli_version or string(payload.get("cli_version"))
            model_provider = model_provider or string(payload.get("model_provider"))
            continue
        if record_type == "response_item":
            parsed = _response_item_events(
                payload,
                timestamp,
                provenance,
                genuine_user_texts=genuine_user_texts,
            )
            events.extend(parsed)
            response_message_count += sum(
                event.kind == EventKind.MESSAGE and event.role in {Role.USER, Role.ASSISTANT}
                for event in parsed
            )
            response_messages.update(
                _message_fingerprint(event)
                for event in parsed
                if event.kind == EventKind.MESSAGE
                and event.role in {Role.USER, Role.ASSISTANT}
                and event.text
            )
        elif record_type == "event_msg":
            event_type = string(payload.get("type"))
            if event_type == "user_message":
                fallback_events.append(
                    Event(
                        kind=EventKind.MESSAGE,
                        role=Role.USER,
                        text=string(payload.get("message")),
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
            elif event_type == "agent_message":
                fallback_events.append(
                    Event(
                        kind=EventKind.MESSAGE,
                        role=Role.ASSISTANT,
                        text=string(payload.get("message")),
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
            elif event_type == "task_started":
                model = model or string(payload.get("model"))
            elif event_type == "thread_name_updated":
                title = string(payload.get("thread_name")) or string(payload.get("name")) or title
            elif event_type == "context_compacted":
                context_compacted_events.append(
                    Event(
                        kind=EventKind.OPAQUE,
                        timestamp=timestamp,
                        payload={"source_event_type": event_type},
                        provenance=provenance,
                    )
                )
            elif event_type == "item_completed":
                item = object_value(payload.get("item"))
                item_type = string(item.get("type"))
                item_text = _item_content_text(item.get("content"))
                if item_type == "UserMessage":
                    fallback_events.append(
                        Event(
                            kind=EventKind.MESSAGE,
                            role=Role.USER,
                            text=item_text,
                            timestamp=timestamp,
                            provenance=provenance,
                        )
                    )
                elif item_type == "AgentMessage":
                    fallback_events.append(
                        Event(
                            kind=EventKind.MESSAGE,
                            role=Role.ASSISTANT,
                            text=item_text,
                            timestamp=timestamp,
                            provenance=provenance,
                        )
                    )
                elif item_type == "ContextCompaction":
                    context_compacted_events.append(
                        Event(
                            kind=EventKind.OPAQUE,
                            timestamp=timestamp,
                            payload={"source_event_type": "context_compacted"},
                            provenance=provenance,
                        )
                    )
                else:
                    events.append(
                        Event(
                            kind=EventKind.OPAQUE,
                            timestamp=timestamp,
                            payload={
                                "source_event_type": (
                                    f"item_completed:{item_type}" if item_type else "item_completed"
                                )
                            },
                            provenance=provenance,
                        )
                    )
            elif event_type not in {"user_message", "agent_message"}:
                events.append(
                    Event(
                        kind=EventKind.OPAQUE,
                        timestamp=timestamp,
                        payload={"source_event_type": event_type or "<missing>"},
                        provenance=provenance,
                    )
                )
        elif record_type == "compacted":
            replacement_history = payload.get("replacement_history")
            if replacement_history is not None and not isinstance(replacement_history, list):
                raise SessionMigrateError("Codex replacement_history must be an array")
            carried = _replacement_history_events(
                replacement_history,
                timestamp,
                provenance,
                genuine_user_texts,
            )
            events.append(
                Event(
                    kind=EventKind.COMPACTION,
                    role=Role.SYSTEM,
                    text=string(payload.get("message")),
                    timestamp=timestamp,
                    payload={
                        **(
                            {"replacement_history_expanded": True}
                            if replacement_history is not None
                            else {}
                        ),
                        # The effective history Codex itself resumes with. Retained so a
                        # caller can rebuild the post-compaction window; the default
                        # expanded-transcript mapping ignores it.
                        **({"carried_events": carried} if carried else {}),
                    },
                    provenance=provenance,
                )
            )
        elif record_type == "turn_context":
            model = model or string(payload.get("model"))
            events.append(
                Event(
                    kind=EventKind.CONTEXT,
                    role=Role.SYSTEM,
                    timestamp=timestamp,
                    payload={"source_record_type": "turn_context"},
                    provenance=provenance,
                )
            )
        elif record_type in {"world_state", "security_risk_score"}:
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    timestamp=timestamp,
                    payload={"source_record_type": record_type},
                    provenance=provenance,
                )
            )
        else:
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    timestamp=timestamp,
                    payload={"source_record_type": record_type or "<missing>"},
                    provenance=provenance,
                )
            )

    if response_message_count == 0:
        events.extend(event for event in fallback_events if event.text)
        events.sort(key=lambda event: event.provenance.record_index)
    else:
        for event in fallback_events:
            fingerprint = _message_fingerprint(event)
            if event.text and response_messages[fingerprint]:
                response_messages[fingerprint] -= 1
                continue
            events.append(
                Event(
                    kind=EventKind.MESSAGE,
                    role=event.role,
                    text=event.text,
                    timestamp=event.timestamp,
                    payload={"ui_only_projection": True},
                    provenance=event.provenance,
                )
            )
        events.sort(key=lambda event: event.provenance.record_index)
    compaction_count = sum(event.kind == EventKind.COMPACTION for event in events)
    events.extend(context_compacted_events[compaction_count:])
    events.sort(key=lambda event: event.provenance.record_index)
    return Session(
        source_format=AgentFormat.CODEX,
        source_path=path.resolve(),
        source_sha256=file_sha256(path),
        session_id=session_id,
        cwd=cwd,
        started_at=started_at,
        cli_version=cli_version,
        model=model,
        title=title,
        events=tuple(events),
        raw_record_count=len(records),
        model_provider=model_provider,
    )


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_CODEX_VERSION,
    model_provider: str = "openai",
    timestamp: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize the model-visible subset accepted by Codex CLI 0.144.4."""

    fallback_timestamp = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    records: list[dict[str, Any]] = [
        {
            "timestamp": fallback_timestamp,
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "id": session_id,
                "timestamp": fallback_timestamp,
                "cwd": str(cwd),
                "originator": "session-migrate",
                "cli_version": cli_version,
                "source": "cli",
                "model_provider": model_provider,
                "history_mode": "legacy",
            },
        }
    ]
    dropped: Counter[str] = Counter()
    generated_tool_ids: deque[str] = deque()
    seen_tool_call_ids: set[str] = set()
    seen_tool_result_ids: set[str] = set()
    for event in session.events:
        event_timestamp = valid_rfc3339(event.timestamp)
        if event.timestamp and not event_timestamp:
            dropped["timestamp:invalid"] += 1
        event_timestamp = event_timestamp or fallback_timestamp
        if (
            event.kind == EventKind.MESSAGE
            and event.text
            and event.role in {Role.USER, Role.ASSISTANT}
        ):
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            if event.role == Role.ASSISTANT:
                records.append(
                    _envelope(
                        event_timestamp,
                        "event_msg",
                        {"type": "agent_message", "message": event.text},
                    )
                )
                content_type = "output_text"
                role = "assistant"
            else:
                records.append(
                    _envelope(
                        event_timestamp,
                        "event_msg",
                        {"type": "user_message", "message": event.text},
                    )
                )
                content_type = "input_text"
                role = "user"
            records.append(
                _envelope(
                    event_timestamp,
                    "response_item",
                    {
                        "type": "message",
                        "role": role,
                        "content": [{"type": content_type, "text": event.text}],
                    },
                )
            )
        elif event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id
            if not call_id:
                call_id = f"call_session_migrate_{uuid.uuid4().hex}"
                generated_tool_ids.append(call_id)
                dropped["tool_call:missing_id"] += 1
            tool_name = event.tool_name
            if not tool_name:
                tool_name = "unknown_tool"
                dropped["tool_call:missing_name"] += 1
            arguments = event.payload.get("input", {})
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            if call_id in seen_tool_call_ids:
                dropped["tool_call:duplicate_id"] += 1
            seen_tool_call_ids.add(call_id)
            records.append(
                _envelope(
                    event_timestamp,
                    "response_item",
                    {
                        "type": "function_call",
                        "name": tool_name,
                        "arguments": arguments,
                        "call_id": call_id,
                    },
                )
            )
            if event.payload.get("namespace"):
                dropped["tool_call:namespace"] += 1
        elif event.kind == EventKind.TOOL_RESULT:
            source_call_id = event.tool_call_id
            call_id = source_call_id
            if not call_id:
                call_id = (
                    generated_tool_ids.popleft()
                    if generated_tool_ids
                    else f"call_missing_{uuid.uuid4().hex}"
                )
                dropped["tool_result:missing_id"] += 1
            elif call_id not in seen_tool_call_ids:
                dropped["tool_result:orphan_id"] += 1
            if source_call_id and source_call_id in seen_tool_result_ids:
                dropped["tool_result:duplicate_id"] += 1
            if source_call_id:
                seen_tool_result_ids.add(source_call_id)
            output, omitted_blocks = _codex_tool_result_output(event)
            dropped.update(omitted_blocks)
            records.append(
                _envelope(
                    event_timestamp,
                    "response_item",
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    },
                )
            )
            if event.payload.get("is_error") is True:
                dropped["tool_result:is_error"] += 1
        elif (
            event.kind == EventKind.CONTEXT
            and event.payload.get("block_type") == "image"
            and event.role == Role.USER
        ):
            image_url = string(event.payload.get("image_url"))
            if image_url:
                records.append(
                    _envelope(
                        event_timestamp,
                        "response_item",
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_image", "image_url": image_url}],
                        },
                    )
                )
            else:
                dropped["context:image"] += 1
        elif event.kind == EventKind.COMPACTION and event.text:
            records.append(
                _envelope(
                    event_timestamp,
                    "compacted",
                    {"message": event.text},
                )
            )
            if event.payload.get("has_boundary_metadata") is True:
                dropped["compaction:boundary_metadata"] += 1
        else:
            dropped[_omission_key(event)] += 1
    if session.title:
        dropped["session:title"] += 1
    return encode_jsonl(records), dict(sorted(dropped.items()))


def rollout_relative_path(session_id: str, timestamp: str) -> Path:
    date = _parse_date(timestamp)
    filename_timestamp = date.strftime("%Y-%m-%dT%H-%M-%S")
    return (
        Path("sessions")
        / date.strftime("%Y")
        / date.strftime("%m")
        / date.strftime("%d")
        / f"rollout-{filename_timestamp}-{session_id}.jsonl"
    )


def _response_item_events(
    payload: dict[str, Any],
    timestamp: str | None,
    provenance: Provenance,
    *,
    genuine_user_texts: frozenset[str],
) -> list[Event]:
    item_type = string(payload.get("type"))
    if item_type == "message":
        role_name = string(payload.get("role"))
        if role_name == "assistant":
            role = Role.ASSISTANT
        elif role_name == "user":
            role = Role.USER
        elif role_name in {"developer", "system"}:
            role = Role.SYSTEM
        else:
            return [
                Event(
                    kind=EventKind.OPAQUE,
                    timestamp=timestamp,
                    payload={"reason": "unknown_message_role"},
                    provenance=provenance,
                )
            ]
        result: list[Event] = []
        content = payload.get("content")
        if not isinstance(content, list):
            return result
        if role == Role.USER:
            marker = _contextual_user_marker(
                _message_content_text(content),
                genuine_user_texts,
            )
            if marker:
                return [
                    Event(
                        kind=EventKind.CONTEXT,
                        role=Role.USER,
                        timestamp=timestamp,
                        payload={
                            "block_type": "codex_injected_user_context",
                            "context_source": marker,
                        },
                        provenance=provenance,
                    )
                ]
        for block_index, block in enumerate(content):
            block_provenance = Provenance(
                provenance.record_index,
                provenance.record_type,
                block_index=block_index,
            )
            if not isinstance(block, dict):
                result.append(Event(kind=EventKind.OPAQUE, provenance=block_provenance))
                continue
            block_type = string(block.get("type"))
            if block_type in {"input_text", "output_text", "text"}:
                text = string(block.get("text"))
                if text:
                    result.append(
                        Event(
                            kind=EventKind.MESSAGE,
                            role=role,
                            text=text,
                            timestamp=timestamp,
                            provenance=block_provenance,
                        )
                    )
            elif block_type in {"input_image", "image"}:
                image_url = string(block.get("image_url")) or string(block.get("url"))
                result.append(
                    Event(
                        kind=EventKind.CONTEXT,
                        role=role,
                        timestamp=timestamp,
                        payload={"block_type": "image", "image_url": image_url},
                        provenance=block_provenance,
                    )
                )
            else:
                result.append(
                    Event(
                        kind=EventKind.OPAQUE,
                        role=role,
                        timestamp=timestamp,
                        payload={"source_block_type": block_type or "<missing>"},
                        provenance=block_provenance,
                    )
                )
        return result
    if item_type in {"function_call", "custom_tool_call"}:
        arguments: Any = payload.get("arguments", payload.get("input", {}))
        if isinstance(arguments, str):
            with suppress(json.JSONDecodeError):
                arguments = json.loads(arguments)
        return [
            Event(
                kind=EventKind.TOOL_CALL,
                role=Role.ASSISTANT,
                timestamp=timestamp,
                tool_name=string(payload.get("name")),
                tool_call_id=string(payload.get("call_id")) or string(payload.get("id")),
                payload={
                    "input": arguments,
                    **(
                        {"namespace": payload["namespace"]}
                        if string(payload.get("namespace"))
                        else {}
                    ),
                },
                provenance=provenance,
            )
        ]
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        normalized_content = _portable_tool_result_content(payload.get("output"))
        return [
            Event(
                kind=EventKind.TOOL_RESULT,
                role=Role.TOOL,
                timestamp=timestamp,
                text=content_text(payload.get("output")),
                tool_call_id=string(payload.get("call_id")),
                payload={"content_blocks": normalized_content},
                provenance=provenance,
            )
        ]
    if item_type == "reasoning":
        return [
            Event(
                kind=EventKind.THINKING,
                role=Role.ASSISTANT,
                timestamp=timestamp,
                payload={"source_item_type": "reasoning"},
                provenance=provenance,
            )
        ]
    if item_type:
        return [
            Event(
                kind=EventKind.OPAQUE,
                timestamp=timestamp,
                payload={"source_item_type": item_type},
                provenance=provenance,
            )
        ]
    return []


def _portable_tool_result_content(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if not isinstance(value, list):
        return [{"type": "opaque"}]
    result: list[dict[str, Any]] = []
    for block in value:
        if not isinstance(block, dict):
            result.append({"type": "opaque"})
            continue
        block_type = string(block.get("type"))
        if block_type in {"input_text", "text"}:
            text = string(block.get("text"))
            if text:
                result.append({"type": "text", "text": text})
        elif block_type in {"input_image", "image"}:
            image_url = string(block.get("image_url")) or string(block.get("url"))
            if image_url:
                result.append({"type": "image", "image_url": image_url})
            else:
                result.append({"type": "opaque"})
        elif block_type == "tool_reference":
            tool_name = string(block.get("tool_name"))
            if tool_name:
                result.append({"type": "tool_reference", "tool_name": tool_name})
            else:
                result.append({"type": "opaque"})
        elif block_type == "input_audio":
            result.append({"type": "audio"})
        else:
            result.append({"type": "opaque"})
    return result


def _codex_tool_result_output(event: Event) -> tuple[str | list[dict[str, Any]], Counter[str]]:
    blocks = event.payload.get("content_blocks")
    if not isinstance(blocks, list):
        return event.text or "", Counter()
    result: list[dict[str, Any]] = []
    omitted: Counter[str] = Counter()
    for portable in blocks:
        if not isinstance(portable, dict):
            omitted["tool_result:block"] += 1
            continue
        block_type = portable.get("type")
        if block_type == "text" and isinstance(portable.get("text"), str):
            result.append({"type": "input_text", "text": portable["text"]})
        elif block_type == "image" and isinstance(portable.get("image_url"), str):
            result.append({"type": "input_image", "image_url": portable["image_url"]})
        else:
            omitted[f"tool_result:{block_type or 'block'}"] += 1
    if not result:
        return event.text or "", omitted
    if len(result) == 1 and result[0].get("type") == "input_text":
        return str(result[0]["text"]), omitted
    return result, omitted


def _envelope(timestamp: str, record_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": timestamp, "type": record_type, "payload": payload}


def _replacement_history_events(
    replacement_history: Any,
    timestamp: str | None,
    provenance: Provenance,
    genuine_user_texts: frozenset[str],
) -> tuple[Event, ...]:
    """Parse the plaintext history Codex carries across a compaction boundary.

    The trailing `compaction` item holds the summary, but it is provider-encrypted in
    every observed rollout, so only the surrounding messages are portable.
    """

    if not isinstance(replacement_history, list):
        return ()
    carried: list[Event] = []
    for item in replacement_history:
        if not isinstance(item, dict):
            continue
        carried.extend(
            _response_item_events(
                item,
                timestamp,
                provenance,
                genuine_user_texts=genuine_user_texts,
            )
        )
    return tuple(carried)


def _message_fingerprint(event: Event) -> tuple[Role | None, str]:
    return event.role, _normalized_user_text(event.text or "")


def _canonical_history_mode(records: Sequence[Any]) -> str | None:
    """Validate the first session_meta and return its history mode."""

    for record in records:
        value = record.value
        if string(value.get("type")) != "session_meta":
            continue
        payload = object_value(value.get("payload"))
        history_mode = string(payload.get("history_mode"))
        if history_mode and history_mode not in SUPPORTED_HISTORY_MODES:
            supported = ", ".join(sorted(SUPPORTED_HISTORY_MODES))
            raise SessionMigrateError(
                f"Codex history mode {history_mode!r} is not supported; expected {supported}"
            )
        if payload.get("history_base") is not None:
            raise SessionMigrateError("Codex history_base lineage is not supported")
        return history_mode
    return None


def _validate_paginated_ordinals(records: Sequence[Any]) -> None:
    """Fail closed when a paginated rollout is not a complete, ordered transcript.

    Paginated rollouts number every record. A missing, non-integer, decreasing, or
    non-zero starting ordinal means this file is not the whole thread, so importing it
    would silently drop history.
    """

    previous: int | None = None
    for record in records:
        ordinal = record.value.get("ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise SessionMigrateError(
                f"Codex paginated rollout is missing an integer 'ordinal' at record {record.index}"
            )
        if previous is None:
            if ordinal != 0:
                raise SessionMigrateError(
                    f"Codex paginated rollout starts at ordinal {ordinal}; expected 0 "
                    "because earlier pages are not present in this file"
                )
        elif ordinal < previous:
            raise SessionMigrateError(
                f"Codex paginated ordinals decrease at record {record.index} "
                f"({ordinal} after {previous})"
            )
        previous = ordinal


def _message_content_text(content: Sequence[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and string(block.get("type")) in _MESSAGE_TEXT_BLOCK_TYPES:
            text = string(block.get("text"))
            if text:
                parts.append(text)
    return "".join(parts)


def _item_content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and string(block.get("type")) in _ITEM_TEXT_BLOCK_TYPES:
            text = string(block.get("text"))
            if text:
                parts.append(text)
    return "".join(parts)


def _ui_user_texts(records: Sequence[Any]) -> frozenset[str]:
    """Collect the prompts Codex actually showed as user turns in its own UI.

    Legacy rollouts record these as ``event_msg.user_message``; paginated rollouts
    record them as ``item_completed`` items. A file uses one form or the other.
    """

    texts: set[str] = set()
    for record in records:
        value = record.value
        if string(value.get("type")) != "event_msg":
            continue
        payload = object_value(value.get("payload"))
        payload_type = string(payload.get("type"))
        if payload_type == "user_message":
            text = string(payload.get("message")) or ""
        elif payload_type == "item_completed":
            item = object_value(payload.get("item"))
            if string(item.get("type")) != "UserMessage":
                continue
            text = _item_content_text(item.get("content"))
        else:
            continue
        normalized = _normalized_user_text(text)
        if normalized:
            texts.add(normalized)
    return frozenset(texts)


def _normalized_user_text(text: str) -> str:
    """Normalize for comparison, dropping the image attachments Codex prepends."""

    without_attachments = _IMAGE_ATTACHMENT_PREFIX.sub("", text)
    return without_attachments.replace("\r\n", "\n").replace("\r", "\n").strip()


def _contextual_user_marker(text: str, genuine_user_texts: frozenset[str]) -> str | None:
    """Name the injected wrapper when a user-role item is not a real prompt.

    Confirmed UI prompts always win, so a genuine message is never reclassified. Text
    that matches no known wrapper is kept as a user message, because sub-agent threads
    receive real instructions that never appear in a UI transcript.
    """

    normalized = _normalized_user_text(text)
    if not normalized or normalized in genuine_user_texts:
        return None
    lowered = normalized.lower()
    for wrapper in CONTEXTUAL_USER_WRAPPERS:
        if lowered.startswith(wrapper):
            return wrapper.lstrip("<").rstrip(">")
    if normalized.startswith(AGENTS_INSTRUCTIONS_PREFIX):
        return "agents_md_instructions"
    return None


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.MESSAGE and event.role == Role.SYSTEM:
        return "message:privileged_role"
    if event.kind == EventKind.COMPACTION and event.payload.get("replacement_history_expanded"):
        return "compaction:replacement_history_expanded"
    if event.kind == EventKind.CONTEXT:
        if event.role == Role.SYSTEM and event.payload.get("block_type") == "image":
            return "context:privileged_image"
        if event.payload.get("source_record_type"):
            return f"context:{event.payload['source_record_type']}"
        return f"context:{event.payload.get('block_type', 'unknown')}"
    if event.kind == EventKind.OPAQUE:
        detail = next(
            (
                event.payload.get(key)
                for key in (
                    "reason",
                    "source_record_type",
                    "source_event_type",
                    "source_block_type",
                    "source_item_type",
                )
                if event.payload.get(key)
            ),
            "unknown",
        )
        return f"opaque:{detail}"
    return event.kind.value


def _parse_date(timestamp: str) -> datetime:
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return datetime.now(UTC)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
