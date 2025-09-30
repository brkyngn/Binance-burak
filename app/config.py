# app/config.py
from typing import Optional, List
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
import json

def _parse_symbols_str(s: str) -> List[str]:
    if not s:
        return []
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            return [x for x in json.loads(s.replace("'", '"')) if str(x).strip()]
        except Exception:
            pass
    return [part.strip() for part in s.split(",") if part.strip()]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False)

    # --- Binance WS ---
    WS_URL: str = os.getenv("WS_URL", "wss://stream.binance.com:9443/stream")
    STREAM: str = os.getenv("STREAM", "aggTrade")
    ENABLE_DEPTH: bool = os.getenv("ENABLE_DEPTH", "true").lower() == "true"
    DEPTH_STREAM: str = os.getenv("DEPTH_STREAM", "bookTicker")
    SYMBOLS_RAW: str = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT")

    # Reconnect/backoff
    BACKOFF_BASE: float = float(os.getenv("BACKOFF_BASE", "2.0"))

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # N8n webhook (opsiyonel)
    N8N_WEBHOOK_URL: Optional[str] = os.getenv("N8N_WEBHOOK_URL")

    # Paper broker
    MAX_POSITIONS: int = int(os.getenv("MAX_POSITIONS", "10"))

    # -------------------------------------------------
    # Eski / genel filtreler
    # -------------------------------------------------
    VWAP_WINDOW_SEC: int = int(os.getenv("VWAP_WINDOW_SEC", "60"))
    ATR_WINDOW_SEC: int = int(os.getenv("ATR_WINDOW_SEC", "60"))
    MIN_TICKS_PER_SEC: float = float(os.getenv("MIN_TICKS_PER_SEC", "2.0"))
    MAX_SPREAD_BPS: float = float(os.getenv("MAX_SPREAD_BPS", "2.0"))

    # Yeni ATR bandı (senin verdiğin aralık)
    ATR_MIN: float = float(os.getenv("ATR_MIN", "0.0008"))
    ATR_MAX: float = float(os.getenv("ATR_MAX", "0.004"))

    # Orderflow temel
    BUY_PRESSURE_MIN: float = float(os.getenv("BUY_PRESSURE_MIN", "0.55"))
    IMB_THRESHOLD: float = float(os.getenv("IMB_THRESHOLD", "0.9"))  # genel
    # Long/Short’a özel imbalance
    IMB_LONG_MIN: float = float(os.getenv("IMB_LONG_MIN", "1.25"))
    IMB_SHORT_MAX: float = float(os.getenv("IMB_SHORT_MAX", "0.80"))

    # Volume spike (son 5s / 60s ortalama 5s)
    VOLUME_SPIKE_MIN: float = float(os.getenv("VOLUME_SPIKE_MIN", "1.5"))

    # VWAP sapmaları
    VWAP_DEV_MAX_LONG: float = float(os.getenv("VWAP_DEV_MAX_LONG", "0.002"))   # ≤0.20%
    SHORT_VWAP_DEV_MIN: float = float(os.getenv("SHORT_VWAP_DEV_MIN", "0.001")) # ≥0.10%
    SHORT_VWAP_DEV_MAX: float = float(os.getenv("SHORT_VWAP_DEV_MAX", "0.002")) # ≤0.20%

    # RSI kısıtları
    RSI_SHORT_MIN: float = float(os.getenv("RSI_SHORT_MIN", "65.0"))

    # S/R yakınlık yüzdesi (±0.15% = 0.0015)
    SR_NEAR_PCT: float = float(os.getenv("SR_NEAR_PCT", "0.0015"))

    # Funding filtresi (opsiyonel)
    FUNDING_MINUTES_BUFFER: int = int(os.getenv("FUNDING_MINUTES_BUFFER", "20"))
    FUNDING_NEXT_TS_MS: Optional[int] = (
        int(os.getenv("FUNDING_NEXT_TS_MS")) if os.getenv("FUNDING_NEXT_TS_MS") else None
    )

    # -------------------------------------------------
    # Yeni: mutlak $ bazlı scalping parametreleri
    # -------------------------------------------------
    AUTO_NOTIONAL_USD: float = float(os.getenv("AUTO_NOTIONAL_USD", "10000"))
    AUTO_LEVERAGE: int = int(os.getenv("AUTO_LEVERAGE", "10"))
    AUTO_MARGIN_USD: float = float(os.getenv("AUTO_MARGIN_USD", "1000"))
    AUTO_ABS_TP_USD: float = float(os.getenv("AUTO_ABS_TP_USD", "50"))
    AUTO_ABS_SL_USD: float = float(os.getenv("AUTO_ABS_SL_USD", "50"))

    # Risk & komisyon
    MAINT_MARGIN_RATE: float = float(os.getenv("MAINT_MARGIN_RATE", "0.004"))
    FEE_RATE: float = float(os.getenv("FEE_RATE", "0.0004"))

    # DB
    DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")

    @property
    def SYMBOLS(self) -> List[str]:
        return _parse_symbols_str(self.SYMBOLS_RAW)

settings = Settings()
