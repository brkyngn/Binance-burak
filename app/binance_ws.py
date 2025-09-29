import asyncio
import json
import aiohttp
import websockets
from websockets import ConnectionClosedError, WebSocketException

from .config import settings
from .logger import logger
from .state import MarketState
from .paper import PaperBroker
from .db import insert_trade


class BinanceWSClient:
    def __init__(self):
        # Konfig
        self.ws_url = settings.WS_URL
        self.trade_stream = settings.STREAM          # ör. "aggTrade"
        self.depth_stream = settings.DEPTH_STREAM    # ör. "bookTicker"
        self.enable_depth = settings.ENABLE_DEPTH

        # Semboller
        self.symbols_l = [s.lower() for s in settings.SYMBOLS]
        self.symbols_u = [s.upper() for s in settings.SYMBOLS]

        # Opsiyonel webhook
        self.n8n_url = settings.N8N_WEBHOOK_URL

        # Durum & paper
        self.state = MarketState(self.symbols_u)
        self.signal_cooldown = {}  # symbol -> ts(ms)
        self.paper = PaperBroker(
            max_positions=settings.MAX_POSITIONS,
            daily_loss_limit=None,
            on_close=lambda rec: asyncio.create_task(insert_trade(rec))
        )

        self._running = False

    # ---------------------------------------------------
    # Yardımcılar
    # ---------------------------------------------------
    def _build_streams(self, per_symbol_stream: str) -> str:
        # btcusdt@aggTrade/ethusdt@aggTrade
        return "/".join(f"{s}@{per_symbol_stream}" for s in self.symbols_l)

    async def _forward_n8n(self, payload: dict):
        if not self.n8n_url:
            return
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as sess:
                async with sess.post(self.n8n_url, json=payload) as resp:
                    if resp.status >= 300:
                        txt = await resp.text()
                        logger.warning("n8n forward non-200: %s %s", resp.status, txt[:200])
        except Exception as e:
            logger.exception("n8n forward error: %s", e)

    # ---------------------------------------------------
    # Otomatik trade: 10$ pozisyon, %3 TP/SL
    # ---------------------------------------------------
    def _maybe_auto_trade(self, sym: str):
        sigs = self.get_signals()
        sig = sigs.get(sym)
        if not sig:
            return

        side = sig.get("side")
        price = self.state.snapshot()[sym]["last_price"]
        if price is None:
            return

        open_pos = self.paper.positions.get(sym)

        # Pozisyon yoksa → yeni aç
        if not open_pos:
            qty = round(10.0 / price, 6)  # 10$ değerinde miktar
            if side == "long":
                stop = price * 0.97       # -%3 stop
                tp   = price * 1.03       # +%3 kar al
            else:
                stop = price * 1.03
                tp   = price * 0.97
            self.paper.open(sym, side, qty, price, stop, tp)
            logger.info(f"AUTO-OPEN {sym} side={side} qty={qty} entry={price:.2f}")

        else:
            # Pozisyon varsa TP/SL kontrolü
            entry = open_pos.entry
            pnl_pct = (price - entry) / entry * (1 if open_pos.side == "long" else -1)
            if pnl_pct >= 0.03 or pnl_pct <= -0.03:
                self.paper.close(sym, price)
                logger.info(f"AUTO-CLOSE {sym} side={open_pos.side} exit={price:.2f} pnl_pct={pnl_pct*100:.2f}%")

    # ---------------------------------------------------
    # Handlers
    # ---------------------------------------------------
    async def _handle_agg_trade(self, data: dict):
        """
        aggTrade payload:
        {"e":"aggTrade","E":...,"s":"BTCUSDT","p":"61750.12","q":"0.001","T":...,"m":false,...}
        """
        sym = data.get("s")
        if not sym or "p" not in data or "q" not in data or "T" not in data:
            return
        try:
            price = float(data["p"])
            qty = float(data["q"])
            ts = int(data["T"])
            buyer_is_maker = bool(data.get("m")) if "m" in data else None
        except Exception:
            return

        ema_f, ema_s = self.state.on_agg_trade(sym, price, qty, ts, buyer_is_maker)
        if ema_f is None or ema_s is None:
            return

        self.paper.mark_to_market(sym, price)

        logger.info("TICK %s p=%s ema5=%.4f ema20=%.4f", sym, data["p"], ema_f, ema_s)

        # Otomatik trade kontrolü
        self._maybe_auto_trade(sym)

        # Webhook'a forward (opsiyonel)
        await self._forward_n8n(data)

    async def _handle_depth(self, data: dict):
        """
        bookTicker payload (combined stream 'symbol@bookTicker'):
        {"s":"BTCUSDT","b":"61700.00","B":"1.234","a":"61700.10","A":"0.987","E":...}
        """
        try:
            d = data.get("data") if "data" in data else data
            sym = d.get("s")
            if not sym:
                return
            best_bid = float(d["b"])
            best_ask = float(d["a"])
            bid_vol  = float(d.get("B", "0"))
            ask_vol  = float(d.get("A", "0"))
            ts       = int(d.get("E") or 0)
        except Exception:
            return

        self.state.on_top(sym, best_bid, best_ask, bid_vol, ask_vol, ts)

    # ---------------------------------------------------
    # WS döngüleri
    # ---------------------------------------------------
    async def _ws_loop(self, per_symbol_stream: str, handler):
        params = self._build_streams(per_symbol_stream)
        url = f"{self.ws_url}?streams={params}"
        logger.info("Connecting WS: %s", url)
        async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_queue=2048) as ws:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                data = msg.get("data") if isinstance(msg, dict) else msg
                if not isinstance(data, dict):
                    continue
                try:
                    await handler(data)
                except Exception as e:
                    logger.warning("handler error (%s): %s", per_symbol_stream, e)

    async def run(self):
        self._running = True
        while self._running:
            try:
                tasks = []
                tasks.append(asyncio.create_task(self._ws_loop(self.trade_stream, self._handle_agg_trade)))
                if self.enable_depth:
                    tasks.append(asyncio.create_task(self._ws_loop(self.depth_stream, self._handle_depth)))
                await asyncio.gather(*tasks)
            except (ConnectionClosedError, WebSocketException, OSError) as e:
                backoff = settings.BACKOFF_BASE
                logger.warning("WS disconnected (%s). Reconnecting in %.1fs", e.__class__.__name__, backoff)
                await asyncio.sleep(backoff)
            except Exception as e:
                logger.exception("WS fatal error: %s", e)
                await asyncio.sleep(2)

    async def stop(self):
        self._running = False

    # ---------------------------------------------------
    # Sinyal getter
    # ---------------------------------------------------
    def get_signals(self) -> dict:
        """
        Basit scalping sinyalleri: EMA cross + RSI + VWAP + ATR + spread + tickrate + orderflow
        """
        sigs = {}
        snap = self.state.snapshot()
        for sym, st in snap.items():
            lp = st.get("last_price")
            ef = st.get("ema_fast")
            es = st.get("ema_slow")
            rsi = st.get("rsi14")
            vwap = st.get("vwap60")
            atr = st.get("atr60")
            spread = st.get("spread_bps")
            tick = st.get("tick_rate_2s")
            bp = st.get("buy_pressure_2s")

            if None in (lp, ef, es, rsi, vwap, atr, spread, tick, bp):
                continue

            max_spread = 5
            min_tick = 1.0
            min_atr = 0.0002
            max_vwap_dev = 0.002   # %0.2 sapma

            vwap_dev = abs(lp - vwap) / vwap if vwap else 0.0

            if spread > max_spread or tick < min_tick or atr < min_atr or vwap_dev > max_vwap_dev:
                continue

            side = None
            if ef > es and rsi < 70 and bp >= 0.55:
                side = "long"
            elif ef < es and rsi > 30 and bp <= 0.45:
                side = "short"

            if side:
                sigs[sym] = {
                    "side": side,
                    "ema_fast": ef,
                    "ema_slow": es,
                    "rsi14": rsi,
                    "vwap_dev": round(vwap_dev*100, 3),
                    "atr60": atr,
                    "spread_bps": spread,
                    "tick_rate": tick,
                    "buy_pressure": bp,
                }
        return sigs
