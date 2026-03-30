"""
ALADDIN BOT v4 — Strategy Module
Split scoring: macro_score + entry_quality scored separately.

Entry requires:
  macro_score  >= 4  (out of 6)
  entry_quality >= 3  (out of 7)
  total_score  >= 8  (out of 13)
  confirm_block: at least one of rsi>=1, volume>=1, trigger>=2

Macro score (0-6):
  daily_trend     0-3
  h4_structure    0-2
  sentiment       0-1

Entry quality (0-7):
  m15_trigger     0-3  (0=nothing, 1=weak setup, 2=confirmed, 3=strong)
  rsi_context     0-2
  volume_quality  0-2

Vetoes (hard skip):
  - daily_trend = 0
  - m15_trigger = 0
  - overextended (price too far from EMA + no retest)
  - volume=0 AND rsi=0 AND trigger<2 (confirm block)
"""
import config
import pandas as pd
import numpy as np
from indicators import (
    add_entry_indicators,
    add_trend_indicators, get_trend_bias,
    add_structure_indicators, get_structure_bias,
)

# Thresholds
MACRO_MIN = 4
ENTRY_MIN = 3
TOTAL_MIN = 8

CONFIDENCE_SIZING = {"HIGH": 1.0, "MEDIUM": 0.7}


def check_mtf_signal(mtf_klines, orderbook=None, sentiment=None):
    """Score-based MTF signal. Returns (signal, decision_log)."""
    df_trend = mtf_klines.get("trend", pd.DataFrame())
    df_struct = mtf_klines.get("structure", pd.DataFrame())
    df_entry = mtf_klines.get("entry", pd.DataFrame())

    dlog = _empty_dlog()

    if df_trend.empty or len(df_trend) < config.TREND_EMA_PERIOD + 10:
        dlog["failed"].append("daily: no data")
        return None, dlog
    if df_struct.empty or len(df_struct) < config.STRUCTURE_EMA_SLOW + 10:
        dlog["failed"].append("4H: no data")
        return None, dlog
    if df_entry.empty or len(df_entry) < config.EMA_PERIOD + 10:
        dlog["failed"].append("15m: no data")
        return None, dlog

    df_trend = add_trend_indicators(df_trend)
    trend_bias, trend_details = get_trend_bias(df_trend)

    df_struct = add_structure_indicators(df_struct)
    struct_bias, struct_details = get_structure_bias(df_struct)

    df_entry = add_entry_indicators(df_entry)
    curr = df_entry.iloc[-2]
    prev = df_entry.iloc[-3]
    prev2 = df_entry.iloc[-4]
    entry_price = float(curr["close"])
    current_atr = float(curr["atr"])

    if current_atr <= 0 or pd.isna(current_atr):
        dlog["failed"].append("ATR invalid")
        return None, dlog

    # ── Score BOTH directions explicitly ──────────────────
    long_sig, long_dl = _score_direction(
        "LONG", trend_bias, trend_details, struct_bias, struct_details,
        curr, prev, prev2, df_entry, entry_price, current_atr,
        orderbook, sentiment,
    )
    short_sig, short_dl = _score_direction(
        "SHORT", trend_bias, trend_details, struct_bias, struct_details,
        curr, prev, prev2, df_entry, entry_price, current_atr,
        orderbook, sentiment,
    )

    long_total = long_dl["total_score"]
    short_total = short_dl["total_score"]
    long_valid = long_sig is not None
    short_valid = short_sig is not None

    # ── Direction selection ────────────────────────────────
    if long_valid and short_valid:
        diff = long_total - short_total
        if diff >= 2:
            chosen_sig, chosen_dl = long_sig, long_dl
            chosen_dl["direction_reason"] = f"L:{long_total} S:{short_total} gap:{diff} → LONG"
        elif diff <= -2:
            chosen_sig, chosen_dl = short_sig, short_dl
            chosen_dl["direction_reason"] = f"L:{long_total} S:{short_total} gap:{abs(diff)} → SHORT"
        else:
            dlog = long_dl if long_total >= short_total else short_dl
            dlog["decision"] = "NEUTRAL"
            dlog["direction_reason"] = f"L:{long_total} S:{short_total} gap:{abs(diff)} → NEUTRAL"
            dlog["long_score"] = long_total
            dlog["short_score"] = short_total
            return None, dlog
    elif long_valid:
        chosen_sig, chosen_dl = long_sig, long_dl
        chosen_dl["direction_reason"] = f"L:{long_total} S:{short_total} → LONG only"
    elif short_valid:
        chosen_sig, chosen_dl = short_sig, short_dl
        chosen_dl["direction_reason"] = f"L:{long_total} S:{short_total} → SHORT only"
    else:
        dlog = long_dl if long_total >= short_total else short_dl
        dlog["decision"] = "NO_TRADE"
        dlog["direction_reason"] = f"L:{long_total} S:{short_total} → NONE"
        dlog["long_score"] = long_total
        dlog["short_score"] = short_total
        return None, dlog

    chosen_dl["long_score"] = long_total
    chosen_dl["short_score"] = short_total
    return chosen_sig, chosen_dl


