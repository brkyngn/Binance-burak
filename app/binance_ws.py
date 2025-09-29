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
        self.paper = PaperBroker(max_positions=settings.MAX_POSITIONS, daily_loss_limit=None)

        self._running = False
        self.paper = PaperBroker(
            max_positions=settings.MAX_POSITIONS,
            daily_loss_limit=None,
            on_close=lambda rec: asyncio.create_task(insert_trade(rec))
)

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
    # Sinyal koşulları + otomatik trade
    # ---------------------------------------------------
    def _check_conditions(self, sym: str) -> str | None:
        st = self.state.symbols.get(sym)
        if not st or st.last_price is None:
            return None

        # Likidite & volatilite
        spread_bps = st.spread_bps()
        if spread_bps is None or spread_bps > settings.MAX_SPREAD_BPS:
            return None

        atr = st.atr_like(settings.ATR_WINDOW_SEC * 1000)
        if atr is None or atr < settings.ATR_MIN or atr > settings.ATR_MAX:
            return None

        if st.tick_rate(2000) < settings.MIN_TICKS_PER_SEC:
            return None

        # Trend/Momentum
        if st.ema_fast.value is None or st.ema_slow.value is None:
            return None
        if st.ema_fast.value <= st.ema_slow.value:
            return None

        vwap = st.vwap(settings.VWAP_WINDOW_SEC * 1000)
        if vwap is None or st.last_price <= vwap:
            return None

        # Order flow
        bp = st.buy_pressure(2000)
        if bp is None or bp < settings.BUY_PRESSURE_MIN:
            return None

        imb = st.imbalance()
        if imb is None or imb < settings.IMB_THRESHOLD:
            return None

        return "BUY"

    def _maybe_auto_trade(self, sym: str):
        decision = self._check_conditions(sym)
        if decision != "BUY":
            return

        # Cooldown
        last = self.signal_cooldown.get(sym, 0)
        ts = self.state.symbols[sym].last_ts or 0
        if ts - last < settings.SIGNAL_COOLDOWN_MS:
            return

        # Zaten açık pozisyon var mı?
        if sym in self.paper.positions:
            return

        st = self.state.symbols[sym]
        price = st.last_price

        # Risk parametreleri
        tp = price * (1 + settings.AUTO_TP_PCT)
        sl = price * (1 - settings.AUTO_SL_PCT)
        try:
            self.paper.open(sym, "long", qty=0.01, price=price, stop=sl, tp=tp)
            logger.info("AUTO-OPEN %s long @ %.4f tp=%.4f sl=%.4f", sym, price, tp, sl)
            self.signal_cooldown[sym] = ts
        except Exception as e:
            logger.warning("AUTO-OPEN failed %s: %s", sym, e)

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
            # m (buyer is maker) -> aggressor SELL; buy_aggr = not m
            buyer_is_maker = bool(data.get("m")) if "m" in data else None
        except Exception:
            return

        # >>> 5 parametreyle çağır (buyer_is_maker dahil) <<<
        ema_f, ema_s = self.state.on_agg_trade(sym, price, qty, ts, buyer_is_maker)
        if ema_f is None or ema_s is None:
            return

        # PnL güncelle & otomatik kapanış (stop/tp)
        self.paper.mark_to_market(sym, price)

        logger.info("TICK %s p=%s ema5=%.4f ema20=%.4f", sym, data["p"], ema_f, ema_s)

        # Otomatik açma
        self._maybe_auto_trade(sym)

        # İsteğe bağlı: webhook'a forward
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
                # Trade akışı
                tasks.append(asyncio.create_task(self._ws_loop(self.trade_stream, self._handle_agg_trade)))
                # Depth/bookTicker akışı (opsiyonel)
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
    # Yeni eklenen sinyal getter (tam bu hizaya dikkat!)
    # ---------------------------------------------------
    def get_signals(self) -> dict:
        """
        Her sembol için anlık sinyal (BUY/NONE) ve ilgili metrikler
        """
        out = {}
        for sym in self.symbols_u:
            st = self.state.symbols.get(sym)
            if not st or st.last_price is None:
                out[sym] = {"decision": None, "reason": "no_data"}
                continue

            decision = "BUY" if self._check_conditions(sym) == "BUY" else "NONE"

            out[sym] = {
                "decision": decision,
                "last_price": st.last_price,
                "ema_fast": st.ema_fast.value,
                "ema_slow": st.ema_slow.value,
                "vwap_sec": settings.VWAP_WINDOW_SEC,
                "vwap": st.vwap(settings.VWAP_WINDOW_SEC * 1000),
                "atr_sec": settings.ATR_WINDOW_SEC,
                "atr": st.atr_like(settings.ATR_WINDOW_SEC * 1000),
                "tick_rate_2s": st.tick_rate(2000),
                "buy_pressure_2s": st.buy_pressure(2000),
                "spread_bps": st.spread_bps(),
                "imbalance": st.imbalance(),
                "cooldown_ms": settings.SIGNAL_COOLDOWN_MS,
            }
        return out
