from __future__ import annotations
import asyncio
import json
import time
from typing import Optional

import aiohttp
import websockets
from websockets import ConnectionClosedError, WebSocketException

from .config import settings
from .logger import logger
from .state import MarketState
from .paper import PaperBroker
from .db import insert_trade, insert_signal


def now_ms() -> int:
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
        self.flip_buffer: dict[str, dict[str, Optional[str] | int]] = {}

        # Pozisyon kapanınca DB'ye yaz
        self.paper = PaperBroker(
            max_positions=settings.MAX_POSITIONS,
            daily_loss_limit=None,
            on_close=lambda rec: asyncio.create_task(insert_trade(rec)),
        )

        # Sinyal loglama örnekleme kontrolü
        self._log_last_ts: dict[str, int] = {}
        self._log_interval_ms = int(getattr(settings, "SIGNAL_LOG_INTERVAL_MS", 1000))

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
    # Geliştirilmiş yön kararı
    # ---------------------------------------------------
    def _decide_side(self, sym: str) -> Optional[str]:
        st = self.state.symbols.get(sym)
        if not st:
            return None

        lp    = st.last_price
        ef    = st.ema_fast.value
        es    = st.ema_slow.value
        vwap  = st.vwap(getattr(settings, "VWAP_WINDOW_SEC", 60) * 1000)
        atr   = st.atr_like(getattr(settings, "ATR_WINDOW_SEC", 60) * 1000)
        spr   = st.spread_bps()
        tick  = st.tick_rate(2000)
        bp    = st.buy_pressure(2000)

        rsi   = getattr(st, "rsi_value", None)
        vdev  = getattr(st, "vwap_dev_pct", None)
        if vdev is None and (vwap and lp):
            vdev = abs(lp - vwap) / vwap

        imb   = st.imbalance() if callable(getattr(st, "imbalance", None)) else getattr(st, "imbalance", None)
        volsp = getattr(st, "vol_spike_5s", None)
        cvd10 = getattr(st, "cvd_10m", None)
        srpct = getattr(st, "sr_dist_pct", None)
        candle= getattr(st, "candle5_dir", None)

        basics = [lp, ef, es, atr, spr, tick]
        if any(x is None for x in basics):
            return None

        if spr > getattr(settings, "MAX_SPREAD_BPS", 0.05):
            return None
        if not (getattr(settings, "ATR_MIN", 0.00012) <= atr <= getattr(settings, "ATR_MAX", 0.004)):
            return None
        if tick < getattr(settings, "MIN_TICKS_PER_SEC", 2.0):
            return None
        if vdev is None or vdev > getattr(settings, "VWAP_DEV_MAX", 0.002):
            return None
        if srpct is not None and srpct <= getattr(settings, "SR_NEAR_PCT", 0.00010):
            return None

        long_ok = (
            ef is not None and es is not None and ef > es and
            bp is not None and bp >= getattr(settings, "BUY_PRESSURE_MIN", 0.50) and
            (rsi is None or rsi <= getattr(settings, "RSI_LONG_MAX", 65)) and
            (imb is None or imb >= getattr(settings, "IMB_LONG_MIN", 1.0))
        )
        if volsp is not None:
            long_ok = long_ok and (volsp >= getattr(settings, "VOL_SPIKE_MIN", 0.20) or (imb is not None and imb >= 2.0 and tick >= 5.0))
        if cvd10 is not None and cvd10 <= getattr(settings, "CVD_ABS_MIN", 50):
            long_ok = long_ok and (cvd10 > 0)

        sellp = (1.0 - bp) if bp is not None else None
        short_ok = (
            ef is not None and es is not None and ef < es and
            sellp is not None and sellp >= getattr(settings, "BUY_PRESSURE_MIN", 0.50) and
            (rsi is not None and rsi >= getattr(settings, "RSI_SHORT_MIN", 75)) and
            (imb is None or imb <= getattr(settings, "IMB_SHORT_MAX", 1.0))
        )
        if vdev is not None:
            short_ok = short_ok and (
                getattr(settings, "VWAP_SHORT_MIN", 0.00010) <= vdev <= getattr(settings, "VWAP_SHORT_MAX", 0.00200)
            )
        if cvd10 is not None and cvd10 >= -getattr(settings, "CVD_ABS_MIN", 50):
            short_ok = short_ok and (cvd10 < 0)

        if candle == "bull" and short_ok and (vdev is not None and vdev < 0.0002):
            short_ok = False
        if candle == "bear" and long_ok and (vdev is not None and vdev < 0.0002):
            long_ok = False

        if long_ok:
            return "long"
        if short_ok:
            return "short"
        return None

    # ---------------------------------------------------
    # Otomatik aç/kapat yardımcıları (Paper)
    # ---------------------------------------------------
    def _calc_auto_params(self, sym: str, side: str, entry_price: float) -> dict:
        lev = int(getattr(settings, "AUTO_LEVERAGE", 10))
        margin = float(getattr(settings, "AUTO_MARGIN_USD", 1000.0))
        notional = float(getattr(settings, "AUTO_NOTIONAL_USD", margin * lev))

        qty = max(1e-8, round(notional / entry_price, 6))

        tp_d = float(getattr(settings, "AUTO_ABS_TP_USD", 25.0))
        sl_d = float(getattr(settings, "AUTO_ABS_SL_USD", 15.0))
        delta_tp = tp_d / qty
        delta_sl = sl_d / qty

        if side == "long":
            tp = entry_price + delta_tp
            sl = entry_price - delta_sl
        else:
            tp = entry_price - delta_tp
            sl = entry_price + delta_sl

        return {
            "lev": lev,
            "margin": margin,
            "notional": notional,
            "qty": qty,
            "tp": tp,
            "sl": sl,
            "tp_d": tp_d,
            "sl_d": sl_d,
        }

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
                "AUTO-OPEN %s %s qty=%s entry=%.2f lev=%dx margin=$%.2f notional=$%.2f tp=%.2f sl=%.2f (±$%.2f)",
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
    # Sinyal logla (örneklemeli)
    # ---------------------------------------------------
    async def _maybe_log_signal(self, sym: str):
        """Her sembol için en fazla _log_interval_ms frekansında 1 kayıt."""
        ts = now_ms()
        last = self._log_last_ts.get(sym, 0)
        if ts - last < self._log_interval_ms:
            return
        self._log_last_ts[sym] = ts

        snap = self.state.snapshot().get(sym, {})
        if not snap:
            return

        # _decide_side ile aynı yön kararı
        side = self._decide_side(sym)

        row = {
            "ts_ms": ts,
            "symbol": sym,
            "last_price": snap.get("last_price"),
            "ema_fast": snap.get("ema_fast"),
            "ema_slow": snap.get("ema_slow"),
            "rsi14": snap.get("rsi14"),
            "vwap60": snap.get("vwap60"),
            "vwap_dev_pct": snap.get("vwap_dev_pct"),
            "atr60": snap.get("atr60"),
            "tick_rate_2s": snap.get("tick_rate_2s"),
            "spread_bps": snap.get("spread_bps"),
            "buy_pressure_2s": snap.get("buy_pressure_2s"),
            "sell_pressure_2s": (1.0 - snap["buy_pressure_2s"]) if snap.get("buy_pressure_2s") is not None else None,
            "imbalance": snap.get("imbalance"),
            "vol_spike_5s": snap.get("vol_spike_5s"),
            "cvd_10m": snap.get("cvd_10m"),
            "sr_dist_pct": snap.get("sr_dist_pct"),
            "candle5_dir": snap.get("candle5_dir"),
            "short_vwap_band_ok": snap.get("short_vwap_band_ok"),
            "side": side,
        }

        try:
            await insert_signal(row)
        except Exception as e:
            logger.warning("insert_signal failed %s: %s", sym, e)

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

        # PnL/SL/TP
        self.paper.mark_to_market(sym, price)

        # Sinyali hesapla
        decision = self._decide_side(sym)

        # Flip veya açılış
        self._maybe_flip(sym, decision, price, ts)

        # Örneklemeli sinyal logla (DB)
        await self._maybe_log_signal(sym)

        # Opsiyonel forward
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
    # WS Döngüleri
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
    # /signals görünümü için
    # ---------------------------------------------------
    def get_signals(self) -> dict:
        out = {}
        snap = self.state.snapshot()
        for sym, st in snap.items():
            out[sym] = {
                "side": self._decide_side(sym),
                **st,  # snapshot içindeki tüm metrikler (vwap_dev_pct, vol_spike_5s vs. varsa)
            }
        return out
