"""
ALADDIN BOT — Coinglass Data Module (Phase 2)
Fetches derivatives market data: OI, Funding Rate, Long/Short Ratio, Liquidations.

Setup:
  1. Register at https://www.coinglass.com
  2. Get free API key from Account → API
  3. Add to .env: COINGLASS_API_KEY=your_key_here

Free tier gives: OI, funding rate, long/short ratio, basic liquidation data.
Paid (Prime) gives: liquidation heatmaps, extended history.
"""
import os
import time
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "")
BASE_URL = "https://open-api-v3.coinglass.com/api"

# Rate limiting
_last_request_time = 0
MIN_REQUEST_INTERVAL = 0.5  # seconds between requests


def _headers():
    return {
        "accept": "application/json",
        "CG-API-KEY": COINGLASS_API_KEY,
    }


def _rate_limit():
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.time()


def _get(endpoint, params=None):
    """Make authenticated GET request to Coinglass API."""
    if not COINGLASS_API_KEY:
        print("[COINGLASS] ⚠️  No API key. Set COINGLASS_API_KEY in .env")
        return None

    _rate_limit()
    url = f"{BASE_URL}{endpoint}"

    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=10)
        data = resp.json()

        if data.get("success") is False or data.get("code") != "0":
            msg = data.get("msg", "Unknown error")
            print(f"[COINGLASS] API error: {msg}")
            return None

        return data.get("data")
    except requests.exceptions.RequestException as e:
        print(f"[COINGLASS] Request error: {e}")
        return None
    except ValueError:
        print(f"[COINGLASS] Invalid JSON response")
        return None


# ═══════════════════════════════════════════════════════════════════
# OPEN INTEREST
# ═══════════════════════════════════════════════════════════════════

def get_open_interest(symbol, exchange="Bybit", interval="h4"):
    """
    Get Open Interest OHLC history for a pair.
    
    interval: m1, m5, m15, m30, h1, h4, h12, d1
    
    Returns list of {t, o, h, l, c} (time, open, high, low, close OI)
    """
    # Strip USDT suffix for Coinglass (uses "BTC" not "BTCUSDT")
    coin = symbol.replace("USDT", "")
    
    data = _get("/futures/openInterest/ohlc-history", {
        "symbol": coin,
        "exchange": exchange,
        "interval": interval,
        "limit": 100,
    })
    return data


def get_oi_aggregated(symbol, interval="h4"):
    """
    Get aggregated OI across all exchanges.
    Better for overall market sentiment.
    """
    coin = symbol.replace("USDT", "")
    
    data = _get("/futures/openInterest/ohlc-aggregated-history", {
        "symbol": coin,
        "interval": interval,
        "limit": 100,
    })
    return data


def analyze_oi(oi_data):
    """
    Analyze OI trend.
    
    Rising OI + Rising price = Trend confirmation (strong)
    Rising OI + Falling price = Bearish pressure building
    Falling OI + Rising price = Short squeeze / weak rally
    Falling OI + Falling price = Capitulation / trend weakening
    
    Returns: {"trend": str, "change_pct": float, "signal": str}
    """
    if not oi_data or len(oi_data) < 5:
        return {"trend": "unknown", "change_pct": 0, "signal": "NEUTRAL"}

    recent = oi_data[-5:]  # Last 5 periods
    
    # OI change
    oi_start = float(recent[0].get("c", recent[0].get("close", 0)))
    oi_end = float(recent[-1].get("c", recent[-1].get("close", 0)))
    
    if oi_start == 0:
        return {"trend": "unknown", "change_pct": 0, "signal": "NEUTRAL"}
    
    oi_change = (oi_end - oi_start) / oi_start * 100

    if oi_change > 2:
        trend = "RISING"
    elif oi_change < -2:
        trend = "FALLING"
    else:
        trend = "FLAT"

    return {
        "trend": trend,
        "change_pct": round(oi_change, 2),
        "oi_current": oi_end,
        "signal": "CONFIRM" if trend == "RISING" else "WEAK" if trend == "FALLING" else "NEUTRAL",
    }


# ═══════════════════════════════════════════════════════════════════
# FUNDING RATE
# ═══════════════════════════════════════════════════════════════════

def get_funding_rate(symbol, exchange="Bybit"):
    """
    Get current and historical funding rate.
    
    Positive funding = longs pay shorts (market bullish, crowded long)
    Negative funding = shorts pay longs (market bearish, crowded short)
    Very high positive = potential long squeeze incoming
    Very negative = potential short squeeze incoming
    """
    coin = symbol.replace("USDT", "")
    
    data = _get("/futures/fundingRate/ohlc-history", {
        "symbol": coin,
        "exchange": exchange,
        "interval": "h8",  # Funding settles every 8h
        "limit": 30,
    })
    return data


