# app/paper.py
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Callable
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

    # Leverage / margin simülasyonu
    leverage: int = 1
    margin_usd: float = 0.0         # yatırılan teminat
    notional_usd: float = 0.0       # pozisyon büyüklüğü (qty * entry)
    liq_price: Optional[float] = None

    # Kapanış
    exit_price: Optional[float] = None
    close_ts: Optional[int] = None

    def as_snapshot(self) -> dict:
        d = asdict(self)
        d["exit"] = self.exit_price
        return d

def _now_ms() -> int:
    return int(time.time() * 1000)

def _calc_liq_price(entry: float, side: str, lev: int, mmr: float) -> float:
    """
    Basit USDT-M isolated likidasyon tahmini:
      long:  liq = entry * (1 - (1/lev - mmr))
      short: liq = entry * (1 + (1/lev - mmr))
    """
    adj = (1.0 / lev) - mmr
    if side == "long":
        return entry * (1 - adj)
    else:
        return entry * (1 + adj)

class PaperBroker:
    def __init__(self, max_positions: int = 10, daily_loss_limit: float | None = None, on_close: Callable[[dict], None] | None = None):
        self.max_positions = max_positions
        self.daily_loss_limit = daily_loss_limit
        self.positions: Dict[str, Position] = {}   # symbol -> Position
        self.closed_count = 0
        self.last_closed: dict | None = None
        self.on_close = on_close

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
        pos.pnl = (price - pos.entry) * pos.qty * sign

        # TP/SL tetikleme
        if pos.tp and ((pos.side == "long" and price >= pos.tp) or (pos.side == "short" and price <= pos.tp)):
            self.close(symbol, price)
            return
        if pos.stop and ((pos.side == "long" and price <= pos.stop) or (pos.side == "short" and price >= pos.stop)):
            self.close(symbol, price)
            return

        # Basit likidasyon tetiklemesi
        if pos.liq_price is not None:
            if (pos.side == "long" and price <= pos.liq_price) or (pos.side == "short" and price >= pos.liq_price):
                self.close(symbol, price)

    def snapshot(self) -> dict:
        return {
            "open": {s: p.as_snapshot() for s, p in self.positions.items()},
            "closed_count": self.closed_count,
            "last_closed": self.last_closed,
        }
