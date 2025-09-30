# app/state.py
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from math import fabs
from typing import Deque, Dict, List, Optional, Tuple


def now_ms() -> int:
    """Epoch ms."""
    return int(time.time() * 1000)


# -----------------------------
# EMA hesaplayıcı
# -----------------------------
class EmaCalc:
    def __init__(self, period: int):
        self.period = int(max(1, period))
        self.k = 2 / (self.period + 1)
        self.value: Optional[float] = None

    def update(self, price: float) -> float:
        if self.value is None:
            self.value = float(price)
        else:
            self.value = float(price) * self.k + self.value * (1 - self.k)
        return self.value


# -----------------------------
# Tek sembolün durumunu tutar
# -----------------------------
class SymbolState:
    def __init__(
        self,
        symbol: str,
        ema_fast: int = 5,
        ema_slow: int = 20,
        trade_maxlen: int = 3000,
        depth_maxlen: int = 200,
    ):
        self.symbol = symbol

        # trade history: (ts, price, qty, is_buy_aggr)
        self.trades: Deque[Tuple[int, float, float, Optional[bool]]] = deque(
            maxlen=trade_maxlen
        )
        self.last_price: Optional[float] = None
        self.last_qty: Optional[float] = None
        self.last_ts: Optional[int] = None

        # EMA
        self.ema_fast = EmaCalc(ema_fast)
        self.ema_slow = EmaCalc(ema_slow)

        # Depth: (best_bid, best_ask, bid_vol, ask_vol)
        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None
        self.bid_vol: float = 0.0
        self.ask_vol: float = 0.0
        self.depth_events: Deque[Tuple[int, float, float, float, float]] = deque(
            maxlen=depth_maxlen
        )

        # --- RSI(14) ---
        self.rsi_period = 14
        self.rsi_gain = 0.0
        self.rsi_loss = 0.0
        self.rsi_value: Optional[float] = None
        self.prev_price: Optional[float] = None

    # ------ Trades ------
    def on_trade(self, price: float, qty: float, ts: int, is_buy_aggr: Optional[bool]):
        """Agg trade geldiğinde çağır."""
        self.last_price = float(price)
        self.last_qty = float(qty)
        self.last_ts = int(ts)
        self.trades.append((ts, float(price), float(qty), is_buy_aggr))

        # EMA
        f = self.ema_fast.update(price)
        s = self.ema_slow.update(price)

        # RSI(14)
        if self.prev_price is not None:
            change = price - self.prev_price
            gain = max(change, 0.0)
            loss = max(-change, 0.0)
            if self.rsi_gain == 0 and self.rsi_loss == 0:
                # ilk değerlemeyi mevcut farkla başlat
                self.rsi_gain = gain
                self.rsi_loss = loss
            else:
                # Wilder's smoothing
                self.rsi_gain = (self.rsi_gain * (self.rsi_period - 1) + gain) / self.rsi_period
                self.rsi_loss = (self.rsi_loss * (self.rsi_period - 1) + loss) / self.rsi_period

            if self.rsi_loss == 0:
                self.rsi_value = 100.0
            else:
                rs = self.rsi_gain / self.rsi_loss
                self.rsi_value = 100.0 - (100.0 / (1.0 + rs))
        self.prev_price = float(price)

        return f, s

    # ------ Depth top-of-book ------
    def on_depth_top(self, best_bid: float, best_ask: float, bid_vol: float, ask_vol: float, ts: int):
        self.best_bid = float(best_bid)
        self.best_ask = float(best_ask)
        self.bid_vol = float(bid_vol)
        self.ask_vol = float(ask_vol)
        self.depth_events.append((int(ts), self.best_bid, self.best_ask, self.bid_vol, self.ask_vol))

    # ------ Metrics ------
    def spread_bps(self) -> Optional[float]:
        if not self.best_bid or not self.best_ask or self.best_bid <= 0:
            return None
        mid = (self.best_ask + self.best_bid) / 2.0
        if mid <= 0:
            return None
        spread = (self.best_ask - self.best_bid) / mid
        return spread * 10000.0  # bps

    def imbalance(self) -> Optional[float]:
        if self.bid_vol <= 0 or self.ask_vol <= 0:
            return None
        return self.bid_vol / self.ask_vol

    def vwap(self, window_ms: int) -> Optional[float]:
        cutoff = (self.last_ts or now_ms()) - window_ms
        num = 0.0
        den = 0.0
        for ts, p, q, _ in reversed(self.trades):
            if ts < cutoff:
                break
            num += p * q
            den += q
        if den <= 0:
            return None
        return num / den

    def atr_like(self, window_ms: int) -> Optional[float]:
        """
        Candles olmadan basit ATR tahmini:
        - pencere içindeki fiyatların min/max’ı ve bir önceki son fiyattan
          TR ~ max(|H-L|, |H-prev|, |L-prev|)
        - normalize: TR / last_price
        """
        cutoff = (self.last_ts or now_ms()) - window_ms
        px = [p for ts, p, _, _ in self.trades if ts >= cutoff]
        if len(px) < 5:
            return None
        H = max(px)
        L = min(px)
        prev = px[0]
        tr = max(H - L, fabs(H - prev), fabs(L - prev))
        if self.last_price and self.last_price > 0:
            return tr / self.last_price
        return None

    def tick_rate(self, lookback_ms: int = 2000) -> float:
        cutoff = (self.last_ts or now_ms()) - lookback_ms
        n = sum(1 for ts, *_ in self.trades if ts >= cutoff)
        return n / (lookback_ms / 1000.0)

    def buy_pressure(self, lookback_ms: int = 2000) -> Optional[float]:
        cutoff = (self.last_ts or now_ms()) - lookback_ms
        buy = 0
        total = 0
        for ts, _, _, is_buy in self.trades:
            if ts < cutoff or is_buy is None:
                continue
            total += 1
            if is_buy:
                buy += 1
        if total == 0:
            return None
        return buy / total


