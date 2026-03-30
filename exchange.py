import fix_ssl
"""
ALADDIN BOT v2 — Exchange Module
Bybit API: data fetching, orders, + sentiment data (funding, OI).
"""
from pybit.unified_trading import HTTP
import pandas as pd
import config
import time


class Exchange:
    def __init__(self):
        """Connect to Bybit."""
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self.client = HTTP(
            testnet=config.TESTNET,
            api_key=config.API_KEY,
            api_secret=config.API_SECRET,
            demo=True,
        )
        self.client.client.verify = False
        self.mode = "TESTNET" if config.TESTNET else "MAINNET"
        print(f"[EXCHANGE] Connected to Bybit {self.mode}")

    # ── Account ───────────────────────────────────────────────────
    def get_balance(self):
        """Get USDT balance."""
        try:
            resp = self.client.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            coins = resp["result"]["list"][0]["coin"]
            for c in coins:
                if c["coin"] == "USDT":
                    return float(c["walletBalance"])
            return 0.0
        except Exception as e:
            print(f"[EXCHANGE] Error getting balance: {e}")
            return 0.0

    def get_positions(self):
        """Get all open positions."""
        try:
            resp = self.client.get_positions(category="linear", settleCoin="USDT")
            positions = []
            for p in resp["result"]["list"]:
                if float(p["size"]) > 0:
                    positions.append({
                        "symbol": p["symbol"],
                        "side": p["side"],
                        "size": float(p["size"]),
                        "entryPrice": float(p["avgPrice"]),
                        "leverage": p["leverage"],
                        "unrealisedPnl": float(p["unrealisedPnl"]),
                    })
            return positions
        except Exception as e:
            print(f"[EXCHANGE] Error getting positions: {e}")
            return []

    # ── Market Data ───────────────────────────────────────────────
    def get_klines(self, symbol, interval=None, limit=None):
        """
        Fetch candlestick data.
        Returns pandas DataFrame: open, high, low, close, volume, timestamp
        """
        if interval is None:
            interval = config.MTF_TIMEFRAMES["entry"]
        if limit is None:
            limit = config.CANDLE_LIMIT_ENTRY

        try:
            resp = self.client.get_kline(
                category="linear",
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
            rows = resp["result"]["list"]
            df = pd.DataFrame(rows, columns=[
                "timestamp", "open", "high", "low", "close", "volume", "turnover"
            ])
            for col in ["open", "high", "low", "close", "volume", "turnover"]:
                df[col] = df[col].astype(float)
            df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
            df = df.sort_values("timestamp").reset_index(drop=True)
            return df
        except Exception as e:
            print(f"[EXCHANGE] Error fetching klines {symbol} {interval}: {e}")
            return pd.DataFrame()

    def get_mtf_klines(self, symbol):
        """
        Fetch klines for all three timeframes at once.
        Returns dict: {"trend": df_daily, "structure": df_4h, "entry": df_15m}
        """
        result = {}
        tf_config = {
            "trend": (config.MTF_TIMEFRAMES["trend"], config.CANDLE_LIMIT_TREND),
            "structure": (config.MTF_TIMEFRAMES["structure"], config.CANDLE_LIMIT_STRUCTURE),
            "entry": (config.MTF_TIMEFRAMES["entry"], config.CANDLE_LIMIT_ENTRY),
        }
        for name, (interval, limit) in tf_config.items():
            df = self.get_klines(symbol, interval=interval, limit=limit)
            result[name] = df
            time.sleep(0.1)  # Rate limit protection
        return result

    def get_orderbook(self, symbol, limit=None):
        """Fetch orderbook."""
        if limit is None:
            limit = config.ORDERBOOK_DEPTH
        try:
            resp = self.client.get_orderbook(
                category="linear", symbol=symbol, limit=limit,
            )
            bids = [(float(p[0]), float(p[1])) for p in resp["result"]["b"]]
            asks = [(float(p[0]), float(p[1])) for p in resp["result"]["a"]]
            return {"bids": bids, "asks": asks}
        except Exception as e:
            print(f"[EXCHANGE] Error fetching orderbook {symbol}: {e}")
            return {"bids": [], "asks": []}

    def get_ticker(self, symbol):
        """Get current price."""
        try:
            resp = self.client.get_tickers(category="linear", symbol=symbol)
            return float(resp["result"]["list"][0]["lastPrice"])
        except Exception as e:
            print(f"[EXCHANGE] Error getting ticker {symbol}: {e}")
            return 0.0

    # ── Sentiment Data (FREE from Bybit) ──────────────────────────
    def get_funding_rate(self, symbol):
        """
        Get current funding rate for a symbol.
        Positive = longs pay shorts (market overheated long).
        Negative = shorts pay longs (market overheated short).
        """
        try:
            resp = self.client.get_tickers(category="linear", symbol=symbol)
            info = resp["result"]["list"][0]
            return {
                "funding_rate": float(info.get("fundingRate", 0)),
                "next_funding_time": info.get("nextFundingTime", ""),
            }
        except Exception as e:
            print(f"[EXCHANGE] Error getting funding rate {symbol}: {e}")
            return {"funding_rate": 0.0, "next_funding_time": ""}

    def get_open_interest(self, symbol):
        """
        Get open interest data.
        Rising OI + rising price = new longs (bullish continuation).
        Rising OI + falling price = new shorts (bearish continuation).
        Falling OI = positions closing (trend weakening).
        """
        try:
            resp = self.client.get_open_interest(
                category="linear",
                symbol=symbol,
                intervalTime="1h",
                limit=48,  # Last 48 hours
            )
            rows = resp["result"]["list"]
            if not rows:
                return {"current": 0, "change_pct": 0, "trend": "UNKNOWN"}

            # Rows come newest first from Bybit
            current_oi = float(rows[0]["openInterest"])
            # Compare to 24h ago
            if len(rows) >= 24:
                past_oi = float(rows[23]["openInterest"])
            else:
                past_oi = float(rows[-1]["openInterest"])

            change_pct = ((current_oi - past_oi) / past_oi * 100) if past_oi > 0 else 0

            if change_pct > config.OI_CHANGE_THRESHOLD:
                trend = "RISING"
            elif change_pct < -config.OI_CHANGE_THRESHOLD:
                trend = "FALLING"
            else:
                trend = "STABLE"

            return {
                "current": current_oi,
                "change_pct": round(change_pct, 2),
                "trend": trend,
            }
        except Exception as e:
            print(f"[EXCHANGE] Error getting OI {symbol}: {e}")
            return {"current": 0, "change_pct": 0, "trend": "UNKNOWN"}

    def get_sentiment(self, symbol):
        """
        Get combined sentiment data for a symbol.
        Returns funding rate + OI in one call.
        """
        funding = self.get_funding_rate(symbol)
        oi = self.get_open_interest(symbol)
        return {
            "funding_rate": funding["funding_rate"],
            "oi_current": oi["current"],
            "oi_change_pct": oi["change_pct"],
            "oi_trend": oi["trend"],
        }

    # ── Orders ────────────────────────────────────────────────────
    def set_leverage(self, symbol, leverage):
        """Set leverage for a symbol."""
        try:
            self.client.set_leverage(
                category="linear", symbol=symbol,
                buyLeverage=str(leverage), sellLeverage=str(leverage),
            )
            print(f"[EXCHANGE] Leverage set to {leverage}x for {symbol}")
        except Exception as e:
            if "leverage not modified" not in str(e).lower():
                print(f"[EXCHANGE] Leverage error {symbol}: {e}")

    def place_order(self, symbol, side, qty, sl_price=None, tp_price=None):
        """Place a market order with optional SL/TP."""
        try:
            params = {
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": "Market",
                "qty": str(qty),
                "timeInForce": "GTC",
            }
            if sl_price:
                params["stopLoss"] = str(sl_price)
            if tp_price:
                params["takeProfit"] = str(tp_price)

            resp = self.client.place_order(**params)
            order_id = resp["result"]["orderId"]
            print(f"[EXCHANGE] Order: {side} {qty} {symbol} | SL:{sl_price} TP:{tp_price} | ID:{order_id}")
            return order_id
        except Exception as e:
            print(f"[EXCHANGE] Order error {symbol}: {e}")
            return None

    def close_position(self, symbol, side, qty):
        """Close a position."""
        close_side = "Sell" if side == "Buy" else "Buy"
        return self.place_order(symbol, close_side, qty)

    def get_qty_info(self, symbol):
        """Get min qty and qty step for proper rounding."""
        try:
            resp = self.client.get_instruments_info(category="linear", symbol=symbol)
            info = resp["result"]["list"][0]
            lot = info["lotSizeFilter"]
            return {
                "min_qty": float(lot["minOrderQty"]),
                "qty_step": float(lot["qtyStep"]),
                "min_notional": float(lot.get("minNotionalValue", 0)),
            }
        except Exception as e:
            print(f"[EXCHANGE] Error getting qty info {symbol}: {e}")
            return {"min_qty": 0.001, "qty_step": 0.001, "min_notional": 0}

    def get_min_qty(self, symbol):
        """Get minimum order quantity."""
        return self.get_qty_info(symbol)["min_qty"]

    def round_qty(self, qty, symbol):
        """Round qty to valid step size for the symbol."""
        info = self.get_qty_info(symbol)
        step = info["qty_step"]
        if step <= 0:
            step = 0.001
        # Floor to step size (not round up — don't exceed margin)
        import math
        rounded = math.floor(qty / step) * step
        # Format to avoid floating point noise
        decimals = max(0, len(str(step).rstrip('0').split('.')[-1])) if '.' in str(step) else 0
        return round(max(rounded, info["min_qty"]), decimals)


# ── Quick Test ────────────────────────────────────────────────────
if __name__ == "__main__":
    ex = Exchange()
    print(f"\nBalance: ${ex.get_balance()}")
    print(f"BTC price: ${ex.get_ticker('BTCUSDT')}")

    # Test MTF klines
    print("\n── MTF Klines Test ──")
    mtf = ex.get_mtf_klines("BTCUSDT")
    for name, df in mtf.items():
        if not df.empty:
            print(f"  {name:10s}: {len(df)} candles | last close: ${df.iloc[-1]['close']:.2f}")

    # Test sentiment
    print("\n── Sentiment Test ──")
    sent = ex.get_sentiment("BTCUSDT")
    print(f"  Funding: {sent['funding_rate']:.6f}")
    print(f"  OI change 24h: {sent['oi_change_pct']:.1f}% ({sent['oi_trend']})")
