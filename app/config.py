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
    PORT: int = _get_int("PORT", 8080)
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Semboller ve akışlar
    SYMBOLS: list[str] = [s.strip() for s in os.getenv("SYMBOLS", "BTCUSDT").split(",") if s.strip()]
    STREAM: str = os.getenv("STREAM", "aggTrade")  # ana akış
    WS_URL: str = os.getenv("WS_URL", "wss://stream.binance.com:9443/stream")

    # Depth akışı (opsiyonel)
    ENABLE_DEPTH: bool = os.getenv("ENABLE_DEPTH", "true").lower() in ("1","true","yes")
    DEPTH_STREAM: str = os.getenv("DEPTH_STREAM", "depth@100ms")  # veya depth5@100ms

    # n8n webhook (opsiyonel)
    N8N_WEBHOOK_URL: str | None = os.getenv("N8N_WEBHOOK_URL")

    # Reconnect backoff
    BACKOFF_BASE: float = _get_float("BACKOFF_BASE", 1.0)
    BACKOFF_MAX: float = _get_float("BACKOFF_MAX", 30.0)

    # Pencere & eşikler
    VWAP_WINDOW_SEC: int = _get_int("VWAP_WINDOW_SEC", 60)
    ATR_WINDOW_SEC: int = _get_int("ATR_WINDOW_SEC", 60)
    MIN_TICKS_PER_SEC: float = _get_float("MIN_TICKS_PER_SEC", 2.0)  # son 2 sn ortalaması

    # Volatilite bandı (ATR)
    ATR_MIN: float = _get_float("ATR_MIN", 0.0008)   # %0.08
    ATR_MAX: float = _get_float("ATR_MAX", 0.0040)   # %0.40

    # Orderflow
    BUY_PRESSURE_MIN: float = _get_float("BUY_PRESSURE_MIN", 0.55)  # son 2 sn buy oranı
    IMB_THRESHOLD: float = _get_float("IMB_THRESHOLD", 1.25)        # bid/ask hacim oranı

    # Spread (bps = 1/10000)
    MAX_SPREAD_BPS: float = _get_float("MAX_SPREAD_BPS", 2.0)  # 2 bps = %0.02

    # Risk / otomatik işlem
    AUTO_TP_PCT: float = _get_float("AUTO_TP_PCT", 0.003)   # +%0.30
    AUTO_SL_PCT: float = _get_float("AUTO_SL_PCT", 0.003)   # -%0.30
    SIGNAL_COOLDOWN_MS: int = _get_int("SIGNAL_COOLDOWN_MS", 2000)
    MAX_POSITIONS: int = _get_int("MAX_POSITIONS", 3)

    import os

class Settings:
    SYMBOLS = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
    STREAM = os.getenv("STREAM", "aggTrade")
    WS_URL = os.getenv("WS_URL", "wss://stream.binance.com:9443/stream")
    ENABLE_DEPTH = os.getenv("ENABLE_DEPTH", "false").lower() == "true"
    DEPTH_STREAM = os.getenv("DEPTH_STREAM", "bookTicker")

    N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")
    MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "3"))

    VWAP_WINDOW_SEC = int(os.getenv("VWAP_WINDOW_SEC", "60"))
    ATR_WINDOW_SEC = int(os.getenv("ATR_WINDOW_SEC", "60"))
    ATR_MIN = float(os.getenv("ATR_MIN", "0.0"))
    ATR_MAX = float(os.getenv("ATR_MAX", "1000000.0"))
    MIN_TICKS_PER_SEC = float(os.getenv("MIN_TICKS_PER_SEC", "0.5"))
    BUY_PRESSURE_MIN = float(os.getenv("BUY_PRESSURE_MIN", "0.55"))
    IMB_THRESHOLD = float(os.getenv("IMB_THRESHOLD", "1.2"))
    MAX_SPREAD_BPS = float(os.getenv("MAX_SPREAD_BPS", "5.0"))
    SIGNAL_COOLDOWN_MS = int(os.getenv("SIGNAL_COOLDOWN_MS", "2000"))

    AUTO_TP_PCT = float(os.getenv("AUTO_TP_PCT", "0.005"))
    AUTO_SL_PCT = float(os.getenv("AUTO_SL_PCT", "0.005"))

    BACKOFF_BASE = float(os.getenv("BACKOFF_BASE", "2.0"))

    DATABASE_URL = os.getenv("DATABASE_URL")   # <<< EKLENDİ

settings = Settings()