def get_funding_exchange_list(symbol):
    """Get funding rate across all exchanges for comparison."""
    coin = symbol.replace("USDT", "")
    
    data = _get("/futures/fundingRate/exchange-list", {
        "symbol": coin,
    })
    return data


def analyze_funding(funding_data):
    """
    Analyze funding rate for trading signal.
    
    Extreme positive (>0.05%) = Market overheated long → contrarian SHORT signal
    Extreme negative (<-0.05%) = Market overheated short → contrarian LONG signal
    Neutral (-0.01% to 0.01%) = No edge from funding
    
    Returns: {"rate": float, "signal": str, "extreme": bool}
    """
    if not funding_data or len(funding_data) < 1:
        return {"rate": 0, "signal": "NEUTRAL", "extreme": False}

    # Get latest funding rate
    latest = funding_data[-1]
    rate = float(latest.get("c", latest.get("close", 0)))
    
    # Average recent funding
    recent_rates = [float(d.get("c", d.get("close", 0))) for d in funding_data[-6:]]
    avg_rate = sum(recent_rates) / len(recent_rates) if recent_rates else 0

    extreme = False
    signal = "NEUTRAL"

    if rate > 0.0005:  # >0.05%
        signal = "BEARISH"  # Crowded longs → contrarian short
        extreme = True if rate > 0.001 else False
    elif rate < -0.0005:  # <-0.05%
        signal = "BULLISH"  # Crowded shorts → contrarian long
        extreme = True if rate < -0.001 else False
    else:
        signal = "NEUTRAL"

    return {
        "rate": round(rate * 100, 4),  # As percentage
        "avg_rate": round(avg_rate * 100, 4),
        "signal": signal,
        "extreme": extreme,
    }


# ═══════════════════════════════════════════════════════════════════
# LONG/SHORT RATIO
# ═══════════════════════════════════════════════════════════════════

def get_long_short_ratio(symbol, exchange="Bybit", interval="h4"):
    """
    Get global long/short account ratio.
    
    >1 = more accounts long than short
    <1 = more accounts short than long
    """
    coin = symbol.replace("USDT", "")
    
    data = _get("/futures/globalLongShortAccountRatio/history", {
        "symbol": coin,
        "exchange": exchange,
        "interval": interval,
        "limit": 30,
    })
    return data


def get_top_ls_ratio(symbol, exchange="Bybit", interval="h4"):
    """
    Get top traders long/short ratio (more informative than global).
    """
    coin = symbol.replace("USDT", "")
    
    data = _get("/futures/topLongShortAccountRatio/history", {
        "symbol": coin,
        "exchange": exchange,
        "interval": interval,
        "limit": 30,
    })
    return data


def analyze_long_short(ls_data):
    """
    Analyze long/short ratio for contrarian signals.
    
    Retail is usually wrong at extremes:
      >65% long = contrarian SHORT signal
      >65% short = contrarian LONG signal
      40-60% = no edge
    
    Returns: {"long_pct": float, "short_pct": float, "signal": str, "extreme": bool}
    """
    if not ls_data or len(ls_data) < 1:
        return {"long_pct": 50, "short_pct": 50, "signal": "NEUTRAL", "extreme": False}

    latest = ls_data[-1]
    
    # API returns longAccount/shortAccount or longRatio
    long_pct = float(latest.get("longAccount", latest.get("longRatio", 50)))
    short_pct = float(latest.get("shortAccount", latest.get("shortRatio", 50)))
    
    # Normalize if raw ratio
    if long_pct + short_pct < 2:  # It's a ratio, not percentage
        total = long_pct + short_pct
        long_pct = long_pct / total * 100
        short_pct = short_pct / total * 100

    extreme = False
    signal = "NEUTRAL"

    if long_pct > 65:
        signal = "BEARISH"  # Too many longs → contrarian
        extreme = True if long_pct > 75 else False
    elif short_pct > 65:
        signal = "BULLISH"  # Too many shorts → contrarian
        extreme = True if short_pct > 75 else False

    return {
        "long_pct": round(long_pct, 1),
        "short_pct": round(short_pct, 1),
        "signal": signal,
        "extreme": extreme,
    }


# ═══════════════════════════════════════════════════════════════════
# LIQUIDATION DATA
# ═══════════════════════════════════════════════════════════════════

