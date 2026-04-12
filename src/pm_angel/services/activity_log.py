"""In-memory activity log for the Analysis page."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class LogEntry:
    timestamp: datetime
    level: str  # info, success, warning, error
    category: str  # poll, detect, risk, execute, sync
    message: str
    details: dict = field(default_factory=dict)


class ActivityLog:
    """Ring buffer of recent bot activity for the Analysis page."""

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

    def poll(self, trader: str, trades_found: int) -> None:
        if trades_found:
            self.add("info", "poll", f"Poll {trader[:12]}... → {trades_found} nouveau(x) trade(s)")
        # Don't log empty polls to avoid noise

    def detect(self, trader: str, market: str, side: str, amount: float, price: float) -> None:
        self.add("info", "detect",
                 f"Trade detecte: {side} ${amount:.2f} @ {price:.4f}",
                 trader=trader, market=market)

    def risk_approved(self, market: str, amount: float) -> None:
        self.add("success", "risk", f"Approuve: ${amount:.2f}", market=market)

    def risk_rejected(self, market: str, amount: float, reason: str) -> None:
        self.add("warning", "risk", f"Rejete: ${amount:.2f} — {reason}", market=market)

    def order_success(self, market: str, side: str, amount: float) -> None:
        self.add("success", "execute", f"Ordre execute: {side} ${amount:.2f}", market=market)

    def order_failed(self, market: str, error: str) -> None:
        self.add("error", "execute", f"Ordre echoue: {error}", market=market)

    def get_entries(self, limit: int = 50, category: str | None = None) -> list[LogEntry]:
        entries = list(self._entries)
        if category:
            entries = [e for e in entries if e.category == category]
        return entries[:limit]


# Global singleton
activity_log = ActivityLog()
