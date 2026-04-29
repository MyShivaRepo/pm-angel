"""Forecast engine: convert MarketSpec + Open-Meteo data into P(YES)."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from pm_angel.api.openmeteo import OpenMeteoClient
from pm_angel.services.weather_parser import MarketSpec, to_celsius

logger = logging.getLogger(__name__)


@dataclass
class ForecastVerdict:
    prob_yes: float  # 0..1
    rationale: str
    raw: dict


def _logistic(x: float, k: float = 1.0) -> float:
    """Standard logistic: 1/(1+e^(-k*x))"""
    try:
        return 1.0 / (1.0 + math.exp(-k * x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _first(values: list | None, default: float = 0.0) -> float:
    if not values:
        return default
    v = values[0]
    return float(v) if v is not None else default


async def compute_prob_yes(spec: MarketSpec, om: OpenMeteoClient) -> ForecastVerdict | None:
    """Compute P(YES) for the given market spec. Returns None if not computable."""
    if spec.parse_status != "ok" or not spec.target_date:
        return None

    if spec.market_type == "rain":
        try:
            d = await om.get_daily(
                spec.lat, spec.lon, spec.target_date,
                ["precipitation_probability_max", "precipitation_sum"],
            )
        except Exception as e:
            logger.warning("Open-Meteo rain query failed: %s", e)
            return None

        if spec.threshold and spec.unit in ("mm", "cm", "in"):
            mm = spec.threshold * (10 if spec.unit == "cm" else 25.4 if spec.unit == "in" else 1)
            forecast_mm = _first(d.get("precipitation_sum"))
            margin = forecast_mm - mm
            p = _logistic(margin, k=2.0)
            return ForecastVerdict(
                prob_yes=p,
                rationale=f"Open-Meteo: precip_sum={forecast_mm:.1f}mm vs seuil={mm:.1f}mm",
                raw=d,
            )
        # Plain "will it rain" → use precipitation_probability_max
        prob_max = _first(d.get("precipitation_probability_max")) / 100.0
        return ForecastVerdict(
            prob_yes=prob_max,
            rationale=f"Open-Meteo: precip_probability_max={prob_max*100:.0f}%",
            raw=d,
        )

    if spec.market_type == "snow":
        try:
            d = await om.get_daily(
                spec.lat, spec.lon, spec.target_date,
                ["snowfall_sum"],
            )
        except Exception as e:
            logger.warning("Open-Meteo snow query failed: %s", e)
            return None

        snow_cm = _first(d.get("snowfall_sum"))
        threshold_cm = spec.threshold or 0.1
        if spec.unit == "mm":
            threshold_cm = (spec.threshold or 1.0) / 10.0
        elif spec.unit == "in":
            threshold_cm = (spec.threshold or 0.04) * 2.54
        margin = snow_cm - threshold_cm
        p = _logistic(margin, k=3.0)
        return ForecastVerdict(
            prob_yes=p,
            rationale=f"Open-Meteo: snowfall={snow_cm:.1f}cm vs seuil={threshold_cm:.2f}cm",
            raw=d,
        )

    if spec.market_type in ("temp_above", "temp_below"):
        try:
            d = await om.get_daily(
                spec.lat, spec.lon, spec.target_date,
                ["temperature_2m_max", "temperature_2m_min"],
            )
        except Exception as e:
            logger.warning("Open-Meteo temp query failed: %s", e)
            return None

        forecast_max = _first(d.get("temperature_2m_max"))
        if spec.threshold is None:
            return None

        threshold_c = to_celsius(spec.threshold, spec.unit) if spec.unit else spec.threshold
        margin = forecast_max - threshold_c
        # sigma=2°C captures forecast uncertainty at 24-72h horizon
        p_above = _logistic(margin, k=0.5)
        p = p_above if spec.market_type == "temp_above" else 1 - p_above
        direction = ">" if spec.market_type == "temp_above" else "<"
        return ForecastVerdict(
            prob_yes=p,
            rationale=f"Open-Meteo: max={forecast_max:.1f}°C {direction} {threshold_c:.1f}°C",
            raw=d,
        )

    return None
