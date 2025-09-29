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
    def __init__(self):
        self.ws_url = settings.WS_URL
        self.stream = settings.STREAM
        self.symbols_l = [s.lower() for s in settings.SYMBOLS]
        self.symbols_u = [s.upper() for s in settings.SYMBOLS]
        self.n8n_url = settings.N8N_WEBHOOK_URL
        self.enable_depth = settings.ENABLE_DEPTH
        self.depth_stream = settings.DEPTH_STREAM

        self.state = MarketState(self.symbols_u)
        self.signal_cooldown = {}  # symbol -> ts(ms)
        self.paper = PaperBroker(max_positions=settings.MAX_POSITIONS, daily_loss_limit=None)

        self._running = False

    def _build_streams(self, per_symbol_stream: str) -> str:
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

    # ---------- Signal engine ----------
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

        # ROC ~ son 3 sn fiyat farkı
        # basit yaklaşım: history'den en eski ve en yeni p (3 sn içi)
        # (state'de hızlı olması için approx atlıyoruz; EMA> ve VWAP> zaten momentum veriyor)

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

        # cooldown
        last = self.signal_cooldown.get(sym, 0)
        ts = self.state.symbols[sym].last_ts or 0
        if ts - last < settings.SIGNAL_COOLDOWN_MS:
            return

        st = self.state.symbols[sym]
        price = st.last_price
        if sym in self.paper.positions:
            return  # pozisyon zaten açık

        # Auto risk
        tp = price * (1 + settings.AUTO_TP_PCT)
        sl = price * (1 - settings.AUTO_SL_PCT)
        try:
            self.paper.open(sym, "long", qty=0.01, price=price, stop=sl, tp=tp)
            logger.info("AUTO-OPEN %s long @ %.4f tp=%.4f sl=%.4f", sym, price, tp, sl)
            self.signal_cooldown[sym] = ts
        except Exception as e:
            logger.warning("AUTO-OPEN failed %s: %s", sym, e)

    # ---------- Handlers ----------
    async def _handle_agg_trade(self, data: dict):
        # { e, E, s, p, q, T, m, ... }
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

        # mark-to-market + otomatik kapanış
        self.paper.mark_to_market(sym, price)

        # bilgi amaçlı tick log (kısaltılmış)
        logger.info("TICK %s p=%s ema5=%.4f ema20=%.4f", sym, data["p"], ema_f, ema_s)

        # Koşullar uygunsa otomatik aç
        self._maybe_auto_trade(sym)

    async def _handle_depth(self, data: dict):
        # Binance depthUpdate farklı endpoint’te gelir, fakat combined stream'de "depth" kısa paketler var.
        # Burada sade bir top-of-book hesaplayalım (bids[0], asks[0], ve top hacimler).
        # Örnek combined 'depth@100ms': {"stream":"btcusdt@depth@100ms","data":{"bids":[["61700.0","1.2"],...],"asks":[...],"E":...,"s":"BTCUSDT"}}
        d = data.get("data") if "data" in data else data
        sym = d.get("s")
        bids = d.get("bids") or []
        asks = d.get("asks") or []
        if not sym or not bids or not asks:
            return
        try:
            best_bid = float(bids[0][0]); best_ask = float(asks[0][0])
            bid_vol = sum(float(b[1]) for b in bids[:5])
            ask_vol = sum(float(a[1]) for a in asks[:5])
            ts = int(d.get("E") or 0)
        except Exception:
            return
        self.state.on_top(sym, best_bid, best_ask, bid_vol, ask_vol, ts)

    # ---------- WebSocket loops ----------
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
                await handler(data)

    async def run(self):
        self._running = True
        while self._running:
            try:
                tasks = []
                # trades
                tasks.append(asyncio.create_task(self._ws_loop(self.stream, self._handle_agg_trade)))
                # depth (opsiyonel)
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
