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
from pm_angel import database as db
from pm_angel.database import close_db, init_db
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
clob: ClobWrapper | None = None
risk_manager = RiskManager(settings)
discovery = TraderDiscovery(data_api, gamma_api)
poller: ActivityPoller | None = None
executor: TradeExecutor | None = None
price_update_task: asyncio.Task | None = None

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _rebuild_clob() -> None:
    """Rebuild CLOB client with current settings credentials."""
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    await init_db(settings.db_path)
    # Load credentials from DB
    await settings.load_from_db()
    # Resolve proxy wallet if not yet stored
    if settings.has_private_key and not settings.proxy_wallet:
        try:
            from eth_account import Account
            eoa = Account.from_key(settings.private_key).address
            profile = await gamma_api.get_public_profile(eoa)
            proxy = profile.get("proxyWallet", "")
            if proxy:
                settings.proxy_wallet = proxy
                await settings.save_to_db("proxy_wallet", proxy)
                # Re-derive credentials with correct signature_type
                temp_clob = ClobWrapper(
                    host=settings.clob_host, private_key=settings.private_key,
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
                logger.info("Proxy wallet resolved and credentials re-derived: %s", proxy)
        except Exception as exc:
            logger.warning("Could not resolve proxy wallet: %s", exc)
    _rebuild_clob()
    # Auto-populate suggested traders on first launch
    await _auto_suggest_traders()
    # Sync existing Polymarket positions
    await _sync_positions()
    # Auto-start bot if credentials are configured
    if settings.has_credentials:
        traders = await _get_tracked_addresses()
        if traders:
            await start_bot()
            logger.info("Bot auto-started with %d traders", len(traders))
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
        "has_credentials": settings.has_credentials,
        "bot_running": poller is not None and poller.is_running,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", context={
        "active_page": "settings",
        "settings": settings,
        "has_private_key": settings.has_private_key,
        "has_credentials": settings.has_credentials,
        "bot_running": poller is not None and poller.is_running,
        "pk_masked": _mask_key(settings.private_key) if settings.private_key else "",
    })


@app.get("/analysis", response_class=HTMLResponse)
async def analysis_page(request: Request):
    from pm_angel.services.activity_log import activity_log

    traders = await _get_tracked_traders()
    entries = activity_log.get_entries(limit=50)
    detected = len([e for e in activity_log.get_entries(limit=500) if e.category == "detect"])
    executed = len([e for e in activity_log.get_entries(limit=500) if e.category == "execute" and e.level == "success"])

    return templates.TemplateResponse(request, "analysis.html", context={
        "active_page": "analysis",
        "bot_running": poller is not None and poller.is_running,
        "traders": traders,
        "entries": entries,
        "detected_count": detected,
        "executed_count": executed,
        "risk": risk_manager.get_summary(),
    })


