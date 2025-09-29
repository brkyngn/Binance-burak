import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from .binance_ws import BinanceWSClient
from .config import settings
from .logger import logger

app = FastAPI(title="Binance WS Relay")
client = BinanceWSClient()

@app.on_event("startup")
async def _startup():
    logger.info("Starting Binance WS consumer… symbols=%s stream=%s", settings.SYMBOLS, settings.STREAM)
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
