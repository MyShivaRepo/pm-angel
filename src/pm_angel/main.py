from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select

from pm_angel.api.clob import ClobWrapper
from pm_angel.api.data_api import DataApiClient
from pm_angel.api.gamma_api import GammaApiClient
from pm_angel.api.openmeteo import OpenMeteoClient
from pm_angel.config import Settings
from pm_angel import database as db
from pm_angel.database import close_db, init_db
from pm_angel.models import Position, WeatherDecision, WeatherMarket
from pm_angel.services.decision_log import decision_log
from pm_angel.services.weather_bot import WeatherBot
from pm_angel.services.weather_parser import CITIES

logger = logging.getLogger(__name__)

# --- Globals ---
settings: Settings = Settings.from_env()
data_api = DataApiClient(settings.data_api_host)
gamma_api = GammaApiClient(settings.gamma_api_host)
openmeteo = OpenMeteoClient(settings.openmeteo_host)
clob: ClobWrapper | None = None
bot: WeatherBot | None = None

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _rebuild_clob() -> None:
    global clob
    if settings.has_credentials:
        clob = ClobWrapper(
            host=settings.clob_host,
            private_key=settings.private_key,
            chain_id=settings.chain_id,
            api_key=settings.clob_api_key,
            api_secret=settings.clob_api_secret,
            api_passphrase=settings.clob_api_passphrase,
            funder=settings.proxy_wallet,
        )
    else:
        clob = None


def _rebuild_bot() -> None:
    global bot
    bot = WeatherBot(gamma_api, openmeteo, clob, settings)


def _mask_key(key: str) -> str:
    if len(key) < 10:
        return "***"
    return key[:6] + "..." + key[-4:]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    await init_db(settings.db_path)
    await settings.load_from_db()

    # Resolve proxy wallet on first launch if missing
    if settings.has_private_key and not settings.proxy_wallet:
        try:
            from eth_account import Account
            eoa = Account.from_key(settings.private_key).address
            profile = await gamma_api.get_public_profile(eoa)
            proxy = profile.get("proxyWallet", "")
            if proxy:
                settings.proxy_wallet = proxy
                await settings.save_to_db("proxy_wallet", proxy)
                logger.info("Proxy wallet resolved: %s", proxy)
        except Exception as exc:
            logger.warning("Could not resolve proxy wallet: %s", exc)

    _rebuild_clob()
    _rebuild_bot()

    # Auto-start bot if credentials are configured
    if settings.has_credentials and bot:
        bot.start()
        logger.info("Bot auto-started")

    logger.info("PM Angel Weather Bot started on %s:%d", settings.host, settings.port)
    yield

    if bot:
        bot.stop()
    await data_api.close()
    await gamma_api.close()
    await openmeteo.close()
    await close_db()


