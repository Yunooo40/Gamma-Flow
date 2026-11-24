"""Central configuration for Open-GEX.

Every tunable parameter lives here. Secrets and environment-specific values
are read from a ``.env`` file (see ``.env.example``).
"""

from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR: Path = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --- Universe -----------------------------------------------------------------
TICKERS: tuple[str, ...] = ("SPY", "QQQ", "IWM")
EXPIRY_FILTERS: tuple[str, ...] = ("0dte", "all")
MAX_DTE_ALL: int = 45  # the "all" filter covers expiries <= 45 calendar days

# --- Cache --------------------------------------------------------------------
CACHE_TTL_SECONDS: int = 900
CACHE_DIR: str = ".cache"  # relative paths are resolved against BASE_DIR

# --- Strike ranges ------------------------------------------------------------
COMPUTE_RANGE_PCT: float = 0.30  # strikes kept for calculations: spot +/- 30 %
DISPLAY_RANGE_PCT: dict[str, float] = {"0dte": 0.03, "all": 0.10}
ZERO_GAMMA_SEARCH_PCT: float = 0.10
ZERO_GAMMA_GRID_POINTS: int = 201
ZERO_GAMMA_CHUNK_SIZE: int = 5000  # contracts per block in the profile matrix

# --- Pricing model ------------------------------------------------------------
RISK_FREE_RATE: float = float(os.getenv("RISK_FREE_RATE", "0.04"))
# Approximate trailing dividend yields: update periodically.
DIVIDEND_YIELDS: dict[str, float] = {"SPY": 0.012, "QQQ": 0.006, "IWM": 0.012}
IV_MIN: float = 0.01
IV_MAX: float = 3.0
MIN_T_MINUTES: int = 15
DAYS_PER_YEAR: int = 365
CONTRACT_MULTIPLIER: int = 100
SPOT_MOVE_PCT: float = 0.01  # GEX is expressed per 1 % move of the underlying
VOL_POINT: float = 0.01  # VEX is expressed per 1 volatility point

# --- Market calendar ----------------------------------------------------------
MARKET_TZ: ZoneInfo = ZoneInfo("America/New_York")
MARKET_CLOSE: time = time(16, 0)

# --- Data sources -------------------------------------------------------------
MASSIVE_API_KEY: str = os.getenv("MASSIVE_API_KEY", "").strip()
MASSIVE_BASE_URL: str = os.getenv("MASSIVE_BASE_URL", "https://api.massive.com").rstrip("/")
MASSIVE_PAGE_LIMIT: int = 250
MASSIVE_0DTE_LOOKAHEAD_DAYS: int = 7
MASSIVE_MAX_PAGES: int = 400  # guard against a pagination loop
HTTP_TIMEOUT: int = 10

# --- Web server ---------------------------------------------------------------
FLASK_HOST: str = "127.0.0.1"
FLASK_PORT: int = 5000
FLASK_DEBUG: bool = os.getenv("FLASK_DEBUG", "0") == "1"
AUTO_REFRESH_SECONDS: int = 300
