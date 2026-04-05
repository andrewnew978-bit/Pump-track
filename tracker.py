#!/usr/bin/env python3
"""
PumpFun Launch Tracker
- Watches all new tokens for 2 minutes, scores with 21 strategies
- Enters tokens scoring >= 60, monitors them in real-time post-entry
- Re-evaluates and exits if conditions change (dev dump, sell spike, reversal, TP, SL)
- Tracks everything in SQLite for the dashboard
"""

import asyncio
import json
import sqlite3
import time
import logging
import os
import csv
import io
from datetime import datetime
from collections import defaultdict
from typing import Dict, Optional, Callable
import websockets
from websockets.exceptions import ConnectionClosed

# ════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════
CONFIG = {
    "WATCH_DEPTH_SECONDS":      120,
    "WINNER_MC_THRESHOLD_SOL":  133,    # ~$20k at $150/SOL
    "PERIODIC_CHECKS_SEC":      [600, 1800, 3600, 21600],
    "PERIODIC_LABELS":          ["10m", "30m", "1h", "6h"],
    "MIN_SCORE_HIGHLIGHT":      60,
    "DB_PATH":                  "tracker.db",
    "WS_URL":                   "wss://pumpportal.fun/api/data",
    "MAX_ACTIVE_WATCHES":       150,
    "MAX_ACTIVE_MONITORS":      50,     # max real-time post-entry monitors
    "TEST_MODE":                False,

    # Scoring: 6.67 = 100/15 — one negative ≈ losing one positive signal
    "NEG_PENALTY_MULTIPLIER":   6.67,

    # Exit conditions (all checked in real-time for entered tokens)
    "TAKE_PROFIT_X":            2.0,    # exit at 2x entry MC
    "STOP_LOSS_PCT":            0.50,   # exit at 50% of entry MC
    "MAX_HOLD_SEC":             86400,  # force-exit after 24h (was 6h)
    "SELL_SPIKE_PCT":           0.70,   # exit if >70% of last 20 trades are sells
    "PEAK_REVERSAL_PCT":        0.30,   # exit if MC fell 30%+ from peak AND below entry

    # Portfolio simulation — set to your typical bet size per trade
    "BET_SIZE_SOL":             0.1,

    # Optional Telegram alerts — fill in to enable
    "TELEGRAM_BOT_TOKEN":       "",     # e.g. "123456:ABC-DEF..."
    "TELEGRAM_CHAT_ID":         "",     # e.g. "-100123456789"
}

# ════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════
log = logging.getLogger("tracker")
log.setLevel(logging.INFO)
_h = logging.StreamHandler()
_h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S"))
log.addHandler(_h)

# ════════════════════════════════════════════════════════
# WEBSOCKET SENDER WRAPPER
# Allows monitor_entry coroutines to use the current live WS
# even after reconnections — just update WsSender.fn on each connect.
# ════════════════════════════════════════════════════════
class WsSender:
    fn: Optional[Callable] = None

    @classmethod
    async def send(cls, data: str):
        if cls.fn:
            try:
                await cls.fn(data)
            except Exception as e:
                log.warning(f"WS send failed: {e}")

