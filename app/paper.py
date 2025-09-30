# app/paper.py
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Callable, Dict, Optional
import time
from .logger import logger

def now_ms() -> int:
    return int(time.time() * 1000)

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
    exit_price: Optional[float] = None
    close_ts: Optional[int] = None

    # Leverage/margin sim
    leverage: Optional[int] = None
    margin_usd: Optional[float] = None
    notional_usd: Optional[float] = None
    maint_margin_rate: Optional[float] = None
    liq_price: Optional[float] = None

class PaperBroker:
    """
    Basit kâğıt (simülasyon) broker:
      - open/close
      - mark_to_market ile PnL + TP/SL + likidasyon kontrolü
      - on_close callback'i ile DB kaydı
    """
    def __init__(self,
                 max_positions: int = 10,
                 daily_loss_limit: Optional[float] = None,
                 on_close: Optional[Callable[[dict], None]] = None):
        self.max_positions = max_positions
        self.daily_loss_limit = daily_loss_limit
        self.on_close = on_close
        self.positions: Dict[str, Position] = {}
        self.closed_count = 0
        self.last_closed: Optional[dict] = None

    # ---- yardımcılar ----
    @staticmethod
    def _approx_liq_price(side: str, entry: float, lev: Optional[int], mmr: Optional[float]) -> Optional[float]:
        """
        Basit yaklaşık likidasyon:
          long  ~ entry * (1 - 1/lev + mmr)
          short ~ entry * (1 + 1/lev - mmr)
        """
        if not lev or lev <= 0 or entry <= 0:
            return None
        mmr = mmr or 0.0
        if side == "long":
            return entry * (1.0 - 1.0/lev + mmr)
        else:
            return entry * (1.0 + 1.0/lev - mmr)

    @staticmethod
    def _pnl(side: str, entry: float, price: float, qty: float) -> float:
        if side == "long":
            return (price - entry) * qty
        else:
            return (entry - price) * qty

    # ---- API ----
    def open(self,
             symbol: str,
             side: str,                 # "long" | "short"
             qty: float,
             price: float,
             stop: Optional[float] = None,
             tp: Optional[float] = None,
             *,
             leverage: Optional[int] = None,
             margin_usd: Optional[float] = None,
             maint_margin_rate: Optional[float] = None,
             notional_usd: Optional[float] = None) -> Position:

        if symbol in self.positions:
            raise ValueError(f"{symbol} already has an open position")
        if len(self.positions) >= self.max_positions:
            raise ValueError("max positions reached")

        entry = float(price)
        qty = float(qty)

        # Notional / margin
        if notional_usd is None:
            # Eğer leverage & margin verildiyse notional = lev * margin
            if leverage and margin_usd:
                notional_usd = float(leverage) * float(margin_usd)
            else:
                notional_usd = qty * entry

        liq_px = self._approx_liq_price(side, entry, leverage, maint_margin_rate)

        pos = Position(
            symbol=symbol.upper(),
            side=side,
            qty=qty,
            entry=entry,
            stop=float(stop) if stop is not None else None,
            tp=float(tp) if tp is not None else None,
            pnl=0.0,
            open_ts=now_ms(),
            leverage=int(leverage) if leverage is not None else None,
            margin_usd=float(margin_usd) if margin_usd is not None else None,
            notional_usd=float(notional_usd) if notional_usd is not None else None,
            maint_margin_rate=float(maint_margin_rate) if maint_margin_rate is not None else None,
            liq_price=float(liq_px) if liq_px is not None else None,
        )
        self.positions[pos.symbol] = pos
        return pos

    def close(self, symbol: str, price: float) -> Position:
        symbol = symbol.upper()
        if symbol not in self.positions:
            raise ValueError("No open position for symbol")
        pos = self.positions.pop(symbol)
        pos.exit_price = float(price)
        pos.close_ts = now_ms()
        pos.pnl = self._pnl(pos.side, pos.entry, pos.exit_price, pos.qty)

        rec = {
            "symbol": pos.symbol,
            "side": pos.side,
            "qty": pos.qty,
            "entry": pos.entry,
            "exit": pos.exit_price,
            "pnl": pos.pnl,
            "leverage": pos.leverage,
            "margin_usd": pos.margin_usd,
            "notional_usd": pos.notional_usd if pos.notional_usd is not None else (pos.qty * pos.entry),
            "liq_price": pos.liq_price,
            "open_ts": pos.open_ts,
            "close_ts": pos.close_ts,
        }
        self.closed_count += 1
        self.last_closed = rec
        try:
            if self.on_close:
                self.on_close(rec)  # async olabileceği için çağıran taraf create_task ile sarmalı
        except Exception as e:
            logger.warning("on_close callback failed: %s", e)
        return pos

    def mark_to_market(self, symbol: str, price: float):
        """PnL + TP/SL + likidasyon kontrolü. Trigger olursa close() eder."""
        symbol = symbol.upper()
        pos = self.positions.get(symbol)
        if not pos:
            return
        price = float(price)

        # PnL güncelle
        pos.pnl = self._pnl(pos.side, pos.entry, price, pos.qty)

        # Likidasyon kontrolü (yaklaşık)
        if pos.liq_price is not None:
            if pos.side == "long" and price <= pos.liq_price:
                logger.info("LIQ %s long @ %.4f → liq %.4f", symbol, price, pos.liq_price)
                self.close(symbol, price)
                return
            if pos.side == "short" and price >= pos.liq_price:
                logger.info("LIQ %s short @ %.4f → liq %.4f", symbol, price, pos.liq_price)
                self.close(symbol, price)
                return

        # TP/SL kontrolü
        if pos.tp is not None:
            if pos.side == "long" and price >= pos.tp:
                self.close(symbol, price)
                return
            if pos.side == "short" and price <= pos.tp:
                self.close(symbol, price)
                return

        if pos.stop is not None:
            if pos.side == "long" and price <= pos.stop:
                self.close(symbol, price)
                return
            if pos.side == "short" and price >= pos.stop:
                self.close(symbol, price)
                return

    def snapshot(self) -> dict:
        """UI için açık pozisyonların özetini döner."""
        open_map = {}
        for sym, p in self.positions.items():
            open_map[sym] = {
                "side": p.side,
                "qty": p.qty,
                "entry": p.entry,
                "tp": p.tp,
                "stop": p.stop,
                "pnl": p.pnl,
                "open_ts": p.open_ts,
                "leverage": p.leverage,
                "margin_usd": p.margin_usd,
                "notional_usd": p.notional_usd if p.notional_usd is not None else (p.qty * p.entry),
                "liq_price": p.liq_price,
            }
        return {
            "open": open_map,
            "closed_count": self.closed_count,
            "last_closed": self.last_closed,
        }
