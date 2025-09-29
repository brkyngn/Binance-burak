import time
from collections import deque

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
    def __init__(self, symbol: str, ema_fast: int = 5, ema_slow: int = 20, maxlen: int = 500):
        self.symbol = symbol
        self.last_price = None
        self.last_qty = None
        self.last_ts = None
        self.ema_fast = EmaCalc(ema_fast)
        self.ema_slow = EmaCalc(ema_slow)
        self.history = deque(maxlen=maxlen)  # (ts, price, qty)

    def on_trade(self, price: float, qty: float, ts: int):
        self.last_price = price
        self.last_qty = qty
        self.last_ts = ts
        f = self.ema_fast.update(price)
        s = self.ema_slow.update(price)
        self.history.append((ts, price, qty))
        return f, s

class MarketState:
    def __init__(self, symbols: list[str], ema_fast: int = 5, ema_slow: int = 20):
        self.symbols = {s: SymbolState(s, ema_fast, ema_slow) for s in symbols}

    def ensure(self, symbol: str):
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolState(symbol)

    def on_agg_trade(self, symbol: str, price: float, qty: float, ts: int):
        self.ensure(symbol)
        return self.symbols[symbol].on_trade(price, qty, ts)

    def snapshot(self):
        out = {}
        for s, st in self.symbols.items():
            out[s] = {
                "last_price": st.last_price,
                "last_qty": st.last_qty,
                "last_ts": st.last_ts,
                "ema_fast": st.ema_fast.value,
                "ema_slow": st.ema_slow.value,
                "history_len": len(st.history),
            }
        return out
