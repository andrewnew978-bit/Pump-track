# PumpFun Tracker

A real-time pump.fun launch tracker that watches every new token, scores it across 23 strategies, monitors entered positions live, and self-optimises strategy weights nightly.

---

## What it does

- **Watches every new token** the moment it launches on pump.fun
- **Scores it over a 2-minute window** using 23 strategies (whale activity, dev behaviour, social signals, volume patterns, wallet history, bundle detection etc.)
- **Enters** if score ≥ 60/100
- **Monitors entered positions in real-time** — exits on take profit (2x), stop loss (50%), dev dump, sell spike, peak reversal, graduation, or 24h timeout
- **Records dev wallet history** — detects serial ruggers
- **Handles graduation events** — auto-exits when a token fills its bonding curve and moves to Raydium
- **Nightly optimiser** adjusts strategy weights based on actual win rate data
- **Web dashboard** with Live Feed, Trade Log, Pumpers, Entries, Missed Winners, Confirmed Wins, Strategy Performance, Score Bands, Portfolio Simulation, Market Conditions
- **AI Export** — one-click download of all trade data as a structured markdown file ready to paste into Claude/GPT-4 for strategy improvement

---

## Quick start

### Requirements
- Python 3.10+
- Free accounts / API keys: none required to start (Telegram alerts optional)

### Install
```bash
pip install -r requirements.txt
```

### Run locally
```bash
# Terminal 1 — tracker (the actual engine)
python tracker.py

# Terminal 2 — dashboard
uvicorn dashboard:app --host 0.0.0.0 --port 8080
```

Then open `http://localhost:8080`

### Run on Railway / Render / Heroku
The `Procfile` handles both processes:
```
web:    uvicorn dashboard:app --host 0.0.0.0 --port $PORT
worker: python -u tracker.py
```

---

## Configuration

All tuneable values are in `CONFIG` at the top of `tracker.py`:

| Key | Default | Description |
|-----|---------|-------------|
| `WATCH_DEPTH_SECONDS` | 120 | How long to watch each new token before scoring |
| `MIN_SCORE_HIGHLIGHT` | 60 | Score threshold to trigger an entry signal |
| `WINNER_MC_THRESHOLD_SOL` | 133 | Peak MC (SOL) to count as a "winner" (~$20k at $150/SOL) |
| `TAKE_PROFIT_X` | 2.0 | Exit multiplier for take profit |
| `STOP_LOSS_PCT` | 0.50 | Exit if MC drops to this fraction of entry MC |
| `MAX_HOLD_SEC` | 86400 | Force-exit after this many seconds (24h) |
| `SELL_SPIKE_PCT` | 0.70 | Exit if >70% of last 20 trades are sells |
| `PEAK_REVERSAL_PCT` | 0.30 | Exit if fell 30%+ from peak AND below entry |
| `NEG_PENALTY_MULTIPLIER` | 6.67 | Penalty per fired negative signal (6.67 ≈ 100/15) |
| `BET_SIZE_SOL` | 0.1 | Bet size used in portfolio simulation |
| `TELEGRAM_BOT_TOKEN` | `""` | Fill in to enable Telegram alerts |
| `TELEGRAM_CHAT_ID` | `""` | Fill in to enable Telegram alerts |

---

## The 23 strategies

### Positive signals (+score)
1. **Whale Sniffer** — ≥2 wallets buying ≥1 SOL in first 30s
2. **Micro Bot Swarm** — ≥20 micro-wallets in first 5s
3. **Holder Velocity** — ≥10 unique wallets/min
4. **Volume Consistency** — buy activity spread across ≥4 of 12 time windows
5. **Dev Hands Off** — developer wallet didn't sell
6. **Strong First Buy** — first trade ≥0.5 SOL
7. **Buy/Sell Ratio** — ≥75% of transactions are buys
8. **Fast to 50 Holders** — 50 unique buyers in ≤90s
9. **Repeat Buyers** — ≥3 wallets bought 2+ times
10. **Strong Early Volume** — ≥2 SOL buy volume in first 30s
11. **Has Twitter** — Twitter linked
12. **Has Telegram** — Telegram linked
13. **Peak Launch Time** — launched 13:00–21:59 UTC (US hours)
14. **Short Ticker** — 2–5 character ticker
15. **Dev Strong Init Buy** — dev initial buy ≥0.3 SOL
16. **Proven Dev** *(strategy 22)* — dev wallet has ≥1 previous winning launch

### Negative signals (−score)
17. **Early Dev Dump** — dev sold during watch window
18. **Wallet Dominance** — one wallet >40% of buy volume
19. **Wash Trading** — ≥3 wallets both bought and sold
20. **No Socials** — no Twitter AND no Telegram
21. **Bundle Detected** — ≥5 buys in first 1 second
22. **Early Sell Pressure** — >40% of transactions are sells
23. **Known Rugger** *(strategy 23)* — dev rugged ≥50% of previous launches

---

## Real-time exit signals

For every entered position, the tracker re-evaluates every 15 seconds:

| Signal | Condition |
|--------|-----------|
| `TAKE_PROFIT_2X` | MC reaches 2× entry MC |
| `STOP_LOSS` | MC drops to 50% of entry MC |
| `TIME_LIMIT` | Held for 24h |
| `DEV_DUMP` | Dev wallet sells post-entry |
| `SELL_SPIKE` | >70% of last 20 trades are sells |
| `PEAK_REVERSAL` | Fell 30%+ from peak AND below entry MC |
| `GRADUATED` | Token fills bonding curve, moves to Raydium |

---

## Database

SQLite (`tracker.db`) with these tables:

- `tokens` — every token seen, with score and strategy results
- `trades` — every trade received from the WebSocket
- `entries` — every entry signal with entry/exit MC and P&L
- `snapshots` — periodic MC checkpoints at 10m/30m/1h/6h
- `strategy_performance` — nightly win rate stats per strategy
- `dev_history` — per-wallet launch outcomes
- `graduations` — tokens that completed the bonding curve

---

## AI Export

Click **🤖 Export** in the dashboard header to download a `.md` file containing:
- All entered trades with full strategy breakdown
- All missed winners with exact signal-level explanation of why they were missed
- Score band win rates
- Strategy win rate table
- Dev wallet history
- A ready-to-paste prompt for Claude/GPT-4

---

## What's not built yet (next steps)

1. **Jupiter swap integration** — the system signals entries but doesn't execute real trades. This is the most important missing piece.
2. **Birdeye/Helius price polling** — live price feed for open positions so exit logic doesn't go blind when WebSocket trades dry up
3. **Telegram alerts** — fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in CONFIG (uses aiohttp, add to requirements if enabling)

---

## File structure

```
tracker.py      — main engine (WebSocket, scoring, exit logic, DB)
dashboard.py    — FastAPI web dashboard
Procfile        — for Railway/Heroku/Render deployment
requirements.txt
README.md
```
