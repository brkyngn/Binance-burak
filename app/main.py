from __future__ import annotations
import asyncio
import os
from fastapi import FastAPI, Body, Request, Query
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from .binance_ws import BinanceWSClient
from .config import settings
from .logger import logger
from .db import init_pool, fetch_recent, fetch_signals, purge_signals_older_than


# -----------------------------
# FastAPI app & templates
# -----------------------------
app = FastAPI(title="Binance WS Relay")
templates = Jinja2Templates(directory="app/templates")

# -----------------------------
# Binance WS Client
# -----------------------------
client = BinanceWSClient()

# -----------------------------
# Root
# -----------------------------
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"

# -----------------------------
# Housekeeping: sinyal purge loop
# -----------------------------
async def _purge_loop():
    days = int(getattr(settings, "SIGNAL_RETENTION_DAYS", 2))
    interval_sec = int(getattr(settings, "SIGNAL_PURGE_INTERVAL_SEC", 3600))
    while True:
        try:
            n = await purge_signals_older_than(days=days)
            if n:
                logger.info("signal_logs purge: deleted %s rows (> %s days)", n, days)
        except Exception as e:
            logger.warning("signal purge failed: %s", e)
        await asyncio.sleep(interval_sec)

# -----------------------------
# Startup & Shutdown
# -----------------------------
@app.on_event("startup")
async def _startup():
    logger.info("Starting Binance WS consumer… symbols=%s stream=%s",
                settings.SYMBOLS, settings.STREAM)
    if settings.DATABASE_URL:
        await init_pool()
        app.state.purge_task = asyncio.create_task(_purge_loop())
    app.state.ws_task = asyncio.create_task(client.run())

@app.on_event("shutdown")
async def _shutdown():
    logger.info("Shutting down…")
    await client.stop()
    for tname in ("purge_task", "ws_task"):
        task = getattr(app.state, tname, None)
        if task:
            task.cancel()

# -----------------------------
# API Endpoints
# -----------------------------
@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True, "symbols": settings.SYMBOLS, "stream": settings.STREAM})

@app.get("/stats")
async def stats():
    return JSONResponse(client.state.snapshot())

@app.get("/signals")
async def signals():
    return JSONResponse(client.get_signals())

# ---- Sinyal geçmişi (analiz için) ----
@app.get("/signals/logs")
async def signals_logs(
    symbol: str | None = Query(None, description="BTCUSDT/ETHUSDT..."),
    hours: int = Query(48, ge=1, le=24*14),
    limit: int = Query(5000, ge=10, le=200000),
):
    if not settings.DATABASE_URL:
        return JSONResponse({"ok": False, "error": "DATABASE_URL not set"}, status_code=400)
    rows = await fetch_signals(symbol=symbol, hours=hours, limit=limit)
    return JSONResponse({"ok": True, "rows": rows})

# ---- Manuel pozisyon API'leri (mevcut) ----
@app.get("/paper/positions")
async def paper_positions():
    snap = client.state.snapshot()
    last_map = {sym: (vals.get("last_price") if isinstance(vals, dict) else None)
                for sym, vals in snap.items()}
    rows = client.paper.snapshot(last_map)
    return JSONResponse(rows)

@app.post("/paper/order")
async def paper_order(
    symbol: str = Body(..., embed=True),
    side: str = Body(..., embed=True),
    qty: float | None = Body(None, embed=True),
    stop: float | None = Body(None, embed=True),
    tp: float | None = Body(None, embed=True),
    leverage: int | None = Body(None, embed=True),
    margin_usd: float | None = Body(None, embed=True),
):
    snap = client.state.snapshot().get(symbol.upper())
    if not snap or snap["last_price"] is None:
        return JSONResponse({"ok": False, "error": "No last price yet"}, status_code=400)
    price = float(snap["last_price"])
    lev = leverage if leverage is not None else settings.LEVERAGE
    margin = margin_usd if margin_usd is not None else settings.MARGIN_PER_TRADE
    eff_qty = qty
    if eff_qty is None and margin is not None and lev is not None:
        eff_qty = round((margin * lev) / price, 6)
    if eff_qty is None or eff_qty <= 0:
        return JSONResponse({"ok": False, "error": "qty must be positive (or provide margin_usd + leverage)"}, status_code=400)
    try:
        pos = client.paper.open(
            symbol.upper(), side, eff_qty, price, stop, tp,
            leverage=lev, margin_usd=margin, maint_margin_rate=settings.MAINT_MARGIN_RATE
        )
        return JSONResponse({"ok": True, "opened": {
            "symbol": pos.symbol, "side": pos.side, "entry": pos.entry, "qty": pos.qty,
            "leverage": pos.leverage, "margin_usd": pos.margin_usd
        }})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@app.post("/paper/close")
async def paper_close(symbol: str = Body(..., embed=True)):
    snap_all = client.state.snapshot()
    snap = snap_all.get(symbol.upper())
    if not snap or snap["last_price"] is None:
        return JSONResponse({"ok": False, "error": "No last price yet"}, status_code=400)
    price = float(snap["last_price"])

    bnb_px = None
    bnb_snap = snap_all.get("BNBUSDT") or snap_all.get("BNBUSD") or snap_all.get("BNBUSDT_PERP")
    if bnb_snap and bnb_snap.get("last_price") is not None:
        try:
            bnb_px = float(bnb_snap["last_price"])
        except Exception:
            bnb_px = None

    try:
        pos = client.paper.close(symbol.upper(), price, bnb_usd_price=bnb_px)
        return JSONResponse({"ok": True, "closed": {
            "symbol": pos.symbol,
            "pnl": pos.pnl,
            "exit": pos.exit_price,
            "fee_open_usd": getattr(pos, "fee_open_usd", None),
            "fee_close_usd": getattr(pos, "fee_close_usd", None),
            "fee_total_usd": getattr(pos, "fee_total_usd", None),
            "fee_currency": getattr(pos, "fee_currency", None),
            "fee_open_bnb": getattr(pos, "fee_open_bnb", None),
            "fee_close_bnb": getattr(pos, "fee_close_bnb", None),
            "fee_total_bnb": getattr(pos, "fee_total_bnb", None),
            "net_pnl": (pos.pnl - getattr(pos, "fee_total_usd", 0.0)) if getattr(pos, "fee_total_usd", None) is not None else None,
        }})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@app.get("/history")
async def history(limit: int = 50):
    if not settings.DATABASE_URL:
        return JSONResponse({"ok": False, "error": "DATABASE_URL not set"}, status_code=400)
    try:
        rows = await fetch_recent(limit=limit)
        return JSONResponse({"ok": True, "rows": rows})
    except Exception as e:
        logger.exception("history error: %s", e)
        return JSONResponse({"ok": False, "error": f"history_failed: {type(e).__name__}: {e}"}, status_code=500)

# -----------------------------
# Dashboard Page
# -----------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    thresholds = {
        "MIN_TICKS_PER_SEC": settings.MIN_TICKS_PER_SEC,
        "MAX_SPREAD_BPS": settings.MAX_SPREAD_BPS,
        "BUY_PRESSURE_MIN": settings.BUY_PRESSURE_MIN,
        "IMB_THRESHOLD": getattr(settings, "IMB_LONG_MIN", 1.0),
        "ATR_MIN": settings.ATR_MIN,
        "ATR_MAX": settings.ATR_MAX,
        "VWAP_WINDOW_SEC": settings.VWAP_WINDOW_SEC,
        "ATR_WINDOW_SEC": settings.ATR_WINDOW_SEC,
    }
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "thresholds": thresholds,
            "fee_rate": settings.FEE_RATE,
        }
    )
