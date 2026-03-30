"""
ALADDIN BOT — Orderflow Engine
Ultra-low-latency aggressive flow detection via Binance Futures WebSocket.

WHY BINANCE, NOT BYBIT:
  Binance Futures has the deepest liquidity in crypto. Orderflow from there
  is more informative than any other venue. Your bot trades Bybit, but reads
  flow from Binance — this is standard practice (cross-exchange signal).

WHY YOU CANNOT FRONT-RUN WHALES:
  Public trade stream = already executed trades. By the time you see a whale
  buy, the price already moved. You're seeing the PAST, not the future.
  What you CAN do: detect sustained aggressive flow and use it as confirmation
  that momentum is real, not just a wick.

HOW TO REDUCE LATENCY:
  1. VPS in Tokyo or Singapore (near Binance matching engine)
  2. WebSocket only, no REST polling
  3. Lightweight processing (deque, no pandas in hot path)
  4. Pre-computed thresholds, no heavy math per tick

ARCHITECTURE:
  WebSocket → buffer (deque, last 60s) → aggregation (10s/30s/60s) → signal

USAGE:
  engine = OrderflowEngine(symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
  await engine.start()
  snap = engine.get_snapshot("BTCUSDT")
  sig = engine.get_signal("BTCUSDT")
  await engine.stop()

Or synchronous wrapper for integration with sync bot:
  engine = OrderflowEngine(symbols=[...])
  engine.start_background()   # starts asyncio in thread
  snap = engine.get_snapshot("BTCUSDT")
  engine.stop_background()
"""
import asyncio
import json
import time
import threading
from collections import deque, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import logging

logger = logging.getLogger("orderflow")

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

# Whale thresholds (USD notional)
WHALE_THRESHOLD = {
    "BTCUSDT": 100_000,
    "ETHUSDT": 50_000,
    "SOLUSDT": 25_000,
    "DEFAULT": 25_000,
}

# Burst: volume in window vs baseline multiplier
BURST_MULT = 3.0
BURST_WINDOW_SEC = 10
BASELINE_WINDOW_SEC = 300  # 5 min baseline

# Imbalance thresholds
IMBALANCE_STRONG = 0.65   # 65% one side = strong signal
IMBALANCE_EXTREME = 0.75  # 75% = extreme

# Signal confidence mapping
SIGNAL_THRESHOLDS = {
    "strong_bull": 0.8,
    "bull": 0.5,
    "neutral_low": 0.2,
    "neutral_high": -0.2,
    "bear": -0.5,
    "strong_bear": -0.8,
}

# Stale detection
STALE_TIMEOUT_SEC = 5  # No data for 5s = stale

# Reconnect
RECONNECT_DELAY_SEC = 3
MAX_RECONNECT_ATTEMPTS = 10

# Buffer
MAX_BUFFER_SEC = 120  # Keep last 2 minutes of trades


# ═══════════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════════

@dataclass
class Trade:
    """Single trade from Binance stream."""
    timestamp: float    # seconds
    price: float
    qty: float
    notional: float     # price * qty
    is_buyer_aggressor: bool  # True = market buy (taker buy)

    __slots__ = ['timestamp', 'price', 'qty', 'notional', 'is_buyer_aggressor']


@dataclass
class Snapshot:
    """Aggregated orderflow snapshot for a symbol."""
    symbol: str
    timestamp: float
    net_flow_10s: float = 0.0   # buy_vol - sell_vol (USD) last 10s
    net_flow_30s: float = 0.0
    net_flow_60s: float = 0.0
    buy_vol_10s: float = 0.0
    sell_vol_10s: float = 0.0
    whale_buys_60s: int = 0     # count of whale buys last 60s
    whale_sells_60s: int = 0
    whale_buy_vol: float = 0.0  # total whale buy volume USD
    whale_sell_vol: float = 0.0
    imbalance: float = 0.5      # 0=all sells, 1=all buys, 0.5=balanced
    burst_score: float = 0.0    # current vol / baseline vol
    signal: str = "neutral"     # bull / bear / neutral
    confidence: float = 0.0     # 0..1
    stale: bool = True
    trade_count_60s: int = 0
    last_price: float = 0.0


# ═══════════════════════════════════════════════════════════════
# ENGINE
# ═══════════════════════════════════════════════════════════════