def get_liquidation_history(symbol, interval="h4"):
    """
    Get aggregated liquidation history.
    Shows where liquidations have been happening.
    """
    coin = symbol.replace("USDT", "")
    
    data = _get("/futures/liquidation/aggregated-history", {
        "symbol": coin,
        "interval": interval,
        "limit": 50,
    })
    return data


def get_liquidation_map(symbol, exchange="Bybit"):
    """
    Get liquidation map — clusters of potential liquidations at price levels.
    Shows WHERE future liquidations will happen if price moves.
    
    NOTE: May require paid API tier for full data.
    """
    coin = symbol.replace("USDT", "")
    
    data = _get("/futures/liquidation/map", {
        "symbol": coin,
        "exchange": exchange,
    })
    return data


def get_liquidation_heatmap(symbol, exchange="Bybit"):
    """
    Get liquidation heatmap data.
    Shows density of liquidation levels around current price.
    
    NOTE: Requires Prime API tier.
    Falls back to liquidation map if unavailable.
    """
    coin = symbol.replace("USDT", "")
    
    # Try heatmap first (paid)
    data = _get("/futures/liquidation/heatmap", {
        "symbol": coin,
        "exchange": exchange,
    })
    
    if data is None:
        # Fallback to free liquidation map
        print(f"[COINGLASS] Heatmap unavailable (needs Prime). Using liquidation map instead.")
        return get_liquidation_map(symbol, exchange)
    
    return data


def analyze_liquidations(liq_data, current_price):
    """
    Analyze liquidation data to find support/resistance zones.
    
    High liquidation clusters act as magnets — price tends to move toward them.
    Clusters above current price = potential resistance / short squeeze target
    Clusters below current price = potential support / long squeeze target
    
    Returns: {
        "clusters_above": [(price, volume)],
        "clusters_below": [(price, volume)],
        "nearest_above": float,
        "nearest_below": float,
        "bias": str,  # Direction of largest cluster = magnet
    }
    """
    if not liq_data:
        return {
            "clusters_above": [],
            "clusters_below": [],
            "nearest_above": None,
            "nearest_below": None,
            "bias": "NEUTRAL",
        }

    clusters_above = []
    clusters_below = []

    # Parse liquidation data
    # Format varies by endpoint; handle both map and heatmap
    if isinstance(liq_data, list):
        for item in liq_data:
            price = float(item.get("price", item.get("p", 0)))
            vol = float(item.get("volume", item.get("v", item.get("vol", 0))))
            
            if price == 0:
                continue
                
            if price > current_price:
                clusters_above.append((price, vol))
            else:
                clusters_below.append((price, vol))
    elif isinstance(liq_data, dict):
        # Handle dict format (heatmap)
        for key in ["longs", "shorts", "data"]:
            if key in liq_data and isinstance(liq_data[key], list):
                for item in liq_data[key]:
                    price = float(item.get("price", item.get("p", 0)))
                    vol = float(item.get("volume", item.get("v", 0)))
                    if price == 0:
                        continue
                    if price > current_price:
                        clusters_above.append((price, vol))
                    else:
                        clusters_below.append((price, vol))

    # Sort by volume (largest first)
    clusters_above.sort(key=lambda x: x[1], reverse=True)
    clusters_below.sort(key=lambda x: x[1], reverse=True)

    # Nearest clusters
    nearest_above = min(clusters_above, key=lambda x: x[0])[0] if clusters_above else None
    nearest_below = max(clusters_below, key=lambda x: x[0])[0] if clusters_below else None

    # Bias: largest cluster acts as magnet
    max_above_vol = clusters_above[0][1] if clusters_above else 0
    max_below_vol = clusters_below[0][1] if clusters_below else 0

    if max_above_vol > max_below_vol * 1.5:
        bias = "BULLISH"  # Big cluster above = price likely moves up (short squeeze)
    elif max_below_vol > max_above_vol * 1.5:
        bias = "BEARISH"  # Big cluster below = price likely moves down (long squeeze)
    else:
        bias = "NEUTRAL"

    return {
        "clusters_above": clusters_above[:5],  # Top 5
        "clusters_below": clusters_below[:5],
        "nearest_above": nearest_above,
        "nearest_below": nearest_below,
        "total_above_vol": sum(v for _, v in clusters_above),
        "total_below_vol": sum(v for _, v in clusters_below),
        "bias": bias,
    }


# ═══════════════════════════════════════════════════════════════════
# COMPOSITE ANALYSIS
# ═══════════════════════════════════════════════════════════════════

