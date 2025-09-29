import asyncio
from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse

from .binance_ws import BinanceWSClient
from .config import settings
from .logger import logger
from .db import init_pool, fetch_recent

app = FastAPI(title="Binance WS Relay")
client = BinanceWSClient()

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
    return JSONResponse(client.paper.snapshot())
    
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import os

# ------ MANUEL ORDER / CLOSE (geri eklendi) ------
@app.post("/paper/order")
async def paper_order(
    symbol: str = Body(..., embed=True),
    side: str = Body(..., embed=True),         # "long" | "short"
    qty: float = Body(1.0, embed=True),
    stop: float | None = Body(None, embed=True),
    tp: float | None = Body(None, embed=True),
):
    snap = client.state.snapshot().get(symbol.upper())
    if not snap or snap["last_price"] is None:
        return JSONResponse({"ok": False, "error": "No last price yet"}, status_code=400)
    price = float(snap["last_price"])
    try:
        pos = client.paper.open(symbol.upper(), side, qty, price, stop, tp)
        return JSONResponse({"ok": True, "opened": {"symbol": pos.symbol, "side": pos.side, "entry": pos.entry}})
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
        return JSONResponse({"ok": True, "closed": {"symbol": pos.symbol, "pnl": pos.pnl, "exit": pos.exit_price}})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
# --------------------------------------------------

@app.get("/history")
async def history(limit: int = 50):
    if not settings.DATABASE_URL:
        return JSONResponse({"ok": False, "error": "DATABASE_URL not set"}, status_code=400)
    rows = await fetch_recent(limit=limit)
    return JSONResponse({"ok": True, "rows": rows})
