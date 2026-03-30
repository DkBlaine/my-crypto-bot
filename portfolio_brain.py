"""
ALADDIN BOT — Portfolio Brain
Dynamic position management with persistence across restarts.

- No fixed max positions
- Risk limited by total R exposure
- Weak positions rotated out for stronger candidates
- Size determined by signal strength
- Saves internal state to disk so restart does not wipe memory
"""
import json
from datetime import datetime
from pathlib import Path


# ── Risk Config ──────────────────────────────────────────────
R_PCT = 2.0              # 1R = 2% of balance
MAX_TOTAL_R = 6.0        # Max portfolio risk
MAX_CLUSTER_R = 3.0      # Max per correlation cluster
MAX_DIR_R = 4.0          # Max in one direction
ROTATION_GAP = 2         # Candidate must beat position by 2+ points
HOLD_MIN_SCORE = 5       # Below → force close
DECAY_SCORE = 6          # Below + in profit → take profit and close

STATE_PATH = Path("data/portfolio_brain_state.json")
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)


def score_to_r(score: int) -> float:
    if score >= 12:
        return 1.0
    if score >= 10:
        return 0.7
    if score >= 8:
        return 0.4
    return 0.0


CLUSTERS = {
    "BTC": ["BTCUSDT"],
    "ETH": ["ETHUSDT"],
    "ALTS": [
        "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
        "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    ],
}


def _cluster_of(symbol: str) -> str:
    for name, syms in CLUSTERS.items():
        if symbol in syms:
            return name
    return "OTHER"


class Position:
    __slots__ = [
        "symbol", "direction", "entry_price", "sl", "tp", "atr",
        "entry_score", "current_score", "size_r",
        "current_price", "pnl", "opened_at", "reasons", "recovered",
    ]

    def __init__(
        self,
        symbol,
        direction,
        entry_price,
        sl,
        tp,
        atr,
        entry_score,
        size_r,
        reasons=None,
        recovered=False,
    ):
        self.symbol = symbol
        self.direction = direction
        self.entry_price = float(entry_price or 0.0)
        self.sl = float(sl or 0.0)
        self.tp = float(tp or 0.0)
        self.atr = float(atr or 0.0)
        self.entry_score = int(entry_score or 0)
        self.current_score = int(entry_score or 0)
        self.size_r = float(size_r or 0.0)
        self.current_price = self.entry_price
        self.pnl = 0.0
        self.opened_at = datetime.now()
        self.reasons = reasons or []
        self.recovered = bool(recovered)

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "sl": self.sl,
            "tp": self.tp,
            "atr": self.atr,
            "entry_score": self.entry_score,
            "current_score": self.current_score,
            "size_r": self.size_r,
            "current_price": self.current_price,
            "pnl": self.pnl,
            "opened_at": self.opened_at.isoformat(),
            "reasons": self.reasons,
            "recovered": self.recovered,
        }

    @classmethod
    def from_dict(cls, data):
        pos = cls(
            symbol=data["symbol"],
            direction=data["direction"],
            entry_price=data.get("entry_price", 0.0),
            sl=data.get("sl", 0.0),
            tp=data.get("tp", 0.0),
            atr=data.get("atr", 0.0),
            entry_score=data.get("entry_score", 0),
            size_r=data.get("size_r", 0.0),
            reasons=data.get("reasons", []),
            recovered=data.get("recovered", False),
        )
        pos.current_score = int(data.get("current_score", pos.entry_score))
        pos.current_price = float(data.get("current_price", pos.entry_price))
        pos.pnl = float(data.get("pnl", 0.0))
        try:
            pos.opened_at = datetime.fromisoformat(data.get("opened_at"))
        except Exception:
            pos.opened_at = datetime.now()
        return pos