# ════════════════════════════════════════════════════════
# DATABASE
# ════════════════════════════════════════════════════════
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tokens (
            mint              TEXT PRIMARY KEY,
            name              TEXT,
            symbol            TEXT,
            developer         TEXT,
            created_at        INTEGER,
            twitter           TEXT,
            telegram          TEXT,
            initial_buy_sol   REAL DEFAULT 0,
            score             INTEGER,
            strategies_json   TEXT,
            stats_json        TEXT,
            watch_complete    INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            mint            TEXT,
            wallet          TEXT,
            sol_amount      REAL,
            token_amount    REAL,
            is_buy          INTEGER,
            timestamp       REAL,
            market_cap_sol  REAL
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            mint            TEXT,
            timestamp       INTEGER,
            market_cap_sol  REAL,
            holder_count    INTEGER DEFAULT 0,
            label           TEXT
        );

        CREATE TABLE IF NOT EXISTS strategy_performance (
            strategy_name   TEXT,
            date            TEXT,
            total_signals   INTEGER DEFAULT 0,
            wins            INTEGER DEFAULT 0,
            win_rate        REAL DEFAULT 0.0,
            avg_return      REAL DEFAULT 0.0,
            weight          REAL DEFAULT 1.0,
            PRIMARY KEY (strategy_name, date)
        );

        CREATE TABLE IF NOT EXISTS entries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            mint            TEXT UNIQUE,
            entry_time      INTEGER,
            entry_mc_sol    REAL,
            score           INTEGER,
            max_mc_sol      REAL DEFAULT 0,
            exit_time       INTEGER,
            exit_mc_sol     REAL,
            exit_reason     TEXT,
            exit_detail     TEXT,
            hold_seconds    INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_trades_mint    ON trades(mint);
        CREATE INDEX IF NOT EXISTS idx_trades_ts      ON trades(mint, timestamp);
        CREATE INDEX IF NOT EXISTS idx_snapshots_mint ON snapshots(mint);
        CREATE INDEX IF NOT EXISTS idx_tokens_score   ON tokens(score);
        CREATE INDEX IF NOT EXISTS idx_entries_mint   ON entries(mint);
        CREATE INDEX IF NOT EXISTS idx_entries_open   ON entries(exit_time);

        -- Dev wallet history: tracks outcomes across all launches by a developer
        CREATE TABLE IF NOT EXISTS dev_history (
            wallet      TEXT,
            mint        TEXT,
            created_at  INTEGER,
            peak_mc_sol REAL DEFAULT 0,
            is_winner   INTEGER DEFAULT 0,
            is_rug      INTEGER DEFAULT 0,
            PRIMARY KEY (wallet, mint)
        );
        CREATE INDEX IF NOT EXISTS idx_dev_wallet ON dev_history(wallet);

        -- Graduation events: tokens that filled their bonding curve
        CREATE TABLE IF NOT EXISTS graduations (
            mint        TEXT PRIMARY KEY,
            symbol      TEXT,
            graduated_at INTEGER,
            market_cap_sol REAL
        );
    """)
    conn.close()
    log.info("Database initialised")


# ════════════════════════════════════════════════════════
# STRATEGY ENGINE  (21 strategies)
# strategies_json now includes label + detail for rich UI display
# ════════════════════════════════════════════════════════
def evaluate_strategies(token: dict, trades: list) -> dict:
    created_at = token.get("created_at", time.time())
    dev_wallet  = token.get("developer", "")
    symbol      = token.get("symbol", "")

    buys         = [t for t in trades if t["is_buy"]]
    sells        = [t for t in trades if not t["is_buy"]]
    unique_buys  = set(t["wallet"] for t in buys)
    unique_sells = set(t["wallet"] for t in sells)

    def age(t):       return t["timestamp"] - created_at
    def buys_in(sec): return [t for t in buys if age(t) <= sec]

    s = {}

    # 1 Whale Sniffer
    whale_w = set(t["wallet"] for t in buys_in(30) if t["sol_amount"] >= 1.0)
    s["whale_sniffer"] = dict(label="🐋 Whale Sniffer", positive=True,
        fired=len(whale_w) >= 2, value=len(whale_w),
        threshold="≥2 wallets ≥1 SOL in first 30s",
        detail=f"{len(whale_w)} whale wallets in first 30s")

    # 2 Micro Bot Swarm
    micro_w = set(t["wallet"] for t in buys_in(5) if t["sol_amount"] < 0.02)
    s["micro_bot_swarm"] = dict(label="🤖 Micro Bot Swarm", positive=True,
        fired=len(micro_w) >= 20, value=len(micro_w),
        threshold="≥20 micro-wallets in first 5s",
        detail=f"{len(micro_w)} micro-wallets in first 5s")

    # 3 Holder Velocity
    elapsed = max(min(time.time(), created_at + 120) - created_at, 1)
    vel = len(unique_buys) / (elapsed / 60)
    s["holder_velocity"] = dict(label="📈 Holder Velocity", positive=True,
        fired=vel >= 10, value=round(vel, 1),
        threshold="≥10 unique wallets/min",
        detail=f"{round(vel,1)} unique wallets/min (needed 10)")

    # 4 Volume Consistency
    windows = defaultdict(list)
    for t in buys:
        windows[int(age(t) / 10)].append(t)
    active_w = sum(1 for v in windows.values() if v)
    s["volume_consistency"] = dict(label="📊 Volume Consistency", positive=True,
        fired=active_w >= 4, value=active_w,
        threshold="Activity in ≥4 of 12 time windows",
        detail=f"Active in {active_w}/12 time windows (needed 4)")

    # 5 Dev Hands Off
    dev_sold = any(t["wallet"] == dev_wallet and not t["is_buy"] for t in trades) if dev_wallet else False
    s["dev_hands_off"] = dict(label="🔒 Dev Hands Off", positive=True,
        fired=not dev_sold, value=int(not dev_sold),
        threshold="Dev didn't sell in watch window",
        detail="Dev holding ✓" if not dev_sold else "Dev sold in watch window")

    # 6 First Buy Size
    first_sol = buys[0]["sol_amount"] if buys else 0
    s["first_buy_size"] = dict(label="💰 Strong First Buy", positive=True,
        fired=first_sol >= 0.5, value=round(first_sol, 3),
        threshold="First buy ≥0.5 SOL",
        detail=f"First buy: {round(first_sol,3)} SOL (needed 0.5)")

    # 7 Buy/Sell Ratio
    total_tx  = len(trades)
    buy_ratio = len(buys) / max(total_tx, 1)
    s["buy_sell_ratio"] = dict(label="⚖️ Buy/Sell Ratio", positive=True,
        fired=buy_ratio >= 0.75, value=round(buy_ratio, 2),
        threshold="≥75% of transactions are buys",
        detail=f"{round(buy_ratio*100)}% buys (needed 75%)")

    # 8 Time to 50 Holders
    seen, wallet_times = set(), []
    for t in sorted(buys, key=lambda x: x["timestamp"]):
        if t["wallet"] not in seen:
            seen.add(t["wallet"])
            wallet_times.append(t["timestamp"])
    if len(wallet_times) >= 50:
        t50 = wallet_times[49] - created_at
        s["time_to_50_holders"] = dict(label="⚡ Fast to 50 Holders", positive=True,
            fired=t50 <= 90, value=round(t50, 1),
            threshold="50 unique holders in ≤90s",
            detail=f"Reached 50 holders in {round(t50,1)}s (needed ≤90s)")
    else:
        s["time_to_50_holders"] = dict(label="⚡ Fast to 50 Holders", positive=True,
            fired=False, value=len(wallet_times),
            threshold="50 unique holders in ≤90s",
            detail=f"Only {len(wallet_times)} unique holders in watch window (needed 50)")

    # 9 Repeat Buyers
    buy_counts = defaultdict(int)
    for t in buys: buy_counts[t["wallet"]] += 1
    repeat = sum(1 for c in buy_counts.values() if c >= 2)
    s["repeat_buyers"] = dict(label="🔄 Repeat Buyers", positive=True,
        fired=repeat >= 3, value=repeat,
        threshold="≥3 wallets bought 2+ times",
        detail=f"{repeat} wallets bought 2+ times (needed 3)")

    # 10 Strong Early Volume
    early_vol = sum(t["sol_amount"] for t in buys_in(30))
    s["early_volume"] = dict(label="💥 Strong Early Volume", positive=True,
        fired=early_vol >= 2.0, value=round(early_vol, 2),
        threshold="≥2 SOL buy volume in first 30s",
        detail=f"{round(early_vol,2)} SOL in first 30s (needed 2.0)")

    # 11 Has Twitter
    has_tw = bool(token.get("twitter"))
    s["has_twitter"] = dict(label="🐦 Has Twitter", positive=True,
        fired=has_tw, value=int(has_tw),
        threshold="Twitter account linked",
        detail="Twitter linked ✓" if has_tw else "No Twitter account")

    # 12 Has Telegram
    has_tg = bool(token.get("telegram"))
    s["has_telegram"] = dict(label="📱 Has Telegram", positive=True,
        fired=has_tg, value=int(has_tg),
        threshold="Telegram linked",
        detail="Telegram linked ✓" if has_tg else "No Telegram group")

    # 13 Peak Launch Time (13–21 UTC = US daytime)
    hour = datetime.utcfromtimestamp(created_at).hour
    peak = hour in range(13, 22)
    s["peak_launch_time"] = dict(label="⏰ Peak Launch Time", positive=True,
        fired=peak, value=hour,
        threshold="Launched 13:00–21:59 UTC (US hours)",
        detail=f"Launched at {hour:02d}:xx UTC — {'peak hours ✓' if peak else 'off-peak (needed 13-21 UTC)'}")

    # 14 Short Ticker (2–5 chars)
    short = 2 <= len(symbol) <= 5
    s["short_ticker"] = dict(label="🏷️ Short Ticker", positive=True,
        fired=short, value=len(symbol),
        threshold="Ticker length 2–5 characters",
        detail=f"${symbol} is {len(symbol)} chars ({'✓' if short else 'needed 2-5 chars'})")

    # 15 Dev Strong Initial Buy
    init_buy = token.get("initial_buy_sol", 0)
    s["strong_initial_buy"] = dict(label="🚀 Dev Strong Init Buy", positive=True,
        fired=init_buy >= 0.3, value=round(init_buy, 3),
        threshold="Dev initial buy ≥0.3 SOL",
        detail=f"Dev initial buy: {round(init_buy,3)} SOL (needed 0.3)")

    # 16 Early Dev Dump (negative)
    s["early_dev_dump"] = dict(label="💀 Early Dev Dump", positive=False,
        fired=dev_sold, value=int(dev_sold),
        threshold="Dev sold during watch window",
        detail="Dev sold in watch window ⚠️" if dev_sold else "Dev held ✓")

    # 17 Single Wallet Dominance (>40%)
    if buys:
        total_vol = sum(t["sol_amount"] for t in buys) or 0.001
        w_vols = defaultdict(float)
        for t in buys: w_vols[t["wallet"]] += t["sol_amount"]
        dom = max(w_vols.values()) / total_vol
        s["single_wallet_dominance"] = dict(label="👁️ Wallet Dominance", positive=False,
            fired=dom >= 0.40, value=round(dom*100, 1),
            threshold="One wallet >40% of buy volume",
            detail=f"Top wallet holds {round(dom*100,1)}% of buy vol ({'⚠️ concentrated' if dom>=0.40 else '✓ distributed'})")
    else:
        s["single_wallet_dominance"] = dict(label="👁️ Wallet Dominance", positive=False,
            fired=False, value=0, threshold="One wallet >40% of buy volume",
            detail="No buy data")

    # 18 Wash Trading (3+ wallets both buy and sell)
    wash = unique_buys & unique_sells
    s["wash_trading"] = dict(label="🔃 Wash Trading", positive=False,
        fired=len(wash) >= 3, value=len(wash),
        threshold="≥3 wallets both bought and sold",
        detail=f"{len(wash)} wallets both bought & sold ({'⚠️ wash trading' if len(wash)>=3 else '✓ clean'})")

    # 19 No Socials
    no_soc = not has_tw and not has_tg
    s["no_socials"] = dict(label="👻 No Socials", positive=False,
        fired=no_soc, value=int(no_soc),
        threshold="No Twitter AND no Telegram",
        detail="No Twitter or Telegram ⚠️" if no_soc else "Has at least one social ✓")

    # 20 Bundle Detector (5+ buys in first 1s)
    first_sec = [t for t in buys if age(t) <= 1.0]
    s["bundle_detector"] = dict(label="📦 Bundle Detected", positive=False,
        fired=len(first_sec) >= 5, value=len(first_sec),
        threshold="≥5 buys in first 1 second (bundle)",
        detail=f"{len(first_sec)} buys in first 1s ({'⚠️ likely bundled' if len(first_sec)>=5 else '✓ no bundle'})")

    # 21 Early Sell Pressure (>40% sells)
    sell_pct = len(sells) / max(total_tx, 1)
    s["early_sell_pressure"] = dict(label="📉 Early Sell Pressure", positive=False,
        fired=sell_pct >= 0.40, value=round(sell_pct*100, 1),
        threshold=">40% of transactions are sells",
        detail=f"{round(sell_pct*100,1)}% of transactions are sells ({'⚠️ high pressure' if sell_pct>=0.40 else '✓ normal'})")

    # 22 & 23 · Dev Wallet History (requires pre-fetched dev_history dict)
    dh = token.get("_dev_history", {})
    if dh.get("total", 0) >= 1:
        total_prev  = dh["total"]
        prev_wins   = dh.get("wins", 0)
        prev_rugs   = dh.get("rugs", 0)
        rug_rate    = prev_rugs / total_prev
        # 22: Proven dev (positive) — has at least one previous winner
        s["dev_proven"] = dict(label="⭐ Proven Dev", positive=True,
            fired=prev_wins >= 1, value=prev_wins,
            threshold="Dev has ≥1 previous winning launch",
            detail=f"Dev has {total_prev} prev launches, {prev_wins} winners ({'✓' if prev_wins>=1 else '—'})")
        # 23: Known rugger (negative) — rugged ≥50% of previous launches
        s["known_rugger"] = dict(label="🚩 Known Rugger", positive=False,
            fired=rug_rate >= 0.50 and total_prev >= 2, value=prev_rugs,
            threshold="Dev rugged ≥50% of previous launches (min 2)",
            detail=f"Dev rugged {prev_rugs}/{total_prev} previous tokens ({'⚠️ serial rugger' if rug_rate>=0.50 else '✓ ok history'})")
    else:
        s["dev_proven"] = dict(label="⭐ Proven Dev", positive=True,
            fired=False, value=0,
            threshold="Dev has ≥1 previous winning launch",
            detail="New wallet — no launch history" if not dh else "Dev has no previous launches")
        s["known_rugger"] = dict(label="🚩 Known Rugger", positive=False,
            fired=False, value=0,
            threshold="Dev rugged ≥50% of previous launches",
            detail="No previous launch history to check")

    return s


def get_dev_history(dev_wallet: str) -> dict:
    """Fetch a developer's launch history from the local DB."""
    if not dev_wallet:
        return {}
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT COUNT(*) as total,
                   SUM(is_winner) as wins,
                   SUM(is_rug)    as rugs
            FROM dev_history WHERE wallet=?
        """, (dev_wallet,))
        row = c.fetchone()
        if row and row["total"] and row["total"] > 0:
            return {"total": row["total"], "wins": row["wins"] or 0, "rugs": row["rugs"] or 0}
        return {}
    finally:
        conn.close()


def compute_score(strategies: dict) -> int:
    weights = _load_weights()
    pos_score = pos_max = neg_pen = 0.0
    penalty = CONFIG["NEG_PENALTY_MULTIPLIER"]
    for name, s in strategies.items():
        w = weights.get(name, 1.0)
        if s["positive"]:
            pos_max += w
            if s["fired"]: pos_score += w
        elif s["fired"]:
            neg_pen += w * penalty
    raw = (pos_score / max(pos_max, 1)) * 100
    return max(0, min(100, round(raw - neg_pen)))


_weights_cache:      dict  = {}
_weights_cache_time: float = 0.0
_WEIGHTS_TTL = 3600


def _load_weights() -> dict:
    global _weights_cache, _weights_cache_time
    if _weights_cache and (time.time() - _weights_cache_time) < _WEIGHTS_TTL:
        return _weights_cache
    try:
        conn = get_db()
        c    = conn.cursor()
        c.execute("SELECT strategy_name, weight FROM strategy_performance WHERE date=(SELECT MAX(date) FROM strategy_performance)")
        rows = c.fetchall()
        conn.close()
        if rows:
            _weights_cache      = {r["strategy_name"]: r["weight"] for r in rows}
            _weights_cache_time = time.time()
            return _weights_cache
    except Exception:
        pass
    return {}


def _invalidate_weights_cache():
    global _weights_cache_time
    _weights_cache_time = 0.0


# ════════════════════════════════════════════════════════
# EXIT SIGNAL EVALUATOR  (post-entry real-time checks)
# ════════════════════════════════════════════════════════
def evaluate_exit_signals(entry_data: dict) -> tuple:
    """
    Returns (exit_reason, detail) if an exit condition is met, else (None, "").
    Called every 15s on active monitored positions.
    """
    trades     = entry_data.get("trades", [])
    entry_mc   = entry_data.get("entry_mc", 0.001)
    entry_time = entry_data.get("entry_time", time.time())
    peak_mc    = entry_data.get("peak_mc", entry_mc)
    token      = entry_data.get("token", {})
    dev_wallet = token.get("developer", "")

    if not trades:
        return None, ""

    current_mc = trades[-1].get("market_cap_sol", 0)
    hold_sec   = time.time() - entry_time

    # 1. Take profit
    if current_mc >= entry_mc * CONFIG["TAKE_PROFIT_X"]:
        roi = current_mc / entry_mc
        return f"TAKE_PROFIT_{CONFIG['TAKE_PROFIT_X']}X", f"MC reached {current_mc:.1f} SOL — {roi:.2f}x from entry of {entry_mc:.1f} SOL"

    # 2. Stop loss
    if entry_mc > 0 and current_mc <= entry_mc * CONFIG["STOP_LOSS_PCT"]:
        drop_pct = (1 - current_mc / entry_mc) * 100
        return "STOP_LOSS", f"MC dropped {drop_pct:.0f}% to {current_mc:.1f} SOL (entry was {entry_mc:.1f} SOL)"

    # 3. Time limit
    if hold_sec >= CONFIG["MAX_HOLD_SEC"]:
        roi = current_mc / entry_mc if entry_mc > 0 else 1
        return "TIME_LIMIT", f"6h limit reached — {roi:.2f}x from entry"

    # 4. Dev dump detected in post-entry trades
    if dev_wallet:
        dev_sells = [t for t in trades if t.get("wallet") == dev_wallet and not t["is_buy"]]
        if dev_sells:
            total_dumped = sum(t.get("sol_amount", 0) for t in dev_sells)
            return "DEV_DUMP", f"Dev wallet sold {total_dumped:.2f} SOL worth after entry — exit immediately"

    # 5. Sell spike in recent trades
    recent = trades[-20:]
    if len(recent) >= 10:
        sell_pct = sum(1 for t in recent if not t["is_buy"]) / len(recent)
        if sell_pct >= CONFIG["SELL_SPIKE_PCT"]:
            return "SELL_SPIKE", f"{round(sell_pct*100)}% of last {len(recent)} trades are sells — momentum reversing"

    # 6. Peak reversal: fell 30%+ from peak AND is now below entry MC
    if peak_mc > 0 and current_mc < entry_mc:
        reversal = (peak_mc - current_mc) / peak_mc
        if reversal >= CONFIG["PEAK_REVERSAL_PCT"]:
            return "PEAK_REVERSAL", (
                f"MC fell {round(reversal*100)}% from peak of {peak_mc:.1f} SOL "
                f"down to {current_mc:.1f} SOL (below entry of {entry_mc:.1f} SOL)"
            )

    return None, ""


# ════════════════════════════════════════════════════════
# CONSOLE DISPLAY
# ════════════════════════════════════════════════════════
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"
DIM    = "\033[2m"


def display_token_result(token, strategies, score, trades):
    buys      = [t for t in trades if t["is_buy"]]
    sells     = [t for t in trades if not t["is_buy"]]
    latest_mc = trades[-1].get("market_cap_sol", 0) if trades else 0
    vol_sol   = sum(t["sol_amount"] for t in buys)
    c   = GREEN if score >= CONFIG["MIN_SCORE_HIGHLIGHT"] else (YELLOW if score >= 40 else RED)
    bar = "█" * (min(score,99)//10) + "░" * (10 - min(score,99)//10)
    pos_fired = [s for s in strategies.values() if  s["positive"] and s["fired"]]
    neg_fired = [s for s in strategies.values() if not s["positive"] and s["fired"]]
    entered   = score >= CONFIG["MIN_SCORE_HIGHLIGHT"]
    print(f"\n{c}{'═'*60}{RESET}")
    print(f"{c}{BOLD}  ${token.get('symbol','???'):<8}{RESET}  {token.get('name','')[:26]:<26}  {token.get('mint','')[:8]}...")
    print(f"  Score: {c}{BOLD}{score}/100{RESET} [{bar}]  MC:{latest_mc:.1f} SOL  {'>> ENTERED <<' if entered else 'SKIP'}")
    print(f"  {DIM}Buys:{len(buys)} Sells:{len(sells)} Vol:{vol_sol:.2f} SOL{RESET}")
    if pos_fired: print(f"  + {' | '.join(s['label'] for s in pos_fired)}")
    if neg_fired: print(f"  - {' | '.join(s['label'] for s in neg_fired)}")
    print(f"{c}{'═'*60}{RESET}")


def display_stats_header():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as n FROM tokens"); total = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM tokens WHERE watch_complete=1"); scored = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM entries"); entered = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM entries WHERE exit_reason LIKE 'TAKE%'"); wins = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM entries WHERE exit_time IS NULL"); open_n = c.fetchone()["n"]
        print(f"\n{CYAN}--- Seen:{total} Scored:{scored} Entered:{entered} Wins:{wins} Open:{open_n} Monitors:{len(active_monitors)} ---{RESET}\n")
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# TELEGRAM ALERTS (optional)
# ════════════════════════════════════════════════════════
async def tg_alert(msg: str):
    token = CONFIG.get("TELEGRAM_BOT_TOKEN", "")
    chat  = CONFIG.get("TELEGRAM_CHAT_ID",   "")
    if not token or not chat:
        return
    try:
        import aiohttp
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={"chat_id": chat, "text": msg, "parse_mode": "HTML"})
    except Exception as e:
        log.warning(f"Telegram alert failed: {e}")


# ════════════════════════════════════════════════════════
# DB PERSISTENCE
# ════════════════════════════════════════════════════════
def save_token_initial(token: dict):
    conn = get_db()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO tokens
                (mint, name, symbol, developer, created_at, twitter, telegram, initial_buy_sol)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (token["mint"], token["name"], token["symbol"], token["developer"],
              int(token["created_at"]), token["twitter"], token["telegram"], token["initial_buy_sol"]))
        conn.commit()
    finally:
        conn.close()


def save_trade_realtime(mint: str, trade: dict):
    try:
        conn = get_db()
        try:
            conn.execute("""
                INSERT OR IGNORE INTO trades
                    (mint, wallet, sol_amount, token_amount, is_buy, timestamp, market_cap_sol)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (mint, trade.get("wallet",""), trade.get("sol_amount",0),
                  trade.get("token_amount",0), 1 if trade.get("is_buy") else 0,
                  trade.get("timestamp", time.time()), trade.get("market_cap_sol",0)))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"Trade save failed ({mint[:8]}): {e}")


