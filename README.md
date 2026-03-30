# ALADDIN BOT v2 — Multi-Timeframe + Sentiment

## What changed from Phase 1

**Phase 1:** 15m only → EMA200 + RSI + Volume + ATR → trade
**Phase 2:** 1D trend → 4H structure → 15m entry → sentiment filter → trade

### New modules
- `strategy.py` — merged MTF signal check (`check_mtf_signal()`)
- `indicators.py` — daily trend + 4H structure indicators
- `exchange.py` — `get_mtf_klines()`, `get_sentiment()` (funding + OI)
- `coinglass_data.py` — optional Coinglass API (OI, funding, L/S ratio, liquidation map)
- `bot.py` — full MTF pipeline with confidence-adjusted sizing

### Signal flow
```
1D EMA50/200 + RSI → BULLISH / BEARISH / NEUTRAL
         ↓
4H EMA20/50 + RSI  → BULLISH / BEARISH / NEUTRAL
         ↓
MTF Filter → confidence: HIGH / MEDIUM / LOW / BLOCKED
         ↓
15m EMA200 + RSI + Volume + ATR → LONG / SHORT / none
         ↓
Sentiment → funding rate + OI trend (Bybit free)
         ↓
Coinglass → OI + funding + L/S ratio + liquidation map (optional)
         ↓
Risk Manager → position size × confidence multiplier
         ↓
Execute
```

### Confidence sizing
| Confidence | Position | Leverage |
|-----------|----------|----------|
| HIGH      | 100%     | default+1 |
| MEDIUM    | 70%      | default   |
| LOW       | 50%      | default-2 |
| BLOCKED   | 0%       | no trade  |

## Setup

```bash
pip install -r requirements.txt
```

### .env
```
BYBIT_API_KEY=your_key
BYBIT_API_SECRET=your_secret
MODE=testnet

# Optional — Coinglass (free tier works)
# Register: https://www.coinglass.com → Account → API
COINGLASS_API_KEY=your_coinglass_key
```

### Run
```bash
python bot.py
```

### Test individual modules
```bash
python exchange.py    # test connection + MTF klines + sentiment
python coinglass_data.py  # test Coinglass (needs API key)
```

## Files
```
bot.py            — main loop (MTF pipeline)
config.py         — all settings
exchange.py       — Bybit API (klines, orders, sentiment)
strategy.py       — MTF signal logic
indicators.py     — TA indicators (daily/4H/15m)
risk_manager.py   — position sizing, daily limits
logger.py         — trade logging
coinglass_data.py — Coinglass API (optional)
fix_ssl.py        — SSL fix for some environments
```