class PortfolioBrain:
    def __init__(self):
        self.positions = {}
        self.action_log = []
        self.balance = 0.0
        self._load_state()

    # ── Persistence ───────────────────────────────────────────
    def _save_state(self):
        try:
            payload = {
                "positions": {sym: pos.to_dict() for sym, pos in self.positions.items()}
            }
            STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"  [BRAIN] Save state error: {e}")

    def _load_state(self):
        if not STATE_PATH.exists():
            return
        try:
            payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            raw_positions = payload.get("positions", {})
            self.positions = {sym: Position.from_dict(p) for sym, p in raw_positions.items()}
        except Exception as e:
            print(f"  [BRAIN] Load state error: {e}")
            self.positions = {}

    # ── Main decision ─────────────────────────────────────────
    def decide(self, candidates, market_scores, open_positions_exchange, balance):
        actions = []
        self.balance = float(balance or 0.0)

        self._sync_positions(open_positions_exchange)

        # Update live scores from scan
        for sym, pos in self.positions.items():
            if sym in market_scores:
                pos.current_score = int(market_scores[sym]["score"] or 0)
                if pos.entry_score <= 0:
                    pos.entry_score = pos.current_score

        # Update pnl/current price from exchange
        for ep in open_positions_exchange:
            sym = ep["symbol"]
            if sym in self.positions:
                self.positions[sym].pnl = float(ep.get("unrealisedPnl", 0) or 0)
                self.positions[sym].current_price = float(
                    ep.get("markPrice", ep.get("lastPrice", ep.get("entryPrice", 0))) or 0
                )

        # Decay / weak profit exits
        for sym, pos in list(self.positions.items()):
            if pos.current_score <= HOLD_MIN_SCORE:
                actions.append({
                    "action": "CLOSE",
                    "symbol": sym,
                    "reason": f"DECAY score {pos.current_score}<={HOLD_MIN_SCORE}",
                })
            elif pos.current_score <= DECAY_SCORE and pos.pnl > 0:
                actions.append({
                    "action": "CLOSE",
                    "symbol": sym,
                    "reason": f"WEAK_PROFIT score {pos.current_score} pnl +${pos.pnl:.2f}",
                })

        # provisional removal so new candidates see updated risk
        for a in actions:
            if a["action"] == "CLOSE":
                self.positions.pop(a["symbol"], None)

        cands = sorted(
            candidates,
            key=lambda x: (
                int(x[2].get("entry_total", 0) or 0),
                int(x[2].get("total_score", 0) or 0),
            ),
            reverse=True,
        )

        for symbol, signal, dlog in cands:
            score = int(dlog.get("total_score", 0) or 0)
            direction = signal["signal"]
            size_r = score_to_r(score)

            if size_r <= 0:
                continue

            # already have same symbol
            if symbol in self.positions:
                existing = self.positions[symbol]
                if (
                    direction == existing.direction
                    and score >= existing.entry_score + 3
                    and self._total_r() + 0.3 <= MAX_TOTAL_R
                ):
                    actions.append({
                        "action": "ADD",
                        "symbol": symbol,
                        "signal": signal,
                        "size_r": 0.3,
                        "reason": f"strengthen {existing.entry_score}→{score}",
                        "score": score,
                    })
                continue

            fit, _reason = self._can_fit(symbol, direction, size_r)
            if fit:
                actions.append({
                    "action": "OPEN",
                    "symbol": symbol,
                    "signal": signal,
                    "size_r": size_r,
                    "score": score,
                    "reason": f"score {score} → {size_r}R",
                })
                # provisional track
                self.positions[symbol] = Position(
                    symbol=symbol,
                    direction=direction,
                    entry_price=signal["entry"],
                    sl=signal["sl"],
                    tp=signal["tp"],
                    atr=signal["atr"],
                    entry_score=score,
                    size_r=size_r,
                    reasons=dlog.get("passed", []),
                    recovered=False,
                )
                continue

            weakest = self._weakest_position(direction)
            if weakest and score >= weakest.current_score + ROTATION_GAP:
                actions.append({
                    "action": "ROTATE",
                    "close_symbol": weakest.symbol,
                    "close_reason": f"score {weakest.current_score} replaced by {score}",
                    "open_symbol": symbol,
                    "signal": signal,
                    "size_r": size_r,
                    "score": score,
                })
                self.positions.pop(weakest.symbol, None)
                self.positions[symbol] = Position(
                    symbol=symbol,
                    direction=direction,
                    entry_price=signal["entry"],
                    sl=signal["sl"],
                    tp=signal["tp"],
                    atr=signal["atr"],
                    entry_score=score,
                    size_r=size_r,
                    reasons=dlog.get("passed", []),
                    recovered=False,
                )

        self.action_log.extend(actions)
        self._save_state()
        return actions

    # ── Execution ─────────────────────────────────────────────
    def execute_actions(self, actions, exchange, risk_mgr, logger, balance):
        results = []
        r_unit = float(balance or 0.0) * R_PCT / 100

        for action in actions:
            act = action["action"]

            if act == "CLOSE":
                sym = action["symbol"]
                ok = self._close_position(sym, exchange)
                print(f"  [BRAIN] CLOSE {sym} | {action['reason']} | {'ok' if ok else 'FAILED'}")
                if ok:
                    risk_mgr.position_closed()
                    self.positions.pop(sym, None)
                    self._save_state()
                results.append({"action": act, "symbol": sym, "ok": ok})

            elif act == "ROTATE":
                close_sym = action["close_symbol"]
                ok_close = self._close_position(close_sym, exchange)
                print(f"  [BRAIN] ROTATE close {close_sym} | {action['close_reason']} | {'ok' if ok_close else 'FAILED'}")
                if ok_close:
                    risk_mgr.position_closed()
                    self.positions.pop(close_sym, None)
                    self._save_state()

                    ok_open = self._open_position(
                        action["open_symbol"],
                        action["signal"],
                        action["size_r"],
                        r_unit,
                        exchange,
                        risk_mgr,
                        logger,
                        entry_score=action.get("score", 0),
                    )
                    results.append({
                        "action": "ROTATE",
                        "closed": close_sym,
                        "opened": action["open_symbol"],
                        "ok": ok_open,
                    })

            elif act == "OPEN":
                ok = self._open_position(
                    action["symbol"],
                    action["signal"],
                    action["size_r"],
                    r_unit,
                    exchange,
                    risk_mgr,
                    logger,
                    entry_score=action.get("score", 0),
                )
                results.append({"action": act, "symbol": action["symbol"], "ok": ok})

            elif act == "ADD":
                ok = self._add_to_position(
                    action["symbol"],
                    action["signal"],
                    action["size_r"],
                    r_unit,
                    exchange,
                    logger,
                )
                results.append({"action": act, "symbol": action["symbol"], "ok": ok})

        return results

    def _open_position(self, symbol, signal, size_r, r_unit, exchange, risk_mgr, logger, entry_score=0):
        direction = signal["signal"]
        margin = round(r_unit * size_r * 5, 2)
        if margin < 5:
            margin = 5

        leverage = 5
        qty = margin * leverage / signal["entry"]
        qty = exchange.round_qty(qty, symbol)

        side = "Buy" if direction == "LONG" else "Sell"
        sl = round(signal["sl"], 4) if signal["entry"] > 1 else round(signal["sl"], 6)
        tp = round(signal["tp"], 4) if signal["entry"] > 1 else round(signal["tp"], 6)
        if signal["entry"] > 1000:
            sl = round(signal["sl"], 2)
            tp = round(signal["tp"], 2)

        print(f"  [BRAIN] OPEN {symbol} {direction} | {size_r}R = ${margin} | SL:{sl} TP:{tp}")

        logger.log_signal(symbol, signal)
        oid = exchange.place_order(symbol=symbol, side=side, qty=qty, sl_price=sl, tp_price=tp)
        if oid:
            logger.log_trade_open(symbol, side, signal["entry"], sl, tp, margin, leverage, qty)
            risk_mgr.position_opened()
            self.positions[symbol] = Position(
                symbol=symbol,
                direction=direction,
                entry_price=signal["entry"],
                sl=sl,
                tp=tp,
                atr=signal["atr"],
                entry_score=int(entry_score or 0),
                size_r=size_r,
                reasons=[],
                recovered=False,
            )
            self._save_state()
            print(f"  [BRAIN] FILLED {oid}")
            return True

        print(f"  [BRAIN] ORDER FAILED {symbol}")
        self.positions.pop(symbol, None)
        self._save_state()
        return False

    def _close_position(self, symbol, exchange):
        try:
            positions = exchange.get_positions()
            for pos in positions:
                if pos["symbol"] == symbol:
                    close_side = "Sell" if pos["side"] == "Buy" else "Buy"
                    oid = exchange.place_order(symbol=symbol, side=close_side, qty=pos["size"])
                    return bool(oid)
            return False
        except Exception as e:
            print(f"  [BRAIN] Close error {symbol}: {e}")
            return False

    def _add_to_position(self, symbol, signal, add_r, r_unit, exchange, logger):
        margin = round(r_unit * add_r * 5, 2)
        if margin < 5:
            return False

        direction = signal["signal"]
        side = "Buy" if direction == "LONG" else "Sell"
        qty = margin * 5 / signal["entry"]
        qty = exchange.round_qty(qty, symbol)

        print(f"  [BRAIN] ADD {symbol} +{add_r}R = ${margin}")
        oid = exchange.place_order(symbol=symbol, side=side, qty=qty)
        if oid:
            if symbol in self.positions:
                self.positions[symbol].size_r += add_r
            self._save_state()
            print(f"  [BRAIN] ADD FILLED {oid}")
            return True
        return False

    # ── Risk checks ────────────────────────────────────────────
    def _can_fit(self, symbol, direction, size_r):
        total = self._total_r()
        if total + size_r > MAX_TOTAL_R:
            return False, f"total R {total+size_r:.1f} > {MAX_TOTAL_R}"

        dir_r = self._dir_r(direction)
        if dir_r + size_r > MAX_DIR_R:
            return False, f"dir R {dir_r+size_r:.1f} > {MAX_DIR_R}"

        cluster = _cluster_of(symbol)
        clust_r = sum(p.size_r for p in self.positions.values() if _cluster_of(p.symbol) == cluster)
        if clust_r + size_r > MAX_CLUSTER_R:
            return False, f"cluster {cluster} R {clust_r+size_r:.1f} > {MAX_CLUSTER_R}"

        return True, "ok"

    def _total_r(self):
        return sum(p.size_r for p in self.positions.values())

    def _dir_r(self, direction):
        return sum(p.size_r for p in self.positions.values() if p.direction == direction)

    def _weakest_position(self, direction=None):
        pool = list(self.positions.values())
        if direction:
            pool = [p for p in pool if p.direction == direction]
        if not pool:
            return None
        return min(pool, key=lambda p: p.current_score)

    def _sync_positions(self, exchange_positions):
        exchange_symbols = {p["symbol"] for p in exchange_positions}

        # remove stale internal positions
        for sym in list(self.positions.keys()):
            if sym not in exchange_symbols:
                self.positions.pop(sym, None)
                print(f"  [BRAIN] {sym} closed externally (TP/SL hit)")

        # recover missing exchange positions
        for ep in exchange_positions:
            sym = ep["symbol"]
            if sym in self.positions:
                continue

            side = str(ep.get("side", ""))
            direction = "LONG" if side == "Buy" else "SHORT"
            entry_price = float(ep.get("entryPrice", 0) or 0)
            mark_price = float(ep.get("markPrice", ep.get("lastPrice", entry_price)) or entry_price)
            pnl = float(ep.get("unrealisedPnl", 0) or 0)
            leverage = float(ep.get("leverage", 5) or 5)
            size = float(ep.get("size", 0) or 0)

            notional = size * entry_price
            margin = (notional / leverage) if leverage > 0 else 0.0
            r_unit = max(1.0, self.balance * (R_PCT / 100.0))
            raw_r = (margin / r_unit) if margin > 0 else 0.4

            if raw_r >= 1.0:
                approx_size_r = 1.0
            elif raw_r >= 0.7:
                approx_size_r = 0.7
            else:
                approx_size_r = 0.4

            recovered = Position(
                symbol=sym,
                direction=direction,
                entry_price=entry_price,
                sl=0.0,
                tp=0.0,
                atr=0.0,
                entry_score=0,
                size_r=min(approx_size_r, MAX_TOTAL_R),
                reasons=["recovered_from_exchange"],
                recovered=True,
            )
            recovered.current_price = mark_price
            recovered.pnl = pnl
            self.positions[sym] = recovered
            print(f"  [BRAIN] Recovered {sym} {direction} from exchange")

        self._save_state()

    def remove_position(self, symbol):
        self.positions.pop(symbol, None)
        self._save_state()

    # ── Status ─────────────────────────────────────────────────
    def print_status(self, balance):
        r_unit = balance * R_PCT / 100
        total = self._total_r()
        long_r = self._dir_r("LONG")
        short_r = self._dir_r("SHORT")

        if not self.positions:
            print(f"\n  [BRAIN] Empty | 0/{MAX_TOTAL_R}R | 1R = ${r_unit:.2f}")
            return

        print(f"\n  [BRAIN] {len(self.positions)} pos | {total:.1f}/{MAX_TOTAL_R}R | "
              f"L:{long_r:.1f}R S:{short_r:.1f}R | 1R=${r_unit:.2f}")

        for sym, pos in sorted(self.positions.items(), key=lambda x: x[1].current_score):
            pnl_s = f"+${pos.pnl:.2f}" if pos.pnl >= 0 else f"-${abs(pos.pnl):.2f}"
            score_delta = pos.current_score - pos.entry_score
            delta_s = f"{'+' if score_delta >= 0 else ''}{score_delta}"
            rec = " *" if pos.recovered else ""
            print(f"    {sym:12s} {pos.direction:5s}{rec} | {pos.size_r:.1f}R | "
                  f"entry:{pos.entry_score} now:{pos.current_score}({delta_s}) | {pnl_s}")
