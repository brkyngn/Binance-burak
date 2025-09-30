# app/config.py
from typing import List, Optional
import os

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Pydantic Settings config
    model_config = SettingsConfigDict(case_sensitive=False)

    # --- Binance WS ---
    WS_URL: str = os.getenv("WS_URL", "wss://stream.binance.com:9443/stream")
    STREAM: str = os.getenv("STREAM", "aggTrade")
    ENABLE_DEPTH: bool = os.getenv("ENABLE_DEPTH", "true").lower() == "true"
    DEPTH_STREAM: str = os.getenv("DEPTH_STREAM", "bookTicker")

    # Symbols (CSV veya JSON array kabul eder)
    SYMBOLS: List[str] = ["BTCUSDT", "ETHUSDT"]

    @field_validator("SYMBOLS", mode="before")
    @classmethod
    def _parse_symbols(cls, v):
        # JSON array gelirse (list) direkt döndür
        if isinstance(v, list):
            return v
        # CSV string gelirse böl
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        # Hiçbiri değilse olduğu gibi bırak (pydantic handle eder)
        return v

    # Reconnect/backoff
    BACKOFF_BASE: float = float(os.getenv("BACKOFF_BASE", "2.0"))

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # N8n webhook (opsiyonel)
    N8N_WEBHOOK_URL: Optional[str] = os.getenv("N8N_WEBHOOK_URL")

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
    DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")


settings = Settings()