def save_token_result(token: dict, trades: list, strategies: dict, score: int):
    conn = get_db()
    try:
        c = conn.cursor()
        c.executemany("""
            INSERT OR IGNORE INTO trades
                (mint, wallet, sol_amount, token_amount, is_buy, timestamp, market_cap_sol)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [(token["mint"], t.get("wallet",""), t.get("sol_amount",0),
               t.get("token_amount",0), 1 if t.get("is_buy") else 0,
               t.get("timestamp", time.time()), t.get("market_cap_sol",0)) for t in trades])

        buys  = [t for t in trades if t["is_buy"]]
        stats = {
            "unique_buyers": len(set(t["wallet"] for t in buys)),
            "total_buys":    len(buys),
            "total_sells":   len(trades) - len(buys),
            "total_trades":  len(trades),
            "total_vol_sol": round(sum(t["sol_amount"] for t in buys), 4),
        }

        # Volume profile across 4 time buckets (0-30s, 30-60s, 60-90s, 90-120s)
        created_at = token.get("created_at", time.time())
        def vol_in(t_start, t_end):
            return round(sum(t["sol_amount"] for t in buys
                             if t_start <= t["timestamp"] - created_at < t_end), 3)
        stats["vol_0_30"]  = vol_in(0,  30)
        stats["vol_30_60"] = vol_in(30, 60)
        stats["vol_60_90"] = vol_in(60, 90)
        stats["vol_90_120"]= vol_in(90, 120)

        # Peak MC and bonding curve % (graduation at ~580 SOL total market cap)
        peak_mc = max((t.get("market_cap_sol", 0) for t in trades), default=0)
        stats["peak_mc_sol"] = round(peak_mc, 2)
        stats["bc_pct"]      = round(min(peak_mc / 580 * 100, 100), 1)
        # Save full strategy data including label, detail, threshold for UI
        strat_save = {
            k: {
                "fired":     v["fired"],
                "value":     v["value"],
                "positive":  v["positive"],
                "label":     v.get("label", k),
                "detail":    v.get("detail", ""),
                "threshold": v.get("threshold", ""),
            }
            for k, v in strategies.items()
        }
        c.execute("""
            UPDATE tokens SET score=?, strategies_json=?, stats_json=?, watch_complete=1
            WHERE mint=?
        """, (score, json.dumps(strat_save), json.dumps(stats), token["mint"]))
        conn.commit()
    finally:
        conn.close()


def record_entry(token: dict, trades: list, score: int) -> float:
    """Log an entry signal. Returns the entry MC."""
    latest_mc = trades[-1].get("market_cap_sol", 0) if trades else 0
    conn = get_db()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO entries (mint, entry_time, entry_mc_sol, score, max_mc_sol)
            VALUES (?, ?, ?, ?, ?)
        """, (token["mint"], int(time.time()), latest_mc, score, latest_mc))
        conn.commit()
    finally:
        conn.close()
    log.info(f"{GREEN}>> ENTERED{RESET}  ${token.get('symbol','')}  score={score}  mc={latest_mc:.1f} SOL")
    return latest_mc