def _score_direction(direction, trend_bias, trend_details, struct_bias, struct_details,
                     curr, prev, prev2, df_entry, entry_price, current_atr,
                     orderbook, sentiment):
    """Score one direction. Returns (signal or None, dlog)."""
    macro_scores = {}
    entry_scores = {}
    passed = []
    failed = []
    trend_rsi = trend_details.get("rsi", 50)

    # ══════════════════════════════════════════════════════
    # MACRO SCORE (0-6)
    # ══════════════════════════════════════════════════════

    # 1. Daily trend (0-3)
    if direction == "LONG":
        if trend_bias == "BULLISH":
            macro_scores["daily"] = 3; passed.append("1D:BULL")
        elif trend_bias == "NEUTRAL":
            macro_scores["daily"] = 1; passed.append("1D:NEUTRAL")
        else:
            macro_scores["daily"] = 0; failed.append("1D:BEAR vs LONG")
        if trend_rsi > 75:
            macro_scores["daily"] = 0; failed.append(f"1D RSI {trend_rsi:.0f}>75")
    else:
        if trend_bias == "BEARISH":
            macro_scores["daily"] = 3; passed.append("1D:BEAR")
        elif trend_bias == "NEUTRAL":
            macro_scores["daily"] = 1; passed.append("1D:NEUTRAL")
        else:
            macro_scores["daily"] = 0; failed.append("1D:BULL vs SHORT")
        if trend_rsi < 25:
            macro_scores["daily"] = 0; failed.append(f"1D RSI {trend_rsi:.0f}<25")

    # 2. 4H structure (0-2)
    target = "BULLISH" if direction == "LONG" else "BEARISH"
    if struct_bias == target:
        macro_scores["h4"] = 2; passed.append(f"4H:{struct_bias}")
    elif struct_bias == "NEUTRAL":
        macro_scores["h4"] = 1; passed.append("4H:NEUTRAL")
    else:
        macro_scores["h4"] = 0; failed.append(f"4H:{struct_bias} vs {direction}")

    # 3. Sentiment (0-1)
    macro_scores["sentiment"] = 0
    if sentiment and config.USE_SENTIMENT:
        fr = sentiment.get("funding_rate", 0)
        oi_trend = sentiment.get("oi_trend", "UNKNOWN")
        if direction == "LONG" and fr > config.FUNDING_RATE_EXTREME:
            failed.append(f"FR {fr:.5f} extreme long")
        elif direction == "SHORT" and fr < config.FUNDING_RATE_NEGATIVE:
            failed.append(f"FR {fr:.5f} extreme short")
        else:
            macro_scores["sentiment"] = 1
            passed.append(f"FR:{fr:+.5f}")

        cg = sentiment.get("coinglass_composite", "")
        if cg:
            if direction == "LONG" and cg in ("STRONG_SHORT", "LEAN_SHORT"):
                macro_scores["sentiment"] = 0; failed.append(f"CG:{cg}")
            elif direction == "SHORT" and cg in ("STRONG_LONG", "LEAN_LONG"):
                macro_scores["sentiment"] = 0; failed.append(f"CG:{cg}")

    macro_total = sum(macro_scores.values())

    # ══════════════════════════════════════════════════════
    # ENTRY QUALITY (0-7)
    # ══════════════════════════════════════════════════════

    # 4. 15m TRIGGER (0-3) — real price action, not just state
    trigger = _score_trigger(direction, curr, prev, prev2, df_entry, entry_price, current_atr)
    entry_scores["trigger"] = trigger["score"]
    passed.extend(trigger["passed"])
    failed.extend(trigger["failed"])

    # 5. RSI context (0-2)
    rsi_val = float(curr["rsi"])
    if direction == "LONG":
        if 30 < rsi_val < 50:
            entry_scores["rsi"] = 2; passed.append(f"RSI recovery {rsi_val:.0f}")
        elif 50 <= rsi_val < 65:
            entry_scores["rsi"] = 1; passed.append(f"RSI momentum {rsi_val:.0f}")
        else:
            entry_scores["rsi"] = 0; failed.append(f"RSI bad zone {rsi_val:.0f}")
    else:
        if 50 < rsi_val < 70:
            entry_scores["rsi"] = 2; passed.append(f"RSI rejection {rsi_val:.0f}")
        elif 35 <= rsi_val <= 50:
            entry_scores["rsi"] = 1; passed.append(f"RSI weak {rsi_val:.0f}")
        else:
            entry_scores["rsi"] = 0; failed.append(f"RSI bad zone {rsi_val:.0f}")

    # 6. Volume quality (0-2)
    vol_ratio = float(curr["volume"] / curr["vol_avg"]) if curr["vol_avg"] > 0 else 0
    candle_bull = curr["close"] > curr["open"]
    right_candle = (direction == "LONG" and candle_bull) or (direction == "SHORT" and not candle_bull)

    if vol_ratio >= 2.0 and right_candle:
        entry_scores["volume"] = 2; passed.append(f"Vol {vol_ratio:.1f}x strong+right")
    elif vol_ratio >= 1.5:
        entry_scores["volume"] = 1 if right_candle else 0
        if right_candle:
            passed.append(f"Vol {vol_ratio:.1f}x ok")
        else:
            failed.append(f"Vol {vol_ratio:.1f}x wrong candle")
    else:
        entry_scores["volume"] = 0; failed.append(f"Vol {vol_ratio:.1f}x weak")

    entry_total = sum(entry_scores.values())
    total = macro_total + entry_total

    # ══════════════════════════════════════════════════════
    # VETOES
    # ══════════════════════════════════════════════════════

    dlog = {
        "direction": direction,
        "macro_scores": macro_scores, "macro_total": macro_total,
        "entry_scores": entry_scores, "entry_total": entry_total,
        "scores": {**macro_scores, **entry_scores},
        "total_score": total, "max_score": 13,
        "confidence": "BLOCKED", "decision": "NO_TRADE",
        "passed": passed, "failed": failed, "reasons": [],
    }

    # Hard vetoes
    if macro_scores["daily"] == 0:
        dlog["reasons"].append("VETO: daily=0")
        return None, dlog

    if entry_scores["trigger"] == 0:
        dlog["reasons"].append("VETO: no trigger")
        return None, dlog

    # Confirm block: need at least one of rsi>=1, volume>=1, trigger>=2
    if entry_scores["rsi"] == 0 and entry_scores["volume"] == 0 and entry_scores["trigger"] < 2:
        dlog["reasons"].append("VETO: no confirmation (rsi=0, vol=0, trigger<2)")
        return None, dlog

    # Overextension veto
    ema200 = float(curr["ema200"])
    dist_pct = abs(entry_price - ema200) / ema200 * 100 if ema200 > 0 else 0
    if dist_pct > 3.0 and entry_scores["trigger"] < 2:
        dlog["reasons"].append(f"VETO: overextended {dist_pct:.1f}% from EMA200, trigger weak")
        failed.append(f"overextended {dist_pct:.1f}%")
        return None, dlog

    # Threshold checks
    if macro_total < MACRO_MIN:
        dlog["reasons"].append(f"macro {macro_total}<{MACRO_MIN}")
        return None, dlog
    if entry_total < ENTRY_MIN:
        dlog["reasons"].append(f"entry {entry_total}<{ENTRY_MIN}")
        return None, dlog
    if total < TOTAL_MIN:
        dlog["reasons"].append(f"total {total}<{TOTAL_MIN}")
        return None, dlog

    # ══════════════════════════════════════════════════════
    # SIGNAL
    # ══════════════════════════════════════════════════════
    if total >= 10:
        confidence = "HIGH"
    else:
        confidence = "MEDIUM"

    dlog["confidence"] = confidence
    dlog["decision"] = "TRADE"

    sl_dist = current_atr * config.ATR_SL_MULT
    tp_dist = sl_dist * config.RR_RATIO

    if direction == "LONG":
        sl = round(entry_price - sl_dist, 8)
        tp = round(entry_price + tp_dist, 8)
    else:
        sl = round(entry_price + sl_dist, 8)
        tp = round(entry_price - tp_dist, 8)

    signal = {
        "signal": direction,
        "entry": entry_price,
        "sl": sl, "tp": tp,
        "atr": current_atr,
        "rsi": float(curr["rsi"]),
        "volume_ratio": vol_ratio,
        "reason": f"SCORE {total}/13 M:{macro_total} E:{entry_total} | {confidence}",
        "confidence": confidence,
        "size_mult": CONFIDENCE_SIZING[confidence],
        "mtf_reason": f"{total}/13 (M{macro_total}+E{entry_total})",
        "mtf": {
            "trend": trend_bias, "structure": struct_bias,
            "trend_details": trend_details, "structure_details": struct_details,
            "confidence": confidence,
            "mtf_reason": f"{total}/13",
            "sentiment": sentiment or {},
            "sentiment_note": "",
            "alignment": f"1D:{trend_bias}/4H:{struct_bias}/15m:{direction} = {total}/13",
            "macro_scores": macro_scores, "entry_scores": entry_scores,
        },
    }

    dlog["reasons"].append(f"ENTRY {direction} {total}/13 ({confidence})")
    return signal, dlog


