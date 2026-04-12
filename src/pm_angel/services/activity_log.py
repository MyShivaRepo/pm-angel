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
        self._detected_trades: deque[dict] = deque(maxlen=500)

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

    def detect(self, trader: str, market: str, side: str, outcome: str, amount: float, price: float) -> None:
        self.add("info", "detect",
                 f"Trade detecte: {side} ${amount:.2f} @ {price:.4f}",
                 trader=trader, market=market, side=side, outcome=outcome, amount=amount, price=price)
        self._detected_trades.appendleft({
            "trader": trader,
            "market": market,
            "side": side,
            "outcome": outcome,
            "amount": amount,
            "price": price,
            "decision": "pending",
            "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        })

    def risk_approved(self, market: str, amount: float) -> None:
        self.add("success", "risk", f"Approuve: ${amount:.2f}", market=market)
        self._update_decision(market, "executed")

    def risk_rejected(self, market: str, amount: float, reason: str) -> None:
        self.add("warning", "risk", f"Rejete: ${amount:.2f} — {reason}", market=market)
        self._update_decision(market, "rejected")

    def order_success(self, market: str, side: str, amount: float) -> None:
        self.add("success", "execute", f"Ordre execute: {side} ${amount:.2f}", market=market)
        self._update_decision(market, "executed")

    def order_failed(self, market: str, error: str) -> None:
        self.add("error", "execute", f"Ordre echoue: {error}", market=market)
        self._update_decision(market, "skipped")

    def market_skipped(self, market: str) -> None:
        self._update_decision(market, "skipped")

    def _update_decision(self, market: str, decision: str) -> None:
        for t in self._detected_trades:
            if t["market"] == market and t["decision"] == "pending":
                t["decision"] = decision
                break

    def get_entries(self, limit: int = 50, category: str | None = None) -> list[LogEntry]:
        entries = list(self._entries)
        if category:
            entries = [e for e in entries if e.category == category]
        return entries[:limit]

    def get_detected_trades(self, limit: int = 100) -> list[dict]:
        return list(self._detected_trades)[:limit]


# Global singleton
activity_log = ActivityLog()
