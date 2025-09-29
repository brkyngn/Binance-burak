import asyncpg
from datetime import datetime 
from typing import Any
from .config import settings
from .logger import logger

_pool: asyncpg.Pool | None = None

DDL = """
CREATE TABLE IF NOT EXISTS trades (
  id BIGSERIAL PRIMARY KEY,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  qty DOUBLE PRECISION NOT NULL,
  entry DOUBLE PRECISION NOT NULL,
  exit DOUBLE PRECISION,
  pnl  DOUBLE PRECISION,
  open_ts  BIGINT NOT NULL,
  close_ts BIGINT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

async def init_pool():
    global _pool
    if _pool:
        return _pool
    logger.info("Connecting PostgreSQL...")
    _pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
        await conn.execute(DDL)
    logger.info("PostgreSQL ready.")
    return _pool

async def insert_trade(row: dict[str, Any]):
    if not _pool:
        await init_pool()
    assert _pool
    sql = """INSERT INTO trades (symbol, side, qty, entry, exit, pnl, open_ts, close_ts)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8)"""
    async with _pool.acquire() as conn:
        await conn.execute(sql,
                           row.get("symbol"),
                           row.get("side"),
                           row.get("qty"),
                           row.get("entry"),
                           row.get("exit"),
                           row.get("pnl"),
                           row.get("open_ts"),
                           row.get("close_ts"))

async def fetch_recent(limit: int = 50) -> list[dict]:
    if not _pool:
        await init_pool()
    assert _pool
    sql = """SELECT id, symbol, side, qty, entry, exit, pnl, open_ts, close_ts, created_at
             FROM trades ORDER BY id DESC LIMIT $1"""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(sql, limit)
    out = []
    for r in rows:
        rec = dict(r)
        # datetime -> ISO8601 string
        if isinstance(rec.get("created_at"), datetime):
            rec["created_at"] = rec["created_at"].isoformat()
        out.append(rec)
    return out
