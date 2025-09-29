import os

class Settings:
    PORT: int = int(os.getenv("PORT", 8080))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    SYMBOLS: list[str] = [s.strip() for s in os.getenv("SYMBOLS", "BTCUSDT").split(",") if s.strip()]
    STREAM: str = os.getenv("STREAM", "aggTrade")
    WS_URL: str = os.getenv("WS_URL", "wss://stream.binance.com:9443/stream")
    N8N_WEBHOOK_URL: str | None = os.getenv("N8N_WEBHOOK_URL")
    BACKOFF_BASE: float = float(os.getenv("BACKOFF_BASE", 1.0))
    BACKOFF_MAX: float = float(os.getenv("BACKOFF_MAX", 30.0))

settings = Settings()
