# app/state.py
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from math import fabs
from typing import Deque, Dict, List, Optional, Tuple

def now_ms() -> int:
    return int(time.time() * 1000)

# -----------------------------
# EMA
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
# Tek sembol state
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

        # trades: (ts, price, qty, is_buy_aggr)
        self.trades: Deque[Tuple[int, float, float, Optional[bool]]] = deque(maxlen=trade_maxlen)
        self.last_price: Optional[float] = None
        self.last_qty: Optional[float] = None
        self.last_ts: Optional[int] = None

        # EMA
        self.ema_fast = EmaCalc(ema_fast)
        self.ema_slow = EmaCalc(ema_slow)

        # Depth/bookTicker
        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None
        self.bid_vol: float = 0.0
        self.ask_vol: float = 0.0
        self.depth_events: Deque[Tuple[int, float, float, float, float]] = deque(maxlen=depth_maxlen)

        # RSI(14)
        self.rsi_period = 14
        self.rsi_gain = 0.0
        self.rsi_loss = 0.0
        self.rsi_value: Optional[float] = None
        self.prev_price: Optional[float] = None

    # ------ Trades ------
    def on_trade(self, price: float, qty: float, ts: int, is_buy_aggr: Optional[bool]):
        self.last_price = float(price)
        self.last_qty = float(qty)
        self.last_ts = int(ts)
        self.trades.append((ts, float(price), float(qty), is_buy_aggr))

        f = self.ema_fast.update(price)
        s = self.ema_slow.update(price)

        # RSI(14), Wilder smoothing
        if self.prev_price is not None:
            change = price - self.prev_price
            gain = max(change, 0.0)
            loss = max(-change, 0.0)
            if self.rsi_gain == 0 and self.rsi_loss == 0:
                self.rsi_gain = gain
                self.rsi_loss = loss
            else:
                self.rsi_gain = (self.rsi_gain * (self.rsi_period - 1) + gain) / self.rsi_period
                self.rsi_loss = (self.rsi_loss * (self.rsi_period - 1) + loss) / self.rsi_period
            if self.rsi_loss == 0:
                self.rsi_value = 100.0
            else:
                rs = self.rsi_gain / self.rsi_loss
                self.rsi_value = 100.0 - (100.0 / (1.0 + rs))
        self.prev_price = float(price)

        return f, s

    # ------ Depth top ------
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
        return ((self.best_ask - self.best_bid) / mid) * 10000.0

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

    def vwap_dev_pct(self, window_ms: int) -> Optional[float]:
        v = self.vwap(window_ms)
        if v is None or not self.last_price:
            return None
        return abs(self.last_price - v) / v

    def atr_like(self, window_ms: int) -> Optional[float]:
        cutoff = (self.last_ts or now_ms()) - window_ms
        px = [p for ts, p, _, _ in self.trades if ts >= cutoff]
        if len(px) < 5:
            return None
        H = max(px); L = min(px); prev = px[0]
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
        buy = 0; total = 0
        for ts, _, _, is_buy in self.trades:
            if ts < cutoff or is_buy is None:
                continue
            total += 1
            if is_buy:
                buy += 1
        if total == 0:
            return None
        return buy / total

    # --------- Yeni: Volume Spike (5s vs 60s ortalama 5s) ----------
    def volume_spike_ratio(self, short_ms: int = 5000, long_ms: int = 60000) -> Optional[float]:
        now = self.last_ts or now_ms()
        cut_s = now - short_ms
        cut_l = now - long_ms
        vol_s = 0.0
        vol_l = 0.0
        for ts, _, q, _ in reversed(self.trades):
            if ts < cut_l:
                break
            vol_l += q
            if ts >= cut_s:
                vol_s += q
        if vol_l <= 0:
            return None
        avg_5s = vol_l * (short_ms / long_ms)  # 60s'lik toplamın 5s'e ölçeklenmiş ortalaması
        if avg_5s <= 0:
            return None
        return vol_s / avg_5s

    # --------- Yeni: CVD (window içi) ----------
    def cvd(self, window_ms: int = 600_000) -> Optional[float]:
        now = self.last_ts or now_ms()
        cut = now - window_ms
        s = 0.0; seen = False
        for ts, _, q, is_buy in reversed(self.trades):
            if ts < cut:
                break
            if is_buy is None:
                continue
            seen = True
            s += (q if is_buy else -q)
        return s if seen else None

    # --------- Yeni: basit S/R yakınlığı ----------
    def sr_near_pct(self, window_ms: int = 1_800_000, swing_k: int = 3) -> Optional[float]:
        """
        Son 30dk içinde basit swing-high/low (k-adımlı) seviyeleri bul,
        mevcut fiyata en yakın seviyenin göreli mesafesini (%) döndür.
        """
        if not self.last_price:
            return None
        cut = (self.last_ts or now_ms()) - window_ms
        px = [(ts, p) for ts, p, _, _ in self.trades if ts >= cut]
        if len(px) < (2 * swing_k + 1):
            return None
        prices = [p for _, p in px]

        levels: List[float] = []
        n = len(prices)
        for i in range(swing_k, n - swing_k):
            p = prices[i]
            left = prices[i - swing_k:i]
            right = prices[i + 1:i + 1 + swing_k]
            if not left or not right:
                continue
            if p > max(left) and p > max(right):
                levels.append(p)  # swing high
            elif p < min(left) and p < min(right):
                levels.append(p)  # swing low

        if not levels:
            return None

        lp = self.last_price
        return min(abs(lp - L) / lp for L in levels)

# -----------------------------
# MarketState
# -----------------------------
class MarketState:
    def __init__(self, symbols: List[str], ema_fast: int = 5, ema_slow: int = 20):
        self.symbols: Dict[str, SymbolState] = {
            s: SymbolState(s, ema_fast, ema_slow) for s in symbols
        }

    def ensure(self, symbol: str):
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolState(symbol)

    def on_agg_trade(self, symbol: str, price: float, qty: float, ts: int, buyer_is_maker: Optional[bool]):
        # Binance 'm' True => buyer is maker => agresör SELL => buy_aggr = not m
        is_buy_aggr = None if buyer_is_maker is None else (not buyer_is_maker)
        self.ensure(symbol)
        return self.symbols[symbol].on_trade(price, qty, ts, is_buy_aggr)

    def on_top(self, symbol: str, best_bid: float, best_ask: float, bid_vol: float, ask_vol: float, ts: int):
        self.ensure(symbol)
        self.symbols[symbol].on_depth_top(best_bid, best_ask, bid_vol, ask_vol, ts)

    def snapshot(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for s, st in self.symbols.items():
            vwap_win = 60_000
            out[s] = {
                "last_price": st.last_price,
                "ema_fast": st.ema_fast.value,
                "ema_slow": st.ema_slow.value,
                "vwap60": st.vwap(vwap_win),
                "vwap_dev_pct": st.vwap_dev_pct(vwap_win),
                "atr60": st.atr_like(60_000),
                "rsi14": st.rsi_value,
                "tick_rate_2s": st.tick_rate(2_000),
                "buy_pressure_2s": st.buy_pressure(2_000),
                "spread_bps": st.spread_bps(),
                "imbalance": st.imbalance(),
                "vol_spike_5s": st.volume_spike_ratio(5_000, 60_000),
                "cvd_10m": st.cvd(600_000),
                "sr_dist_pct": st.sr_near_pct(1_800_000, 3),
                "last_ts": st.last_ts,
            }
        return out
