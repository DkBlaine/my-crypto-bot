"""
ALADDIN BOT — Position Manager
Active management of open positions between entry and SL/TP.

Functions:
  1. Breakeven: move SL to entry after +1R
  2. Lock profit: move SL to +0.5R after +1.5R
  3. Thesis invalidation: exit if original setup conditions broken
  4. Force exit: emergency close on adverse flow/volatility

Runs every scan cycle (15m). Does NOT replace SL/TP on exchange —
instead modifies them via Bybit API when conditions met.
"""
import config
import time
from datetime import datetime


# ── Config ────────────────────────────────────────────────────
BREAKEVEN_THRESHOLD_R = 1.0     # Move SL to entry after +1R
LOCK_PROFIT_THRESHOLD_R = 1.5   # Move SL to +0.5R after +1.5R
LOCK_PROFIT_LEVEL_R = 0.5       # Where to put SL when locking

# Force exit triggers
ATR_SPIKE_MULT = 3.0            # ATR 3x normal = panic
ADVERSE_CANDLE_MULT = 2.5       # Candle > 2.5x ATR against position
ORDERFLOW_VETO_CONF = 0.7       # OF confidence threshold for force exit


class PositionManager:
    """Manages open positions: breakeven, trail, thesis check, force exit."""

    def __init__(self, exchange):
        self.exchange = exchange
        # Track thesis per position: {symbol: {direction, entry_reasons, ...}}
        self.theses = {}
        self.actions_log = []

    def register_thesis(self, symbol, direction, entry_price, sl, tp, atr, reasons):
        """
        Record WHY a position was opened.
        Called when bot opens a new trade.
        """
        sl_distance = abs(entry_price - sl)
        self.theses[symbol] = {
            "direction": direction,
            "entry_price": entry_price,
            "original_sl": sl,
            "original_tp": tp,
            "current_sl": sl,
            "atr_at_entry": atr,
            "sl_distance": sl_distance,  # 1R in price
            "reasons": reasons,
            "opened_at": datetime.now().isoformat(),
            "breakeven_done": False,
            "lock_done": False,
            "partial_done": False,
        }
        print(f"  [PM] Thesis registered: {symbol} {direction} entry:{entry_price} SL:{sl} TP:{tp}")

    def check_positions(self, positions, market_data=None, orderflow_engine=None):
        """
        Main loop: check all open positions for management actions.
        Called every scan cycle.

        positions: list from exchange.get_positions()
        market_data: {symbol: {close, atr, rsi, ema200, ...}}
        orderflow_engine: OrderflowEngine instance or None

        Returns list of actions taken.
        """
        actions = []

        for pos in positions:
            symbol = pos["symbol"]
            side = pos["side"]  # "Buy" or "Sell"
            direction = "LONG" if side == "Buy" else "SHORT"
            entry_price = pos["entryPrice"]
            current_pnl = pos["unrealisedPnl"]
            size = pos["size"]

            thesis = self.theses.get(symbol)
            if not thesis:
                # Position opened before bot started or manually
                continue

            # Current market price
            mkt = market_data.get(symbol, {}) if market_data else {}
            current_price = mkt.get("close", 0)
            current_atr = mkt.get("atr", thesis["atr_at_entry"])

            if current_price <= 0:
                continue

            sl_dist = thesis["sl_distance"]  # 1R in price terms

            # Calculate R-multiple of current move
            if direction == "LONG":
                r_multiple = (current_price - entry_price) / sl_dist if sl_dist > 0 else 0
            else:
                r_multiple = (entry_price - current_price) / sl_dist if sl_dist > 0 else 0

            # ── 1. Breakeven ────────────────────────────────
            action = self._check_breakeven(symbol, direction, entry_price, r_multiple, thesis)
            if action:
                actions.append(action)

            # ── 2. Lock profit ──────────────────────────────
            action = self._check_lock_profit(symbol, direction, entry_price, r_multiple, sl_dist, thesis)
            if action:
                actions.append(action)

            # ── 3. Thesis invalidation ──────────────────────
            action = self._check_thesis(symbol, direction, entry_price, mkt, thesis)
            if action:
                actions.append(action)

            # ── 4. Force exit ───────────────────────────────
            action = self._check_force_exit(
                symbol, direction, entry_price, current_price,
                current_atr, r_multiple, mkt, orderflow_engine, thesis,
            )
            if action:
                actions.append(action)

        self.actions_log.extend(actions)
        return actions

    # ── Management checks ─────────────────────────────────────

    def _check_breakeven(self, symbol, direction, entry, r_mult, thesis):
        """Move SL to breakeven after +1R."""
        if thesis["breakeven_done"]:
            return None

        if r_mult >= BREAKEVEN_THRESHOLD_R:
            # Small buffer above/below entry to avoid being stopped at exact entry
            buffer = thesis["atr_at_entry"] * 0.1

            if direction == "LONG":
                new_sl = round(entry + buffer, 6)
            else:
                new_sl = round(entry - buffer, 6)

            success = self._modify_sl(symbol, new_sl, direction)
            if success:
                thesis["breakeven_done"] = True
                thesis["current_sl"] = new_sl
                return {
                    "action": "BREAKEVEN",
                    "symbol": symbol,
                    "r_multiple": round(r_mult, 2),
                    "new_sl": new_sl,
                    "time": datetime.now().isoformat(),
                }
        return None

    def _check_lock_profit(self, symbol, direction, entry, r_mult, sl_dist, thesis):
        """Lock profit by moving SL to +0.5R after +1.5R."""
        if thesis["lock_done"]:
            return None

        if r_mult >= LOCK_PROFIT_THRESHOLD_R:
            lock_offset = sl_dist * LOCK_PROFIT_LEVEL_R

            if direction == "LONG":
                new_sl = round(entry + lock_offset, 6)
            else:
                new_sl = round(entry - lock_offset, 6)

            success = self._modify_sl(symbol, new_sl, direction)
            if success:
                thesis["lock_done"] = True
                thesis["current_sl"] = new_sl
                return {
                    "action": "LOCK_PROFIT",
                    "symbol": symbol,
                    "r_multiple": round(r_mult, 2),
                    "new_sl": new_sl,
                    "locked_r": LOCK_PROFIT_LEVEL_R,
                    "time": datetime.now().isoformat(),
                }
        return None

    def _check_thesis(self, symbol, direction, entry, mkt, thesis):
        """
        Check if the original thesis is still valid.
        If thesis invalidated AND position in profit → close.
        If thesis invalidated AND position in loss → tighten SL.
        """
        if not mkt:
            return None

        close = mkt.get("close", 0)
        ema200 = mkt.get("ema200", 0)
        rsi = mkt.get("rsi", 50)

        if close <= 0 or ema200 <= 0:
            return None

        invalidated = False
        reasons = []

        if direction == "SHORT":
            # Short thesis broken if price reclaims above EMA200 on 15m
            if close > ema200:
                invalidated = True
                reasons.append("price reclaimed above EMA200")

            # RSI showing strong bullish momentum
            if rsi > 65:
                reasons.append(f"RSI {rsi:.0f} bullish")

        elif direction == "LONG":
            if close < ema200:
                invalidated = True
                reasons.append("price lost EMA200")
            if rsi < 35:
                reasons.append(f"RSI {rsi:.0f} bearish")

        if invalidated and len(reasons) >= 1:
            r_mult = self._calc_r(direction, entry, close, thesis["sl_distance"])

            if r_mult > 0:
                # In profit + thesis broken → close
                return {
                    "action": "THESIS_EXIT",
                    "symbol": symbol,
                    "reason": "; ".join(reasons),
                    "r_multiple": round(r_mult, 2),
                    "recommendation": "CLOSE",
                    "time": datetime.now().isoformat(),
                }
            else:
                # In loss + thesis broken → tighten SL (move closer by 30%)
                current_sl = thesis["current_sl"]
                tighter = self._tighten_sl(direction, close, current_sl, 0.3)
                if tighter and tighter != current_sl:
                    self._modify_sl(symbol, tighter, direction)
                    thesis["current_sl"] = tighter
                    return {
                        "action": "THESIS_TIGHTEN",
                        "symbol": symbol,
                        "reason": "; ".join(reasons),
                        "new_sl": tighter,
                        "time": datetime.now().isoformat(),
                    }
        return None

    def _check_force_exit(self, symbol, direction, entry, price, atr,
                          r_mult, mkt, orderflow_engine, thesis):
        """
        Emergency exit on adverse conditions:
        - ATR spike (panic/news event)
        - Adverse candle > 2.5x ATR
        - Orderflow strongly against position
        """
        entry_atr = thesis["atr_at_entry"]
        alerts = []

        # ATR spike
        if entry_atr > 0 and atr > entry_atr * ATR_SPIKE_MULT:
            alerts.append(f"ATR spike {atr/entry_atr:.1f}x")

        # Adverse move check (candle range vs normal ATR)
        candle_range = mkt.get("candle_range", 0)
        if candle_range > 0 and entry_atr > 0:
            if candle_range > entry_atr * ADVERSE_CANDLE_MULT:
                # Check if move is against us
                if direction == "LONG" and price < entry:
                    alerts.append(f"adverse candle {candle_range/entry_atr:.1f}x ATR")
                elif direction == "SHORT" and price > entry:
                    alerts.append(f"adverse candle {candle_range/entry_atr:.1f}x ATR")

        # Orderflow veto
        if orderflow_engine:
            of = orderflow_engine.get_signal(symbol)
            if not of.get("stale", True):
                of_sig = of.get("signal", "neutral")
                of_conf = of.get("confidence", 0)

                if of_conf >= ORDERFLOW_VETO_CONF:
                    if direction == "LONG" and of_sig == "bear":
                        alerts.append(f"OF bear conf:{of_conf:.2f}")
                    elif direction == "SHORT" and of_sig == "bull":
                        alerts.append(f"OF bull conf:{of_conf:.2f}")

        if len(alerts) >= 2:
            # Multiple adverse signals → force exit
            return {
                "action": "FORCE_EXIT",
                "symbol": symbol,
                "alerts": alerts,
                "r_multiple": round(r_mult, 2),
                "recommendation": "CLOSE",
                "time": datetime.now().isoformat(),
            }
        elif len(alerts) == 1 and r_mult > 0.5:
            # One alert + in decent profit → close to protect
            return {
                "action": "PROTECTIVE_EXIT",
                "symbol": symbol,
                "alerts": alerts,
                "r_multiple": round(r_mult, 2),
                "recommendation": "CLOSE",
                "time": datetime.now().isoformat(),
            }

        return None

    # ── Helpers ────────────────────────────────────────────────

    def _calc_r(self, direction, entry, price, sl_dist):
        """Calculate R-multiple of current move."""
        if sl_dist <= 0:
            return 0
        if direction == "LONG":
            return (price - entry) / sl_dist
        return (entry - price) / sl_dist

    def _tighten_sl(self, direction, price, current_sl, factor):
        """Move SL closer to price by factor (0-1)."""
        gap = abs(price - current_sl)
        move = gap * factor

        if direction == "LONG":
            return round(current_sl + move, 6)
        else:
            return round(current_sl - move, 6)

    def _modify_sl(self, symbol, new_sl, direction):
        """
        Modify stop loss on exchange.
        Uses Bybit's set_trading_stop endpoint.
        """
        try:
            side = "Buy" if direction == "LONG" else "Sell"
            self.exchange.client.set_trading_stop(
                category="linear",
                symbol=symbol,
                positionIdx=0,
                stopLoss=str(round(new_sl, 4) if new_sl > 1 else round(new_sl, 6)),
            )
            print(f"  [PM] {symbol} SL moved to {new_sl}")
            return True
        except Exception as e:
            print(f"  [PM] Failed to modify SL for {symbol}: {e}")
            return False

    def execute_action(self, action):
        """Execute a position management action."""
        rec = action.get("recommendation", "")
        symbol = action["symbol"]

        if rec == "CLOSE":
            return self._close_position(symbol)

        return False

    def _close_position(self, symbol):
        """Close a position by market order."""
        try:
            positions = self.exchange.get_positions()
            for pos in positions:
                if pos["symbol"] == symbol:
                    side = pos["side"]
                    size = pos["size"]
                    close_side = "Sell" if side == "Buy" else "Buy"

                    oid = self.exchange.place_order(
                        symbol=symbol,
                        side=close_side,
                        qty=size,
                    )
                    if oid:
                        print(f"  [PM] CLOSED {symbol} | ID: {oid}")
                        # Clean up thesis
                        self.theses.pop(symbol, None)
                        return True
            return False
        except Exception as e:
            print(f"  [PM] Close failed {symbol}: {e}")
            return False

    def remove_thesis(self, symbol):
        """Remove thesis when position closes (TP/SL hit)."""
        self.theses.pop(symbol, None)

    def get_summary(self):
        """Get summary of managed positions."""
        return {
            symbol: {
                "direction": t["direction"],
                "breakeven": t["breakeven_done"],
                "locked": t["lock_done"],
                "current_sl": t["current_sl"],
            }
            for symbol, t in self.theses.items()
        }
