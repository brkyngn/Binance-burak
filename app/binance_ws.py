# app/binance_ws.py
import asyncio
import json
from typing import Optional

import aiohttp
import websockets
from websockets import ConnectionClosedError, WebSocketException

from .config import settings
from .logger import logger
from .state import MarketState
from .paper import PaperBroker
from .db import insert_trade

def now_ms() -> int:
    # küçük yardımcı (funding filtresi için)
    import time
    return int(time.time() * 1000)

class BinanceWSClient:
    def __init__(self):
        # --- Konfig ---
        self.ws_url = settings.WS_URL
        self.trade_stream = settings.STREAM            # "aggTrade"
        self.depth_stream = settings.DEPTH_STREAM      # "bookTicker"
        self.enable_depth = settings.ENABLE_DEPTH

        # --- Semboller ---
        self.symbols_l = [s.lower() for s in settings.SYMBOLS]
        self.symbols_u = [s.upper() for s in settings.SYMBOLS]

        # Opsiyonel webhook (n8n)
        self.n8n_url = settings.N8N_WEBHOOK_URL

        # --- Durum & Paper Broker ---
        self.state = MarketState(self.symbols_u)
        self.signal_cooldown: dict[str, int] = {}  # symbol -> last_signal_ts(ms)

        # Flip için arka arkaya ters sinyal sayacı
        # ör: {"BTCUSDT": {"want": "short", "count": 1}}
        self.flip_buffer: dict[str, dict[str, Optional[str] | int]] = {}

        # Pozisyon kapanınca DB'ye yaz
        self.paper = PaperBroker(
            max_positions=settings.MAX_POSITIONS,
            daily_loss_limit=None,
            on_close=lambda rec: asyncio.create_task(insert_trade(rec)),
        )

        self._running = False

    # ---------------------------------------------------
    # Yardımcılar
    # ---------------------------------------------------
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

    # ---------------------------------------------------
    # Sinyal koşulları (yön kararı)
    # ---------------------------------------------------
    def _funding_ok(self) -> bool:
        nxt = getattr(settings, "FUNDING_NEXT_TS_MS", None)
        if not nxt:
            return True  # funding zamanı bilinmiyorsa filtre geçsin
        mins_left = (nxt - now_ms()) / 60000.0
        return mins_left >= settings.FUNDING_MINUTES_BUFFER

    def _decide_side(self, sym: str) -> Optional[str]:
        """
        GÜNCEL STRATEJİ (uygulanabilir olanlar):
        LONG için Tümü:
          Tick/s ≥ 2.0
          Spread ≤ 2.0 bps
          BuyPress ≥ 0.55
          Imbalance ≥ 1.25
          ATR ∈ [0.0008, 0.004]
          VWAP sapma ≤ 0.20%
          Volume Spike > 1.5x
          CVD10m > 0
          Funding zamanına > 20 dk (opsiyonel)
          Yakın destek/direnç yok (sr_dist_pct > SR_NEAR_PCT)

        SHORT için Tümü:
          Tick/s ≥ 2.0
          Spread ≤ 2.0 bps
          SellPress ≥ 0.55  (=> buy_pressure ≤ 0.45)
          Imbalance ≤ 0.80
          ATR ∈ [0.0008, 0.004]
          Fiyat, VWAP'ın +0.10% ~ +0.20% bandında ÜSTÜNDE
          Volume Spike > 1.5x + (mum yönüne bakılması lazım; burada sadece spike>1.5x uyguluyoruz)
          CVD10m < 0
          RSI > 65
          Yakın direnç (sr_dist_pct ≤ SR_NEAR_PCT)
          Funding rate > +0.01%  (HARİCİ veri, uygulanmıyor → pass)
        """
        st = self.state.symbols.get(sym)
        if not st or st.last_price is None:
            return None

        lp   = st.last_price
        ef   = st.ema_fast.value
        es   = st.ema_slow.value
        rsi  = st.rsi_value
        vwap = st.vwap(settings.VWAP_WINDOW_SEC * 1000)
        vdev = st.vwap_dev_pct(settings.VWAP_WINDOW_SEC * 1000)
        atr  = st.atr_like(settings.ATR_WINDOW_SEC * 1000)
        spr  = st.spread_bps()
        tick = st.tick_rate(2000)
        bp   = st.buy_pressure(2000)
        imb  = st.imbalance()
        vsp  = st.volume_spike_ratio(5000, 60000)
        cvd10= st.cvd(600_000)
        sr_d = st.sr_near_pct(1_800_000, 3)

        # Temel veri kontrolleri
        key_vals = (lp, ef, es, vwap, vdev, atr, spr, tick, bp, imb, vsp, cvd10, sr_d)
        if any(v is None for v in key_vals):
            return None

        # Ortak filtreler
        if tick < settings.MIN_TICKS_PER_SEC:
            return None
        if spr > settings.MAX_SPREAD_BPS:
            return None
        if atr < settings.ATR_MIN or atr > settings.ATR_MAX:
            return None
        if not self._funding_ok():
            return None

        # LONG kuralları
        long_ok = (
            ef > es and
            bp >= settings.BUY_PRESSURE_MIN and
            imb >= settings.IMB_LONG_MIN and
            vdev <= settings.VWAP_DEV_MAX_LONG and
            vsp is not None and vsp > settings.VOLUME_SPIKE_MIN and
            cvd10 is not None and cvd10 > 0 and
            (sr_d is None or sr_d > settings.SR_NEAR_PCT) and
            (rsi is None or rsi < 70)  # aşırı değil
        )

        # SHORT kuralları
        short_band_ok = (
            vdev is not None and
            lp is not None and vwap is not None and
            (lp > vwap) and
            (settings.SHORT_VWAP_DEV_MIN <= (lp - vwap) / vwap <= settings.SHORT_VWAP_DEV_MAX)
        )
        sell_press = (bp is not None) and (bp <= (1.0 - settings.BUY_PRESSURE_MIN))  # >=0.55 sell press
        short_ok = (
            ef < es and
            sell_press and
            imb <= settings.IMB_SHORT_MAX and
            short_band_ok and
            vsp is not None and vsp > settings.VOLUME_SPIKE_MIN and
            cvd10 is not None and cvd10 < 0 and
            (rsi is not None and rsi >= settings.RSI_SHORT_MIN) and
            (sr_d is not None and sr_d <= settings.SR_NEAR_PCT)
        )

        if long_ok:
            return "long"
        if short_ok:
            return "short"
        return None

    # ---------------------------------------------------
    # Pozisyon aç/kapat yardımcıları (TP/SL mutlak $)
    # ---------------------------------------------------
    def _calc_auto_params(self, sym: str, side: str, entry_price: float) -> dict:
        lev = int(getattr(settings, "AUTO_LEVERAGE", 10))
        margin = float(getattr(settings, "AUTO_MARGIN_USD", 1000.0))
        notional = float(getattr(settings, "AUTO_NOTIONAL_USD", margin * lev))
        qty = max(1e-8, round(notional / entry_price, 6))

        tp_d = float(getattr(settings, "AUTO_ABS_TP_USD", 50.0))
        sl_d = float(getattr(settings, "AUTO_ABS_SL_USD", 50.0))
        delta_tp = tp_d / qty
        delta_sl = sl_d / qty

        if side == "long":
            tp = entry_price + delta_tp
            sl = entry_price - delta_sl
        else:
            tp = entry_price - delta_tp
            sl = entry_price + delta_sl

        return {"lev": lev, "margin": margin, "notional": notional,
                "qty": qty, "tp": tp, "sl": sl, "tp_d": tp_d, "sl_d": sl_d}

    def _open_auto(self, sym: str, side: str, price: float, ts: int):
        last = self.signal_cooldown.get(sym, 0)
        if ts - last < int(getattr(settings, "SIGNAL_COOLDOWN_MS", 3000)):
            return
        if sym in self.paper.positions:
            return

        prm = self._calc_auto_params(sym, side, price)
        try:
            self.paper.open(
                sym, side,
                qty=prm["qty"], price=price,
                stop=prm["sl"], tp=prm["tp"],
                leverage=prm["lev"], margin_usd=prm["margin"],
                maint_margin_rate=settings.MAINT_MARGIN_RATE,
            )
            self.signal_cooldown[sym] = ts
            logger.info(
                "AUTO-OPEN %s %s qty=%s entry=%.4f lev=%dx margin=$%.2f notional=$%.2f tp=%.4f sl=%.4f (±$%.2f)",
                sym, side, prm["qty"], price, prm["lev"], prm["margin"], prm["notional"], prm["tp"], prm["sl"], prm["tp_d"]
            )
        except Exception as e:
            logger.warning("AUTO-OPEN failed %s: %s", sym, e)

    def _maybe_flip(self, sym: str, decision: Optional[str], price: float, ts: int):
        pos = self.paper.positions.get(sym)
        if not pos or decision is None:
            if decision is not None and not pos:
                self._open_auto(sym, decision, price, ts)
            return

        opposite = "short" if pos.side == "long" else "long"
        if decision != opposite:
            self.flip_buffer.pop(sym, None)
            return

        fb = self.flip_buffer.get(sym) or {"want": opposite, "count": 0}
        if fb.get("want") != opposite:
            fb = {"want": opposite, "count": 0}
        fb["count"] = int(fb["count"]) + 1
        self.flip_buffer[sym] = fb
        logger.info("FLIP-CANDIDATE %s need=%s count=%s", sym, opposite, fb["count"])

        if fb["count"] >= 2:
            try:
                self.paper.close(sym, price)
                logger.info("FLIP %s: closed %s @ %.4f", sym, pos.side, price)
            except Exception as e:
                logger.warning("Flip close failed %s: %s", sym, e)
                self.flip_buffer.pop(sym, None)
                return
            self._open_auto(sym, opposite, price, ts)
            self.flip_buffer.pop(sym, None)

    # ---------------------------------------------------
    # Handlers
    # ---------------------------------------------------
    async def _handle_agg_trade(self, data: dict):
        d = data.get("data") if "data" in data else data
        sym = d.get("s")
        if not sym or "p" not in d or "q" not in d or "T" not in d:
            return
        try:
            price = float(d["p"])
            qty = float(d["q"])
            ts = int(d["T"])
            buyer_is_maker = bool(d.get("m")) if "m" in d else None
        except Exception:
            return

        ema_f, ema_s = self.state.on_agg_trade(sym, price, qty, ts, buyer_is_maker)

        # mark-to-market (TP/SL/liq ve canlı pnl)
        self.paper.mark_to_market(sym, price)

        # debug
        if ema_f is not None and ema_s is not None:
            logger.info("TICK %s p=%.8f ema_fast=%.4f ema_slow=%.4f", sym, price, ema_f, ema_s)

        decision = self._decide_side(sym)
        self._maybe_flip(sym, decision, price, ts)

        await self._forward_n8n(d)

    async def _handle_depth(self, data: dict):
        d = data.get("data") if "data" in data else data
        try:
            sym = d.get("s")
            if not sym:
                return
            best_bid = float(d["b"])
            best_ask = float(d["a"])
            bid_vol = float(d.get("B", "0"))
            ask_vol = float(d.get("A", "0"))
            ts = int(d.get("E") or 0)
        except Exception:
            return

        self.state.on_top(sym, best_bid, best_ask, bid_vol, ask_vol, ts)

    # ---------------------------------------------------
    # WS döngüsü
    # ---------------------------------------------------
    async def _ws_loop(self, per_symbol_stream: str, handler):
        params = self._build_streams(per_symbol_stream)
        url = f"{self.ws_url}?streams={params}"
        logger.info("Connecting WS: %s", url)
        async with websockets.connect(
            url, ping_interval=20, ping_timeout=20, max_queue=2048
        ) as ws:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                data = msg.get("data") if isinstance(msg, dict) else msg
                if not isinstance(data, dict):
                    continue
                try:
                    await handler(msg)
                except Exception as e:
                    logger.warning("handler error (%s): %s", per_symbol_stream, e)

    async def run(self):
        self._running = True
        while self._running:
            try:
                tasks = [asyncio.create_task(self._ws_loop(self.trade_stream, self._handle_agg_trade))]
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
    # /signals görünümü
    # ---------------------------------------------------
    def get_signals(self) -> dict:
        out = {}
        snap = self.state.snapshot()
        for sym, st in snap.items():
            # short band dev'i hesaplayalım (kolay debug için)
            lp = st.get("last_price")
            vwap = st.get("vwap60")
            short_band = None
            if lp is not None and vwap is not None and vwap > 0:
                dev = (lp - vwap) / vwap
                short_band = (settings.SHORT_VWAP_DEV_MIN <= dev <= settings.SHORT_VWAP_DEV_MAX)

            out[sym] = {
                "side": self._decide_side(sym),
                "last_price": st.get("last_price"),
                "ema_fast": st.get("ema_fast"),
                "ema_slow": st.get("ema_slow"),
                "rsi14": st.get("rsi14"),
                "vwap60": st.get("vwap60"),
                "vwap_dev_pct": st.get("vwap_dev_pct"),
                "atr60": st.get("atr60"),
                "tick_rate_2s": st.get("tick_rate_2s"),
                "spread_bps": st.get("spread_bps"),
                "buy_pressure_2s": st.get("buy_pressure_2s"),
                "imbalance": st.get("imbalance"),
                "vol_spike_5s": st.get("vol_spike_5s"),
                "cvd_10m": st.get("cvd_10m"),
                "sr_dist_pct": st.get("sr_dist_pct"),
                "short_vwap_band_ok": short_band,
            }
        return out