def _score_trigger(direction, curr, prev, prev2, df, price, atr):
    """
    Score the 15m trigger — actual price action events, not just state.

    0 = no trigger (just EMA state, nothing happening)
    1 = weak setup (EMA aligned + some momentum)
    2 = confirmed trigger (breakdown/breakout + close confirmation)
    3 = strong trigger (sweep + rejection, or breakdown + retest fail)
    """
    score = 0
    passed = []
    failed = []

    # Get recent price action
    recent_highs = df["high"].iloc[-10:-2]
    recent_lows = df["low"].iloc[-10:-2]
    local_high = float(recent_highs.max())
    local_low = float(recent_lows.min())

    close = float(curr["close"])
    open_ = float(curr["open"])
    high = float(curr["high"])
    low = float(curr["low"])
    prev_close = float(prev["close"])
    prev_high = float(prev["high"])
    prev_low = float(prev["low"])
    ema200 = float(curr["ema200"])

    if direction == "LONG":
        # EMA context (not a trigger by itself, just context)
        above_ema = close > ema200

        if not above_ema:
            # Price below EMA200 — this is NOT a long trigger
            failed.append("price < EMA200 (no long context)")
            return {"score": 0, "passed": passed, "failed": failed}

        # Check for actual trigger events:

        # A) Pullback reclaim: was below prev low, closed back above
        pullback_reclaim = low < prev_low and close > prev_low
        if pullback_reclaim:
            score += 2
            passed.append("pullback reclaim")

        # B) Breakout above local high with close confirmation
        breakout = close > local_high and prev_close < local_high
        if breakout:
            score += 2
            passed.append("breakout above local high")

        # C) Higher low + momentum candle (close near high)
        higher_low = low > prev_low and float(prev["low"]) > float(prev2["low"])
        strong_close = (close - low) > 0.7 * (high - low) if (high - low) > 0 else False
        if higher_low and strong_close:
            score += 1
            passed.append("higher low + strong close")

        # D) Bounce from EMA with volume (simple momentum)
        near_ema = abs(low - ema200) < atr * 0.5
        if near_ema and close > open_:
            score += 1
            passed.append("bounce from EMA200")

        # E) RSI recovery trigger
        rsi_trigger = curr.get("rsi_exit_oversold", False)
        if rsi_trigger:
            score += 1
            passed.append("RSI exit oversold")

        if score == 0:
            failed.append("no long trigger event")

    else:  # SHORT
        below_ema = close < ema200

        if not below_ema:
            failed.append("price > EMA200 (no short context)")
            return {"score": 0, "passed": passed, "failed": failed}

        # A) Failed rally: went above prev high, closed back below
        failed_rally = high > prev_high and close < prev_high
        if failed_rally:
            score += 2
            passed.append("failed rally")

        # B) Breakdown below local low with close confirmation
        breakdown = close < local_low and prev_close > local_low
        if breakdown:
            score += 2
            passed.append("breakdown below local low")

        # C) Lower high + weak close (close near low)
        lower_high = high < prev_high and float(prev["high"]) < float(prev2["high"])
        weak_close = (high - close) > 0.7 * (high - low) if (high - low) > 0 else False
        if lower_high and weak_close:
            score += 1
            passed.append("lower high + weak close")

        # D) Rejection from EMA
        near_ema = abs(high - ema200) < atr * 0.5
        if near_ema and close < open_:
            score += 1
            passed.append("rejection from EMA200")

        # E) RSI exit overbought
        rsi_trigger = curr.get("rsi_exit_overbought", False)
        if rsi_trigger:
            score += 1
            passed.append("RSI exit overbought")

        if score == 0:
            failed.append("no short trigger event")

    return {"score": min(score, 3), "passed": passed, "failed": failed}


