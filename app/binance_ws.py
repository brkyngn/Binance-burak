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

        # Pozisyon kapanınca DB'ye yaz
        self.paper = PaperBroker(
            max_positions=settings.MAX_POSITIONS,
            daily_loss_limit=None,
            on_close=lambda rec: asyncio.create_task(insert_trade(rec)),
        )

        # Flip sayacı (pozisyon varken ters yöne geçmek için ardışık sinyal onayı)
        # ör: {"BTCUSDT": {"want": "long"/"short", "count": int, "last_ts": int}}
        self.flip_state: dict[str, dict] = {}

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

    def _reset_flip_state(self, sym: str):
        if sym in self.flip_state:
            del self.flip_state[sym]

    # ---------------------------------------------------
    # Sinyal koşulları (yön kararı)
    # ---------------------------------------------------
    def _decide_side(self, sym: str) -> Optional[str]:
        """
        Basit yön kararı:
          - Filtreler: spread, atr, tickrate, vwap sapması
          - Trend/Momentum: EMA cross
          - Orderflow: buy_pressure
        Dönüş: "long" | "short" | None
        """
        st = self.state.symbols.get(sym)
        if not st or st.last_price is None:
            return None

        lp = st.last_price
        ef = st.ema_fast.value
        es = st.ema_slow.value
        rsi = getattr(st, "rsi_val", None)  # state.py RSI hesaplıyor
        vwap = st.vwap(settings.VWAP_WINDOW_SEC * 1000)
        atr = st.atr_like(settings.ATR_WINDOW_SEC * 1000)
        spread = st.spread_bps()
        tick = st.tick_rate(2000)
        bp = st.buy_pressure(2000)

        # Veri eksikse karar yok
        if None in (lp, ef, es, vwap, atr, spread, tick, bp):
            return None

        # Filtreler
        if spread > settings.MAX_SPREAD_BPS:
            return None
        if atr < settings.ATR_MIN or atr > settings.ATR_MAX:
            return None
        if tick < settings.MIN_TICKS_PER_SEC:
            return None

        vwap_dev = abs(lp - vwap) / vwap if vwap else 0.0
        if vwap_dev > 0.002:  # %0.2 sapma sınırı
            return None

        # Yön
        if ef > es and (bp >= settings.BUY_PRESSURE_MIN):
            if rsi is None or rsi < 70:
                return "long"
        if ef < es and (bp <= 1 - settings.BUY_PRESSURE_MIN):
            if rsi is None or rsi > 30:
                return "short"

        return None

    # ---------------------------------------------------
    # Otomatik trade (açılış + flip)
    # ---------------------------------------------------
    def _maybe_auto_trade(self, sym: str):
        side = self._decide_side(sym)
        if side is None:
            # sinyal yoksa flip sayacı sıfırla
            self._reset_flip_state(sym)
            return

        st = self.state.symbols.get(sym)
        if not st or st.last_price is None:
            self._reset_flip_state(sym)
            return

        price = st.last_price
        ts = st.last_ts or 0

        # ---- Pozisyon VAR: flip mantığı ----
        if sym in self.paper.positions:
            cur = self.paper.positions[sym]

            # Aynı yön sinyali → flip sayacını sıfırla, yeni pozisyon açma
            if cur.side == side:
                self._reset_flip_state(sym)
                return

            # Ters yön sinyali: ardışık onay sayacı
            if settings.FLIP_ENABLED:
                stt = self.flip_state.get(sym, {"want": side, "count": 0, "last_ts": 0})
                if stt.get("want") != side:
                    stt = {"want": side, "count": 0, "last_ts": 0}
                stt["count"] += 1
                stt["last_ts"] = ts
                self.flip_state[sym] = stt

                need = max(1, int(settings.FLIP_CONFIRM_COUNT if hasattr(settings, "FLIP_CONFIRM_COUNT") else 2))
                if stt["count"] < need:
                    # Yeterli karşı sinyal birikmedi, bekle
                    return

                # Cooldown/interval korumaları (flip'te atlamak isteyebiliriz)
                last = self.signal_cooldown.get(sym, 0)
                cd_ms = getattr(settings, "SIGNAL_COOLDOWN_MS", 3000)
                if (not getattr(settings, "FLIP_BYPASS_COOLDOWN", True)) and (ts - last < cd_ms):
                    return
                if ts - last < getattr(settings, "FLIP_MIN_INTERVAL_MS", 500):
                    return

                # 1) Mevcutı kapat
                try:
                    self.paper.close(sym, price)
                    logger.info("FLIP %s: closed %s at %.6f (pnl=%.6f)", sym, cur.side, price, cur.pnl)
                except Exception as e:
                    logger.warning("FLIP %s: close failed: %s", sym, e)
                    return

                # 2) Ters yönü aç (mutlak $ TP/SL)
                lev = int(getattr(settings, "AUTO_LEVERAGE", settings.LEVERAGE))
                margin = float(getattr(settings, "AUTO_MARGIN_USD", settings.MARGIN_PER_TRADE))
                notional = float(getattr(settings, "AUTO_NOTIONAL_USD", margin * lev))
                qty = max(1e-8, round(notional / price, 6))

                tp_d = float(getattr(settings, "AUTO_ABS_TP_USD", 50.0))
                sl_d = float(getattr(settings, "AUTO_ABS_SL_USD", 50.0))
                d_tp = tp_d / qty
                d_sl = sl_d / qty

                if side == "long":
                    tp = price + d_tp
                    sl = price - d_sl
                else:
                    tp = price - d_tp
                    sl = price + d_sl

                try:
                    self.paper.open(
                        sym, side, qty, price, sl, tp,
                        leverage=lev, margin_usd=margin, maint_margin_rate=settings.MAINT_MARGIN_RATE
                    )
                    self.signal_cooldown[sym] = ts
                    self._reset_flip_state(sym)
                    logger.info(
                        "FLIP %s → %s qty=%s entry=%.6f lev=%dx margin=$%.2f notional=$%.2f tp=%.6f sl=%.6f (±$%.2f)",
                        sym, side, qty, price, lev, margin, notional, tp, sl, tp_d
                    )
                except Exception as e:
                    logger.warning("FLIP %s: open failed: %s", sym, e)
                return

            # flip kapalı ise hiçbir şey yapma
            return

        # ---- Pozisyon YOK: normal otomatik açılış ----
        self._reset_flip_state(sym)
        last = self.signal_cooldown.get(sym, 0)
        cd_ms = getattr(settings, "SIGNAL_COOLDOWN_MS", 3000)
        if ts - last < cd_ms:
            return

        lev = int(getattr(settings, "AUTO_LEVERAGE", settings.LEVERAGE))
        margin = float(getattr(settings, "AUTO_MARGIN_USD", settings.MARGIN_PER_TRADE))
        notional = float(getattr(settings, "AUTO_NOTIONAL_USD", margin * lev))
        qty = max(1e-8, round(notional / price, 6))
        if qty <= 0:
            return

        tp_d = float(getattr(settings, "AUTO_ABS_TP_USD", 50.0))
        sl_d = float(getattr(settings, "AUTO_ABS_SL_USD", 50.0))
        d_tp = tp_d / qty
        d_sl = sl_d / qty

        if side == "long":
            tp = price + d_tp
            sl = price - d_sl
        else:
            tp = price - d_tp
            sl = price + d_sl

        try:
            self.paper.open(
                sym, side, qty, price, sl, tp,
                leverage=lev, margin_usd=margin, maint_margin_rate=settings.MAINT_MARGIN_RATE
            )
            self.signal_cooldown[sym] = ts
            logger.info("AUTO-OPEN %s %s qty=%s entry=%.6f lev=%dx margin=$%.2f notional=$%.2f tp=%.6f sl=%.6f (±$%.2f)",
                        sym, side, qty, price, lev, margin, notional, tp, sl, tp_d)
        except Exception as e:
            logger.warning("AUTO-OPEN failed %s: %s", sym, e)

    # ---------------------------------------------------
    # Handlers
    # ---------------------------------------------------
    async def _handle_agg_trade(self, data: dict):
        """
        aggTrade payload (combined stream'te data altında gelir):
        {
          "e":"aggTrade","E":...,"s":"BTCUSDT",
          "p":"61750.12","q":"0.001","T":...,"m":false,...
        }
        """
        d = data.get("data") if "data" in data else data
        sym = d.get("s")
        if not sym or "p" not in d or "q" not in d or "T" not in d:
            return
        try:
            price = float(d["p"])
            qty = float(d["q"])
            ts = int(d["T"])
            buyer_is_maker = bool(d.get("m")) if "m" in d else None  # m=True → buyer is maker → sell aggressor
        except Exception:
            return

        # State güncelle
        ema_f, ema_s = self.state.on_agg_trade(sym, price, qty, ts, buyer_is_maker)

        # Paper PnL & stop/tp/liq kontrol
        self.paper.mark_to_market(sym, price)

        if ema_f is not None and ema_s is not None:
            logger.info("TICK %s p=%.8f ema_fast=%.4f ema_slow=%.4f", sym, price, ema_f, ema_s)

        # Otomatik açma / flip dene
        self._maybe_auto_trade(sym)

        # Opsiyonel forward
        await self._forward_n8n(d)

    async def _handle_depth(self, data: dict):
        """
        bookTicker payload (combined stream'te data altında gelebilir):
        {"s":"BTCUSDT","b":"61700.00","B":"1.234","a":"61700.10","A":"0.987","E":...}
        """
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
                    await handler(msg)  # handler içinde "data" kontrolü var
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
    # Basit sinyal görünümü (/signals için)
    # ---------------------------------------------------
    def get_signals(self) -> dict:
        """
        Her sembol için basit bir sinyal özeti döner:
          - side: "long" | "short" | None (şu anki kurallara göre)
          - last_price, ema_fast, ema_slow, rsi14, vwap60, atr60, tick rate, spread, buy pressure
        """
        out = {}
        snap = self.state.snapshot()
        for sym, st in snap.items():
            out[sym] = {
                "side": self._decide_side(sym),
                "last_price": st.get("last_price"),
                "ema_fast": st.get("ema_fast"),
                "ema_slow": st.get("ema_slow"),
                "rsi14": st.get("rsi14"),
                "vwap60": st.get("vwap60"),
                "atr60": st.get("atr60"),
                "tick_rate_2s": st.get("tick_rate_2s"),
                "spread_bps": st.get("spread_bps"),
                "buy_pressure_2s": st.get("buy_pressure_2s"),
                "imbalance": st.get("imbalance"),
            }
        return out
