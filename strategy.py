"""
ALADDIN BOT — Strategy Module
Generates trading signals based on technical analysis.

LONG Signal (all must be true):
  1. Price above EMA200 (uptrend)
  2. RSI just exited oversold zone (momentum shift)
  3. Volume spike detected (smart money entering)
  4. No sell wall in orderbook above current price

SHORT Signal (all must be true):
  1. Price below EMA200 (downtrend)
  2. RSI just exited overbought zone
  3. Volume spike detected
  4. No buy wall in orderbook below current price

SL/TP:
  - SL = Entry ± (ATR * 2.0)
  - TP = Entry ± (ATR * 2.0 * 3.0) = 1:3 R:R
"""
import config
import pandas as pd
from indicators import add_all_indicators


def check_signal(df, orderbook=None):
    """
    Check latest candle for a trading signal.
    
    Returns:
        dict with signal info, or None if no signal.
        {
            "signal": "LONG" or "SHORT",
            "entry": float,
            "sl": float,
            "tp": float,
            "atr": float,
            "rsi": float,
            "reason": str,
        }
    """
    if len(df) < config.EMA_PERIOD + 5:
        return None
    
    # Add indicators
    df = add_all_indicators(df)
    
    # Get latest completed candle (not the current forming one)
    curr = df.iloc[-2]  # -1 is current (incomplete), -2 is last closed
    prev = df.iloc[-3]
    
    entry_price = curr["close"]
    current_atr = curr["atr"]
    
    if current_atr <= 0 or pd.isna(current_atr):
        return None
    
    # Calculate SL/TP distances
    sl_distance = current_atr * config.ATR_SL_MULT
    tp_distance = sl_distance * config.RR_RATIO
    
    # ── CHECK LONG ───────────────────────────────────────────
    long_conditions = {
        "above_ema": curr["above_ema"],
        "rsi_exit_oversold": curr["rsi_exit_oversold"] or (curr["rsi"] > 30 and curr["rsi"] < 50 and prev["rsi"] < 35),
        "volume_spike": curr["vol_spike"],
    }
    
    # ── CHECK SHORT ──────────────────────────────────────────
    short_conditions = {
        "below_ema": curr["below_ema"],
        "rsi_exit_overbought": curr["rsi_exit_overbought"] or (curr["rsi"] < 70 and curr["rsi"] > 50 and prev["rsi"] > 65),
        "volume_spike": curr["vol_spike"],
    }
    
    # Check orderbook walls if available
    has_wall = False
    if orderbook:
        has_wall = check_orderbook_wall(orderbook, entry_price, curr["close"])
    
    # ── GENERATE SIGNAL ──────────────────────────────────────
    if all(long_conditions.values()) and not has_wall:
        sl_price = round(entry_price - sl_distance, 8)
        tp_price = round(entry_price + tp_distance, 8)
        
        reasons = [k for k, v in long_conditions.items() if v]
        return {
            "signal": "LONG",
            "entry": entry_price,
            "sl": sl_price,
            "tp": tp_price,
            "atr": current_atr,
            "rsi": curr["rsi"],
            "volume_ratio": curr["volume"] / curr["vol_avg"] if curr["vol_avg"] > 0 else 0,
            "reason": f"LONG: {', '.join(reasons)}",
        }
    
    if all(short_conditions.values()) and not has_wall:
        sl_price = round(entry_price + sl_distance, 8)
        tp_price = round(entry_price - tp_distance, 8)
        
        reasons = [k for k, v in short_conditions.items() if v]
        return {
            "signal": "SHORT",
            "entry": entry_price,
            "sl": sl_price,
            "tp": tp_price,
            "atr": current_atr,
            "rsi": curr["rsi"],
            "volume_ratio": curr["volume"] / curr["vol_avg"] if curr["vol_avg"] > 0 else 0,
            "reason": f"SHORT: {', '.join(reasons)}",
        }
    
    return None


def check_orderbook_wall(orderbook, price, direction_price):
    """
    Check if there's a massive sell wall above (for longs)
    or buy wall below (for shorts).
    Returns True if wall detected (= don't trade).
    """
    asks = orderbook.get("asks", [])
    bids = orderbook.get("bids", [])
    
    if not asks or not bids:
        return False
    
    # Average order size
    all_sizes = [s for _, s in asks[:10]] + [s for _, s in bids[:10]]
    if not all_sizes:
        return False
    avg_size = sum(all_sizes) / len(all_sizes)
    threshold = avg_size * config.WALL_THRESHOLD_MULT
    
    # Check for walls near current price (within 0.5%)
    price_range = price * 0.005
    
    # Sell walls above (bad for longs)
    for ask_price, ask_size in asks:
        if ask_price <= price + price_range and ask_size > threshold:
            print(f"  [STRATEGY] Sell wall detected at {ask_price} (size: {ask_size:.0f}, avg: {avg_size:.0f})")
            return True
    
    # Buy walls below (bad for shorts) 
    for bid_price, bid_size in bids:
        if bid_price >= price - price_range and bid_size > threshold:
            print(f"  [STRATEGY] Buy wall detected at {bid_price} (size: {bid_size:.0f}, avg: {avg_size:.0f})")
            return True
    
    return False


def get_signal_summary(df):
    """Get a readable summary of current indicators (for debugging)."""
    df = add_all_indicators(df)
    curr = df.iloc[-2]
    
    return {
        "close": curr["close"],
        "ema200": round(curr["ema200"], 2),
        "rsi": round(curr["rsi"], 2),
        "atr": round(curr["atr"], 6),
        "above_ema": curr["above_ema"],
        "vol_spike": curr["vol_spike"],
        "vol_ratio": round(curr["volume"] / curr["vol_avg"], 2) if curr["vol_avg"] > 0 else 0,
    }
    print("test")
    
