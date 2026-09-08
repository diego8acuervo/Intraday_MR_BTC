from decimal import Decimal
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (one level above qsCrypto package)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# ── Binance environments ──────────────────────────────────────────
BINANCE_ENVIRONMENTS = {
    "spot": {
        "streaming": {
            "production": "wss://stream.binance.com:9443",
            "testnet": "wss://testnet.binance.vision",
        },
        "api": {
            "production": "https://api.binance.com",
            "testnet": "https://testnet.binance.vision",
        },
    },
    "usdm_futures": {
        "streaming": {
            "production": "wss://fstream.binance.com",
            "testnet": "wss://stream.binancefuture.com",
        },
        "api": {
            "production": "https://fapi.binance.com",
            "testnet": "https://testnet.binancefuture.com",
        },
    },
    "coinm_futures": {
        "streaming": {
            "production": "wss://dstream.binance.com",
            "testnet": "wss://dstream.binancefuture.com",
        },
        "api": {
            "production": "https://dapi.binance.com",
            "testnet": "https://testnet.binancefuture.com",
        },
    },
}

# ── Mode / market selection ───────────────────────────────────────
TEST_MODE = os.environ.get("TEST_MODE", "true").lower() in ("true", "1", "yes")
BINANCE_MARKET_TYPE = os.environ.get("BINANCE_MARKET_TYPE", "usdm_futures")

# ── Data / output directories ────────────────────────────────────
CSV_DATA_DIR = os.environ.get("QSCRYPTO_CSV_DATA_DIR", None)
OUTPUT_RESULTS_DIR = os.environ.get("QSCRYPTO_OUTPUT_RESULTS_DIR", None)

# ── Account settings ─────────────────────────────────────────────
BASE_CURRENCY = "USDT"
EQUITY = Decimal("100000.00")

# ── CoinAPI settings ─────────────────────────────────────────────
COINAPI_KEY = os.environ.get("COINAPI_KEY", "")
COINAPI_REST_URL = os.environ.get("COINAPI_REST_URL", "https://rest.coinapi.io/v1")
COINAPI_WS_URL = os.environ.get("COINAPI_WS_URL", "wss://ws.coinapi.io/v1/")
