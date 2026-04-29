"""Parse Polymarket weather market titles into structured specs."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal

logger = logging.getLogger(__name__)


# Canonical city table: city → (country_code, lat, lon, aliases)
CITIES: dict[str, dict] = {
    "London": {"country": "GB", "lat": 51.5074, "lon": -0.1278, "aliases": ["london"]},
    "New York": {"country": "US", "lat": 40.7128, "lon": -74.0060, "aliases": ["new york", "nyc", "new york city"]},
    "Paris": {"country": "FR", "lat": 48.8566, "lon": 2.3522, "aliases": ["paris"]},
    "Tokyo": {"country": "JP", "lat": 35.6762, "lon": 139.6503, "aliases": ["tokyo"]},
    "Seoul": {"country": "KR", "lat": 37.5665, "lon": 126.9780, "aliases": ["seoul"]},
    "Berlin": {"country": "DE", "lat": 52.5200, "lon": 13.4050, "aliases": ["berlin"]},
    "Madrid": {"country": "ES", "lat": 40.4168, "lon": -3.7038, "aliases": ["madrid"]},
    "Rome": {"country": "IT", "lat": 41.9028, "lon": 12.4964, "aliases": ["rome", "roma"]},
    "Moscow": {"country": "RU", "lat": 55.7558, "lon": 37.6173, "aliases": ["moscow"]},
    "Sydney": {"country": "AU", "lat": -33.8688, "lon": 151.2093, "aliases": ["sydney"]},
    "Los Angeles": {"country": "US", "lat": 34.0522, "lon": -118.2437, "aliases": ["los angeles", "la "]},
    "Chicago": {"country": "US", "lat": 41.8781, "lon": -87.6298, "aliases": ["chicago"]},
    "Miami": {"country": "US", "lat": 25.7617, "lon": -80.1918, "aliases": ["miami"]},
    "San Francisco": {"country": "US", "lat": 37.7749, "lon": -122.4194, "aliases": ["san francisco", "sf"]},
    "Houston": {"country": "US", "lat": 29.7604, "lon": -95.3698, "aliases": ["houston"]},
    "Dallas": {"country": "US", "lat": 32.7767, "lon": -96.7970, "aliases": ["dallas"]},
    "Boston": {"country": "US", "lat": 42.3601, "lon": -71.0589, "aliases": ["boston"]},
    "Toronto": {"country": "CA", "lat": 43.6532, "lon": -79.3832, "aliases": ["toronto"]},
    "Mumbai": {"country": "IN", "lat": 19.0760, "lon": 72.8777, "aliases": ["mumbai"]},
    "Delhi": {"country": "IN", "lat": 28.7041, "lon": 77.1025, "aliases": ["delhi", "new delhi"]},
    "Hong Kong": {"country": "HK", "lat": 22.3193, "lon": 114.1694, "aliases": ["hong kong"]},
    "Singapore": {"country": "SG", "lat": 1.3521, "lon": 103.8198, "aliases": ["singapore"]},
    "Dubai": {"country": "AE", "lat": 25.2048, "lon": 55.2708, "aliases": ["dubai"]},
    "Bangkok": {"country": "TH", "lat": 13.7563, "lon": 100.5018, "aliases": ["bangkok"]},
    "Mexico City": {"country": "MX", "lat": 19.4326, "lon": -99.1332, "aliases": ["mexico city"]},
    "Sao Paulo": {"country": "BR", "lat": -23.5505, "lon": -46.6333, "aliases": ["sao paulo", "são paulo"]},
    "Buenos Aires": {"country": "AR", "lat": -34.6037, "lon": -58.3816, "aliases": ["buenos aires"]},
    "Cairo": {"country": "EG", "lat": 30.0444, "lon": 31.2357, "aliases": ["cairo"]},
    "Lagos": {"country": "NG", "lat": 6.5244, "lon": 3.3792, "aliases": ["lagos"]},
    "Istanbul": {"country": "TR", "lat": 41.0082, "lon": 28.9784, "aliases": ["istanbul"]},
}


MarketType = Literal["rain", "snow", "temp_above", "temp_below", "unknown"]
Unit = Literal["C", "F", "mm", "cm", "in", ""]


@dataclass
class MarketSpec:
    raw_title: str
    market_type: MarketType
    city: str
    country_code: str
    lat: float
    lon: float
    target_date: date | None
    threshold: float | None
    unit: Unit
    parse_status: str  # "ok" or "unparseable"
    parse_notes: str


def _find_city(title: str) -> tuple[str, str, float, float] | None:
    title_lower = title.lower()
    # Sort by alias length desc to match "new york" before "york"
    candidates = sorted(
        ((alias, name, info) for name, info in CITIES.items() for alias in info["aliases"]),
        key=lambda x: -len(x[0]),
    )
    for alias, name, info in candidates:
        # Use word-boundary regex
        if re.search(rf"\b{re.escape(alias)}\b", title_lower):
            return name, info["country"], info["lat"], info["lon"]
    return None


_TEMP_THRESHOLD_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:°|degrees?\s*)?(C|F|celsius|fahrenheit)?", re.IGNORECASE)
_PRECIP_THRESHOLD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|cm|inches?|in)\b", re.IGNORECASE)


def _detect_type_and_threshold(title: str) -> tuple[MarketType, float | None, Unit]:
    t = title.lower()

    # Rain / precipitation
    if any(kw in t for kw in ["rain", "rainfall", "precipitation", "precipitate"]):
        m = _PRECIP_THRESHOLD_RE.search(t)
        if m:
            value = float(m.group(1))
            unit_raw = m.group(2).lower()
            unit: Unit = "mm" if unit_raw == "mm" else ("cm" if unit_raw == "cm" else "in")
            return "rain", value, unit
        return "rain", None, ""

    # Snow
    if any(kw in t for kw in ["snow", "snowfall"]):
        m = _PRECIP_THRESHOLD_RE.search(t)
        if m:
            return "snow", float(m.group(1)), m.group(2).lower().replace("inches", "in").replace("inch", "in")  # type: ignore
        return "snow", None, ""

    # Temperature above/below
    has_temp = any(kw in t for kw in ["temperature", "temp ", "hottest", "coldest", "warm", "cold"])
    if has_temp or "high" in t or "low" in t:
        # Determine direction
        below_kw = ["below", "less than", "under", "lower than", "<", "coldest"]
        above_kw = ["above", "more than", "over", "higher than", "greater than", ">", "hottest", "highest"]

        # Find threshold value
        m = _TEMP_THRESHOLD_RE.search(title)
        if m:
            value = float(m.group(1))
            unit_raw = (m.group(2) or "").lower()
            unit: Unit = "F" if unit_raw.startswith("f") else "C"
            # If no explicit unit, guess by value range
            if not unit_raw:
                unit = "F" if value > 50 else "C"

            # Direction: default to above
            if any(kw in t for kw in below_kw):
                return "temp_below", value, unit
            return "temp_above", value, unit

    return "unknown", None, ""


def parse(title: str, end_date: datetime | None = None) -> MarketSpec:
    """Parse a market title into a structured spec."""
    title = (title or "").strip()
    notes: list[str] = []

    # City
    city_info = _find_city(title)
    if not city_info:
        return MarketSpec(
            raw_title=title,
            market_type="unknown",
            city="",
            country_code="",
            lat=0.0,
            lon=0.0,
            target_date=None,
            threshold=None,
            unit="",
            parse_status="unparseable",
            parse_notes="Aucune ville reconnue",
        )
    city, country, lat, lon = city_info

    # Type & threshold
    market_type, threshold, unit = _detect_type_and_threshold(title)
    if market_type == "unknown":
        return MarketSpec(
            raw_title=title,
            market_type="unknown",
            city=city,
            country_code=country,
            lat=lat,
            lon=lon,
            target_date=None,
            threshold=None,
            unit="",
            parse_status="unparseable",
            parse_notes="Type de marche non reconnu (rain/snow/temp)",
        )

    # Target date: prefer end_date from market metadata
    target_date: date | None = None
    if end_date:
        # Convert to UTC date
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        target_date = end_date.astimezone(timezone.utc).date()
    if not target_date:
        notes.append("Pas de date de resolution")

    return MarketSpec(
        raw_title=title,
        market_type=market_type,
        city=city,
        country_code=country,
        lat=lat,
        lon=lon,
        target_date=target_date,
        threshold=threshold,
        unit=unit,
        parse_status="ok" if target_date else "unparseable",
        parse_notes="; ".join(notes),
    )


def to_celsius(value: float, unit: Unit) -> float:
    if unit == "F":
        return (value - 32) * 5 / 9
    return value
