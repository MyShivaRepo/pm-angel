"""In-memory ring buffer for live bot activity log."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class LogEntry:
    timestamp: datetime
    level: str  # info, success, warning, error
    category: str  # discover, parse, forecast, decide, execute
    message: str
    details: dict = field(default_factory=dict)


class DecisionLog:
    def __init__(self, maxlen: int = 200):
        self._entries: deque[LogEntry] = deque(maxlen=maxlen)

    def add(self, level: str, category: str, message: str, **details) -> None:
        self._entries.appendleft(LogEntry(
            timestamp=datetime.now(timezone.utc),
            level=level,
            category=category,
            message=message,
            details=details,
        ))

    def info(self, category: str, message: str, **details) -> None:
        self.add("info", category, message, **details)

    def success(self, category: str, message: str, **details) -> None:
        self.add("success", category, message, **details)

    def warning(self, category: str, message: str, **details) -> None:
        self.add("warning", category, message, **details)

    def error(self, category: str, message: str, **details) -> None:
        self.add("error", category, message, **details)

    def get_entries(self, limit: int = 100) -> list[LogEntry]:
        return list(self._entries)[:limit]


# Global singleton
decision_log = DecisionLog()
