# app/db.py
from __future__ import annotations
import json
from typing import Any, List, Optional
from datetime import datetime, timezone
from time import time

import asyncpg
from .config import settings

_pool: Optional[asyncpg.pool.Pool] = None

# ---- Ana tablo (trades) ----
DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty DOUBLE PRECISION NOT NULL,
    entry DOUBLE PRECISION NOT NULL,
    exit DOUBLE PRECISION,
    pnl DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
"""

# ---- trades için sonradan eklenen kolonlar ----
MIGRATIONS = [
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS leverage INT;",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS margin_usd DOUBLE PRECISION;",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS notional_usd DOUBLE PRECISION;",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS liq_price DOUBLE PRECISION;",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS open_ts BIGINT;",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS close_ts BIGINT;",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS raw JSONB;",
]

# ---- Sinyal log tablosu ----
SIGNAL_DDL = """
CREATE TABLE IF NOT EXISTS signal_logs (
    id BIGSERIAL PRIMARY KEY,
    ts_ms BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    last_price DOUBLE PRECISION,
    ema_fast DOUBLE PRECISION,
    ema_slow DOUBLE PRECISION,
    rsi14 DOUBLE PRECISION,
    vwap60 DOUBLE PRECISION,
    vwap_dev_pct DOUBLE PRECISION,
    atr60 DOUBLE PRECISION,
    tick_rate_2s DOUBLE PRECISION,
    spread_bps DOUBLE PRECISION,
    buy_pressure_2s DOUBLE PRECISION,
    sell_pressure_2s DOUBLE PRECISION,
    imbalance DOUBLE PRECISION,
    vol_spike_5s DOUBLE PRECISION,
    cvd_10m DOUBLE PRECISION,
    sr_dist_pct DOUBLE PRECISION,
    candle5_dir TEXT,
    short_vwap_band_ok BOOLEAN,
    side TEXT
);
CREATE INDEX IF NOT EXISTS idx_signal_logs_ts ON signal_logs(ts_ms);
CREATE INDEX IF NOT EXISTS idx_signal_logs_sym_ts ON signal_logs(symbol, ts_ms);
"""


# ---------------------------------
# Init & migrations
# ---------------------------------
async def init_pool():
    """DB pool oluşturur, tablo ve kolonları hazırlar."""
    global _pool
    if not settings.DATABASE_URL:
        return
    _pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
        # trades tablosu
        await conn.execute(DDL)
        for sql in MIGRATIONS:
            try:
                await conn.execute(sql)
            except Exception:
                pass  # eşzamanlı deploy vb.

        # signal_logs tablosu
        await conn.execute(SIGNAL_DDL)


async def ping() -> bool:
    if not settings.DATABASE_URL:
        return False
    global _pool
    if _pool is None:
        await init_pool()
    try:
        async with _pool.acquire() as conn:
            await conn.fetchval("SELECT 1;")
        return True
    except Exception:
        return False


# ---------------------------------
# Trades işlemleri
# ---------------------------------
async def insert_trade(rec: dict):
    if not settings.DATABASE_URL:
        return
    global _pool
    if _pool is None:
        await init_pool()
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO trades (
                symbol, side, qty, entry, exit, pnl,
                leverage, margin_usd, notional_usd, liq_price,
                open_ts, close_ts, raw
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
            );
            """,
            rec.get("symbol"),
            rec.get("side"),
            float(rec.get("qty") or 0),
            float(rec.get("entry") or 0),
            float(rec.get("exit")) if rec.get("exit") is not None else None,
            float(rec.get("pnl") or 0),
            int(rec.get("leverage")) if rec.get("leverage") is not None else None,
            float(rec.get("margin_usd")) if rec.get("margin_usd") is not None else None,
            float(rec.get("notional_usd")) if rec.get("notional_usd") is not None else None,
            float(rec.get("liq_price")) if rec.get("liq_price") is not None else None,
            int(rec.get("open_ts")) if rec.get("open_ts") is not None else None,
            int(rec.get("close_ts")) if rec.get("close_ts") is not None else None,
            json.dumps(rec),
        )


def _fmt_ts_ms(ms: int | None) -> str | None:
    if not ms:
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.strftime("%d-%m-%Y %H:%M:%S")
    except Exception:
        return None