def exit_entry_in_db(mint: str, exit_mc: float, exit_reason: str, exit_detail: str, hold_sec: int):
    conn = get_db()
    try:
        conn.execute("""
            UPDATE entries
            SET exit_time=?, exit_mc_sol=?, exit_reason=?, exit_detail=?, hold_seconds=?,
                max_mc_sol=MAX(COALESCE(max_mc_sol,0), ?)
            WHERE mint=? AND exit_time IS NULL
        """, (int(time.time()), exit_mc, exit_reason, exit_detail, hold_sec, exit_mc, mint))
        conn.commit()
    finally:
        conn.close()


def update_entry_max_mc(mint: str, current_mc: float):
    conn = get_db()
    try:
        conn.execute("UPDATE entries SET max_mc_sol=MAX(COALESCE(max_mc_sol,0),?) WHERE mint=?",
                     (current_mc, mint))
        conn.commit()
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# ACTIVE STORES
# ════════════════════════════════════════════════════════
active_watches:  Dict[str, dict] = {}   # mint → {token, trades}
active_monitors: Dict[str, dict] = {}   # mint → {token, trades, entry_mc, ...}


# ════════════════════════════════════════════════════════
# REAL-TIME ENTRY MONITOR
# Keeps WebSocket subscription alive for entered positions,
# re-evaluates exit conditions every 15s until exit triggered.
# ════════════════════════════════════════════════════════
async def monitor_entry(mint: str, token: dict, entry_mc: float, entry_score: int):
    sym = token.get("symbol", mint[:6])

    if len(active_monitors) >= CONFIG["MAX_ACTIVE_MONITORS"]:
        log.warning(f"[Monitor] Max monitors reached — not monitoring ${sym}")
        return

    active_monitors[mint] = {
        "token":      token,
        "trades":     [],
        "entry_mc":   entry_mc,
        "entry_score": entry_score,
        "entry_time": time.time(),
        "peak_mc":    entry_mc,
    }

    # Re-subscribe to this token's trades (we just unsubscribed in watch_token)
    await WsSender.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
    log.info(f"[Monitor] Watching ${sym} post-entry  (entry_mc={entry_mc:.1f} SOL)")

    try:
        while mint in active_monitors:
            await asyncio.sleep(15)

            data = active_monitors.get(mint)
            if not data:
                break

            exit_reason, exit_detail = evaluate_exit_signals(data)

            if exit_reason:
                trades     = data["trades"]
                current_mc = trades[-1].get("market_cap_sol", entry_mc) if trades else entry_mc
                hold_sec   = int(time.time() - data["entry_time"])
                roi        = current_mc / entry_mc if entry_mc > 0 else 1

                exit_entry_in_db(mint, current_mc, exit_reason, exit_detail, hold_sec)

                icon = "🏆" if exit_reason.startswith("TAKE") else "🔴"
                log.info(f"{icon} EXIT [{exit_reason}]  ${sym}  "
                         f"{entry_mc:.1f}→{current_mc:.1f} SOL  {roi:.2f}x  {hold_sec//60}m  |  {exit_detail}")

                await tg_alert(
                    f"{icon} <b>EXIT [{exit_reason}]</b>  ${sym}\n"
                    f"Entry: {entry_mc:.1f} SOL → Exit: {current_mc:.1f} SOL  ({roi:.2f}x)\n"
                    f"Held: {hold_sec//60}m\n{exit_detail}"
                )
                break

    finally:
        active_monitors.pop(mint, None)
        await WsSender.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]}))
        log.info(f"[Monitor] Closed ${sym}")


