# app/paper.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Dict, Optional, List
import time

from .logger import logger
from .config import settings

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
    pnl: float = 0.0          # anlık/ham PnL (fee hariç)
    open_ts: int = 0
    exit_price: Optional[float] = None
    close_ts: Optional[int] = None

    # Leverage/margin sim
    leverage: Optional[int] = None
    margin_usd: Optional[float] = None
    notional_usd: Optional[float] = None
    maint_margin_rate: Optional[float] = None
    liq_price: Optional[float] = None

    # son görülen fiyat (snapshot fallback)
    last_price: Optional[float] = None

    # --- Fees ---
    fee_open_usd: float = 0.0
    fee_close_usd: float = 0.0
    fee_total_usd: float = 0.0
    fee_currency: str = "USD"           # "USD" veya "BNB"
    fee_open_bnb: Optional[float] = None
    fee_close_bnb: Optional[float] = None
    fee_total_bnb: Optional[float] = None

class PaperBroker:
    def __init__(
        self,
        max_positions: int = 10,
        daily_loss_limit: Optional[float] = None,
        on_close: Optional[Callable[[dict], None]] = None
    ):
        self.max_positions = max_positions
        self.daily_loss_limit = daily_loss_limit
        self.on_close = on_close
        self.positions: Dict[str, Position] = {}
        self.closed_count = 0
        self.last_closed: Optional[dict] = None

    # ---- helpers ----
    @staticmethod
    def _approx_liq_price(side: str, entry: float, lev: Optional[int], mmr: Optional[float]) -> Optional[float]:
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

    def _fee_rate_effective(self, for_open: bool) -> float:
        # maker/taker seçimi: sadeleştirme için tek mod kullanıyoruz
        base = settings.FEE_TAKER if settings.FEE_MODE.lower() == "taker" else settings.FEE_MAKER
        # BNB ile ödeme indirimi
        if settings.PAY_FEES_IN_BNB:
            base *= (1.0 - settings.BNB_FEE_DISCOUNT)
        return float(base)

    # ---- API ----
    def open(
        self,
        symbol: str,
        side: str,           # "long" | "short"
        qty: float,
        price: float,
        stop: Optional[float] = None,
        tp: Optional[float] = None,
        *,
        leverage: Optional[int] = None,
        margin_usd: Optional[float] = None,
        maint_margin_rate: Optional[float] = None,
        notional_usd: Optional[float] = None
    ) -> Position:

        if symbol.upper() in self.positions:
            raise ValueError(f"{symbol} already has an open position")
        if len(self.positions) >= self.max_positions:
            raise ValueError("max positions reached")

        entry = float(price)
        qty = float(qty)

        # Notional / margin
        if notional_usd is None:
            if leverage and margin_usd:
                notional_usd = float(leverage) * float(margin_usd)
            else:
                notional_usd = qty * entry

        liq_px = self._approx_liq_price(side, entry, leverage, maint_margin_rate)

        # --- Açılış ücreti ---
        fee_rate_open = self._fee_rate_effective(for_open=True)
        fee_open_usd = float(notional_usd) * fee_rate_open

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
            last_price=None,
            # fees
            fee_open_usd=fee_open_usd,
            fee_currency="BNB" if settings.PAY_FEES_IN_BNB else "USD",
        )
        self.positions[pos.symbol] = pos
        return pos

    def close(self, symbol: str, price: float, *, bnb_usd_price: Optional[float] = None) -> Position:
        symbol = symbol.upper()
        if symbol not in self.positions:
            raise ValueError("No open position for symbol")
        pos = self.positions.pop(symbol)

        pos.exit_price = float(price)
        pos.close_ts = now_ms()
        pos.pnl = self._pnl(pos.side, pos.entry, pos.exit_price, pos.qty)  # ham PnL (fee hariç)

        # --- Kapanış ücreti ---
        close_notional = pos.exit_price * pos.qty
        fee_rate_close = self._fee_rate_effective(for_open=False)
        pos.fee_close_usd = close_notional * fee_rate_close

        # --- Toplam fee (USD) ---
        pos.fee_total_usd = float((pos.fee_open_usd or 0.0) + (pos.fee_close_usd or 0.0))

        # --- BNB karşılığı (opsiyonel) ---
        if settings.PAY_FEES_IN_BNB:
            bnb_px = float(bnb_usd_price) if bnb_usd_price else None
            if bnb_px and bnb_px > 0:
                pos.fee_open_bnb  = (pos.fee_open_usd  / bnb_px) if pos.fee_open_usd  else 0.0
                pos.fee_close_bnb = (pos.fee_close_usd / bnb_px) if pos.fee_close_usd else 0.0
                pos.fee_total_bnb = (pos.fee_total_usd / bnb_px)
            else:
                pos.fee_open_bnb = pos.fee_close_bnb = pos.fee_total_bnb = None

        # DB kaydı (net PnL dahil)
        rec = {
            "symbol": pos.symbol,
            "side": pos.side,
            "qty": float(pos.qty),
            "entry": float(pos.entry),
            "exit": float(pos.exit_price),
            "pnl": float(pos.pnl),                             # ham PnL
            "net_pnl": float(pos.pnl - pos.fee_total_usd),    # net (fee sonrası)
            "leverage": int(pos.leverage) if pos.leverage is not None else None,
            "margin_usd": float(pos.margin_usd) if pos.margin_usd is not None else None,
            "notional_usd": float(pos.notional_usd) if pos.notional_usd is not None else float(pos.qty * pos.entry),
            "liq_price": float(pos.liq_price) if pos.liq_price is not None else None,
            "fee_open_usd": float(pos.fee_open_usd) if pos.fee_open_usd is not None else None,
            "fee_close_usd": float(pos.fee_close_usd) if pos.fee_close_usd is not None else None,
            "fee_total_usd": float(pos.fee_total_usd),
            "fee_currency": pos.fee_currency,
            "fee_open_bnb": float(pos.fee_open_bnb) if pos.fee_open_bnb is not None else None,
            "fee_close_bnb": float(pos.fee_close_bnb) if pos.fee_close_bnb is not None else None,
            "fee_total_bnb": float(pos.fee_total_bnb) if pos.fee_total_bnb is not None else None,
            "open_ts": int(pos.open_ts),
            "close_ts": int(pos.close_ts) if pos.close_ts is not None else None,
        }
        self.closed_count += 1
        self.last_closed = rec
        try:
            if self.on_close:
                self.on_close(rec)
        except Exception as e:
            logger.warning("on_close callback failed: %s", e)
        return pos

    def mark_to_market(self, symbol: str, price: float):
        symbol = symbol.upper()
        pos = self.positions.get(symbol)
        if not pos:
            return
        price = float(price)
        pos.pnl = self._pnl(pos.side, pos.entry, price, pos.qty)  # ham PnL
        pos.last_price = price

        # Likidasyon
        if pos.liq_price is not None:
            if pos.side == "long" and price <= pos.liq_price:
                logger.info("LIQ %s long @ %.4f → liq %.4f", symbol, price, pos.liq_price)
                self.close(symbol, price)
                return
            if pos.side == "short" and price >= pos.liq_price:
                logger.info("LIQ %s short @ %.4f → liq %.4f", symbol, price, pos.liq_price)
                self.close(symbol, price)
                return

        # TP/SL
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

    def snapshot(self, last_price_map: Optional[Dict[str, float]] = None) -> List[dict]:
        out: List[dict] = []
        lpm = last_price_map or {}
        for sym, p in self.positions.items():
            try:
                entry = float(p.entry); qty = float(p.qty); side = str(p.side)
                lp_raw = lpm.get(sym, p.last_price)
                last_price = float(lp_raw) if lp_raw is not None else None
                notional = float(p.notional_usd) if p.notional_usd is not None else (entry * qty)
                pnl = p.pnl
                if last_price is not None:
                    pnl = self._pnl(side, entry, last_price, qty)
                row = {
                    "symbol": sym,
                    "side": side,
                    "qty": qty,
                    "entry": entry,
                    "liq_price": float(p.liq_price) if p.liq_price is not None else None,
                    "last_price": last_price,
                    "pnl": float(pnl) if pnl is not None else None,
                    "notional_usd": float(notional),
                    "leverage": int(p.leverage) if p.leverage is not None else None,
                    # fees (açılış fee'i bilgi için)
                    "fee_open_usd": float(p.fee_open_usd) if p.fee_open_usd is not None else None,
                    "fee_currency": p.fee_currency,
                }
                out.append(row)
            except Exception as e:
                out.append({"symbol": sym, "error": f"snapshot_row_error: {type(e).__name__}: {e}"})
        return out
