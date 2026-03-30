"""
ALADDIN BOT v5 — Portfolio Brain
Dynamic position management. No fixed limits.
Brain decides: open, close, rotate, size — all based on signal strength.
"""
import time
import json
import os
from datetime import datetime
import config
from exchange import Exchange
from indicators import add_entry_indicators, add_trend_indicators, get_trend_bias
from risk_manager import RiskManager
from position_manager import PositionManager
from portfolio_brain import PortfolioBrain
from logger import Logger

try:
    from coinglass_data import get_full_analysis, COINGLASS_API_KEY
    HAS_COINGLASS = bool(COINGLASS_API_KEY)
except ImportError:
    HAS_COINGLASS = False

HAS_ORDERFLOW = False
orderflow_engine = None
if getattr(config, "USE_ORDERFLOW", False):
    try:
        from orderflow import OrderflowEngine, orderflow_score_adjustment
        HAS_ORDERFLOW = True
    except ImportError:
        pass


def run_bot():
    print("=" * 64)
    print("  ALADDIN BOT v5 — Portfolio Brain")
    print(f"  Mode: {'TESTNET' if config.TESTNET else 'MAINNET'}")
    print(f"  Pairs: {len(config.PAIRS)}")
    print(f"  MTF: {config.MTF_TIMEFRAMES['trend']}>{config.MTF_TIMEFRAMES['structure']}>{config.MTF_TIMEFRAMES['entry']}")
    print(f"  Risk: 1R=2% | Max 6R total | Max 3R/cluster")
    print(f"  Sizing: 12-13→1R | 10-11→0.7R | 8-9→0.4R")
    print("=" * 64)

    exchange = Exchange()
    risk = RiskManager()
    log = Logger()
    pos_mgr = PositionManager(exchange)
    brain = PortfolioBrain()
    dlogger = DecisionLogger()

    balance = exchange.get_balance()
    print(f"\n[BOT] Balance: ${balance:.2f}")
    if balance <= 0:
        print("[BOT] Balance is 0.")
        return

    print("[BOT] Setting leverage...")
    for pair in config.PAIRS:
        exchange.set_leverage(pair, config.DEFAULT_LEVERAGE)

    # Orderflow
    global orderflow_engine
    if HAS_ORDERFLOW:
        orderflow_engine = OrderflowEngine(symbols=config.ORDERFLOW_SYMBOLS)
        orderflow_engine.start_background()
        print(f"[BOT] Orderflow: ON ({len(config.ORDERFLOW_SYMBOLS)} symbols)")
    else:
        print(f"[BOT] Orderflow: OFF")

    htf_cache = {}
    HTF_TTL = 3600

    print(f"\n[BOT] Started.\n")

    while True:
        try:
            balance = exchange.get_balance()
            can_trade, reason = risk.can_trade(balance)
            if not can_trade:
                print(f"[BOT] Paused: {reason}")
                time.sleep(60)
                continue

            print(f"\n{'='*64}")
            print(f"  SCAN | ${balance:.2f} | {datetime.now().strftime('%H:%M')}")
            print(f"{'='*64}")

            open_positions = exchange.get_positions()

            # ── Phase 1: Scan all pairs ────────────────────
            candidates = []
            market_scores = {}

            for pair in config.PAIRS:
                try:
                    sig, dlog = _scan_pair(pair, exchange, htf_cache, HTF_TTL)
                    dlogger.log_decision(pair, dlog)

                    # Store score for brain's position tracking
                    market_scores[pair] = {
                        "score": dlog.get("total_score", 0),
                        "direction": dlog.get("direction", "?"),
                    }

                    if sig:
                        # Orderflow adjustment
                        if HAS_ORDERFLOW and orderflow_engine:
                            of_sig = orderflow_engine.get_signal(pair)
                            adj = orderflow_score_adjustment(of_sig, sig["signal"])
                            dlog["total_score"] += adj
                            if adj != 0:
                                key = "passed" if adj > 0 else "failed"
                                dlog[key].append(f"OF:{of_sig['signal']}({of_sig['confidence']:.1f})")

                        candidates.append((pair, sig, dlog))
                    else:
                        _print_nosig(pair, dlog)

                except Exception as e:
                    print(f"  [ERR] {pair}: {e}")
                time.sleep(0.3)

            # ── Phase 2: Brain decides ─────────────────────
            actions = brain.decide(candidates, market_scores,
                                   open_positions, balance)

            if candidates:
                print(f"\n  CANDIDATES ({len(candidates)}):")
                for pair, sig, dl in sorted(candidates,
                        key=lambda x: x[2].get("total_score", 0), reverse=True):
                    of_str = ""
                    if HAS_ORDERFLOW and orderflow_engine:
                        of = orderflow_engine.get_signal(pair)
                        if not of.get("stale"):
                            of_str = f" OF:{of['signal']}({of['confidence']:.1f})"
                    print(f"    {pair:12s} | {sig['signal']} | "
                          f"M:{dl['macro_total']} E:{dl['entry_total']} "
                          f"T:{dl['total_score']}/13{of_str}")

            # ── Phase 3: Execute brain's decisions ─────────
            if actions:
                print(f"\n  BRAIN ACTIONS ({len(actions)}):")
                results = brain.execute_actions(actions, exchange, risk, log, balance)
                # Refresh positions
                open_positions = exchange.get_positions()

            # ── Phase 4: Position manager (breakeven/trail) ─
            if open_positions:
                mkt_data = _get_market_data(open_positions, exchange)
                of_eng = orderflow_engine if HAS_ORDERFLOW else None
                pm_actions = pos_mgr.check_positions(open_positions, mkt_data, of_eng)

                for act in pm_actions:
                    a_type = act["action"]
                    sym = act["symbol"]
                    if a_type in ("BREAKEVEN", "LOCK_PROFIT", "THESIS_TIGHTEN"):
                        print(f"  [PM] {sym} {a_type} → SL:{act.get('new_sl','')}")
                    elif a_type in ("THESIS_EXIT", "FORCE_EXIT", "PROTECTIVE_EXIT"):
                        print(f"  [PM] {sym} {a_type} R:{act.get('r_multiple','')} | "
                              f"{act.get('alerts', act.get('reason',''))}")
                        pos_mgr.execute_action(act)
                        risk.position_closed()
                        brain.remove_position(sym)

                # Sync externally closed positions
                open_syms = {p["symbol"] for p in open_positions}
                for sym in list(pos_mgr.theses.keys()):
                    if sym not in open_syms:
                        pos_mgr.remove_thesis(sym)

            # ── Status ─────────────────────────────────────
            brain.print_status(balance)

            st = risk.get_status(balance)
            print(f"\n  Daily: ${st['daily_pnl']:.2f}/${st['daily_limit']:.2f} | "
                  f"Losses: {st['consecutive_losses']} | Trades: {st['trades_today']}")

            dlogger.save_summary(candidates)

            wait = int(config.MTF_TIMEFRAMES["entry"]) * 60
            print(f"\n  Next in {config.MTF_TIMEFRAMES['entry']}m...")
            time.sleep(wait)

        except KeyboardInterrupt:
            print("\n[BOT] Stopped.")
            break
        except Exception as e:
            print(f"[BOT] Error: {e}")
            time.sleep(30)


