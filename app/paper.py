# app/paper.py
from dataclasses import dataclass, asdict
from typing import Dict, Optional
import time

@dataclass
class Position:
    symbol: str
    side: str                 # "long" | "short"
    qty: float
    entry: float
    stop: Optional[float] = None
    tp: Optional[float] = None
    pnl: float = 0.0
    open_ts: int = 0

    # Leverage / margin
    leverage: int = 1
    margin_usd: float = 0.0         # yatırılan teminat
    notional_usd: float = 0.0       # pozisyon büyüklüğü
    liq_price: Optional[float] = None

    # Close fields
    exit_price: Optional[float] = None
    close_ts: Optional[int] = None

    def as_snapshot(self) -> dict:
        d = asdict(self)
        # Frontend için isim uyumları
        d["stop"] = self.stop
        d["tp"] = self.tp
        d["exit"] = self.exit_price
        return d


def _now_ms() -> int:
    return int(time.time() * 1000)


def _calc_liq_price(entry: float, side: str, lev: int, mmr: float) -> float:
    """
    Basitleştirilmiş USDT-M isolated likidasyon tahmini:
      long:  liq = entry * (1 - (1/lev - mmr))
      short: liq = entry * (1 + (1/lev - mmr))
    """
    adj = (1.0 / lev) - mmr
    if side == "long":
        return entry * (1 - adj)
    else:
        return entry * (1 + adj)


class PaperBroker:
    def __init__(self, max_positions: int = 10, daily_loss_limit: float | None = None, on_close=None):
        self.max_positions = max_positions
        self.daily_loss_limit = daily_loss_limit
        self.positions: Dict[str, Position] = {}   # symbol -> Position
        self.closed_count = 0
        self.last_closed: dict | None = None
        self.on_close = on_close

    # ---- API ----
    def open(
        self, symbol: str, side: str, qty: float, price: float,
        stop: float | None = None, tp: float | None = None,
        leverage: int = 1, margin_usd: float = 0.0, maint_margin_rate: float = 0.004
    ) -> Position:
        if symbol in self.positions:
            raise ValueError(f"Position already open for {symbol}")
        if len(self.positions) >= self.max_positions:
            raise ValueError("Max positions reached")

        pos = Position(
            symbol=symbol, side=side, qty=qty, entry=price,
            stop=stop, tp=tp, pnl=0.0, open_ts=_now_ms(),
            leverage=leverage,
            margin_usd=margin_usd,
            notional_usd=qty * price,
            liq_price=_calc_liq_price(price, side, leverage, maint_margin_rate),
        )
        self.positions[symbol] = pos
        return pos

    def close(self, symbol: str, price: float) -> Position:
        if symbol not in self.positions:
            raise ValueError("No open position for symbol")
        pos = self.positions.pop(symbol)
        # realize pnl
        sign = 1.0 if pos.side == "long" else -1.0
        pos.pnl += (price - pos.entry) * pos.qty * sign
        pos.exit_price = price
        pos.close_ts = _now_ms()
        self.closed_count += 1
        self.last_closed = pos.as_snapshot()
        if self.on_close:
            try:
                self.on_close(self.last_closed)
            except Exception:
                pass
        return pos

    def mark_to_market(self, symbol: str, price: float) -> None:
        if symbol not in self.positions:
            return
        pos = self.positions[symbol]
        sign = 1.0 if pos.side == "long" else -1.0
        unreal = (price - pos.entry) * pos.qty * sign
        pos.pnl = unreal

        # TP / SL kontrolü
        if pos.tp and ((pos.side == "long" and price >= pos.tp) or (pos.side == "short" and price <= pos.tp)):
            self.close(symbol, price)
            return
        if pos.stop and ((pos.side == "long" and price <= pos.stop) or (pos.side == "short" and price >= pos.stop)):
            self.close(symbol, price)
            return
        # Basit likidasyon kontrolü
        if pos.liq_price is not None:
            if (pos.side == "long" and price <= pos.liq_price) or (pos.side == "short" and price >= pos.liq_price):
                self.close(symbol, price)

    def snapshot(self) -> dict:
        return {
            "open": {s: p.as_snapshot() for s, p in self.positions.items()},
            "closed_count": self.closed_count,
            "last_closed": self.last_closed,
        }
import time
from typing import Callable, Optional

class PaperPosition:
    def __init__(self, symbol: str, side: str, qty: float, entry: float,
                 stop: float | None = None, tp: float | None = None):
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.entry = entry
        self.stop = stop
        self.tp = tp
        self.open_ts = int(time.time() * 1000)
        self.close_ts = None
        self.closed = False
        self.exit_price = None
        self.pnl = 0.0

    def mark(self, last_price: float):
        if self.closed:
            return self.pnl
        if self.side == "long":
            self.pnl = (last_price - self.entry) * self.qty
        else:
            self.pnl = (self.entry - last_price) * self.qty
        return self.pnl

    def should_exit(self, last_price: float) -> bool:
        if self.closed:
            return False
        if self.stop is not None:
            if self.side == "long" and last_price <= self.stop:
                return True
            if self.side == "short" and last_price >= self.stop:
                return True
        if self.tp is not None:
            if self.side == "long" and last_price >= self.tp:
                return True
            if self.side == "short" and last_price <= self.tp:
                return True
        return False

    def close(self, exit_price: float):
        if self.closed:
            return
        self.mark(exit_price)
        self.closed = True
        self.exit_price = exit_price
        self.close_ts = int(time.time() * 1000)

class PaperBroker:
    def __init__(self, max_positions: int = 5, daily_loss_limit: float | None = None,
                 on_close: Optional[Callable[[dict], None]] = None):
        self.positions: dict[str, PaperPosition] = {}
        self.history: list[dict] = []
        self.max_positions = max_positions
        self.daily_loss_limit = daily_loss_limit
        self.on_close = on_close

    def open(self, symbol: str, side: str, qty: float, price: float,
             stop: float | None = None, tp: float | None = None):
        if len(self.positions) >= self.max_positions:
            raise ValueError("Max open positions reached")
        if symbol in self.positions and not self.positions[symbol].closed:
            raise ValueError("Position already open for symbol")
        pos = PaperPosition(symbol, side, qty, price, stop, tp)
        self.positions[symbol] = pos
        return pos

    def close(self, symbol: str, price: float):
        pos = self.positions.get(symbol)
        if not pos or pos.closed:
            raise ValueError("No open position for symbol")
        pos.close(price)
        rec = {
            "symbol": pos.symbol,
            "side": pos.side,
            "qty": pos.qty,
            "entry": pos.entry,
            "exit": pos.exit_price,
            "pnl": pos.pnl,
            "open_ts": pos.open_ts,
            "close_ts": pos.close_ts,
        }
        self.history.append(rec)
        del self.positions[symbol]

        if self.on_close:
            try:
                self.on_close(rec)
            except Exception:
                pass
        return pos

    def mark_to_market(self, symbol: str, price: float):
        pos = self.positions.get(symbol)
        if not pos:
            return None
        pnl = pos.mark(price)
        if pos.should_exit(price):
            self.close(symbol, price)
        return pnl

    def snapshot(self):
        open_pos = {}
        for s, p in self.positions.items():
            open_pos[s] = {
                "side": p.side,
                "qty": p.qty,
                "entry": p.entry,
                "stop": p.stop,
                "tp": p.tp,
                "pnl": p.pnl,
                "open_ts": p.open_ts
            }
        return {
            "open": open_pos,
            "closed_count": len(self.history),
            "last_closed": self.history[-1] if self.history else None
        }