class OrderflowEngine:
    """
    Async WebSocket engine for Binance Futures aggTrade stream.

    Binance field `m` (buyer is maker):
      m = True  → buyer is MAKER → seller is TAKER → aggressive SELL
      m = False → buyer is TAKER → aggressive BUY
    """

    def __init__(self, symbols: List[str] = None):
        if symbols is None:
            symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

        self.symbols = [s.upper() for s in symbols]
        self.buffers: Dict[str, deque] = {
            s: deque() for s in self.symbols
        }
        self.last_trade_time: Dict[str, float] = {
            s: 0.0 for s in self.symbols
        }
        self._running = False
        self._ws_task = None
        self._loop = None
        self._thread = None

    # ── Async interface ───────────────────────────────────

    async def start(self):
        """Start WebSocket connections (async)."""
        self._running = True
        self._ws_task = asyncio.create_task(self._run_websocket())
        logger.info(f"[OF] Started for {len(self.symbols)} symbols")

    async def stop(self):
        """Stop WebSocket connections."""
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        logger.info("[OF] Stopped")

    # ── Sync interface (for integration with sync bot) ────

    def start_background(self):
        """Start in background thread (for sync code)."""
        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._running = True
            self._loop.run_until_complete(self._run_websocket())

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        # Wait for connection
        time.sleep(2)
        logger.info("[OF] Background thread started")

    def stop_background(self):
        """Stop background thread."""
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    # ── Data access ───────────────────────────────────────

    def get_snapshot(self, symbol: str) -> Snapshot:
        """Get current aggregated snapshot for a symbol."""
        symbol = symbol.upper()
        now = time.time()

        if symbol not in self.buffers:
            return Snapshot(symbol=symbol, timestamp=now, stale=True)

        buf = self.buffers[symbol]
        self._cleanup_buffer(symbol, now)

        if not buf:
            return Snapshot(symbol=symbol, timestamp=now, stale=True)

        # Stale check
        last_t = self.last_trade_time.get(symbol, 0)
        stale = (now - last_t) > STALE_TIMEOUT_SEC

        # Aggregate
        snap = self._aggregate(symbol, buf, now)
        snap.stale = stale
        return snap

    def get_signal(self, symbol: str) -> dict:
        """
        Get signal for integration with bot scoring.
        Returns: {"signal": "bull"/"bear"/"neutral", "confidence": 0..1, "stale": bool}
        """
        snap = self.get_snapshot(symbol)
        return {
            "signal": snap.signal,
            "confidence": snap.confidence,
            "stale": snap.stale,
            "net_flow_30s": snap.net_flow_30s,
            "imbalance": snap.imbalance,
            "burst_score": snap.burst_score,
            "whale_buys": snap.whale_buys_60s,
            "whale_sells": snap.whale_sells_60s,
        }

    # ── WebSocket ─────────────────────────────────────────

    async def _run_websocket(self):
        """Main WebSocket loop with reconnect."""
        attempt = 0

        while self._running and attempt < MAX_RECONNECT_ATTEMPTS:
            try:
                await self._connect_and_stream()
                attempt = 0  # Reset on clean disconnect
            except Exception as e:
                attempt += 1
                logger.warning(f"[OF] WS error (attempt {attempt}): {e}")
                if self._running:
                    await asyncio.sleep(RECONNECT_DELAY_SEC)

        if attempt >= MAX_RECONNECT_ATTEMPTS:
            logger.error("[OF] Max reconnect attempts reached")

    async def _connect_and_stream(self):
        """Connect to Binance combined stream and process messages."""
        try:
            import websockets
        except ImportError:
            logger.error("[OF] pip install websockets")
            # Fallback: try aiohttp
            await self._connect_aiohttp()
            return

        # Combined stream URL for all symbols
        streams = "/".join(f"{s.lower()}@aggTrade" for s in self.symbols)
        url = f"wss://fstream.binance.com/stream?streams={streams}"

        logger.info(f"[OF] Connecting to {url[:60]}...")

        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            logger.info("[OF] Connected to Binance Futures")

            while self._running:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    data = json.loads(msg)

                    # Combined stream wraps in {"stream": "...", "data": {...}}
                    if "data" in data:
                        self._process_trade(data["data"])
                    else:
                        self._process_trade(data)

                except asyncio.TimeoutError:
                    # No data in 10s — send ping
                    continue

    async def _connect_aiohttp(self):
        """Fallback using aiohttp if websockets not installed."""
        try:
            import aiohttp
        except ImportError:
            logger.error("[OF] Need either 'websockets' or 'aiohttp' package")
            return

        streams = "/".join(f"{s.lower()}@aggTrade" for s in self.symbols)
        url = f"wss://fstream.binance.com/stream?streams={streams}"

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url) as ws:
                logger.info("[OF] Connected via aiohttp")
                async for msg in ws:
                    if not self._running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if "data" in data:
                            self._process_trade(data["data"])

    def _process_trade(self, data: dict):
        """
        Process single aggTrade message.

        Binance aggTrade fields:
          s = symbol
          p = price (string)
          q = quantity (string)
          T = trade time (ms)
          m = buyer is maker

        CRITICAL: m=True means buyer placed limit order (maker),
        seller hit it (taker/aggressor). So:
          m=True  → aggressive SELL
          m=False → aggressive BUY
        """
        try:
            symbol = data["s"].upper()
            price = float(data["p"])
            qty = float(data["q"])
            notional = price * qty
            # m=True → buyer is maker → SELL is aggressive
            is_buyer_aggressor = not data["m"]
            ts = float(data["T"]) / 1000.0  # ms to seconds

            trade = Trade(
                timestamp=ts,
                price=price,
                qty=qty,
                notional=notional,
                is_buyer_aggressor=is_buyer_aggressor,
            )

            if symbol in self.buffers:
                self.buffers[symbol].append(trade)
                self.last_trade_time[symbol] = time.time()

        except (KeyError, ValueError) as e:
            pass  # Skip malformed messages

    # ── Aggregation ───────────────────────────────────────

    def _cleanup_buffer(self, symbol: str, now: float):
        """Remove trades older than MAX_BUFFER_SEC."""
        buf = self.buffers[symbol]
        cutoff = now - MAX_BUFFER_SEC
        while buf and buf[0].timestamp < cutoff:
            buf.popleft()

    def _aggregate(self, symbol: str, buf: deque, now: float) -> Snapshot:
        """Aggregate buffer into snapshot."""
        whale_thresh = WHALE_THRESHOLD.get(symbol, WHALE_THRESHOLD["DEFAULT"])

        # Windows
        buy_10, sell_10 = 0.0, 0.0
        buy_30, sell_30 = 0.0, 0.0
        buy_60, sell_60 = 0.0, 0.0
        whale_buys, whale_sells = 0, 0
        whale_buy_vol, whale_sell_vol = 0.0, 0.0
        burst_vol = 0.0
        baseline_vol = 0.0
        count_60 = 0
        last_price = 0.0

        t_10 = now - 10
        t_30 = now - 30
        t_60 = now - 60
        t_burst = now - BURST_WINDOW_SEC
        t_baseline = now - BASELINE_WINDOW_SEC

        for trade in buf:
            t = trade.timestamp
            n = trade.notional
            is_buy = trade.is_buyer_aggressor

            # Baseline volume (last 5 min)
            if t >= t_baseline:
                baseline_vol += n

            # 60s window
            if t >= t_60:
                count_60 += 1
                last_price = trade.price
                if is_buy:
                    buy_60 += n
                else:
                    sell_60 += n

                # Whale detection
                if n >= whale_thresh:
                    if is_buy:
                        whale_buys += 1
                        whale_buy_vol += n
                    else:
                        whale_sells += 1
                        whale_sell_vol += n

            # 30s window
            if t >= t_30:
                if is_buy:
                    buy_30 += n
                else:
                    sell_30 += n

            # 10s window
            if t >= t_10:
                if is_buy:
                    buy_10 += n
                else:
                    sell_10 += n

            # Burst window
            if t >= t_burst:
                burst_vol += n

        # Calculations
        net_10 = buy_10 - sell_10
        net_30 = buy_30 - sell_30
        net_60 = buy_60 - sell_60

        total_60 = buy_60 + sell_60
        imbalance = buy_60 / total_60 if total_60 > 0 else 0.5

        # Burst score: current window vs baseline (normalized)
        baseline_per_sec = baseline_vol / BASELINE_WINDOW_SEC if BASELINE_WINDOW_SEC > 0 else 0
        burst_per_sec = burst_vol / BURST_WINDOW_SEC if BURST_WINDOW_SEC > 0 else 0
        burst_score = burst_per_sec / baseline_per_sec if baseline_per_sec > 0 else 0

        # Generate signal
        signal, confidence = self._calc_signal(
            net_10, net_30, net_60, imbalance, burst_score,
            whale_buys, whale_sells, whale_buy_vol, whale_sell_vol,
        )

        return Snapshot(
            symbol=symbol,
            timestamp=now,
            net_flow_10s=round(net_10, 2),
            net_flow_30s=round(net_30, 2),
            net_flow_60s=round(net_60, 2),
            buy_vol_10s=round(buy_10, 2),
            sell_vol_10s=round(sell_10, 2),
            whale_buys_60s=whale_buys,
            whale_sells_60s=whale_sells,
            whale_buy_vol=round(whale_buy_vol, 2),
            whale_sell_vol=round(whale_sell_vol, 2),
            imbalance=round(imbalance, 4),
            burst_score=round(burst_score, 2),
            signal=signal,
            confidence=round(confidence, 3),
            trade_count_60s=count_60,
            last_price=last_price,
        )

    def _calc_signal(self, net_10, net_30, net_60, imbalance, burst,
                     whale_buys, whale_sells, whale_buy_vol, whale_sell_vol):
        """
        Calculate composite signal from orderflow metrics.
        Returns (signal: str, confidence: 0..1)

        Logic:
          - Net flow direction across timeframes
          - Imbalance strength
          - Whale activity
          - Burst confirms momentum

        Scoring: -1.0 (strong bear) to +1.0 (strong bull)
        """
        score = 0.0

        # Net flow consensus (weighted: recent > older)
        if net_10 > 0: score += 0.15
        elif net_10 < 0: score -= 0.15

        if net_30 > 0: score += 0.20
        elif net_30 < 0: score -= 0.20

        if net_60 > 0: score += 0.10
        elif net_60 < 0: score -= 0.10

        # Imbalance (strong signal)
        if imbalance > IMBALANCE_EXTREME:
            score += 0.25
        elif imbalance > IMBALANCE_STRONG:
            score += 0.15
        elif imbalance < (1 - IMBALANCE_EXTREME):
            score -= 0.25
        elif imbalance < (1 - IMBALANCE_STRONG):
            score -= 0.15

        # Whale activity
        whale_diff = whale_buys - whale_sells
        if whale_diff >= 2:
            score += 0.20
        elif whale_diff >= 1:
            score += 0.10
        elif whale_diff <= -2:
            score -= 0.20
        elif whale_diff <= -1:
            score -= 0.10

        # Whale volume tilt
        total_whale = whale_buy_vol + whale_sell_vol
        if total_whale > 0:
            whale_tilt = (whale_buy_vol - whale_sell_vol) / total_whale
            score += whale_tilt * 0.10

        # Burst amplifier (doesn't change direction, amplifies confidence)
        burst_mult = min(burst / BURST_MULT, 1.0) if burst > 1.0 else 0.0

        # Final
        raw_confidence = abs(score)
        if burst_mult > 0:
            raw_confidence = min(1.0, raw_confidence + burst_mult * 0.15)

        confidence = min(1.0, raw_confidence)

        if score > 0.2:
            signal = "bull"
        elif score < -0.2:
            signal = "bear"
        else:
            signal = "neutral"

        return signal, confidence