# ════════════════════════════════════════════════════════
# WATCH TOKEN  (2-minute initial watch)
# ════════════════════════════════════════════════════════
async def watch_token(mint: str, token: dict):
    active_watches[mint] = {"token": token, "trades": []}

    await WsSender.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))

    await asyncio.sleep(CONFIG["WATCH_DEPTH_SECONDS"])

    data   = active_watches.pop(mint, {})
    trades = data.get("trades", [])

    # Fetch dev wallet history and inject into token dict for strategy 22/23
    token["_dev_history"] = get_dev_history(token.get("developer", ""))

    strategies = evaluate_strategies(token, trades)
    score      = compute_score(strategies)

    save_token_result(token, trades, strategies, score)
    display_token_result(token, strategies, score, trades)

    # Unsubscribe from watch — monitor_entry will re-subscribe if entered
    await WsSender.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]}))

    if score >= CONFIG["MIN_SCORE_HIGHLIGHT"]:
        entry_mc = record_entry(token, trades, score)
        sym = token.get("symbol", "")
        await tg_alert(
            f"🎯 <b>ENTRY SIGNAL</b>  ${sym}\n"
            f"Score: {score}/100  |  MC: {entry_mc:.1f} SOL\n"
            f"TP: {CONFIG['TAKE_PROFIT_X']}x  SL: {int(CONFIG['STOP_LOSS_PCT']*100)}%"
        )
        # Hand off to real-time monitor
        asyncio.create_task(monitor_entry(mint, token, entry_mc, score))


