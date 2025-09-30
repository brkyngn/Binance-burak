# app/config.py
from typing import Optional, List
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
import json


# -----------------------------------------------------
# Yardımcı: SYMBOLS stringini CSV veya JSON'dan listeye çevir
# -----------------------------------------------------
def _parse_symbols_str(s: str) -> List[str]:
    if not s:
        return []
    s = s.strip()
    # JSON array ise (tek tırnakları düzelt)
    if s.startswith("[") and s.endswith("]"):
        try:
            return [x for x in json.loads(s.replace("'", '"')) if str(x).strip()]
        except Exception:
            pass
    # CSV fallback
    return [part.strip() for part in s.split(",") if part.strip()]


# -----------------------------------------------------
# Ana Settings
# -----------------------------------------------------
class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False)

    # --- Binance WS ---
    WS_URL: str = os.getenv("WS_URL", "wss://stream.binance.com:9443/stream")
    STREAM: str = os.getenv("STREAM", "aggTrade")
    ENABLE_DEPTH: bool = os.getenv("ENABLE_DEPTH", "true").lower() == "true"
    DEPTH_STREAM: str = os.getenv("DEPTH_STREAM", "bookTicker")

    # Sadece STRING okuyup kendimiz parse edeceğiz
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
    # Scalping / sinyal filtreleri
    # -------------------------------------------------
    VWAP_WINDOW_SEC: int = int(os.getenv("VWAP_WINDOW_SEC", "60"))
    ATR_WINDOW_SEC: int = int(os.getenv("ATR_WINDOW_SEC", "60"))
    MIN_TICKS_PER_SEC: float = float(os.getenv("MIN_TICKS_PER_SEC", "1.0"))
    MAX_SPREAD_BPS: float = float(os.getenv("MAX_SPREAD_BPS", "5"))
    ATR_MIN: float = float(os.getenv("ATR_MIN", "0.0002"))
    ATR_MAX: float = float(os.getenv("ATR_MAX", "0.05"))
    BUY_PRESSURE_MIN: float = float(os.getenv("BUY_PRESSURE_MIN", "0.55"))
    IMB_THRESHOLD: float = float(os.getenv("IMB_THRESHOLD", "0.9"))

    # -------------------------------------------------
    # Eski yüzde bazlı TP/SL (artık kullanılmayacak ama kalsın)
    # -------------------------------------------------
    AUTO_TP_PCT: float = float(os.getenv("AUTO_TP_PCT", "0.03"))
    AUTO_SL_PCT: float = float(os.getenv("AUTO_SL_PCT", "0.03"))

    # -------------------------------------------------
    # Yeni: mutlak $ bazlı scalping parametreleri
    # -------------------------------------------------
    AUTO_NOTIONAL_USD: float = float(os.getenv("AUTO_NOTIONAL_USD", "10000"))      # pozisyon büyüklüğü
    AUTO_LEVERAGE: int = int(os.getenv("AUTO_LEVERAGE", "10"))                     # kaldıraç
    AUTO_MARGIN_USD: float = float(os.getenv("AUTO_MARGIN_USD", "1000"))           # marjin
    AUTO_ABS_TP_USD: float = float(os.getenv("AUTO_ABS_TP_USD", "50"))             # +$50 kârda kapat
    AUTO_ABS_SL_USD: float = float(os.getenv("AUTO_ABS_SL_USD", "50"))             # -$50 zararda kapat

    # -------------------------------------------------
    # Risk & komisyon
    # -------------------------------------------------
    MAINT_MARGIN_RATE: float = float(os.getenv("MAINT_MARGIN_RATE", "0.004"))
    FEE_RATE: float = float(os.getenv("FEE_RATE", "0.0004"))

    # -------------------------------------------------
    # DB
    # -------------------------------------------------
    DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")

    # Türetilmiş propery
    @property
    def SYMBOLS(self) -> List[str]:
        return _parse_symbols_str(self.SYMBOLS_RAW)

    # --- Fees (Futures) ---
    FEE_MODE: str = os.getenv("FEE_MODE", "taker")          # "taker" | "maker"
    FEE_TAKER: float = float(os.getenv("FEE_TAKER", "0.0004"))
    FEE_MAKER: float = float(os.getenv("FEE_MAKER", "0.0002"))

    # BNB ile komisyon ödeme simülasyonu
    PAY_FEES_IN_BNB: bool = os.getenv("PAY_FEES_IN_BNB", "false").lower() == "true"
    BNB_FEE_DISCOUNT: float = float(os.getenv("BNB_FEE_DISCOUNT", "0.10"))  # %10 ind.


settings = Settings()
