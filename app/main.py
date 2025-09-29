import asyncio
from fastapi import FastAPI
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
    # PostgreSQL havuzu ve tablo (DATABASE_URL varsa)
    if settings.DATABASE_URL:
        await init_pool()
    # WS tüketicisini arka planda başlat
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

@app.get("/history")
async def history(limit: int = 50):
    if not settings.DATABASE_URL:
        return JSONResponse({"ok": False, "error": "DATABASE_URL not set"}, status_code=400)
    rows = await fetch_recent(limit=limit)
    return JSONResponse({"ok": True, "rows": rows})
