"""
ALADDIN BOT v4 — Risk Manager
Portfolio-level risk: heat cap, margin check, vol-adjusted sizing.
"""
import config
import time
from datetime import datetime, timedelta

# Portfolio limits
MAX_PORTFOLIO_HEAT_PCT = 2.5   # Max total open risk as % of balance
MARGIN_RESERVE_PCT = 15        # Keep 15% balance as buffer


class RiskManager:
    def __init__(self):
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.last_reset_date = datetime.now().date()
        self.trades_today = 0
        self.stopped = False
        self.stop_until = None
        self.open_position_count = 0
        print("[RISK] Risk Manager initialized")

    def reset_daily(self):
        today = datetime.now().date()
        if today != self.last_reset_date:
            print(f"[RISK] New day — reset")
            self.daily_pnl = 0.0
            self.trades_today = 0
            self.last_reset_date = today
            if self.stop_until and datetime.now() > self.stop_until:
                self.stopped = False
                self.stop_until = None
                self.consecutive_losses = 0

    def can_trade(self, balance):
        self.reset_daily()
        if self.stopped:
            rem = f" until {self.stop_until.strftime('%H:%M')}" if self.stop_until else ""
            return False, f"STOPPED{rem}"
        max_loss = balance * (config.MAX_DAILY_LOSS_PCT / 100)
        if abs(self.daily_pnl) >= max_loss and self.daily_pnl < 0:
            self._activate_stop()
            return False, f"Daily loss limit ${abs(self.daily_pnl):.2f}>=${max_loss:.2f}"
        if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            self._activate_stop()
            return False, f"Consecutive losses: {self.consecutive_losses}"
        # Position limits handled by portfolio_brain, not here
        return True, "OK"

    def calculate_position_size(self, balance, atr, entry_price, leverage=None):
        if leverage is None:
            leverage = config.DEFAULT_LEVERAGE

        sl_distance = atr * config.ATR_SL_MULT
        sl_pct = sl_distance / entry_price

        max_margin = balance * (config.MAX_POSITION_PCT / 100)
        max_loss = balance * (config.MAX_DAILY_LOSS_PCT / 100)
        risk_margin = max_loss / (sl_pct * leverage) if sl_pct > 0 else max_margin
        margin = min(max_margin, risk_margin)

        # Vol-adjusted: if ATR is high relative to price, reduce size
        atr_pct = (atr / entry_price) * 100
        if atr_pct > 2.0:
            vol_mult = 2.0 / atr_pct  # shrink proportionally
            margin *= max(0.3, vol_mult)  # floor at 30%

        notional = margin * leverage
        qty = notional / entry_price

        return {
            "margin": round(margin, 2),
            "qty": qty,
            "leverage": leverage,
            "notional": round(notional, 2),
            "sl_distance": sl_distance,
            "sl_pct": round(sl_pct * 100, 2),
            "max_loss": round(margin * sl_pct * leverage, 2),
            "max_loss_pct": round(margin * sl_pct * leverage / balance * 100, 2) if balance > 0 else 0,
        }

    def check_portfolio_heat(self, balance, open_positions, new_margin, new_sl_pct, new_leverage):
        """
        Check if adding this trade exceeds portfolio heat cap.
        Returns (ok, reason).
        """
        # Estimate current open risk
        # Rough: each position risks ~max_loss_pct of balance
        # For simplicity: count existing positions * avg risk
        existing_risk_pct = len(open_positions) * (config.MAX_DAILY_LOSS_PCT / 100) * 100
        new_risk_pct = (new_margin * new_sl_pct / 100 * new_leverage) / balance * 100 if balance > 0 else 0
        total_heat = existing_risk_pct + new_risk_pct

        if total_heat > MAX_PORTFOLIO_HEAT_PCT:
            return False, f"Portfolio heat {total_heat:.1f}% > {MAX_PORTFOLIO_HEAT_PCT}%"
        return True, f"Heat {total_heat:.1f}%"

    def check_margin_available(self, balance, open_positions, required_margin):
        """
        Pre-trade margin check. Ensure we have enough free margin.
        Returns (ok, reason).
        """
        # Estimate used margin from open positions
        used_margin = 0
        for pos in open_positions:
            # Rough estimate: position value / leverage
            pos_value = pos.get("size", 0) * pos.get("entryPrice", 0)
            pos_leverage = float(pos.get("leverage", config.DEFAULT_LEVERAGE))
            if pos_leverage > 0:
                used_margin += pos_value / pos_leverage

        reserve = balance * (MARGIN_RESERVE_PCT / 100)
        available = balance - used_margin - reserve

        if required_margin > available:
            return False, f"Margin ${required_margin:.2f} > available ${available:.2f} (used:${used_margin:.0f} reserve:${reserve:.0f})"
        return True, f"Available ${available:.2f}"

    def record_trade(self, pnl):
        self.daily_pnl += pnl
        self.trades_today += 1
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def position_opened(self):
        self.open_position_count += 1

    def position_closed(self):
        self.open_position_count = max(0, self.open_position_count - 1)

    def _activate_stop(self):
        self.stopped = True
        self.stop_until = datetime.now() + timedelta(hours=config.COOLDOWN_AFTER_STOP)
        print(f"[RISK] STOPPED until {self.stop_until.strftime('%H:%M')}")

    def get_status(self, balance):
        max_daily = balance * (config.MAX_DAILY_LOSS_PCT / 100)
        can, reason = self.can_trade(balance)
        return {
            "can_trade": can, "reason": reason,
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_limit": round(max_daily, 2),
            "consecutive_losses": self.consecutive_losses,
            "trades_today": self.trades_today,
            "open_positions": self.open_position_count,
            "stopped": self.stopped,
        }
