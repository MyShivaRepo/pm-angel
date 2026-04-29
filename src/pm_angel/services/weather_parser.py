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
    "Amsterdam": {"country": "NL", "lat": 52.3676, "lon": 4.9041, "aliases": ["amsterdam"]},
    "Brussels": {"country": "BE", "lat": 50.8503, "lon": 4.3517, "aliases": ["brussels", "bruxelles"]},
    "Vienna": {"country": "AT", "lat": 48.2082, "lon": 16.3738, "aliases": ["vienna", "wien"]},
    "Stockholm": {"country": "SE", "lat": 59.3293, "lon": 18.0686, "aliases": ["stockholm"]},
    "Oslo": {"country": "NO", "lat": 59.9139, "lon": 10.7522, "aliases": ["oslo"]},
    "Copenhagen": {"country": "DK", "lat": 55.6761, "lon": 12.5683, "aliases": ["copenhagen"]},
    "Helsinki": {"country": "FI", "lat": 60.1699, "lon": 24.9384, "aliases": ["helsinki"]},
    "Dublin": {"country": "IE", "lat": 53.3498, "lon": -6.2603, "aliases": ["dublin"]},
    "Lisbon": {"country": "PT", "lat": 38.7223, "lon": -9.1393, "aliases": ["lisbon", "lisboa"]},
    "Athens": {"country": "GR", "lat": 37.9838, "lon": 23.7275, "aliases": ["athens"]},
    "Warsaw": {"country": "PL", "lat": 52.2297, "lon": 21.0122, "aliases": ["warsaw"]},
    "Prague": {"country": "CZ", "lat": 50.0755, "lon": 14.4378, "aliases": ["prague"]},
    "Beijing": {"country": "CN", "lat": 39.9042, "lon": 116.4074, "aliases": ["beijing"]},
    "Shanghai": {"country": "CN", "lat": 31.2304, "lon": 121.4737, "aliases": ["shanghai"]},
    "Taipei": {"country": "TW", "lat": 25.0330, "lon": 121.5654, "aliases": ["taipei"]},
    "Manila": {"country": "PH", "lat": 14.5995, "lon": 120.9842, "aliases": ["manila"]},
    "Jakarta": {"country": "ID", "lat": -6.2088, "lon": 106.8456, "aliases": ["jakarta"]},
    "Kuala Lumpur": {"country": "MY", "lat": 3.1390, "lon": 101.6869, "aliases": ["kuala lumpur"]},
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


_TEMP_THRESHOLD_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*(?:°\s*(C|F)?|degrees?\s*(C|F)?|(C|F)\b)",
    re.IGNORECASE,
)
_PRECIP_THRESHOLD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|cm|inches?|in)\b", re.IGNORECASE)
_RAIN_RE = re.compile(r"\b(rain|rainfall|precipit\w*)\b", re.IGNORECASE)
_SNOW_RE = re.compile(r"\b(snow|snowfall)\b", re.IGNORECASE)
_TEMP_RE = re.compile(r"\b(temperature|hottest|coldest|warmest|highest\s+temp|lowest\s+temp)\b", re.IGNORECASE)


def _detect_type_and_threshold(title: str) -> tuple[MarketType, float | None, Unit]:
    t = title.lower()

    # Rain / precipitation (word-boundary to avoid "Uk-rain-e")
    if _RAIN_RE.search(title):
        m = _PRECIP_THRESHOLD_RE.search(title)
        if m:
            value = float(m.group(1))
            unit_raw = m.group(2).lower()
            unit: Unit = "mm" if unit_raw == "mm" else ("cm" if unit_raw == "cm" else "in")
            return "rain", value, unit
        return "rain", None, ""

    # Snow
    if _SNOW_RE.search(title):
        m = _PRECIP_THRESHOLD_RE.search(title)
        if m:
            unit_raw = m.group(2).lower()
            unit_norm: Unit = "mm" if unit_raw == "mm" else ("cm" if unit_raw == "cm" else "in")
            return "snow", float(m.group(1)), unit_norm
        return "snow", None, ""

    # Temperature above/below
    if _TEMP_RE.search(title):
        below_kw = ["below", "less than", "under", "lower than", "<", "coldest", "or below"]

        # Threshold MUST have an explicit °/degrees/C/F marker to avoid
        # picking up dates like "April 29" or year numbers.
        m = _TEMP_THRESHOLD_RE.search(title)
        if not m:
            return "unknown", None, ""

        value = float(m.group(1))
        # The unit is in one of three optional groups depending on which form matched
        unit_raw = (m.group(2) or m.group(3) or m.group(4) or "").lower()
        unit: Unit = "F" if unit_raw.startswith("f") else "C"

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
