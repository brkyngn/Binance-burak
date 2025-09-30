from pydantic import BaseSettings
import os

def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return default

def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default


class Settings:
    # Genel
    PORT: int = _get_int("PORT", 8080)
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")   # <<< GEREKLİ

    # Semboller ve akışlar
    SYMBOLS: list[str] = [s.strip() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
    STREAM: str = os.getenv("STREAM", "aggTrade")
    WS_URL: str = os.getenv("WS_URL", "wss://stream.binance.com:9443/stream")

    # Depth/bookTicker
    ENABLE_DEPTH: bool = os.getenv("ENABLE_DEPTH", "true").lower() in ("1", "true", "yes")
    DEPTH_STREAM: str = os.getenv("DEPTH_STREAM", "bookTicker")  # 'bookTicker' önerilir

    # Opsiyonel n8n webhook
    N8N_WEBHOOK_URL: str | None = os.getenv("N8N_WEBHOOK_URL")

    # Reconnect backoff
    BACKOFF_BASE: float = _get_float("BACKOFF_BASE", 1.0)
    BACKOFF_MAX: float = _get_float("BACKOFF_MAX", 30.0)

    # Pencere & eşikler
    VWAP_WINDOW_SEC: int = _get_int("VWAP_WINDOW_SEC", 60)
    ATR_WINDOW_SEC: int = _get_int("ATR_WINDOW_SEC", 60)
    MIN_TICKS_PER_SEC: float = _get_float("MIN_TICKS_PER_SEC", 2.0)

    ATR_MIN: float = _get_float("ATR_MIN", 0.0008)   # %0.08
    ATR_MAX: float = _get_float("ATR_MAX", 0.0040)   # %0.40

    BUY_PRESSURE_MIN: float = _get_float("BUY_PRESSURE_MIN", 0.55)  # >= %55
    IMB_THRESHOLD: float = _get_float("IMB_THRESHOLD", 1.25)        # bid/ask vol oranı
    MAX_SPREAD_BPS: float = _get_float("MAX_SPREAD_BPS", 2.0)       # 2 bps = %0.02

    # Risk / otomatik işlem
    AUTO_TP_PCT: float = _get_float("AUTO_TP_PCT", 0.003)   # +%0.30
    AUTO_SL_PCT: float = _get_float("AUTO_SL_PCT", 0.003)   # -%0.30
    SIGNAL_COOLDOWN_MS: int = _get_int("SIGNAL_COOLDOWN_MS", 2000)
    MAX_POSITIONS: int = _get_int("MAX_POSITIONS", 3)

    # PostgreSQL
    DATABASE_URL: str | None = os.getenv("DATABASE_URL")

    LEVERAGE: int = int(os.getenv("LEVERAGE", "10"))              # 10x
    MARGIN_PER_TRADE: float = float(os.getenv("MARGIN_PER_TRADE", "10"))  # 10$ marj
    MAINT_MARGIN_RATE: float = float(os.getenv("MAINT_MARGIN_RATE", "0.004"))  # 0.4% (yaklaşık)
    FEE_RATE: float = float(os.getenv("FEE_RATE", "0.0004"))      # 4 bps varsayılan


settings = Settings()