async def fetch_recent(limit: int = 50) -> List[dict[str, Any]]:
    if not settings.DATABASE_URL:
        return []
    global _pool
    if _pool is None:
        await init_pool()
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT symbol, side, qty, entry, exit, pnl,
                   leverage, margin_usd, notional_usd, liq_price,
                   open_ts, close_ts, created_at
            FROM trades
            ORDER BY id DESC
            LIMIT $1;
            """,
            int(limit),
        )
    out: List[dict[str, Any]] = []
    for r in rows:
        created_at_str = r["created_at"].strftime("%d-%m-%Y %H:%M:%S") if r["created_at"] else None
        out.append({
            "symbol": r["symbol"],
            "side": r["side"],
            "qty": float(r["qty"]) if r["qty"] is not None else None,
            "entry": float(r["entry"]) if r["entry"] is not None else None,
            "exit": float(r["exit"]) if r["exit"] is not None else None,
            "pnl": float(r["pnl"]) if r["pnl"] is not None else None,
            "leverage": int(r["leverage"]) if r["leverage"] is not None else None,
            "margin_usd": float(r["margin_usd"]) if r["margin_usd"] is not None else None,
            "notional_usd": float(r["notional_usd"]) if r["notional_usd"] is not None else None,
            "liq_price": float(r["liq_price"]) if r["liq_price"] is not None else None,
            "open_ts": int(r["open_ts"]) if r["open_ts"] is not None else None,
            "close_ts": int(r["close_ts"]) if r["close_ts"] is not None else None,
            "opened_at": _fmt_ts_ms(r["open_ts"]),
            "closed_at": _fmt_ts_ms(r["close_ts"]),
            "created_at": created_at_str,
        })
    return out


# ---------------------------------
# Signal Logs işlemleri
# ---------------------------------
async def insert_signal(row: dict):
    """Tek satır sinyal kaydı ekler."""
    if not settings.DATABASE_URL:
        return
    global _pool
    if _pool is None:
        await init_pool()

    q = """
        INSERT INTO signal_logs(
            ts_ms, symbol, last_price, ema_fast, ema_slow, rsi14,
            vwap60, vwap_dev_pct,
            atr60, tick_rate_2s, spread_bps,
            buy_pressure_2s, sell_pressure_2s, imbalance,
            vol_spike_5s, cvd_10m,
            sr_dist_pct, candle5_dir, short_vwap_band_ok,
            side
        ) VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
            $12,$13,$14,$15,$16,$17,$18,$19,$20
        );
    """
    vals = (
        row.get("ts_ms"), row.get("symbol"),
        row.get("last_price"), row.get("ema_fast"), row.get("ema_slow"), row.get("rsi14"),
        row.get("vwap60"), row.get("vwap_dev_pct"),
        row.get("atr60"), row.get("tick_rate_2s"), row.get("spread_bps"),
        row.get("buy_pressure_2s"), row.get("sell_pressure_2s"), row.get("imbalance"),
        row.get("vol_spike_5s"), row.get("cvd_10m"),
        row.get("sr_dist_pct"), row.get("candle5_dir"), row.get("short_vwap_band_ok"),
        row.get("side"),
    )
    async with _pool.acquire() as conn:
        await conn.execute(q, *vals)


async def fetch_signals(symbol: Optional[str] = None, hours: int = 48, limit: int = 5000) -> List[dict[str, Any]]:
    """Belirtilen saat kadar geriye dönük sinyalleri döner."""
    if not settings.DATABASE_URL:
        return []
    global _pool
    if _pool is None:
        await init_pool()

    cutoff = int(time() * 1000) - hours * 3600 * 1000
    if symbol:
        q = """
            SELECT * FROM signal_logs
            WHERE symbol = $1 AND ts_ms >= $2
            ORDER BY ts_ms DESC
            LIMIT $3;
        """
        args = (symbol.upper(), cutoff, limit)
    else:
        q = """
            SELECT * FROM signal_logs
            WHERE ts_ms >= $1
            ORDER BY ts_ms DESC
            LIMIT $2;
        """
        args = (cutoff, limit)

    async with _pool.acquire() as conn:
        rows = await conn.fetch(q, *args)
    return [dict(r) for r in rows]


async def purge_signals_older_than(days: int = 2) -> int:
    """X günden eski sinyalleri siler."""
    if not settings.DATABASE_URL:
        return 0
    global _pool
    if _pool is None:
        await init_pool()

    cutoff = int(time() * 1000) - days * 24 * 3600 * 1000
    async with _pool.acquire() as conn:
        res = await conn.execute("DELETE FROM signal_logs WHERE ts_ms < $1;", cutoff)
    try:
        return int(res.split()[-1])
    except Exception:
        return 0
