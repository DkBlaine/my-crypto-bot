"""
ALADDIN BOT — Logger
Records all trades and events to file + console.
"""
import os
import json
from datetime import datetime


class Logger:
    def __init__(self, log_file="logs/trades.log"):
        self.log_file = log_file
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        print(f"[LOG] Logger initialized → {log_file}")

    def log_signal(self, symbol, signal_data):
        """Log a detected signal."""
        entry = {
            "time": datetime.now().isoformat(),
            "type": "SIGNAL",
            "symbol": symbol,
            **signal_data,
        }
        self._write(entry)
        print(f"[SIGNAL] {symbol} | {signal_data['signal']} | Entry:{signal_data['entry']} SL:{signal_data['sl']} TP:{signal_data['tp']} | RSI:{signal_data['rsi']:.1f} | {signal_data['reason']}")

    def log_trade_open(self, symbol, side, entry, sl, tp, margin, leverage, qty):
        """Log when a trade is opened."""
        entry_data = {
            "time": datetime.now().isoformat(),
            "type": "OPEN",
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "margin": margin,
            "leverage": leverage,
            "qty": qty,
        }
        self._write(entry_data)
        print(f"[TRADE] OPENED {side} {symbol} | Entry:{entry} SL:{sl} TP:{tp} | Margin:${margin} {leverage}x")

    def log_trade_close(self, symbol, side, entry, exit_price, pnl, reason):
        """Log when a trade is closed."""
        entry_data = {
            "time": datetime.now().isoformat(),
            "type": "CLOSE",
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "exit": exit_price,
            "pnl": pnl,
            "reason": reason,
        }
        self._write(entry_data)
        color = "+" if pnl >= 0 else ""
        print(f"[TRADE] CLOSED {side} {symbol} | {color}${pnl:.2f} | {reason}")

    def log_risk_event(self, event, details=""):
        """Log risk management events."""
        entry = {
            "time": datetime.now().isoformat(),
            "type": "RISK",
            "event": event,
            "details": details,
        }
        self._write(entry)
        print(f"[RISK] {event}: {details}")

    def log_scan(self, results):
        """Log market scan results (summary)."""
        signals = [r for r in results if r is not None]
        print(f"[SCAN] Scanned {len(results)} pairs | {len(signals)} signals found")

    def _write(self, data):
        """Write entry to log file."""
        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(data, default=_json_fix) + "\n")
        except Exception as e:
            print(f"[LOG] Write error: {e}")


def _json_fix(obj):
    """Handle numpy types for JSON serialization."""
    import numpy as np
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)