@app.get("/api/analysis/log", response_class=HTMLResponse)
async def analysis_log(request: Request, category: str | None = None):
    from pm_angel.services.activity_log import activity_log

    entries = activity_log.get_entries(limit=50, category=category)
    return templates.TemplateResponse(request, "partials/log_entries.html", context={
        "entries": entries,
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
    if running and clob:
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


# --- Credentials Setup (from web UI) ---

@app.post("/api/setup/api-credentials", response_class=HTMLResponse)
async def save_api_credentials(
    api_key: str = Form(...),
    api_secret: str = Form(...),
    api_passphrase: str = Form(...),
):
    key = api_key.strip()
    secret = api_secret.strip()
    passphrase = api_passphrase.strip()

    if not key or not secret or not passphrase:
        return HTMLResponse(
            '<p class="text-warning">Les 3 champs sont obligatoires.</p>'
        )

    settings.clob_api_key = key
    settings.clob_api_secret = secret
    settings.clob_api_passphrase = passphrase
    await settings.save_to_db("clob_api_key", key)
    await settings.save_to_db("clob_api_secret", secret)
    await settings.save_to_db("clob_api_passphrase", passphrase)

    _rebuild_clob()
    logger.info("API credentials saved from web UI")

    return HTMLResponse(
        '<p class="text-success">Credentials API sauvegardees ! Vous pouvez maintenant demarrer le bot.</p>'
    )


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
            f'<p class="text-warning">Cle invalide : caracteres non-hexadecimaux trouves: '
            f'{", ".join(repr(c) for c in invalid[:5])}.</p>'
        )
    if len(pk) != 64:
        return HTMLResponse(
            f'<p class="text-warning">Cle invalide : {len(pk)} caracteres au lieu de 64.</p>'
        )
    pk = "0x" + pk.lower()

    settings.private_key = pk
    await settings.save_to_db("private_key", pk)
    logger.info("Private key saved to database")

    # Resolve proxy wallet and auto-derive API credentials
    try:
        # Get proxy wallet address
        from eth_account import Account
        eoa = Account.from_key(pk).address
        profile = await gamma_api.get_public_profile(eoa)
        proxy = profile.get("proxyWallet", "")
        if proxy:
            settings.proxy_wallet = proxy
            await settings.save_to_db("proxy_wallet", proxy)
            logger.info("Proxy wallet found: %s", proxy)

        # Derive credentials with signature_type=1 (Magic Link) and funder=proxy
        temp_clob = ClobWrapper(
            host=settings.clob_host,
            private_key=pk,
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
        logger.info("API credentials auto-derived and saved (proxy=%s)", proxy)
        return HTMLResponse(
            '<p class="text-success">Cle privee sauvegardee et credentials API generees automatiquement !</p>'
            '<script>setTimeout(function(){location.reload()},1500)</script>'
        )
    except Exception as exc:
        logger.error("Auto key derivation failed: %s", exc)
        return HTMLResponse(
            f'<p class="text-success">Cle privee sauvegardee.</p>'
            f'<p class="text-warning">Generation auto des credentials echouee ({exc}). '
            f'Saisissez-les manuellement ci-dessous.</p>'
        )


# --- Bot Control ---

@app.post("/api/bot/start")
async def start_bot():
    global poller, executor, price_update_task
    if poller and poller.is_running:
        return JSONResponse({"status": "already_running"})

    if not settings.has_credentials:
        return JSONResponse(
            {"error": "Credentials API non configurees. Allez dans Settings."},
            status_code=400,
        )

    if not clob:
        _rebuild_clob()
    if not clob:
        return JSONResponse({"error": "Impossible de creer le client CLOB"}, status_code=400)

    # Load tracked traders from DB
    traders = await _get_tracked_addresses()
    if not traders:
        return JSONResponse(
            {"error": "Aucun trader suivi. Ajoutez des traders depuis Settings ou Leaderboard."},
            status_code=400,
        )

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
            if clob:
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

    if db.async_session is None:
        return HTMLResponse('<p class="text-warning">Database not ready</p>')

    async with db.async_session() as session:
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
    if db.async_session is None:
        return HTMLResponse("")

    async with db.async_session() as session:
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


@app.post("/api/traders/refresh-suggestions", response_class=HTMLResponse)
async def refresh_suggestions():
    """Fetch top traders from leaderboard and add them automatically."""
    count = await _auto_suggest_traders(force=True)
    return HTMLResponse(
        f'<p class="text-success">{count} traders ajoutes depuis le leaderboard !</p>'
    )


async def _render_traders_list(request: Request) -> HTMLResponse:
    traders = await _get_tracked_traders()
    if not traders:
        return HTMLResponse(
            '<p class="text-muted" style="padding:1rem 0;">Aucun trader suivi. '
            'Cliquez "Ajouter les meilleurs traders" pour en ajouter automatiquement.</p>'
        )

    html_parts = []
    for t in traders:
        pnl_class = "positive" if t.pnl >= 0 else "negative" if t.pnl < 0 else ""
        pnl_str = f' | PnL: <span class="{pnl_class}">${t.pnl:,.0f}</span>' if t.pnl else ""
        html_parts.append(
            f'<div class="trader-item">'
            f'  <div class="trader-info">'
            f'    <span class="trader-name">{t.username or "Anonyme"}{pnl_str}</span>'
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
    settings.max_position_usd = max_position_usd
    settings.max_total_exposure_usd = max_total_exposure_usd
    settings.daily_loss_limit_usd = daily_loss_limit_usd
    settings.position_scale_factor = position_scale_factor
    settings.poll_interval_seconds = poll_interval_seconds

    # Persist to DB
    await settings.save_to_db("max_position_usd", str(max_position_usd))
    await settings.save_to_db("max_total_exposure_usd", str(max_total_exposure_usd))
    await settings.save_to_db("daily_loss_limit_usd", str(daily_loss_limit_usd))
    await settings.save_to_db("position_scale_factor", str(position_scale_factor))
    await settings.save_to_db("poll_interval_seconds", str(poll_interval_seconds))

    # Update risk manager
    risk_manager._max_position_usd = max_position_usd
    risk_manager._max_total_exposure = max_total_exposure_usd
    risk_manager._daily_loss_limit = daily_loss_limit_usd
    risk_manager._min_order_usd = settings.min_order_usd

    return JSONResponse({"status": "saved"})


# --- Auto-suggest traders ---

async def _auto_suggest_traders(force: bool = False) -> int:
    """Automatically add top traders from the leaderboard if none are tracked."""
    if db.async_session is None:
        return 0

    # Check if we already have tracked traders
    if not force:
        existing = await _get_tracked_traders()
        if existing:
            return 0

    try:
        traders = await discovery.get_top_traders(
            category="OVERALL",
            time_period="MONTH",
            order_by="PNL",
            limit=10,
            min_pnl=100,
        )
    except Exception as exc:
        logger.warning("Could not fetch leaderboard for auto-suggestions: %s", exc)
        return 0

    count = 0
    async with db.async_session() as session:
        for t in traders[:5]:  # Top 5 traders
            if not t.address:
                continue
            existing = await session.execute(
                select(TrackedTrader).where(TrackedTrader.address == t.address)
            )
            if existing.scalar_one_or_none():
                continue
            trader = TrackedTrader(
                address=t.address,
                username=t.username,
                pnl=t.pnl,
                volume=t.volume,
                is_active=True,
            )
            session.add(trader)
            count += 1
        await session.commit()

    if count:
        logger.info("Auto-added %d traders from leaderboard", count)
    return count


# --- Sync Polymarket Account ---

async def _get_proxy_wallet() -> str:
    """Get the proxy wallet address for the current user."""
    if not settings.private_key:
        return ""
    try:
        from eth_account import Account
        eoa = Account.from_key(settings.private_key).address
        profile = await gamma_api.get_public_profile(eoa)
        return profile.get("proxyWallet", "")
    except Exception:
        return ""


async def _sync_positions() -> int:
    """Sync trades from Polymarket account into our database."""
    proxy = await _get_proxy_wallet()
    if not proxy or db.async_session is None:
        return 0

    try:
        activities = await data_api.get_activity(proxy, limit=100, activity_type="TRADE")
    except Exception as exc:
        logger.warning("Could not fetch account activity: %s", exc)
        return 0

    count = 0
    async with db.async_session() as session:
        for a in activities:
            tx_hash = a.get("transactionHash", "")
            if not tx_hash:
                continue

            # Check if already in DB
            existing = await session.execute(
                select(Bet).where(Bet.token_id == tx_hash)
            )
            if existing.scalar_one_or_none():
                continue

            price = float(a.get("price", 0))
            usd_size = float(a.get("usdcSize", 0))
            side = a.get("side", "BUY")
            outcome = a.get("outcome", "")
            title = a.get("title", "")

            bet = Bet(
                market_title=title,
                condition_id=a.get("conditionId", ""),
                token_id=tx_hash,  # Use tx_hash as unique ID for synced trades
                side=side if side else "BUY",
                outcome=outcome if outcome else ("Yes" if a.get("outcomeIndex", 0) == 0 else "No"),
                amount_usd=usd_size,
                entry_price=price,
                current_price=price,
                status="active",
                pnl_absolute=0.0,
                pnl_percent=0.0,
                source_trader="Mon compte",
            )
            session.add(bet)
            count += 1

        await session.commit()

    if count:
        logger.info("Synced %d trades from Polymarket account", count)
    return count


@app.post("/api/sync", response_class=HTMLResponse)
async def sync_account():
    count = await _sync_positions()
    if count:
        return HTMLResponse(f'<p class="text-success">{count} trades synchronises !</p>')
    return HTMLResponse('<p class="text-muted">Aucun nouveau trade a synchroniser.</p>')


# --- Helpers ---

def _mask_key(key: str) -> str:
    if len(key) < 10:
        return "***"
    return key[:6] + "..." + key[-4:]


async def _get_bets() -> list[Bet]:
    if db.async_session is None:
        return []
    async with db.async_session() as session:
        result = await session.execute(
            select(Bet)
            .where(Bet.status.in_(["pending", "active", "settled"]))
            .order_by(Bet.created_at.desc())
            .limit(100)
        )
        return list(result.scalars().all())


async def _get_stats() -> dict[str, Any]:
    if db.async_session is None:
        return {"active_count": 0, "total_pnl": 0, "total_exposure": 0, "tracked_traders": 0}

    async with db.async_session() as session:
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

    balance = 0.0
    if clob:
        try:
            balance = await clob.get_balance()
        except Exception:
            pass

    return {
        "balance": balance,
        "active_count": active.scalar() or 0,
        "total_pnl": pnl.scalar() or 0.0,
        "total_exposure": exposure.scalar() or 0.0,
        "tracked_traders": traders.scalar() or 0,
    }


async def _count_active_bets() -> int:
    if db.async_session is None:
        return 0
    async with db.async_session() as session:
        result = await session.execute(
            select(func.count()).select_from(Bet).where(Bet.status == "active")
        )
        return result.scalar() or 0


async def _get_tracked_traders() -> list[TrackedTrader]:
    if db.async_session is None:
        return []
    async with db.async_session() as session:
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
