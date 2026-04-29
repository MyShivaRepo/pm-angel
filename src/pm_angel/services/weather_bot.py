"""Orchestrator for the weather trading bot."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from pm_angel.api.clob import ClobWrapper
from pm_angel.api.gamma_api import GammaApiClient
from pm_angel.api.openmeteo import OpenMeteoClient
from pm_angel.config import Settings
from pm_angel import database as db
from pm_angel.models import Position, WeatherDecision, WeatherMarket
from pm_angel.services.decision_log import decision_log
from pm_angel.services.forecast_engine import ForecastVerdict, compute_prob_yes
from pm_angel.services.weather_parser import MarketSpec, parse

logger = logging.getLogger(__name__)


def _parse_outcome_prices(market: dict) -> tuple[float, float]:
    """Extract (yes_price, no_price) from a Polymarket market dict."""
    raw = market.get("outcomePrices") or market.get("outcome_prices")
    if not raw:
        return 0.0, 0.0
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return 0.0, 0.0
    if isinstance(raw, list) and len(raw) >= 2:
        try:
            return float(raw[0]), float(raw[1])
        except (TypeError, ValueError):
            return 0.0, 0.0
    return 0.0, 0.0


def _parse_token_ids(market: dict) -> tuple[str, str]:
    """Extract (yes_token_id, no_token_id) from market data."""
    raw = market.get("clobTokenIds") or market.get("clob_token_ids")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    if isinstance(raw, list) and len(raw) >= 2:
        return str(raw[0]), str(raw[1])
    return "", ""


def _parse_end_date(market: dict) -> datetime | None:
    end = market.get("endDate") or market.get("end_date") or market.get("endDateIso")
    if not end:
        return None
    try:
        # Handle both "2026-04-25T12:00:00Z" and "...+00:00"
        end_str = str(end).replace("Z", "+00:00")
        dt = datetime.fromisoformat(end_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class WeatherBot:
    def __init__(
        self,
        gamma: GammaApiClient,
        openmeteo: OpenMeteoClient,
        clob: ClobWrapper | None,
        settings: Settings,
    ):
        self._gamma = gamma
        self._om = openmeteo
        self._clob = clob
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._running = False
        self._active_markets: set[str] = set()  # condition_ids with active position
        self._active_events: set[str] = set()   # event slugs with at least one active position
        self._cycle_event_bets: set[str] = set()  # event slugs already bet during current cycle

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        decision_log.info("system", "Bot demarre")
        logger.info("WeatherBot started")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        decision_log.info("system", "Bot arrete")
        logger.info("WeatherBot stopped")

    async def _loop(self) -> None:
        # Load active markets from DB
        await self._load_active_markets()
        # First tick immediately
        try:
            await self._tick()
        except Exception:
            logger.exception("Initial tick failed")

        while self._running:
            try:
                await asyncio.sleep(max(60, self._settings.forecast_poll_minutes * 60))
            except asyncio.CancelledError:
                break
            try:
                await self._tick()
            except Exception:
                logger.exception("Tick failed")
                decision_log.error("system", f"Erreur cycle: {type(__import__('sys').exc_info()[1]).__name__}")

    async def _load_active_markets(self) -> None:
        if db.async_session is None:
            return
        async with db.async_session() as session:
            result = await session.execute(
                select(Position.condition_id).where(Position.status == "active").distinct()
            )
            cids = list(result.scalars().all())
            for cid in cids:
                self._active_markets.add(cid)
            # Load corresponding event slugs from WeatherMarket for stronger dedup
            if cids:
                ev_result = await session.execute(
                    select(WeatherMarket.event_id).where(WeatherMarket.condition_id.in_(cids))
                )
                for ev in ev_result.scalars().all():
                    if ev:
                        self._active_events.add(ev)

    async def run_once(self) -> dict:
        """Run a single tick on demand. Returns summary stats."""
        return await self._tick()

    async def _tick(self) -> dict:
        decision_log.info("discover", "Recherche des marches meteo...")
        try:
            markets = await self._gamma.get_weather_markets(limit=200)
        except Exception as exc:
            decision_log.error("discover", f"Echec decouverte marches: {exc}")
            return {"discovered": 0}

        decision_log.info("discover", f"{len(markets)} marches meteo decouverts")
        # Reset per-cycle event lock (one bet per event per cycle)
        self._cycle_event_bets = set()
        # Group markets by event to compute the best opportunity per event
        markets_by_event: dict[str, list[dict]] = {}
        for m in markets:
            ev_slug = m.get("eventSlug") or m.get("eventTitle") or m.get("conditionId", "")
            markets_by_event.setdefault(ev_slug, []).append(m)

        n_parsed_ok = 0
        n_skipped = 0
        n_executed = 0

        for m in markets:
            cid = m.get("conditionId") or m.get("condition_id")
            if not cid:
                continue
            title = m.get("question") or m.get("title") or ""
            event_title = m.get("eventTitle") or ""
            slug = m.get("slug") or ""
            # Use eventSlug as stable event identifier (unique across Polymarket)
            event_id = m.get("eventSlug") or m.get("eventTitle") or ""

            end_date = _parse_end_date(m)
            yes_price, no_price = _parse_outcome_prices(m)
            yes_token, no_token = _parse_token_ids(m)

            # Combine event + market title to maximize parser hit rate
            combined_title = f"{event_title} {title}".strip() if event_title else title
            spec = parse(combined_title, end_date)

            # Filter: only configured cities
            if spec.city and self._settings.cities and spec.city not in self._settings.cities:
                spec.parse_status = "unparseable"
                spec.parse_notes = (spec.parse_notes + " ; " if spec.parse_notes else "") + f"ville {spec.city} non surveillee"

            # Compute forecast
            verdict: ForecastVerdict | None = None
            if spec.parse_status == "ok":
                n_parsed_ok += 1
                try:
                    verdict = await compute_prob_yes(spec, self._om)
                except Exception as exc:
                    decision_log.warning("forecast", f"Erreur prevision pour {spec.city}: {exc}")

            # Compute edge
            edge = None
            if verdict is not None and yes_price > 0:
                edge_yes = verdict.prob_yes - yes_price
                edge_no = (1 - verdict.prob_yes) - no_price
                # signed: positive = YES underpriced
                edge = edge_yes if abs(edge_yes) >= abs(edge_no) else edge_no

            # Persist market state
            await self._upsert_market(
                condition_id=cid, event_id=event_id, title=title, slug=slug,
                spec=spec, yes_price=yes_price, no_price=no_price,
                yes_token_id=yes_token, no_token_id=no_token,
                verdict=verdict, edge=edge,
            )

            # Decision
            if verdict is None or yes_price <= 0:
                n_skipped += 1
                continue

            edge_yes = verdict.prob_yes - yes_price
            edge_no = (1 - verdict.prob_yes) - no_price
            best_edge = max(edge_yes, edge_no)
            min_edge = self._settings.min_edge_pct

            decision_str = "SKIP"
            reason = ""
            target_token = ""
            target_outcome = ""
            target_price = 0.0

            ev_slug = m.get("eventSlug") or m.get("eventTitle") or ""
            is_neg_risk = bool(m.get("negRisk"))

            if cid in self._active_markets:
                decision_str = "SKIP"
                reason = "Position deja active sur ce marche"
            elif ev_slug and ev_slug in self._active_events:
                decision_str = "SKIP"
                reason = "Position deja active sur cet event"
            elif ev_slug and ev_slug in self._cycle_event_bets:
                decision_str = "SKIP"
                reason = "Pari deja place sur cet event ce cycle"
            elif is_neg_risk:
                decision_str = "SKIP"
                reason = "Marche NegRisk non supporte (Magic Link)"
            elif best_edge < min_edge:
                decision_str = "SKIP"
                reason = f"Edge {best_edge*100:.1f}% < seuil {min_edge*100:.0f}%"
            elif edge_yes >= edge_no:
                decision_str = "BUY_YES"
                reason = f"YES sous-evalue: prob={verdict.prob_yes*100:.0f}% vs prix={yes_price*100:.0f}%"
                target_token = yes_token
                target_outcome = "Yes"
                target_price = yes_price
            else:
                decision_str = "BUY_NO"
                reason = f"NO sous-evalue: prob={(1-verdict.prob_yes)*100:.0f}% vs prix={no_price*100:.0f}%"
                target_token = no_token
                target_outcome = "No"
                target_price = no_price

            # Sizing
            amount = 0.0
            if decision_str != "SKIP":
                edge_mag = max(edge_yes, edge_no)
                amount = self._settings.max_bet_usd * min(edge_mag / 0.30, 1.0)
                amount = max(self._settings.min_bet_usd, min(amount, self._settings.max_bet_usd))

                # Exposure cap
                exposure = await self._current_exposure()
                if exposure + amount > self._settings.max_total_exposure_usd:
                    decision_str = "SKIP"
                    reason = f"Exposition max atteinte ($-{exposure:.2f}/${self._settings.max_total_exposure_usd:.0f})"

            # Persist decision
            position_id: int | None = None
            if decision_str != "SKIP" and target_token and self._clob:
                try:
                    neg_risk = bool(m.get("negRisk"))
                    decision_log.info("execute", f"Pari {target_outcome} ${amount:.2f} sur {title[:40]}")
                    result = await self._clob.place_market_order(
                        token_id=target_token,
                        amount_usd=amount,
                        side="BUY",
                        neg_risk=neg_risk,
                    )
                    position_id = await self._save_position(
                        condition_id=cid, token_id=target_token,
                        market_title=title, outcome=target_outcome,
                        amount_usd=amount, entry_price=target_price,
                        status="active",
                    )
                    self._active_markets.add(cid)
                    if ev_slug:
                        self._active_events.add(ev_slug)
                        self._cycle_event_bets.add(ev_slug)
                    n_executed += 1
                    decision_log.success("execute", f"Pari pris: {target_outcome} ${amount:.2f} sur {title[:40]}")
                except Exception as exc:
                    decision_log.error("execute", f"Echec ordre: {exc}")
                    decision_str = "SKIP"
                    reason = f"Echec execution: {exc}"

            await self._save_decision(
                condition_id=cid, market_title=title,
                yes_price=yes_price, no_price=no_price,
                forecast_prob_yes=verdict.prob_yes, edge=best_edge,
                decision=decision_str, reason=reason,
                position_id=position_id,
            )

            if decision_str == "SKIP":
                n_skipped += 1

        decision_log.info(
            "summary",
            f"Cycle termine: {n_parsed_ok} marches analyses, {n_executed} pris, {n_skipped} ignores",
        )

        # Update PnL on active positions
        await self._update_position_prices()

        return {
            "discovered": len(markets),
            "parsed_ok": n_parsed_ok,
            "executed": n_executed,
            "skipped": n_skipped,
        }

    async def _upsert_market(
        self, *, condition_id: str, event_id: str, title: str, slug: str,
        spec: MarketSpec, yes_price: float, no_price: float,
        yes_token_id: str, no_token_id: str,
        verdict: ForecastVerdict | None, edge: float | None,
    ) -> None:
        if db.async_session is None:
            return
        async with db.async_session() as session:
            existing = await session.get(WeatherMarket, condition_id)
            now = datetime.now(timezone.utc)
            if existing:
                existing.title = title
                existing.slug = slug
                existing.event_id = event_id
                existing.city = spec.city
                existing.country_code = spec.country_code
                existing.resolves_at = datetime.combine(spec.target_date, datetime.min.time(), tzinfo=timezone.utc) if spec.target_date else None
                existing.market_type = spec.market_type
                existing.threshold_value = spec.threshold
                existing.threshold_unit = spec.unit or ""
                existing.yes_token_id = yes_token_id
                existing.no_token_id = no_token_id
                existing.yes_price = yes_price
                existing.no_price = no_price
                if verdict:
                    existing.forecast_prob_yes = verdict.prob_yes
                    existing.forecast_rationale = verdict.rationale
                    existing.forecast_updated_at = now
                existing.edge = edge
                existing.parse_status = spec.parse_status
                existing.parse_notes = spec.parse_notes
                existing.last_seen_at = now
            else:
                wm = WeatherMarket(
                    condition_id=condition_id,
                    event_id=event_id,
                    title=title,
                    slug=slug,
                    city=spec.city,
                    country_code=spec.country_code,
                    resolves_at=datetime.combine(spec.target_date, datetime.min.time(), tzinfo=timezone.utc) if spec.target_date else None,
                    market_type=spec.market_type,
                    threshold_value=spec.threshold,
                    threshold_unit=spec.unit or "",
                    yes_token_id=yes_token_id,
                    no_token_id=no_token_id,
                    yes_price=yes_price,
                    no_price=no_price,
                    forecast_prob_yes=verdict.prob_yes if verdict else None,
                    forecast_rationale=verdict.rationale if verdict else "",
                    forecast_updated_at=now if verdict else None,
                    edge=edge,
                    parse_status=spec.parse_status,
                    parse_notes=spec.parse_notes,
                    last_seen_at=now,
                )
                session.add(wm)
            await session.commit()

    async def _save_decision(
        self, *, condition_id: str, market_title: str,
        yes_price: float, no_price: float,
        forecast_prob_yes: float, edge: float,
        decision: str, reason: str, position_id: int | None,
    ) -> None:
        if db.async_session is None:
            return
        async with db.async_session() as session:
            entry = WeatherDecision(
                condition_id=condition_id,
                market_title=market_title,
                market_yes_price=yes_price,
                market_no_price=no_price,
                forecast_prob_yes=forecast_prob_yes,
                edge=edge,
                decision=decision,
                reason=reason,
                position_id=position_id,
            )
            session.add(entry)
            await session.commit()

    async def _save_position(
        self, *, condition_id: str, token_id: str, market_title: str,
        outcome: str, amount_usd: float, entry_price: float, status: str,
    ) -> int:
        if db.async_session is None:
            return 0
        async with db.async_session() as session:
            pos = Position(
                condition_id=condition_id,
                token_id=token_id,
                market_title=market_title,
                side="BUY",
                outcome=outcome,
                amount_usd=amount_usd,
                entry_price=entry_price,
                current_price=entry_price,
                status=status,
            )
            session.add(pos)
            await session.commit()
            return pos.id

    async def _current_exposure(self) -> float:
        if db.async_session is None:
            return 0.0
        async with db.async_session() as session:
            result = await session.execute(
                select(func.coalesce(func.sum(Position.amount_usd), 0.0))
                .where(Position.status == "active")
            )
            return float(result.scalar() or 0.0)

    async def _update_position_prices(self) -> None:
        """Refresh current_price and PnL on active positions."""
        if db.async_session is None or self._clob is None:
            return
        async with db.async_session() as session:
            result = await session.execute(
                select(Position).where(Position.status == "active")
            )
            positions = result.scalars().all()
            for pos in positions:
                try:
                    price = await self._clob.get_midpoint(pos.token_id)
                    pos.current_price = price
                    pos.update_pnl()
                except Exception:
                    continue
            await session.commit()