# -----------------------------
# Piyasa durumu (birden çok sembol)
# -----------------------------
class MarketState:
    def __init__(self, symbols: List[str], ema_fast: int = 5, ema_slow: int = 20):
        self.symbols: Dict[str, SymbolState] = {
            s: SymbolState(s, ema_fast, ema_slow) for s in symbols
        }

    def ensure(self, symbol: str):
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolState(symbol)

    # aggTrade
    def on_agg_trade(self, symbol: str, price: float, qty: float, ts: int, buyer_is_maker: Optional[bool]):
        """
        Binance 'm' (buyer_is_maker) True ise aggressor = SELL, yani buy_aggr = not m
        """
        is_buy_aggr = None if buyer_is_maker is None else (not buyer_is_maker)
        self.ensure(symbol)
        return self.symbols[symbol].on_trade(price, qty, ts, is_buy_aggr)

    # depth top
    def on_top(self, symbol: str, best_bid: float, best_ask: float, bid_vol: float, ask_vol: float, ts: int):
        self.ensure(symbol)
        self.symbols[symbol].on_depth_top(best_bid, best_ask, bid_vol, ask_vol, ts)

    def snapshot(self) -> Dict[str, dict]:
        """
        UI/endpoint için sinyal özeti.
        Dönüş örneği:
        {
          "BTCUSDT": {
            "last_price": ...,
            "ema_fast": ...,
            "ema_slow": ...,
            "vwap60": ...,
            "atr60": ...,
            "rsi14": ...,
            "tick_rate_2s": ...,
            "buy_pressure_2s": ...,
            "spread_bps": ...,
            "imbalance": ...,
            "last_ts": ...
          }, ...
        }
        """
        out: Dict[str, dict] = {}
        for s, st in self.symbols.items():
            out[s] = {
                "last_price": st.last_price,
                "ema_fast": st.ema_fast.value,
                "ema_slow": st.ema_slow.value,
                "vwap60": st.vwap(60_000),
                "atr60": st.atr_like(60_000),
                "rsi14": st.rsi_value,
                "tick_rate_2s": st.tick_rate(2_000),
                "buy_pressure_2s": st.buy_pressure(2_000),
                "spread_bps": st.spread_bps(),
                "imbalance": st.imbalance(),
                "last_ts": st.last_ts,
            }
        return out