def _check_wall(orderbook, price, direction):
    """Orderbook wall check."""
    asks = orderbook.get("asks", [])
    bids = orderbook.get("bids", [])
    if not asks or not bids:
        return False
    all_sizes = [s for _, s in asks[:10]] + [s for _, s in bids[:10]]
    if not all_sizes:
        return False
    avg = sum(all_sizes) / len(all_sizes)
    thresh = avg * config.WALL_THRESHOLD_MULT
    rng = price * 0.005
    if direction == "LONG":
        return any(p <= price + rng and s > thresh for p, s in asks)
    return any(p >= price - rng and s > thresh for p, s in bids)


def _empty_dlog():
    return {
        "direction": None, "macro_scores": {}, "entry_scores": {},
        "scores": {}, "macro_total": 0, "entry_total": 0,
        "total_score": 0, "max_score": 13, "confidence": "BLOCKED",
        "decision": "NO_TRADE", "passed": [], "failed": [], "reasons": [],
    }


# Legacy compat
def check_signal(df, orderbook=None):
    from indicators import add_all_indicators
    if len(df) < config.EMA_PERIOD + 5: return None
    df = add_all_indicators(df)
    c, p = df.iloc[-2], df.iloc[-3]
    pr, a = c["close"], c["atr"]
    if a <= 0 or pd.isna(a): return None
    sd, td = a*config.ATR_SL_MULT, a*config.ATR_SL_MULT*config.RR_RATIO
    vr = c["volume"]/c["vol_avg"] if c["vol_avg"]>0 else 0
    if c["above_ema"] and (c["rsi_exit_oversold"] or (30<c["rsi"]<50 and p["rsi"]<35)) and c["vol_spike"]:
        return {"signal":"LONG","entry":pr,"sl":round(pr-sd,8),"tp":round(pr+td,8),"atr":a,"rsi":c["rsi"],"volume_ratio":vr,"reason":"LONG"}
    if c["below_ema"] and (c["rsi_exit_overbought"] or (50<c["rsi"]<70 and p["rsi"]>65)) and c["vol_spike"]:
        return {"signal":"SHORT","entry":pr,"sl":round(pr+sd,8),"tp":round(pr-td,8),"atr":a,"rsi":c["rsi"],"volume_ratio":vr,"reason":"SHORT"}
    return None

def get_signal_summary(df):
    from indicators import add_all_indicators
    df = add_all_indicators(df)
    c = df.iloc[-2]
    return {"close":c["close"],"ema200":round(c["ema200"],2),"rsi":round(c["rsi"],2),"atr":round(c["atr"],6),
            "above_ema":c["above_ema"],"vol_spike":c["vol_spike"],
            "vol_ratio":round(c["volume"]/c["vol_avg"],2) if c["vol_avg"]>0 else 0}
