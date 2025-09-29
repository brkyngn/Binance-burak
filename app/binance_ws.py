import asyncio
import json
import aiohttp
import websockets
from websockets import ConnectionClosedError, WebSocketException

from .config import settings
from .logger import logger
from .state import MarketState
from .paper import PaperBroker


class BinanceWSClient:
    """
    Binance çoklu stream WebSocket tüketicisi.
    SYMBOLS: settings.SYMBOLS (örn: ["BTCUSDT", "ETHUSDT"])
    STREAM : settings.STREAM  (örn: "aggTrade" | "trade" | "kline_1s" | "depth@100ms")
    """

    def __init__(self):
        self.ws_url = settings.WS_URL
        self.stream = settings.STREAM
        self.symbols = [s.lower() for s in settings.SYMBOLS]
        self.n8n_url = settings.N8N_WEBHOOK_URL

        # EMA + sinyal
        self.state = MarketState([s.upper() for s in settings.SYMBOLS])
        self.signal_cooldown = {}         # sembol -> son sinyal zamanı (ms)

        # paper-trading broker
        self.paper = PaperBroker(max_positions=5, daily_loss_limit=None)

        self._running = False

    def _build_params(self) -> str:
        """btcusdt@aggTrade/ethusdt@aggTrade formatında stream paramı hazırlar"""
        streams = [f"{sym}@{self.stream}" for sym in self.symbols]
        return "/".join(streams)

    async def _forward_n8n(self, payload: dict):
        """Varsa n8n webhook’una POST eder"""
        if not self.n8n_url:
            return
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(self.n8n_url, json=payload) as resp:
                    if resp.status >= 300:
                        text = await resp.text()
                        logger.warning("n8n forward non-200: %s %s", resp.status, text[:200])
        except Exception as e:
            logger.exception("n8n forward error: %s", e)

    async def _handle_agg_trade(self, data: dict):
        """
        aggTrade örnek:
        {\"e\":\"aggTrade\",\"E\":...,\"s\":\"BTCUSDT\",\"p\":\"61750.12\",\"q\":\"0.001\",\"T\":...}
        """
        sym = data.get("s")
        if not sym or "p" not in data or "q" not in data or "T" not in data:
            return
        try:
            price = float(data["p"])
            qty = float(data["q"])
            ts = int(data["T"])
        except (ValueError, TypeError):
            return

        # EMA güncelle
        ema_f, ema_s = self.state.on_agg_trade(sym, price, qty, ts)
        if ema_f is None or ema_s is None:
            return

        logger.info("TICK %s p=%s q=%s ema5=%.4f ema20=%.4f",
                    sym, data["p"], data["q"], ema_f, ema_s)

        # Basit sinyal
        last = self.signal_cooldown.get(sym, 0)
        if ts - last > 2000:    # 2 sn cooldown
            if ema_f > ema_s:
                logger.info("SIGNAL %s BUY (ema5>ema20)", sym)
                self.signal_cooldown[sym] = ts
            elif ema_f < ema_s:
                logger.info("SIGNAL %s EXIT/SELL (ema5<ema20)", sym)
                self.signal_cooldown[sym] = ts

        # Paper pozisyonu mark-to-market
        self.paper.mark_to_market(sym, price)

    async def _consume(self):
        """WS’e bağlanır ve mesajları işler"""
        params = self._build_params()
        url = f"{self.ws_url}?streams={params}"
        logger.info("Connecting WS: %s", url)

        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            max_queue=2048
        ) as ws:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                data = msg.get("data") if isinstance(msg, dict) else msg
                if not isinstance(data, dict):
                    continue

                if data.get("e") == "aggTrade":
                    await self._handle_agg_trade(data)

                await self._forward_n8n(data)

    async def run(self):
        """Bağlantı koparsa backoff ile tekrar bağlanır"""
        self._running = True
        attempt = 0
        while self._running:
            try:
                await self._consume()
                attempt = 0
            except (ConnectionClosedError, WebSocketException, OSError) as e:
                attempt += 1
                backoff = min(settings.BACKOFF_BASE * (2 ** (attempt - 1)), settings.BACKOFF_MAX)
                logger.warning("WS disconnected (%s). Reconnecting in %.1fs",
                               e.__class__.__name__, backoff)
                await asyncio.sleep(backoff)
            except Exception as e:
                logger.exception("WS fatal error: %s", e)
                await asyncio.sleep(2)

    async def stop(self):
        self._running = False
