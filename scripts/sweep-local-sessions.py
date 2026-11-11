#!/usr/bin/env python3
"""Convert every local session through common routes and group the failures.

Run this after an agent CLI updates: it loads each Claude, Codex, Copilot, and
Antigravity session the way ``transfer`` does by default (cut at the last
compaction), converts it to each target in memory, and prints one line per
distinct failure with a count and an example file. Nothing is installed and no
conversation content is printed.
"""

from __future__ import annotations

import argparse
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    load_session,
    truncate_to_last_compaction,
)
from session_migrate.errors import NoCompactionBoundaryError, SessionMigrateError
from session_migrate.model import AgentFormat, TargetFormat

SOURCES = {
    AgentFormat.CLAUDE: ".claude/projects/*/*.jsonl",
    AgentFormat.CODEX: ".codex/sessions/*/*/*/rollout-*.jsonl",
    AgentFormat.COPILOT: ".copilot/session-state/*/events.jsonl",
    AgentFormat.ANTIGRAVITY: ".gemini/antigravity-cli/conversations/*.db",
}
ROUTES = {
    AgentFormat.CLAUDE: ((TargetFormat.CODEX, None), (TargetFormat.ANTIGRAVITY, "1.4.12")),
    AgentFormat.CODEX: ((TargetFormat.CLAUDE, None),),
    AgentFormat.COPILOT: ((TargetFormat.CLAUDE, None),),
    AgentFormat.ANTIGRAVITY: ((TargetFormat.CLAUDE, None),),
}
# Failures that are a session's own state rather than a reader or writer gap.
EXPECTED = (
    "sub-agent conversations cannot be converted directly",
    "conversation is empty",
    "no resumable conversation history",
    "has no resumable user conversation history",
    "requires at least one portable user message",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--full-history", action="store_true", help="do not cut at compactions")
    args = parser.parse_args()

    cwd = Path(tempfile.gettempdir())
    converted: Counter[str] = Counter()
    failures: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    started = time.monotonic()
    for source_format, pattern in SOURCES.items():
        paths = sorted(args.home.glob(pattern))
        for path in paths:
            name = path.parent.name if path.name == "events.jsonl" else path.name
            try:
                session = _load(path, source_format, full_history=args.full_history)
            except SessionMigrateError as exc:
                failures[(f"{source_format.value} load", str(exc))].append(name)
                continue
            for target, version in ROUTES[source_format]:
                route = f"{source_format.value}->{target.value}"
                try:
                    convert_session(
                        session,
                        ConversionOptions(
                            target_format=target, cwd=cwd, target_cli_version=version
                        ),
                    )
                    converted[route] += 1
                except SessionMigrateError as exc:
                    failures[(route, str(exc))].append(name)
        print(f"{source_format.value}: {len(paths)} sessions ({time.monotonic() - started:.0f}s)")

    print("converted:", dict(converted))
    unexpected = 0
    for (route, message), names in sorted(failures.items(), key=lambda item: -len(item[1])):
        expected = any(marker in message for marker in EXPECTED)
        unexpected += 0 if expected else len(names)
        label = "expected" if expected else "FAILURE "
        print(f"{label} {len(names):5d}  {route:22s} {message[:110]}  e.g. {names[0][:40]}")
    return 1 if unexpected else 0


def _load(path: Path, source_format: AgentFormat, *, full_history: bool):
    if full_history:
        return load_session(path, source_format)
    try:
        session = load_session(path, source_format, since_last_compaction=True)
    except NoCompactionBoundaryError:
        session = load_session(path, source_format)
    return truncate_to_last_compaction(session)[0]


if __name__ == "__main__":
    raise SystemExit(main())
