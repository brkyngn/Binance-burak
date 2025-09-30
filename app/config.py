# app/config.py
from pydantic_settings import BaseSettings  # <- v2: BaseSettings buraya taşındı
from typing import List
import os

def _csv_env(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default)
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]

class Settings(BaseSettings):
    # --- Binance WS ---
    WS_URL: str = os.getenv("WS_URL", "wss://stream.binance.com:9443/stream")
    STREAM: str = os.getenv("STREAM", "aggTrade")
    ENABLE_DEPTH: bool = os.getenv("ENABLE_DEPTH", "true").lower() == "true"
    DEPTH_STREAM: str = os.getenv("DEPTH_STREAM", "bookTicker")

    # Symbols (CSV)
    SYMBOLS: List[str] = _csv_env("SYMBOLS", "BTCUSDT,ETHUSDT")

    # Reconnect/backoff
    BACKOFF_BASE: float = float(os.getenv("BACKOFF_BASE", "2.0"))

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # N8n webhook (opsiyonel)
    N8N_WEBHOOK_URL: str | None = os.getenv("N8N_WEBHOOK_URL")

    # Paper broker
    MAX_POSITIONS: int = int(os.getenv("MAX_POSITIONS", "10"))

    # Otomatik TP/SL yüzdesi (ör. 0.03 = %3)
    AUTO_TP_PCT: float = float(os.getenv("AUTO_TP_PCT", "0.03"))
    AUTO_SL_PCT: float = float(os.getenv("AUTO_SL_PCT", "0.03"))

    # Sinyal filtreleri (scalping)
    VWAP_WINDOW_SEC: int = int(os.getenv("VWAP_WINDOW_SEC", "60"))
    ATR_WINDOW_SEC: int = int(os.getenv("ATR_WINDOW_SEC", "60"))
    MIN_TICKS_PER_SEC: float = float(os.getenv("MIN_TICKS_PER_SEC", "1.0"))
    MAX_SPREAD_BPS: float = float(os.getenv("MAX_SPREAD_BPS", "5"))  # bps
    ATR_MIN: float = float(os.getenv("ATR_MIN", "0.0002"))
    ATR_MAX: float = float(os.getenv("ATR_MAX", "0.05"))
    BUY_PRESSURE_MIN: float = float(os.getenv("BUY_PRESSURE_MIN", "0.55"))
    IMB_THRESHOLD: float = float(os.getenv("IMB_THRESHOLD", "0.9"))

    # Leverage / Margin
    LEVERAGE: int = int(os.getenv("LEVERAGE", "10"))
    MARGIN_PER_TRADE: float = float(os.getenv("MARGIN_PER_TRADE", "10"))
    MAINT_MARGIN_RATE: float = float(os.getenv("MAINT_MARGIN_RATE", "0.004"))
    FEE_RATE: float = float(os.getenv("FEE_RATE", "0.0004"))

    # DB
    DATABASE_URL: str | None = os.getenv("DATABASE_URL")

    model_config = {
        "case_sensitive": False
    }

settings = Settings()