# ════════════════════════════════════════════════════════
# PERIODIC MC CHECKER  (non-entered tokens + snapshot backup)
# ════════════════════════════════════════════════════════
async def periodic_checker():
    checks = list(zip(CONFIG["PERIODIC_CHECKS_SEC"], CONFIG["PERIODIC_LABELS"]))

    while True:
        await asyncio.sleep(60)
        now  = time.time()
        conn = get_db()
        try:
            c = conn.cursor()

            # Snapshot checkpoints for all scored tokens
            c.execute("SELECT mint, created_at, symbol FROM tokens WHERE watch_complete=1")
            for row in c.fetchall():
                mint, created_at, sym = row["mint"], row["created_at"], row["symbol"]
                for offset, label in checks:
                    target = created_at + offset
                    if not (target <= now <= target + 90):
                        continue
                    c.execute("SELECT id FROM snapshots WHERE mint=? AND label=?", (mint, label))
                    if c.fetchone():
                        continue
                    c.execute("SELECT market_cap_sol FROM trades WHERE mint=? ORDER BY timestamp DESC LIMIT 1", (mint,))
                    tr = c.fetchone()
                    if tr and tr["market_cap_sol"]:
                        mc = tr["market_cap_sol"]
                        c.execute("INSERT INTO snapshots (mint, timestamp, market_cap_sol, label) VALUES (?,?,?,?)",
                                  (mint, int(now), mc, label))
                        if mc >= CONFIG["WINNER_MC_THRESHOLD_SOL"]:
                            log.info(f"{GREEN}WINNER [{label}]{RESET}  ${sym}  MC={mc:.1f} SOL")

            # Fallback exit check for entries NOT currently being monitored
            # (e.g. after restart when active_monitors is empty)
            c.execute("""
                SELECT e.mint, e.entry_time, e.entry_mc_sol, e.max_mc_sol, t.symbol
                FROM entries e JOIN tokens t ON e.mint=t.mint
                WHERE e.exit_time IS NULL
            """)
            for entry in c.fetchall():
                if entry["mint"] in active_monitors:
                    continue  # already handled in real-time
                c.execute("SELECT market_cap_sol FROM trades WHERE mint=? ORDER BY timestamp DESC LIMIT 1",
                          (entry["mint"],))
                tr = c.fetchone()
                if not tr or not tr["market_cap_sol"]:
                    continue
                current_mc = tr["market_cap_sol"]
                entry_mc   = entry["entry_mc_sol"] or 0.001
                hold_sec   = int(now - entry["entry_time"])
                new_max    = max(entry["max_mc_sol"] or 0, current_mc)
                c.execute("UPDATE entries SET max_mc_sol=? WHERE mint=?", (new_max, entry["mint"]))

                exit_reason = None
                if current_mc >= entry_mc * CONFIG["TAKE_PROFIT_X"]:
                    exit_reason = f"TAKE_PROFIT_{CONFIG['TAKE_PROFIT_X']}X"
                elif current_mc <= entry_mc * CONFIG["STOP_LOSS_PCT"]:
                    exit_reason = "STOP_LOSS"
                elif hold_sec >= CONFIG["MAX_HOLD_SEC"]:
                    exit_reason = "TIME_LIMIT"

                if exit_reason:
                    c.execute("""UPDATE entries SET exit_time=?,exit_mc_sol=?,exit_reason=?,hold_seconds=?
                                 WHERE mint=?""", (int(now), current_mc, exit_reason, hold_sec, entry["mint"]))
                    log.info(f"[Fallback] EXIT [{exit_reason}]  ${entry['symbol']}")

            # Record dev wallet outcomes for tokens with 6h+ of snapshot data
            # A token is a "winner" if it hit threshold, a "rug" if it barely moved
            c.execute("""
                SELECT t.mint, t.developer, t.score,
                       MAX(s.market_cap_sol) as peak_mc
                FROM tokens t JOIN snapshots s ON t.mint=s.mint
                WHERE t.watch_complete=1 AND t.developer IS NOT NULL AND t.developer != ''
                  AND t.created_at < ?
                GROUP BY t.mint
            """, (now - 21600,))  # only tokens older than 6h (have meaningful outcome)
            for row in c.fetchall():
                if not row["developer"]:
                    continue
                peak_mc   = row["peak_mc"] or 0
                is_winner = 1 if peak_mc >= CONFIG["WINNER_MC_THRESHOLD_SOL"] else 0
                is_rug    = 1 if peak_mc < 10 else 0  # never reached 10 SOL MC = effectively rugged
                c.execute("""
                    INSERT OR REPLACE INTO dev_history
                        (wallet, mint, created_at, peak_mc_sol, is_winner, is_rug)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (row["developer"], row["mint"], int(now), peak_mc, is_winner, is_rug))

            conn.commit()
        except Exception as e:
            log.error(f"[periodic_checker] {e}")
        finally:
            conn.close()


# ════════════════════════════════════════════════════════
# NIGHTLY OPTIMISER
# ════════════════════════════════════════════════════════
async def nightly_optimiser():
    while True:
        now    = datetime.utcnow()
        to_mid = ((23 - now.hour) * 3600 + (59 - now.minute) * 60 + (60 - now.second) % 60)
        log.info(f"[Optimiser] Next run in {to_mid//3600}h {(to_mid%3600)//60}m")
        await asyncio.sleep(to_mid)
        _run_optimiser()
        _invalidate_weights_cache()


def _run_optimiser():
    log.info("[Optimiser] Running...")
    conn = get_db()
    try:
        c     = conn.cursor()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        c.execute("""
            SELECT t.mint, t.strategies_json, MAX(s.market_cap_sol) as peak_mc
            FROM tokens t JOIN snapshots s ON t.mint=s.mint
            WHERE t.watch_complete=1 AND t.strategies_json IS NOT NULL
            GROUP BY t.mint
        """)
        rows = c.fetchall()
        if len(rows) < 10:
            log.info("[Optimiser] Not enough data yet (need 10+ with snapshots).")
            return
        stats = defaultdict(lambda: {"total": 0, "wins": 0})
        for row in rows:
            try:
                strats    = json.loads(row["strategies_json"])
                is_winner = (row["peak_mc"] or 0) >= CONFIG["WINNER_MC_THRESHOLD_SOL"]
            except Exception:
                continue
            for name, s in strats.items():
                if s.get("fired"):
                    stats[name]["total"] += 1
                    if is_winner: stats[name]["wins"] += 1
        for name, stat in stats.items():
            if stat["total"] < 5:
                continue
            wr     = stat["wins"] / stat["total"]
            weight = max(0.2, min(2.0, 0.2 + wr * 1.6))
            c.execute("""INSERT OR REPLACE INTO strategy_performance
                             (strategy_name, date, total_signals, wins, win_rate, weight)
                         VALUES (?,?,?,?,?,?)""",
                      (name, today, stat["total"], stat["wins"], round(wr, 4), weight))
        conn.commit()
        log.info("[Optimiser] Weights updated")
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# SIMULATION MODE
# ════════════════════════════════════════════════════════
async def run_simulation():
    import random, string
    log.info("SIMULATION MODE")
    scenarios = [
        {"symbol":"MOON","name":"Moon Token","twitter":"x.com/moon","telegram":"t.me/moon","initial_buy_sol":1.2,
         "trades_config":{"whale_count":3,"micro_count":0,"total_buys":80,"total_sells":8,"spread_windows":10,"dev_sells":False}},
        {"symbol":"SWARM","name":"Swarm Bot","twitter":"","telegram":"t.me/swarm","initial_buy_sol":0.1,
         "trades_config":{"whale_count":0,"micro_count":25,"total_buys":60,"total_sells":5,"spread_windows":2,"dev_sells":False}},
        {"symbol":"RUG","name":"Totally Safe","twitter":"","telegram":"","initial_buy_sol":0.05,
         "trades_config":{"whale_count":0,"micro_count":0,"total_buys":15,"total_sells":12,"spread_windows":1,"dev_sells":True,"bundle":True}},
        {"symbol":"MID","name":"Average Launch","twitter":"x.com/mid","telegram":"","initial_buy_sol":0.3,
         "trades_config":{"whale_count":1,"micro_count":5,"total_buys":35,"total_sells":6,"spread_windows":5,"dev_sells":False}},
    ]
    for i, sc in enumerate(scenarios):
        now  = time.time()
        mint = "SIM" + "".join(random.choices(string.ascii_uppercase + string.digits, k=40))
        cfg  = sc["trades_config"]
        token = {"mint":mint,"name":sc["name"],"symbol":sc["symbol"],"developer":"DEV"+"x"*40,
                 "created_at":now,"twitter":sc["twitter"],"telegram":sc["telegram"],"initial_buy_sol":sc["initial_buy_sol"]}
        save_token_initial(token)
        trades = []
        if cfg.get("bundle"):
            for _ in range(7):
                trades.append({"wallet":f"BND{random.randint(1,5):03d}"+"x"*39,"sol_amount":0.5,
                               "token_amount":500000,"is_buy":True,"timestamp":now+0.2+random.random()*0.5,"market_cap_sol":5})
        for j in range(cfg["whale_count"]):
            trades.append({"wallet":f"WHL{j:03d}"+"x"*39,"sol_amount":1.2+random.random(),"token_amount":800000,
                           "is_buy":True,"timestamp":now+5+j*8,"market_cap_sol":10+j*5})
        for j in range(cfg["micro_count"]):
            trades.append({"wallet":f"MCR{j:03d}"+"x"*39,"sol_amount":0.01+random.random()*0.01,"token_amount":5000,
                           "is_buy":True,"timestamp":now+random.random()*4,"market_cap_sol":3})
        for j in range(cfg["total_buys"]):
            ws2 = (j/max(cfg["total_buys"],1))*(cfg["spread_windows"]/12)*120
            trades.append({"wallet":f"REG{j:03d}"+"x"*39,"sol_amount":0.05+random.random()*0.3,"token_amount":50000,
                           "is_buy":True,"timestamp":now+ws2+random.random()*5,"market_cap_sol":5+j*0.5})
        for j in range(cfg["total_sells"]):
            trades.append({"wallet":f"SLL{j:03d}"+"x"*39,"sol_amount":0.1,"token_amount":100000,
                           "is_buy":False,"timestamp":now+60+j*5,"market_cap_sol":40})
        if cfg["dev_sells"]:
            trades.append({"wallet":token["developer"],"sol_amount":2.0,"token_amount":2000000,
                           "is_buy":False,"timestamp":now+30,"market_cap_sol":15})
        trades.sort(key=lambda x: x["timestamp"])
        strategies = evaluate_strategies(token, trades)
        score      = compute_score(strategies)
        log.info(f"[SIM {i+1}] ${sc['symbol']} score={score}")
        save_token_result(token, trades, strategies, score)
        display_token_result(token, strategies, score, trades)
        if score >= CONFIG["MIN_SCORE_HIGHLIGHT"]:
            record_entry(token, trades, score)
        await asyncio.sleep(0.5)
    print("\nSimulation complete.")


# ════════════════════════════════════════════════════════
# MAIN WEBSOCKET LOOP
# ════════════════════════════════════════════════════════
async def run_live():
    reconnect_delay = 5

    while True:
        try:
            async with websockets.connect(
                CONFIG["WS_URL"], ping_interval=30, ping_timeout=10,
                close_timeout=5, max_size=2**23,
            ) as ws:
                log.info("Connected to PumpPortal WebSocket")
                WsSender.fn = ws.send
                reconnect_delay = 5

                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeGraduations"}))

                # Re-subscribe to all active monitors after reconnect
                if active_monitors:
                    mints = list(active_monitors.keys())
                    await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": mints}))
                    log.info(f"Re-subscribed to {len(mints)} active monitors after reconnect")

                log.info(f"Subscribed | Active monitors: {len(active_monitors)}\n")
                last_stats = time.time()

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if time.time() - last_stats > 300:
                        display_stats_header()
                        last_stats = time.time()

                    tx_type = data.get("txType", "")
                    mint    = data.get("mint",   "")
                    if not mint:
                        continue

                    # ── New token launch ───────────────────
                    if tx_type == "create":
                        token = {
                            "mint":            mint,
                            "name":            data.get("name",            "Unknown"),
                            "symbol":          data.get("symbol",          "???"),
                            "developer":       data.get("traderPublicKey", ""),
                            "created_at":      time.time(),
                            "twitter":         data.get("twitter",         ""),
                            "telegram":        data.get("telegram",        ""),
                            "initial_buy_sol": data.get("initialBuy",      0),
                        }
                        log.info(f"NEW  ${token['symbol']:<8}  {token['name'][:26]}"
                                 f"  {'T' if token['twitter'] else ''}{'G' if token['telegram'] else ''}")
                        save_token_initial(token)
                        if len(active_watches) < CONFIG["MAX_ACTIVE_WATCHES"]:
                            asyncio.create_task(watch_token(mint, token))
                        else:
                            log.warning(f"Max watches reached — skipping {mint[:8]}")

                    # ── Graduation event ───────────────────
                    elif tx_type == "graduation" or data.get("bondingCurveComplete"):
                        sym      = data.get("symbol", mint[:6])
                        mc_sol   = data.get("marketCapSol", 0)
                        log.info(f"{GREEN}🎓 GRADUATED{RESET}  ${sym}  MC={mc_sol:.1f} SOL  {mint[:8]}...")

                        # Record graduation
                        try:
                            conn = get_db()
                            conn.execute("""
                                INSERT OR IGNORE INTO graduations (mint, symbol, graduated_at, market_cap_sol)
                                VALUES (?, ?, ?, ?)
                            """, (mint, sym, int(time.time()), mc_sol))
                            conn.commit()
                            conn.close()
                        except Exception:
                            pass

                        # Exit any open entry on this token
                        if mint in active_monitors:
                            mon  = active_monitors[mint]
                            hold = int(time.time() - mon["entry_time"])
                            roi  = mc_sol / mon["entry_mc"] if mon["entry_mc"] > 0 and mc_sol > 0 else 1
                            exit_entry_in_db(mint, mc_sol, "GRADUATED",
                                             f"Token graduated to Raydium — bonding curve filled at {mc_sol:.1f} SOL ({roi:.2f}x)",
                                             hold)
                            active_monitors.pop(mint, None)
                            await WsSender.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]}))
                            log.info(f"🎓 Auto-exited entry on ${sym}  {mon['entry_mc']:.1f}→{mc_sol:.1f} SOL  {roi:.2f}x  {hold//60}m held")
                            await tg_alert(
                                f"🎓 <b>GRADUATED — AUTO EXIT</b>  ${sym}\n"
                                f"{mon['entry_mc']:.1f} → {mc_sol:.1f} SOL  ({roi:.2f}x)\n"
                                f"Bonding curve filled — moved to Raydium"
                            )

                    # ── Trade event ────────────────────────
                    elif tx_type in ("buy", "sell"):
                        trade = {
                            "wallet":         data.get("traderPublicKey", ""),
                            "sol_amount":     data.get("solAmount",       0),
                            "token_amount":   data.get("tokenAmount",     0),
                            "is_buy":         tx_type == "buy",
                            "timestamp":      time.time(),
                            "market_cap_sol": data.get("marketCapSol",   0),
                        }

                        # Route to initial watch window
                        if mint in active_watches:
                            active_watches[mint]["trades"].append(trade)
                            save_trade_realtime(mint, trade)

                        # Route to real-time entry monitor
                        if mint in active_monitors:
                            mon = active_monitors[mint]
                            mon["trades"].append(trade)
                            mc = trade["market_cap_sol"]
                            if mc > mon.get("peak_mc", 0):
                                mon["peak_mc"] = mc
                                update_entry_max_mc(mint, mc)
                            save_trade_realtime(mint, trade)

        except ConnectionClosed as e:
            WsSender.fn = None
            log.warning(f"Disconnected ({e}). Reconnect in {reconnect_delay}s...")
        except Exception as e:
            WsSender.fn = None
            log.error(f"Error: {e}. Reconnect in {reconnect_delay}s...")

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)


# ════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════
async def main():
    init_db()
    log.info(
        f"PumpFun Tracker | watch={CONFIG['WATCH_DEPTH_SECONDS']}s | "
        f"threshold={CONFIG['WINNER_MC_THRESHOLD_SOL']} SOL | "
        f"entry>={CONFIG['MIN_SCORE_HIGHLIGHT']} | "
        f"TP={CONFIG['TAKE_PROFIT_X']}x SL={int(CONFIG['STOP_LOSS_PCT']*100)}% | "
        f"mode={'SIM' if CONFIG['TEST_MODE'] else 'LIVE'}"
    )
    tg_enabled = bool(CONFIG.get("TELEGRAM_BOT_TOKEN") and CONFIG.get("TELEGRAM_CHAT_ID"))
    log.info(f"Telegram alerts: {'ENABLED' if tg_enabled else 'disabled (fill TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)'}")

    asyncio.create_task(periodic_checker())
    asyncio.create_task(nightly_optimiser())

    if CONFIG["TEST_MODE"]:
        await run_simulation()
    else:
        await run_live()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
