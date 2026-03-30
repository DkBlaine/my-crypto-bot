"""
ALADDIN BOT v2 — Technical Indicators
Multi-timeframe indicator sets: daily, 4H, 15m.
"""
import pandas as pd
import numpy as np


def ema(series, period):
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    """Relative Strength Index."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(df, period=14):
    """Average True Range."""
    high = df["high"]
    low = df["low"]
    close = df["close"].shift(1)
    tr1 = high - low
    tr2 = (high - close).abs()
    tr3 = (low - close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(span=period, adjust=False).mean()


def volume_spike(df, period=20, multiplier=1.5):
    """Check if current volume is a spike."""
    avg_vol = df["volume"].rolling(window=period).mean()
    return df["volume"] > (avg_vol * multiplier)


# ── Entry TF (15m) — same as Phase 1 ────────────────────────────
def add_entry_indicators(df):
    """Add all indicators for the 15m entry timeframe."""
    from config import (
        EMA_PERIOD, RSI_PERIOD, ATR_PERIOD,
        VOLUME_AVG_PERIOD, VOLUME_SPIKE_MULT
    )
    df = df.copy()
    df["ema200"] = ema(df["close"], EMA_PERIOD)
    df["rsi"] = rsi(df["close"], RSI_PERIOD)
    df["atr"] = atr(df, ATR_PERIOD)
    df["vol_spike"] = volume_spike(df, VOLUME_AVG_PERIOD, VOLUME_SPIKE_MULT)
    df["vol_avg"] = df["volume"].rolling(window=VOLUME_AVG_PERIOD).mean()
    df["above_ema"] = df["close"] > df["ema200"]
    df["below_ema"] = df["close"] < df["ema200"]
    df["rsi_prev"] = df["rsi"].shift(1)
    df["rsi_exit_oversold"] = (df["rsi"] > 30) & (df["rsi_prev"] <= 30)
    df["rsi_exit_overbought"] = (df["rsi"] < 70) & (df["rsi_prev"] >= 70)
    return df


# ── Trend TF (Daily) ────────────────────────────────────────────
def add_trend_indicators(df):
    """
    Daily timeframe indicators for macro trend.
    Returns bias: "BULLISH", "BEARISH", or "NEUTRAL"
    """
    from config import TREND_EMA_PERIOD, TREND_RSI_PERIOD
    df = df.copy()
    df["ema_trend"] = ema(df["close"], TREND_EMA_PERIOD)
    df["rsi_trend"] = rsi(df["close"], TREND_RSI_PERIOD)

    # Trend slope: EMA direction over last 5 candles
    df["ema_slope"] = df["ema_trend"] - df["ema_trend"].shift(5)

    return df


def get_trend_bias(df):
    """
    Determine daily trend bias from indicators.
    
    BULLISH: price > EMA50, EMA rising, RSI > 45
    BEARISH: price < EMA50, EMA falling, RSI < 55
    NEUTRAL: mixed signals
    """
    if len(df) < 10:
        return "NEUTRAL", {}

    curr = df.iloc[-1]
    details = {
        "close": curr["close"],
        "ema": round(curr["ema_trend"], 2),
        "rsi": round(curr["rsi_trend"], 1),
        "ema_slope": round(curr["ema_slope"], 4),
        "above_ema": curr["close"] > curr["ema_trend"],
    }

    above = curr["close"] > curr["ema_trend"]
    rising = curr["ema_slope"] > 0
    rsi_val = curr["rsi_trend"]

    if above and rising and rsi_val > 45:
        return "BULLISH", details
    elif not above and not rising and rsi_val < 55:
        return "BEARISH", details
    else:
        return "NEUTRAL", details


# ── Structure TF (4H) ───────────────────────────────────────────
def add_structure_indicators(df):
    """
    4H timeframe indicators for intermediate structure.
    EMA20/50 crossover system + RSI.
    """
    from config import STRUCTURE_EMA_FAST, STRUCTURE_EMA_SLOW, STRUCTURE_RSI_PERIOD
    df = df.copy()
    df["ema_fast"] = ema(df["close"], STRUCTURE_EMA_FAST)
    df["ema_slow"] = ema(df["close"], STRUCTURE_EMA_SLOW)
    df["rsi_struct"] = rsi(df["close"], STRUCTURE_RSI_PERIOD)

    # Structure signals
    df["ema_bullish"] = df["ema_fast"] > df["ema_slow"]  # fast above slow
    df["ema_bearish"] = df["ema_fast"] < df["ema_slow"]

    # Higher lows / lower highs detection (simple swing)
    df["swing_high"] = (
        (df["high"] > df["high"].shift(1)) &
        (df["high"] > df["high"].shift(-1) if len(df) > 1 else True)
    )
    df["swing_low"] = (
        (df["low"] < df["low"].shift(1)) &
        (df["low"] < df["low"].shift(-1) if len(df) > 1 else True)
    )

    return df


def get_structure_bias(df):
    """
    Determine 4H structure bias.
    
    BULLISH: EMA20 > EMA50, price > EMA20, RSI 40-70
    BEARISH: EMA20 < EMA50, price < EMA20, RSI 30-60
    NEUTRAL: mixed
    """
    if len(df) < 10:
        return "NEUTRAL", {}

    curr = df.iloc[-1]
    details = {
        "close": curr["close"],
        "ema_fast": round(curr["ema_fast"], 2),
        "ema_slow": round(curr["ema_slow"], 2),
        "rsi": round(curr["rsi_struct"], 1),
        "ema_bullish": bool(curr["ema_bullish"]),
    }

    ema_bull = curr["ema_bullish"]
    above_fast = curr["close"] > curr["ema_fast"]
    rsi_val = curr["rsi_struct"]

    if ema_bull and above_fast and 40 < rsi_val < 75:
        return "BULLISH", details
    elif not ema_bull and not above_fast and 25 < rsi_val < 60:
        return "BEARISH", details
    else:
        return "NEUTRAL", details


# ── Legacy compatibility ─────────────────────────────────────────
def add_all_indicators(df, config=None):
    """Backward compatible — adds entry TF indicators."""
    return add_entry_indicators(df)
