from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from pm_angel.api.clob import ClobWrapper
from pm_angel.api.data_api import DataApiClient
from pm_angel.api.gamma_api import GammaApiClient
from pm_angel.config import Settings
from pm_angel.database import async_session, close_db, init_db
from pm_angel.models import Bet, TrackedTrader
from pm_angel.services.activity_poller import ActivityPoller
from pm_angel.services.risk_manager import RiskManager
from pm_angel.services.trade_executor import TradeExecutor, update_prices
from pm_angel.services.trader_discovery import TraderDiscovery

logger = logging.getLogger(__name__)

# --- Globals ---
settings: Settings = Settings.from_env()
data_api = DataApiClient(settings.data_api_host)
gamma_api = GammaApiClient(settings.gamma_api_host)
clob = ClobWrapper(
    host=settings.clob_host,
    private_key=settings.private_key,
    chain_id=settings.chain_id,
    api_key=settings.clob_api_key,
    api_secret=settings.clob_api_secret,
    api_passphrase=settings.clob_api_passphrase,
)
risk_manager = RiskManager(settings)
discovery = TraderDiscovery(data_api, gamma_api)
poller: ActivityPoller | None = None
executor: TradeExecutor | None = None
price_update_task: asyncio.Task | None = None

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    await init_db(settings.db_path)
    logger.info("PM Angel started on %s:%d", settings.host, settings.port)
    yield
    await _stop_bot()
    await data_api.close()
    await gamma_api.close()
    await close_db()
    logger.info("PM Angel shutdown complete")


