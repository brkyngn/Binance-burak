# app/db.py
import json
from typing import Any, List
from datetime import datetime, timezone

import asyncpg
from .config import settings

_pool: asyncpg.pool.Pool | None = None

# ---- Ana tablo şeması (CREATE IF NOT EXISTS) ----
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

# ---- Sonradan eklenen kolonları idempotent şekilde ekle ----
MIGRATIONS = [
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS leverage INT;",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS margin_usd DOUBLE PRECISION;",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS notional_usd DOUBLE PRECISION;",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS liq_price DOUBLE PRECISION;",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS open_ts BIGINT;",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS close_ts BIGINT;",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS raw JSONB;",
]

async def init_pool():
    """DB pool + DDL + idempotent migration."""
    global _pool
    if not settings.DATABASE_URL:
        return
    _pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
        # Tablo yoksa oluştur
        await conn.execute(DDL)
        # Eksik kolonları ekle
        for sql in MIGRATIONS:
            try:
                await conn.execute(sql)
            except Exception:
                # eşzamanlı deploy vs. durumlarda safe-ignore
                pass

async def ping() -> bool:
    """Basit bağlantı testi."""
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

async def insert_trade(rec: dict):
    """
    rec: PaperBroker.close sonrası gelen snapshot
    Beklenen alanlar: symbol, side, qty, entry, exit, pnl, leverage, margin_usd,
                      notional_usd, liq_price, open_ts, close_ts
    """
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

def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None

async def fetch_recent(limit: int = 50) -> List[dict[str, Any]]:
    """Son işlemler (JSON-serializable)."""
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
        created_at_iso = r["created_at"].isoformat() if r["created_at"] else None
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
            "opened_at": _ms_to_iso(r["open_ts"]),
            "closed_at": _ms_to_iso(r["close_ts"]),
            "created_at": created_at_iso,
        })
    return out