app = FastAPI(title="PM Angel - Weather Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --- Page Routes ---

@app.get("/", response_class=HTMLResponse)
async def index():
    return RedirectResponse("/markets", status_code=302)


@app.get("/markets", response_class=HTMLResponse)
async def markets_page(request: Request):
    return templates.TemplateResponse(request, "markets.html", context={
        "active_page": "markets",
    })


@app.get("/analysis", response_class=HTMLResponse)
async def analysis_page(request: Request):
    return templates.TemplateResponse(request, "analysis.html", context={
        "active_page": "analysis",
    })


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    stats = await _get_stats()
    return templates.TemplateResponse(request, "dashboard.html", context={
        "active_page": "dashboard",
        "stats": stats,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", context={
        "active_page": "settings",
        "settings": settings,
        "has_credentials": settings.has_credentials,
        "bot_running": bot is not None and bot.is_running,
        "all_cities": list(CITIES.keys()),
        "pk_masked": _mask_key(settings.private_key) if settings.private_key else "",
    })


# --- HTMX Partials ---

@app.get("/api/status", response_class=HTMLResponse)
async def status_bar(request: Request):
    running = bot is not None and bot.is_running
    balance = None
    if running and clob:
        try:
            balance = await clob.get_balance()
        except Exception:
            pass
    if running:
        bal_str = f" | ${balance:.2f} USDC" if balance is not None else ""
        return HTMLResponse(
            f'<span class="status-dot online"></span> Bot actif{bal_str}'
        )
    return HTMLResponse('<span class="status-dot offline"></span> Bot arrete')


@app.get("/api/markets/table", response_class=HTMLResponse)
async def markets_table(request: Request):
    if db.async_session is None:
        return HTMLResponse('<tr><td colspan="8" class="empty-state">DB non initialisee</td></tr>')
    async with db.async_session() as session:
        result = await session.execute(
            select(WeatherMarket).order_by(desc(WeatherMarket.last_seen_at)).limit(100)
        )
        markets = list(result.scalars().all())

    if not markets:
        return HTMLResponse(
            '<tr><td colspan="8" class="empty-state">Aucun marche meteo decouvert. '
            'Demarrez le bot dans Settings.</td></tr>'
        )

    rows = []
    for m in markets:
        edge_str = f"{m.edge*100:+.1f}%" if m.edge is not None else "—"
        edge_class = "positive" if (m.edge or 0) > 0 else "negative" if (m.edge or 0) < 0 else ""
        forecast_str = f"{m.forecast_prob_yes*100:.0f}%" if m.forecast_prob_yes is not None else "—"
        resolves = m.resolves_at.strftime("%d/%m") if m.resolves_at else "—"
        type_label = {
            "rain": "Pluie", "snow": "Neige",
            "temp_above": "Temp >", "temp_below": "Temp <",
            "unknown": "Inconnu",
        }.get(m.market_type, m.market_type)
        threshold = ""
        if m.threshold_value is not None:
            threshold = f" {m.threshold_value:g}{m.threshold_unit}"
        type_full = f"{type_label}{threshold}"
        rows.append(
            f'<tr>'
            f'<td>{m.title[:60]}</td>'
            f'<td>{m.city or "—"}</td>'
            f'<td>{type_full}</td>'
            f'<td>{resolves}</td>'
            f'<td>{m.yes_price*100:.0f}c / {m.no_price*100:.0f}c</td>'
            f'<td>{forecast_str}</td>'
            f'<td class="{edge_class}">{edge_str}</td>'
            f'<td><span class="muted">{m.parse_status}</span></td>'
            f'</tr>'
        )
    return HTMLResponse("\n".join(rows))


@app.get("/api/decisions/table", response_class=HTMLResponse)
async def decisions_table(request: Request):
    if db.async_session is None:
        return HTMLResponse('<tr><td colspan="7" class="empty-state">DB non initialisee</td></tr>')
    async with db.async_session() as session:
        result = await session.execute(
            select(WeatherDecision).order_by(desc(WeatherDecision.decided_at)).limit(100)
        )
        decisions = list(result.scalars().all())

    if not decisions:
        return HTMLResponse(
            '<tr><td colspan="7" class="empty-state">Aucune decision pour le moment.</td></tr>'
        )

    rows = []
    for d in decisions:
        decision_class = {
            "BUY_YES": "positive",
            "BUY_NO": "positive",
            "SKIP": "muted",
        }.get(d.decision, "")
        decision_label = {
            "BUY_YES": "ACHETE YES",
            "BUY_NO": "ACHETE NO",
            "SKIP": "Ignore",
        }.get(d.decision, d.decision)
        edge_str = f"{d.edge*100:+.1f}%" if d.edge is not None else "—"
        forecast_str = f"{d.forecast_prob_yes*100:.0f}%" if d.forecast_prob_yes is not None else "—"
        rows.append(
            f'<tr>'
            f'<td>{d.market_title[:60]}</td>'
            f'<td>{d.market_yes_price*100:.0f}c</td>'
            f'<td>{forecast_str}</td>'
            f'<td>{edge_str}</td>'
            f'<td class="{decision_class}">{decision_label}</td>'
            f'<td class="muted" style="font-size:0.85em">{d.reason}</td>'
            f'<td>{d.decided_at.strftime("%H:%M:%S")}</td>'
            f'</tr>'
        )
    return HTMLResponse("\n".join(rows))


@app.get("/api/positions/table", response_class=HTMLResponse)
async def positions_table(request: Request):
    if db.async_session is None:
        return HTMLResponse('<tr><td colspan="7" class="empty-state">DB non initialisee</td></tr>')
    async with db.async_session() as session:
        result = await session.execute(
            select(Position).order_by(desc(Position.created_at)).limit(100)
        )
        positions = list(result.scalars().all())

    if not positions:
        return HTMLResponse(
            '<tr><td colspan="7" class="empty-state">Aucun pari pris. Demarrez le bot.</td></tr>'
        )

    rows = []
    for p in positions:
        status_label = {
            "pending": "A prendre",
            "active": "En cours",
            "settled": "Termine",
            "failed": "Echoue",
        }.get(p.status, p.status)
        pnl_class = "positive" if p.pnl_absolute >= 0 else "negative"
        sign = "+" if p.pnl_absolute >= 0 else ""
        outcome_class = "outcome-yes" if p.outcome.lower() == "yes" else "outcome-no"
        rows.append(
            f'<tr class="bet-row {p.status}">'
            f'<td>{p.market_title[:60]}</td>'
            f'<td><span class="outcome-badge {outcome_class}">{p.outcome}</span></td>'
            f'<td>${p.amount_usd:.2f}</td>'
            f'<td><span class="status-badge {p.status}">{status_label}</span></td>'
            f'<td class="{pnl_class}">{sign}${p.pnl_absolute:.2f}</td>'
            f'<td class="{pnl_class}">{sign}{p.pnl_percent:.1f}%</td>'
            f'<td>{p.created_at.strftime("%d/%m %H:%M")}</td>'
            f'</tr>'
        )
    return HTMLResponse("\n".join(rows))


@app.get("/api/log", response_class=HTMLResponse)
async def get_log(request: Request):
    entries = decision_log.get_entries(limit=50)
    if not entries:
        return HTMLResponse(
            '<div class="log-empty">Aucune activite. Demarrez le bot.</div>'
        )
    html_parts = []
    for e in entries:
        css = f"log-{e.level}"
        html_parts.append(
            f'<div class="log-line {css}">'
            f'<span class="log-time">{e.timestamp.strftime("%H:%M:%S")}</span> '
            f'<span class="log-cat">[{e.category}]</span> '
            f'<span>{e.message}</span>'
            f'</div>'
        )
    return HTMLResponse("\n".join(html_parts))


# --- Bot Control ---

@app.post("/api/bot/start")
async def start_bot():
    if not settings.has_credentials:
        return JSONResponse({"error": "Credentials non configurees"}, status_code=400)
    if not clob:
        _rebuild_clob()
    if not bot:
        _rebuild_bot()
    if bot:
        bot.start()
        return JSONResponse({"status": "started"})
    return JSONResponse({"error": "Bot non initialise"}, status_code=500)


@app.post("/api/bot/stop")
async def stop_bot():
    if bot:
        bot.stop()
    return JSONResponse({"status": "stopped"})


@app.post("/api/bot/run-once")
async def run_once():
    """Trigger a single tick of the bot (for testing)."""
    if not bot:
        return JSONResponse({"error": "Bot non initialise"}, status_code=400)
    if not settings.has_credentials:
        return JSONResponse({"error": "Credentials non configurees"}, status_code=400)
    try:
        result = await bot.run_once()
        return JSONResponse({"status": "ok", "result": result})
    except Exception as exc:
        logger.exception("run_once failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


# --- Setup ---

@app.post("/api/setup/private-key", response_class=HTMLResponse)
async def save_private_key(private_key: str = Form(...)):
    pk = private_key.strip().strip("'\"").strip()
    if pk.lower().startswith("0x"):
        pk = pk[2:]
    pk = pk.replace(" ", "").replace("-", "").replace("\n", "").replace("\r", "")

    hex_chars = set("0123456789abcdefABCDEF")
    invalid = [c for c in pk if c not in hex_chars]
    if invalid:
        return HTMLResponse(
            f'<p class="text-warning">Cle invalide : caracteres non-hexa: {", ".join(repr(c) for c in invalid[:5])}</p>'
        )
    if len(pk) != 64:
        return HTMLResponse(
            f'<p class="text-warning">Cle invalide : {len(pk)} caracteres au lieu de 64</p>'
        )
    pk = "0x" + pk.lower()

    settings.private_key = pk
    await settings.save_to_db("private_key", pk)

    # Resolve proxy wallet + derive credentials
    try:
        from eth_account import Account
        eoa = Account.from_key(pk).address
        profile = await gamma_api.get_public_profile(eoa)
        proxy = profile.get("proxyWallet", "")
        if proxy:
            settings.proxy_wallet = proxy
            await settings.save_to_db("proxy_wallet", proxy)

        temp_clob = ClobWrapper(
            host=settings.clob_host, private_key=pk,
            chain_id=settings.chain_id,
            api_key="", api_secret="", api_passphrase="",
            funder=proxy,
        )
        creds = await temp_clob.derive_api_creds(funder=proxy)
        settings.clob_api_key = creds["api_key"]
        settings.clob_api_secret = creds["api_secret"]
        settings.clob_api_passphrase = creds["api_passphrase"]
        await settings.save_to_db("clob_api_key", creds["api_key"])
        await settings.save_to_db("clob_api_secret", creds["api_secret"])
        await settings.save_to_db("clob_api_passphrase", creds["api_passphrase"])
        _rebuild_clob()
        _rebuild_bot()
        return HTMLResponse(
            '<p class="text-success">Cle privee + credentials API generees automatiquement !</p>'
            '<script>setTimeout(function(){location.reload()},1500)</script>'
        )
    except Exception as exc:
        logger.error("Key derivation failed: %s", exc)
        return HTMLResponse(
            f'<p class="text-success">Cle privee sauvegardee.</p>'
            f'<p class="text-warning">Generation auto echouee: {exc}</p>'
        )


@app.post("/api/settings")
async def update_settings(
    cities: str = Form(""),
    min_edge_pct: float = Form(10.0),
    min_bet_usd: float = Form(1.0),
    max_bet_usd: float = Form(10.0),
    max_total_exposure_usd: float = Form(80.0),
    forecast_poll_minutes: float = Form(60.0),
):
    cities_list = [c.strip() for c in cities.split(",") if c.strip()]
    if cities_list:
        settings.cities = cities_list
        await settings.save_to_db("cities", ",".join(cities_list))

    settings.min_edge_pct = min_edge_pct / 100.0  # input is %, store as fraction
    settings.min_bet_usd = min_bet_usd
    settings.max_bet_usd = max_bet_usd
    settings.max_total_exposure_usd = max_total_exposure_usd
    settings.forecast_poll_minutes = forecast_poll_minutes

    await settings.save_to_db("min_edge_pct", str(settings.min_edge_pct))
    await settings.save_to_db("min_bet_usd", str(min_bet_usd))
    await settings.save_to_db("max_bet_usd", str(max_bet_usd))
    await settings.save_to_db("max_total_exposure_usd", str(max_total_exposure_usd))
    await settings.save_to_db("forecast_poll_minutes", str(forecast_poll_minutes))

    return JSONResponse({"status": "saved"})


# --- Helpers ---

async def _get_stats() -> dict[str, Any]:
    if db.async_session is None:
        return {
            "balance": 0.0, "positions_value": 0.0, "portfolio": 0.0,
            "active_count": 0, "total_pnl": 0.0, "total_exposure": 0.0,
        }

    async with db.async_session() as session:
        active = await session.execute(
            select(func.count()).select_from(Position).where(Position.status == "active")
        )
        pnl = await session.execute(
            select(func.coalesce(func.sum(Position.pnl_absolute), 0)).select_from(Position)
        )
        exposure = await session.execute(
            select(func.coalesce(func.sum(Position.amount_usd), 0))
            .select_from(Position).where(Position.status == "active")
        )

    balance = 0.0
    positions_value = 0.0
    if clob:
        try:
            balance = await clob.get_balance()
        except Exception:
            pass
    if settings.proxy_wallet:
        try:
            positions = await data_api.get_positions(settings.proxy_wallet)
            positions_value = sum(float(p.get("currentValue", p.get("value", 0))) for p in positions)
        except Exception:
            pass

    return {
        "balance": balance,
        "positions_value": positions_value,
        "portfolio": balance + positions_value,
        "active_count": active.scalar() or 0,
        "total_pnl": float(pnl.scalar() or 0.0),
        "total_exposure": float(exposure.scalar() or 0.0),
    }


def run():
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