app = FastAPI(title="PM Angel", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --- Page Routes ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    stats = await _get_stats()
    bets = await _get_bets()
    return templates.TemplateResponse(request, "dashboard.html", context={
        "active_page": "dashboard",
        "stats": stats,
        "bets": bets,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", context={
        "active_page": "settings",
        "settings": settings,
        "has_credentials": settings.has_credentials,
        "bot_running": poller is not None and poller.is_running,
    })


@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(request: Request):
    return templates.TemplateResponse(request, "leaderboard.html", context={
        "active_page": "leaderboard",
    })


# --- HTMX API Routes ---

@app.get("/api/bets/table", response_class=HTMLResponse)
async def bets_table(request: Request):
    bets = await _get_bets()
    return templates.TemplateResponse(request, "partials/bets_table.html", context={
        "bets": bets,
    })


@app.get("/api/status", response_class=HTMLResponse)
async def status_bar(request: Request):
    running = poller is not None and poller.is_running
    balance = None
    positions = 0
    if running and settings.has_credentials:
        try:
            balance = await clob.get_balance()
        except Exception:
            pass
        positions = await _count_active_bets()
    return templates.TemplateResponse(request, "partials/status_bar.html", context={
        "status": {
            "running": running,
            "balance_usdc": balance,
            "total_positions": positions,
        },
    })


@app.get("/api/leaderboard", response_class=HTMLResponse)
async def leaderboard_data(
    request: Request,
    category: str = "OVERALL",
    time_period: str = "MONTH",
):
    try:
        traders = await discovery.get_top_traders(
            category=category,
            time_period=time_period,
            limit=25,
        )
    except Exception as exc:
        logger.error("Leaderboard fetch failed: %s", exc)
        traders = []

    rows = []
    for t in traders:
        rows.append(
            f'<tr>'
            f'<td>{t.rank}</td>'
            f'<td>{t.username or t.address[:12] + "..."}</td>'
            f'<td class="{"positive" if t.pnl >= 0 else "negative"}">'
            f'{"+" if t.pnl >= 0 else ""}${t.pnl:,.2f}</td>'
            f'<td>${t.volume:,.0f}</td>'
            f'<td><button class="btn btn-sm btn-primary" '
            f'hx-post="/api/traders" hx-vals=\'{{"address": "{t.address}"}}\' '
            f'hx-swap="none" '
            f'hx-on::after-request="this.textContent=\'Ajoute\'">Suivre</button></td>'
            f'</tr>'
        )

    if not rows:
        return HTMLResponse(
            '<tr><td colspan="5" class="empty-state">Aucun resultat</td></tr>'
        )
    return HTMLResponse("\n".join(rows))


# --- Bot Control ---

@app.post("/api/bot/start")
async def start_bot():
    global poller, executor, price_update_task
    if poller and poller.is_running:
        return JSONResponse({"status": "already_running"})

    if not settings.has_credentials:
        return JSONResponse({"error": "No API credentials configured"}, status_code=400)

    # Load tracked traders from DB
    traders = await _get_tracked_addresses()
    if not traders:
        traders = settings.target_traders

    if not traders:
        return JSONResponse({"error": "No target traders configured"}, status_code=400)

    poller = ActivityPoller(data_api, gamma_api, traders, settings.poll_interval_seconds)
    executor = TradeExecutor(clob, risk_manager, gamma_api, settings)

    poller.start()
    executor.start(poller.trade_queue)

    # Start periodic price updater (every 60s)
    price_update_task = asyncio.create_task(_price_update_loop())

    logger.info("Bot started with %d target traders", len(traders))
    return JSONResponse({"status": "started", "traders": len(traders)})


@app.post("/api/bot/stop")
async def stop_bot():
    await _stop_bot()
    return JSONResponse({"status": "stopped"})


async def _stop_bot():
    global poller, executor, price_update_task
    if poller:
        poller.stop()
        poller = None
    if executor:
        executor.stop()
        executor = None
    if price_update_task:
        price_update_task.cancel()
        price_update_task = None


async def _price_update_loop():
    while True:
        try:
            await asyncio.sleep(60)
            await update_prices(clob)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Price update error")


# --- Trader Management ---

@app.post("/api/traders", response_class=HTMLResponse)
async def add_trader(request: Request, address: str = Form(...)):
    address = address.strip()
    if not address.startswith("0x") or len(address) < 10:
        return HTMLResponse('<p class="text-warning">Adresse invalide</p>')

    if async_session is None:
        return HTMLResponse('<p class="text-warning">Database not ready</p>')

    async with async_session() as session:
        existing = await session.execute(
            select(TrackedTrader).where(TrackedTrader.address == address)
        )
        if existing.scalar_one_or_none():
            return await _render_traders_list(request)

        trader = TrackedTrader(address=address)
        # Try to fetch username
        try:
            profile = await gamma_api.get_public_profile(address)
            trader.username = profile.get("name", profile.get("username", ""))
        except Exception:
            pass

        session.add(trader)
        await session.commit()

    # Add to active poller if running
    if poller and poller.is_running:
        poller.add_trader(address)

    return await _render_traders_list(request)


@app.get("/api/traders/list", response_class=HTMLResponse)
async def traders_list(request: Request):
    return await _render_traders_list(request)


@app.delete("/api/traders/{address}", response_class=HTMLResponse)
async def remove_trader(request: Request, address: str):
    if async_session is None:
        return HTMLResponse("")

    async with async_session() as session:
        result = await session.execute(
            select(TrackedTrader).where(TrackedTrader.address == address)
        )
        trader = result.scalar_one_or_none()
        if trader:
            await session.delete(trader)
            await session.commit()

    if poller:
        poller.remove_trader(address)

    return await _render_traders_list(request)


async def _render_traders_list(request: Request) -> HTMLResponse:
    traders = await _get_tracked_traders()
    if not traders:
        return HTMLResponse('<p class="text-muted" style="padding:1rem 0;">Aucun trader suivi</p>')

    html_parts = []
    for t in traders:
        html_parts.append(
            f'<div class="trader-item">'
            f'  <div class="trader-info">'
            f'    <span class="trader-name">{t.username or "Anonyme"}</span>'
            f'    <span class="trader-address">{t.address}</span>'
            f'  </div>'
            f'  <button class="btn btn-sm btn-danger" '
            f'    hx-delete="/api/traders/{t.address}" '
            f'    hx-target="#traders-list" hx-swap="innerHTML">Retirer</button>'
            f'</div>'
        )
    return HTMLResponse("\n".join(html_parts))


# --- Settings API ---

@app.post("/api/settings")
async def update_settings(
    max_position_usd: float = Form(100),
    max_total_exposure_usd: float = Form(500),
    daily_loss_limit_usd: float = Form(50),
    position_scale_factor: float = Form(0.1),
    poll_interval_seconds: float = Form(15),
):
    global settings
    settings = Settings(
        private_key=settings.private_key,
        clob_api_key=settings.clob_api_key,
        clob_api_secret=settings.clob_api_secret,
        clob_api_passphrase=settings.clob_api_passphrase,
        target_traders=settings.target_traders,
        max_position_usd=max_position_usd,
        max_total_exposure_usd=max_total_exposure_usd,
        daily_loss_limit_usd=daily_loss_limit_usd,
        position_scale_factor=position_scale_factor,
        poll_interval_seconds=poll_interval_seconds,
        host=settings.host,
        port=settings.port,
    )
    # Update risk manager with new settings
    risk_manager._max_position_usd = max_position_usd
    risk_manager._max_total_exposure = max_total_exposure_usd
    risk_manager._daily_loss_limit = daily_loss_limit_usd
    risk_manager._min_order_usd = settings.min_order_usd

    return JSONResponse({"status": "saved"})


@app.post("/api/setup/derive-keys")
async def derive_keys():
    if not settings.private_key:
        return HTMLResponse(
            '<p class="text-warning">Renseignez PK dans .env d\'abord</p>'
        )
    try:
        creds = await clob.derive_api_creds()
        return HTMLResponse(
            f'<div class="help-text">'
            f'<p class="text-success">Credentials generees ! Ajoutez-les dans .env :</p>'
            f'<code>CLOB_API_KEY={creds["api_key"]}</code><br>'
            f'<code>CLOB_API_SECRET={creds["api_secret"]}</code><br>'
            f'<code>CLOB_API_PASSPHRASE={creds["api_passphrase"]}</code>'
            f'</div>'
        )
    except Exception as exc:
        return HTMLResponse(f'<p class="text-warning">Erreur: {exc}</p>')


# --- Helpers ---

async def _get_bets() -> list[Bet]:
    if async_session is None:
        return []
    async with async_session() as session:
        result = await session.execute(
            select(Bet).order_by(Bet.created_at.desc()).limit(100)
        )
        return list(result.scalars().all())


async def _get_stats() -> dict[str, Any]:
    if async_session is None:
        return {"active_count": 0, "total_pnl": 0, "total_exposure": 0, "tracked_traders": 0}

    async with async_session() as session:
        active = await session.execute(
            select(func.count()).select_from(Bet).where(Bet.status == "active")
        )
        pnl = await session.execute(
            select(func.coalesce(func.sum(Bet.pnl_absolute), 0)).select_from(Bet)
        )
        exposure = await session.execute(
            select(func.coalesce(func.sum(Bet.amount_usd), 0))
            .select_from(Bet)
            .where(Bet.status == "active")
        )
        traders = await session.execute(
            select(func.count()).select_from(TrackedTrader).where(TrackedTrader.is_active.is_(True))
        )

    return {
        "active_count": active.scalar() or 0,
        "total_pnl": pnl.scalar() or 0.0,
        "total_exposure": exposure.scalar() or 0.0,
        "tracked_traders": traders.scalar() or 0,
    }


async def _count_active_bets() -> int:
    if async_session is None:
        return 0
    async with async_session() as session:
        result = await session.execute(
            select(func.count()).select_from(Bet).where(Bet.status == "active")
        )
        return result.scalar() or 0


async def _get_tracked_traders() -> list[TrackedTrader]:
    if async_session is None:
        return []
    async with async_session() as session:
        result = await session.execute(
            select(TrackedTrader).where(TrackedTrader.is_active.is_(True))
        )
        return list(result.scalars().all())


async def _get_tracked_addresses() -> list[str]:
    traders = await _get_tracked_traders()
    return [t.address for t in traders]


def run():
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