def _scan_pair(pair, exchange, htf_cache, htf_ttl):
    """Scan one pair. Returns (signal_or_None, dlog)."""
    now = time.time()
    cached = htf_cache.get(pair)
    if cached and (now - cached["ts"] < htf_ttl):
        df_e = exchange.get_klines(pair, interval=config.MTF_TIMEFRAMES["entry"],
                                    limit=config.CANDLE_LIMIT_ENTRY)
        mtf = {"trend": cached["trend"], "structure": cached["structure"], "entry": df_e}
    else:
        mtf = exchange.get_mtf_klines(pair)
        htf_cache[pair] = {"trend": mtf["trend"], "structure": mtf["structure"], "ts": now}

    sentiment = None
    if config.USE_SENTIMENT:
        try:
            sentiment = exchange.get_sentiment(pair)
        except Exception:
            pass

    if HAS_COINGLASS and sentiment is not None:
        try:
            price = exchange.get_ticker(pair)
            cg = get_full_analysis(pair, current_price=price)
            sentiment["coinglass_composite"] = cg.get("composite", "")
        except Exception:
            pass

    orderbook = exchange.get_orderbook(pair)
    return check_mtf_signal(mtf, orderbook=orderbook, sentiment=sentiment)


def _print_nosig(pair, dlog):
    """Compact no-signal line."""
    score = dlog.get("total_score", 0)
    ls = dlog.get("long_score", "?")
    ss = dlog.get("short_score", "?")
    dr = dlog.get("direction_reason", "")

    veto = ""
    reasons = dlog.get("reasons", [])
    fail = dlog["failed"][0] if dlog.get("failed") else ""
    if reasons:
        veto = reasons[0]
    elif fail:
        veto = fail

    display = f"{dr} | {veto[:25]}" if dr and veto and "NONE" in dr else (dr or veto or "")
    if len(display) > 55:
        display = display[:55] + ".."

    print(f"  {pair:12s} | L:{ls} S:{ss} T:{score:2d}/13 | {display}")


def _get_market_data(positions, exchange):
    """Fetch current 15m data for position management."""
    mkt = {}
    for pos in positions:
        sym = pos["symbol"]
        try:
            df = exchange.get_klines(sym, interval=config.MTF_TIMEFRAMES["entry"], limit=20)
            if df.empty or len(df) < 5:
                continue
            df = add_entry_indicators(df)
            c = df.iloc[-2]
            mkt[sym] = {
                "close": float(c["close"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "atr": float(c["atr"]),
                "rsi": float(c["rsi"]),
                "ema200": float(c["ema200"]),
                "candle_range": float(c["high"] - c["low"]),
            }
        except Exception:
            pass
        time.sleep(0.2)
    return mkt


class DecisionLogger:
    def __init__(self, path="logs/decisions.jsonl"):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def log_decision(self, pair, dlog):
        entry = {
            "time": datetime.now().isoformat(),
            "pair": pair,
            "direction": dlog.get("direction"),
            "macro_total": dlog.get("macro_total", 0),
            "entry_total": dlog.get("entry_total", 0),
            "total_score": dlog.get("total_score", 0),
            "long_score": dlog.get("long_score"),
            "short_score": dlog.get("short_score"),
            "confidence": dlog.get("confidence", "BLOCKED"),
            "decision": dlog.get("decision", "NO_TRADE"),
            "macro_scores": dlog.get("macro_scores", {}),
            "entry_scores": dlog.get("entry_scores", {}),
            "passed": dlog.get("passed", []),
            "failed": dlog.get("failed", []),
            "reasons": dlog.get("reasons", []),
            "direction_reason": dlog.get("direction_reason", ""),
        }
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass

    def save_summary(self, candidates):
        if candidates:
            print(f"\n  [LOG] Candidates: {len(candidates)} | Logged to {self.path}")


if __name__ == "__main__":
    run_bot()
