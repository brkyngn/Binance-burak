import time

class PaperPosition:
    def __init__(self, symbol: str, side: str, qty: float, entry: float, stop: float | None = None, tp: float | None = None):
        self.symbol = symbol
        self.side = side  # "long" | "short"
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
    def __init__(self, max_positions: int = 5, daily_loss_limit: float | None = None):
        self.positions: dict[str, PaperPosition] = {}
        self.history: list[dict] = []
        self.max_positions = max_positions
        self.daily_loss_limit = daily_loss_limit

    def open(self, symbol: str, side: str, qty: float, price: float, stop: float | None = None, tp: float | None = None):
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
        self.history.append({
            "symbol": pos.symbol,
            "side": pos.side,
            "qty": pos.qty,
            "entry": pos.entry,
            "exit": pos.exit_price,
            "pnl": pos.pnl,
            "open_ts": pos.open_ts,
            "close_ts": pos.close_ts,
        })
        del self.positions[symbol]
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
                "side": p.side, "qty": p.qty, "entry": p.entry,
                "stop": p.stop, "tp": p.tp, "pnl": p.pnl, "open_ts": p.open_ts
            }
        return {
            "open": open_pos,
            "closed_count": len(self.history),
            "last_closed": self.history[-1] if self.history else None
        }
