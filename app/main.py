import asyncio
import os
from fastapi import FastAPI, Body, Request
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from .binance_ws import BinanceWSClient
from .config import settings
from .logger import logger
from .db import init_pool, fetch_recent


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
# Root (cron ping için 200 OK)
# -----------------------------
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"

# -----------------------------
# Startup & Shutdown
# -----------------------------
@app.on_event("startup")
async def _startup():
    logger.info("Starting Binance WS consumer… symbols=%s stream=%s",
                settings.SYMBOLS, settings.STREAM)
    if settings.DATABASE_URL:
        await init_pool()
    app.state.task = asyncio.create_task(client.run())

@app.on_event("shutdown")
async def _shutdown():
    logger.info("Shutting down…")
    await client.stop()
    task = getattr(app.state, "task", None)
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

@app.get("/paper/positions")
async def paper_positions():
    # Market state'ten sembol -> last_price haritası çıkar
    snap = client.state.snapshot()  # {"BTCUSDT": {"last_price": ...}, ...}
    last_map = {sym: (vals.get("last_price") if isinstance(vals, dict) else None)
                for sym, vals in snap.items()}

    # Paper trader'a son fiyatları geçirerek pozisyonları listele
    rows = client.paper.snapshot(last_map)  # list[dict]: symbol, side, qty, entry, last_price, pnl, ...

    # UI array'i doğrudan okuyabiliyor; basitçe liste dön
    return JSONResponse(rows)

# ------ MANUEL ORDER / CLOSE ------
@app.post("/paper/order")
async def paper_order(
    symbol: str = Body(..., embed=True),
    side: str = Body(..., embed=True),                   # "long" | "short"
    qty: float | None = Body(None, embed=True),          # opsiyonel; margin varsa otomatik hesaplanır
    stop: float | None = Body(None, embed=True),
    tp: float | None = Body(None, embed=True),
    leverage: int | None = Body(None, embed=True),       # yeni: kaldıraç
    margin_usd: float | None = Body(None, embed=True),   # yeni: marj ($)
):
    snap = client.state.snapshot().get(symbol.upper())
    if not snap or snap["last_price"] is None:
        return JSONResponse({"ok": False, "error": "No last price yet"}, status_code=400)
    price = float(snap["last_price"])

    # Varsayılanları settings’ten al
    lev = leverage if leverage is not None else settings.LEVERAGE
    margin = margin_usd if margin_usd is not None else settings.MARGIN_PER_TRADE

    # qty hesabı: margin*leverage / price (margin+lev varsa)
    eff_qty = qty
    if eff_qty is None and margin is not None and lev is not None:
        eff_qty = round((margin * lev) / price, 6)

    if eff_qty is None or eff_qty <= 0:
        return JSONResponse(
            {"ok": False, "error": "qty must be positive (or provide margin_usd + leverage)"},
            status_code=400,
        )

    try:
        pos = client.paper.open(
            symbol.upper(), side, eff_qty, price, stop, tp,
            leverage=lev, margin_usd=margin, maint_margin_rate=settings.MAINT_MARGIN_RATE
        )
        return JSONResponse({"ok": True, "opened": {
            "symbol": pos.symbol,
            "side": pos.side,
            "entry": pos.entry,
            "qty": pos.qty,
            "leverage": pos.leverage,
            "margin_usd": pos.margin_usd
        }})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@app.post("/paper/close")
async def paper_close(symbol: str = Body(..., embed=True)):
    snap = client.state.snapshot().get(symbol.upper())
    if not snap or snap["last_price"] is None:
        return JSONResponse({"ok": False, "error": "No last price yet"}, status_code=400)
    price = float(snap["last_price"])
    try:
        pos = client.paper.close(symbol.upper(), price)
        return JSONResponse({"ok": True, "closed": {
            "symbol": pos.symbol, "pnl": pos.pnl, "exit": pos.exit_price
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
        # burada hata mesajını döndürerek hızlı teşhis yaparız
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
        "IMB_THRESHOLD": settings.IMB_THRESHOLD,
        "ATR_MIN": settings.ATR_MIN,
        "ATR_MAX": settings.ATR_MAX,
        "VWAP_WINDOW_SEC": settings.VWAP_WINDOW_SEC,
        "ATR_WINDOW_SEC": settings.ATR_WINDOW_SEC,
    }
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "thresholds": thresholds}
    )
