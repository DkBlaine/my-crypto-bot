"""
ALADDIN BOT v2 — Configuration
Phase 2: Multi-Timeframe + Sentiment
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── API ──────────────────────────────────────────────────────────
API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
MODE = os.getenv("MODE", "testnet")  # "testnet" or "mainnet"
TESTNET = MODE == "testnet"

# ── Trading Pairs ────────────────────────────────────────────────
PAIRS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "DOTUSDT",
]

# ── Multi-Timeframe ─────────────────────────────────────────────
# Top-down: Daily trend → 4H structure → 15m entry
# Bot only enters when ALL three timeframes align
MTF_TIMEFRAMES = {
    "trend":     "D",     # Daily — overall direction
    "structure": "240",   # 4H   — intermediate structure
    "entry":     "15",    # 15m  — precise entry
}
CANDLE_LIMIT_TREND = 100       # Daily candles
CANDLE_LIMIT_STRUCTURE = 200   # 4H candles
CANDLE_LIMIT_ENTRY = 250       # 15m candles

# ── Strategy: Daily Trend ────────────────────────────────────────
TREND_EMA_PERIOD = 50          # EMA50 on daily = macro trend
TREND_RSI_PERIOD = 14

# ── Strategy: 4H Structure ──────────────────────────────────────
STRUCTURE_EMA_FAST = 20        # EMA20 on 4H
STRUCTURE_EMA_SLOW = 50        # EMA50 on 4H
STRUCTURE_RSI_PERIOD = 14

# ── Strategy: 15m Entry (same as Phase 1) ────────────────────────
EMA_PERIOD = 200
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
VOLUME_SPIKE_MULT = 1.5
VOLUME_AVG_PERIOD = 20
ATR_PERIOD = 14
ATR_SL_MULT = 2.0
RR_RATIO = 3.0

# ── Sentiment (Bybit free data) ─────────────────────────────────
USE_SENTIMENT = True
FUNDING_RATE_EXTREME = 0.01    # > 1% = overheated longs, avoid longs
FUNDING_RATE_NEGATIVE = -0.005 # < -0.5% = overheated shorts, avoid shorts
OI_CHANGE_THRESHOLD = 5.0     # OI change > 5% in 24h = noteworthy
OI_LOOKBACK_HOURS = 24

# ── Risk Management ──────────────────────────────────────────────
MAX_POSITION_PCT = 30
MAX_LEVERAGE = 10
DEFAULT_LEVERAGE = 5
MAX_DAILY_LOSS_PCT = 2
MAX_CONSECUTIVE_LOSSES = 3
COOLDOWN_AFTER_STOP = 24
MAX_OPEN_POSITIONS = 3

# ── Orderbook ────────────────────────────────────────────────────
ORDERBOOK_DEPTH = 25
WALL_THRESHOLD_MULT = 5.0

# ── Logging ──────────────────────────────────────────────────────
LOG_FILE = "logs/trades.log"
LOG_LEVEL = "INFO"

# ── Orderflow (Binance Futures WebSocket) ───────────────────────
USE_ORDERFLOW = True          # Set True to enable (needs websockets pkg)
ORDERFLOW_SYMBOLS = PAIRS      # Same pairs as trading
