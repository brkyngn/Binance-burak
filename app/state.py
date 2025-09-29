import time
from collections import deque
from math import fabs

def now_ms() -> int:
    return int(time.time() * 1000)

class EmaCalc:
    def __init__(self, period: int):
        self.period = period
        self.value = None
        self.k = 2 / (period + 1)

    def update(self, price: float):
        if self.value is None:
            self.value = price
        else:
            self.value = price * self.k + self.value * (1 - self.k)
        return self.value

class SymbolState:
    def __init__(self, symbol: str, ema_fast: int = 5, ema_slow: int = 20,
                 trade_maxlen: int = 3000, depth_maxlen: int = 200):
        self.symbol = symbol
        # trade history: (ts, price, qty, is_buy_aggr)
        self.trades = deque(maxlen=trade_maxlen)
        self.last_price = None
        self.last_qty = None
        self.last_ts = None

        # EMA
        self.ema_fast = EmaCalc(ema_fast)
        self.ema_slow = EmaCalc(ema_slow)

        # Depth: (best_bid, best_ask, bid_vol, ask_vol)
        self.best_bid = None
        self.best_ask = None
        self.bid_vol = 0.0
        self.ask_vol = 0.0
        self.depth_events = deque(maxlen=depth_maxlen)  # (ts, best_bid, best_ask, bid_vol, ask_vol)

    # ------ Trades ------
    def on_trade(self, price: float, qty: float, ts: int, is_buy_aggr: bool | None):
        self.last_price = price
        self.last_qty = qty
        self.last_ts = ts
        self.trades.append((ts, price, qty, is_buy_aggr))
        f = self.ema_fast.update(price)
        s = self.ema_slow.update(price)
        return f, s

    # ------ Depth ------
    def on_depth_top(self, best_bid: float, best_ask: float, bid_vol: float, ask_vol: float, ts: int):
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.bid_vol = bid_vol
        self.ask_vol = ask_vol
        self.depth_events.append((ts, best_bid, best_ask, bid_vol, ask_vol))

    # ------ Metrics ------
    def spread_bps(self) -> float | None:
        if not self.best_bid or not self.best_ask or self.best_bid <= 0:
            return None
        spread = (self.best_ask - self.best_bid) / ((self.best_ask + self.best_bid) / 2.0)
        return spread * 10000.0  # bps

    def imbalance(self) -> float | None:
        if self.bid_vol <= 0 or self.ask_vol <= 0:
            return None
        return self.bid_vol / self.ask_vol

    def vwap(self, window_ms: int) -> float | None:
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

    def atr_like(self, window_ms: int) -> float | None:
        """
        Candles olmadan basit ATR tahmini:
        - pencere içindeki fiyatların min/max’ı ve bir önceki son fiyattan TR ~ max(|H-L|, |H-prev|, |L-prev|)
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

    def buy_pressure(self, lookback_ms: int = 2000) -> float | None:
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

class MarketState:
    def __init__(self, symbols: list[str], ema_fast: int = 5, ema_slow: int = 20):
        self.symbols = {s: SymbolState(s, ema_fast, ema_slow) for s in symbols}

    def ensure(self, symbol: str):
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolState(symbol)

    # aggTrade
    def on_agg_trade(self, symbol: str, price: float, qty: float, ts: int, buyer_is_maker: bool | None):
        # Binance 'm' => buyer is maker; aggressor = SELL, dolayısıyla buy_aggr = not m
        is_buy_aggr = None if buyer_is_maker is None else (not buyer_is_maker)
        self.ensure(symbol)
        return self.symbols[symbol].on_trade(price, qty, ts, is_buy_aggr)

    # depth top
    def on_top(self, symbol: str, best_bid: float, best_ask: float, bid_vol: float, ask_vol: float, ts: int):
        self.ensure(symbol)
        self.symbols[symbol].on_depth_top(best_bid, best_ask, bid_vol, ask_vol, ts)

    def snapshot(self):
        out = {}
        for s, st in self.symbols.items():
            out[s] = {
                "last_price": st.last_price,
                "ema_fast": st.ema_fast.value,
                "ema_slow": st.ema_slow.value,
                "vwap60": st.vwap(60000),
                "atr60": st.atr_like(60000),
                "tick_rate_2s": st.tick_rate(2000),
                "buy_pressure_2s": st.buy_pressure(2000),
                "spread_bps": st.spread_bps(),
                "imbalance": st.imbalance(),
                "last_ts": st.last_ts,
            }
        return out
