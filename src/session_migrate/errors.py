"""User-facing failures with content-safe messages."""


class SessionMigrateError(Exception):
    """Base error for expected parse, validation, and installation failures."""


class JsonlError(SessionMigrateError):
    """A JSONL stream is unreadable or structurally invalid."""


class NoCompactionBoundaryError(SessionMigrateError):
    """A session has no compaction to cut at, so its full transcript is the window."""

    def __init__(self) -> None:
        super().__init__(
            "--since-last-compaction found no compaction boundary in this session; "
            "the full transcript is already its effective window"
        )


class FormatDetectionError(SessionMigrateError):
    """The agent session format cannot be identified safely."""
