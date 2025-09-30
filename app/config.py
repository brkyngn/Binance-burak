# app/config.py
from typing import Optional, List
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_symbols_str(s: str) -> List[str]:
    """
    Env'den gelen SYMBOLS değerini listeye çevirir.
    Kabul ettiği formatlar:
      - CSV: "BTCUSDT,ETHUSDT"
      - JSON benzeri: ["BTCUSDT","ETHUSDT"]   (tek tırnaklı da gelse çalıştırmaya çalışıyoruz)
    """
    if not s:
        return []
    s = s.strip()

    # JSON dizi gibi görünüyorsa önce kenar köşeleri topla
    if s.startswith("[") and s.endswith("]"):
        # çift tırnak yoksa düzelt
        s_clean = s.replace("'", '"')
        try:
            import json
            arr = json.loads(s_clean)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass  # aşağıda CSV'ye düşelim

    # CSV split
    return [part.strip() for part in s.split(",") if part.strip()]


class Settings(BaseSettings):
    # Pydantic v2 settings config
    model_config = SettingsConfigDict(case_sensitive=False)

    # --- Binance WS ---
    WS_URL: str = os.getenv("WS_URL", "wss://stream.binance.com:9443/stream")
    STREAM: str = os.getenv("STREAM", "aggTrade")
    ENABLE_DEPTH: bool = os.getenv("ENABLE_DEPTH", "true").lower() == "true"
    DEPTH_STREAM: str = os.getenv("DEPTH_STREAM", "bookTicker")

    # SYMBOLS'ü doğrudan List[str] olarak tanımlamıyoruz.
    # Env'den STRING alıyoruz, sonra property ile listeye çeviriyoruz.
    SYMBOLS_RAW: str = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT")

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
    MAX_SPREAD_BPS: float = float(os.getenv("MAX_SPREAD_BPS", "5"))
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

    # ---- Türetilmiş alanlar ----
    @property
    def SYMBOLS(self) -> List[str]:
        return _parse_symbols_str(self.SYMBOLS_RAW)


settings = Settings()
