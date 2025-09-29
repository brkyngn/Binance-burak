import asyncio
import json
import aiohttp
import websockets
from websockets import ConnectionClosedError, WebSocketException
from .config import settings
from .logger import logger

class BinanceWSClient:
    def __init__(self):
        self.ws_url = settings.WS_URL
        self.stream = settings.STREAM
        self.symbols = [s.lower() for s in settings.SYMBOLS]
        self.n8n_url = settings.N8N_WEBHOOK_URL
        self._running = False

    def _build_params(self):
        streams = [f"{sym}@{self.stream}" for sym in self.symbols]
        return "/".join(streams)

    async def _forward_n8n(self, payload: dict):
        if not self.n8n_url:
            return
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as sess:
                async with sess.post(self.n8n_url, json=payload) as resp:
                    if resp.status >= 300:
                        logger.warning("n8n forward non-200: %s", resp.status)
        except Exception as e:
            logger.exception("n8n forward error: %s", e)

    async def _consume(self):
        url = f"{self.ws_url}?streams={self._build_params()}"
        logger.info("Connecting WS: %s", url)
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                data = msg.get("data") or msg
                if isinstance(data, dict):
                    await self._forward_n8n(data)

    async def run(self):
        self._running = True
        attempt = 0
        while self._running:
            try:
                await self._consume()
                attempt = 0
            except (ConnectionClosedError, WebSocketException, OSError) as e:
                attempt += 1
                backoff = min(settings.BACKOFF_BASE * (2 ** (attempt - 1)), settings.BACKOFF_MAX)
                logger.warning("WS disconnected (%s). Reconnecting in %.1fs", e.__class__.__name__, backoff)
                await asyncio.sleep(backoff)
            except Exception as e:
                logger.exception("WS fatal error: %s", e)
                await asyncio.sleep(2)

    async def stop(self):
        self._running = False
