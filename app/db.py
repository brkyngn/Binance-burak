# app/db.py
import json
from typing import Any, List
from datetime import datetime, timezone

import asyncpg
from .config import settings

_pool: asyncpg.pool.Pool | None = None

DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty DOUBLE PRECISION NOT NULL,
    entry DOUBLE PRECISION NOT NULL,
    exit DOUBLE PRECISION,
    pnl DOUBLE PRECISION NOT NULL,
    leverage INT,
    margin_usd DOUBLE PRECISION,
    notional_usd DOUBLE PRECISION,
    liq_price DOUBLE PRECISION,
    open_ts BIGINT,
    close_ts BIGINT,
    raw JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
"""

async def init_pool():
    global _pool
    if not settings.DATABASE_URL:
        return
    _pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
        await conn.execute(DDL)

async def insert_trade(rec: dict):
    """
    rec: PaperBroker.close sonrası gelen snapshot
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
            )
            """,
            rec.get("symbol"),
            rec.get("side"),
            float(rec.get("qty") or 0),
            float(rec.get("entry") or 0),
            float(rec.get("exit") or 0) if rec.get("exit") is not None else None,
            float(rec.get("pnl") or 0),
            int(rec.get("leverage") or 0) if rec.get("leverage") is not None else None,
            float(rec.get("margin_usd") or 0) if rec.get("margin_usd") is not None else None,
            float(rec.get("notional_usd") or 0) if rec.get("notional_usd") is not None else None,
            float(rec.get("liq_price") or 0) if rec.get("liq_price") is not None else None,
            int(rec.get("open_ts") or 0) if rec.get("open_ts") is not None else None,
            int(rec.get("close_ts") or 0) if rec.get("close_ts") is not None else None,
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
            LIMIT $1
            """,
            limit,
        )
    out = []
    for r in rows:
        out.append({
            "symbol": r["symbol"],
            "side": r["side"],
            "qty": float(r["qty"]),
            "entry": float(r["entry"]),
            "exit": float(r["exit"]) if r["exit"] is not None else None,
            "pnl": float(r["pnl"]),
            "leverage": r["leverage"],
            "margin_usd": float(r["margin_usd"]) if r["margin_usd"] is not None else None,
            "notional_usd": float(r["notional_usd"]) if r["notional_usd"] is not None else None,
            "liq_price": float(r["liq_price"]) if r["liq_price"] is not None else None,
            "open_ts": r["open_ts"],
            "close_ts": r["close_ts"],
            "opened_at": _ms_to_iso(r["open_ts"]),
            "closed_at": _ms_to_iso(r["close_ts"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return out