def get_full_analysis(symbol, current_price=None, exchange="Bybit"):
    """
    Run all Coinglass analyses for a symbol.
    
    Returns composite score:
      - OI trend
      - Funding rate signal
      - Long/short ratio signal
      - Liquidation magnet bias
    
    Composite: 
      All align LONG = strong LONG confirmation
      All align SHORT = strong SHORT confirmation
      Mixed = reduce position size or skip
    """
    print(f"[COINGLASS] Fetching data for {symbol}...")

    # Fetch all data
    oi_data = get_oi_aggregated(symbol)
    funding_data = get_funding_rate(symbol, exchange)
    ls_data = get_long_short_ratio(symbol, exchange)
    liq_data = get_liquidation_map(symbol, exchange)

    # Analyze
    oi = analyze_oi(oi_data)
    funding = analyze_funding(funding_data)
    ls = analyze_long_short(ls_data)
    liq = analyze_liquidations(liq_data, current_price) if current_price else {
        "bias": "NEUTRAL", "nearest_above": None, "nearest_below": None,
        "clusters_above": [], "clusters_below": [],
    }

    # ── Composite Score ──────────────────────────────────────
    signals = {
        "oi": oi["signal"],        # CONFIRM / WEAK / NEUTRAL
        "funding": funding["signal"],  # BULLISH / BEARISH / NEUTRAL
        "ls_ratio": ls["signal"],     # BULLISH / BEARISH / NEUTRAL
        "liquidation": liq["bias"],   # BULLISH / BEARISH / NEUTRAL
    }

    # Count bullish/bearish signals
    bullish = sum(1 for s in signals.values() if s in ("BULLISH", "CONFIRM"))
    bearish = sum(1 for s in signals.values() if s == "BEARISH")
    
    # OI WEAK counts as mild bearish
    if oi["signal"] == "WEAK":
        bearish += 0.5

    if bullish >= 3:
        composite = "STRONG_LONG"
    elif bullish >= 2 and bearish == 0:
        composite = "LEAN_LONG"
    elif bearish >= 3:
        composite = "STRONG_SHORT"
    elif bearish >= 2 and bullish == 0:
        composite = "LEAN_SHORT"
    else:
        composite = "MIXED"

    # Confidence 0-100
    confidence = int(max(bullish, bearish) / len(signals) * 100)

    result = {
        "symbol": symbol,
        "oi": oi,
        "funding": funding,
        "long_short": ls,
        "liquidation": liq,
        "signals": signals,
        "composite": composite,
        "confidence": confidence,
        "has_extreme": funding["extreme"] or ls["extreme"],
    }

    return result


def print_coinglass_dashboard(analysis):
    """Print formatted Coinglass analysis."""
    a = analysis
    print(f"\n{'─' * 60}")
    print(f"  COINGLASS: {a['symbol']}  │  {a['composite']} ({a['confidence']}%)")
    print(f"{'─' * 60}")

    # OI
    oi = a["oi"]
    print(f"  OI        │ {oi['trend']:8s} ({oi['change_pct']:+.1f}%) │ {oi['signal']}")

    # Funding
    fr = a["funding"]
    extreme_mark = " ⚠️" if fr["extreme"] else ""
    print(f"  Funding   │ {fr['rate']:+.4f}% (avg: {fr['avg_rate']:+.4f}%) │ {fr['signal']}{extreme_mark}")

    # L/S Ratio
    ls = a["long_short"]
    extreme_mark = " ⚠️" if ls["extreme"] else ""
    print(f"  L/S Ratio │ L:{ls['long_pct']:.0f}% / S:{ls['short_pct']:.0f}% │ {ls['signal']}{extreme_mark}")

    # Liquidation
    liq = a["liquidation"]
    above = f"${liq['nearest_above']:,.0f}" if liq["nearest_above"] else "—"
    below = f"${liq['nearest_below']:,.0f}" if liq["nearest_below"] else "—"
    print(f"  Liq Map   │ ↑{above} ↓{below} │ {liq['bias']}")

    print(f"{'─' * 60}")


# ── Quick Test ───────────────────────────────────────────────────
if __name__ == "__main__":
    if not COINGLASS_API_KEY:
        print("Set COINGLASS_API_KEY in .env to test")
        print("Get free key: https://www.coinglass.com → Account → API")
    else:
        for sym in ["BTCUSDT", "ETHUSDT"]:
            analysis = get_full_analysis(sym, current_price=87000 if "BTC" in sym else 2000)
            print_coinglass_dashboard(analysis)