# ═══════════════════════════════════════════════════════════════
# INTEGRATION HELPERS
# ═══════════════════════════════════════════════════════════════

def orderflow_score_adjustment(of_signal: dict, trade_direction: str) -> int:
    """
    Convert orderflow signal to score adjustment for bot strategy.

    Returns:
      +1  if orderflow confirms direction
       0  if neutral or stale
      -1  if orderflow contradicts (use as veto or size reduction)

    Integration in strategy.py:
      adjustment = orderflow_score_adjustment(of.get_signal("BTCUSDT"), "LONG")
      total_score += adjustment
    """
    if of_signal.get("stale", True):
        return 0

    sig = of_signal.get("signal", "neutral")
    conf = of_signal.get("confidence", 0)

    if conf < 0.3:
        return 0  # Low confidence = ignore

    if trade_direction == "LONG":
        if sig == "bull":
            return 1   # Confirmed
        elif sig == "bear" and conf > 0.5:
            return -1  # Veto
    elif trade_direction == "SHORT":
        if sig == "bear":
            return 1   # Confirmed
        elif sig == "bull" and conf > 0.5:
            return -1  # Veto

    return 0


# ═══════════════════════════════════════════════════════════════
# STANDALONE TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    engine = OrderflowEngine(symbols=symbols)

    async def main():
        await engine.start()
        print(f"\nMonitoring orderflow for {symbols}...")
        print("Press Ctrl+C to stop\n")

        try:
            while True:
                await asyncio.sleep(5)
                for sym in symbols:
                    snap = engine.get_snapshot(sym)
                    stale_mark = " STALE" if snap.stale else ""
                    print(
                        f"  {sym:12s} | "
                        f"net10s:{snap.net_flow_10s:>+12,.0f} | "
                        f"net30s:{snap.net_flow_30s:>+12,.0f} | "
                        f"W:{snap.whale_buys_60s}B/{snap.whale_sells_60s}S | "
                        f"imb:{snap.imbalance:.2f} | "
                        f"burst:{snap.burst_score:.1f}x | "
                        f"{snap.signal:7s} conf:{snap.confidence:.2f}"
                        f"{stale_mark}"
                    )
                print()
        except KeyboardInterrupt:
            await engine.stop()
            print("\nStopped.")

    asyncio.run(main())
