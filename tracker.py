#!/usr/bin/env python3
"""
PumpFun Launch Tracker
Tracks all pump.fun launches, scores with 21 strategies,
logs outcomes, and self-optimizes strategy weights nightly.
"""

import asyncio
import json
import sqlite3
import time
import logging
import sys
import os
from datetime import datetime
from collections import defaultdict
from typing import Dict, List
import websockets
from websockets.exceptions import ConnectionClosed

# ════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════
CONFIG = {
    "WATCH_DEPTH_SECONDS":      120,
    "WINNER_MC_THRESHOLD_USD":  20_000,
    "PERIODIC_CHECKS_SEC":      [600, 1800, 3600, 21600],
    "PERIODIC_LABELS":          ["10m", "30m", "1h", "6h"],
    "MIN_SCORE_HIGHLIGHT":      60,
    "DB_PATH":                  "tracker.db",
    "WS_URL":                   "wss://pumpportal.fun/api/data",
    "SOL_PRICE_USD":            150,        # update manually or pull live
    "MAX_ACTIVE_WATCHES":       150,
    "TEST_MODE":                False,      # set True to run with simulated data
}

# ════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════
log = logging.getLogger("tracker")
log.setLevel(logging.INFO)
_h = logging.StreamHandler()
_h.setFormatter(logging.Formatter("%(asctime)s │ %(levelname)-7s │ %(message)s", "%H:%M:%S"))
log.addHandler(_h)

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
            market_cap_sol  REAL,
            market_cap_usd  REAL
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            mint            TEXT,
            timestamp       INTEGER,
            market_cap_usd  REAL,
            holder_count    INTEGER DEFAULT 0,
            label           TEXT
        );

        CREATE TABLE IF NOT EXISTS strategy_performance (
            strategy_name   TEXT,
            date            TEXT,
            total_signals   INTEGER DEFAULT 0,
            wins            INTEGER DEFAULT 0,
            win_rate        REAL DEFAULT 0.0,
            weight          REAL DEFAULT 1.0,
            PRIMARY KEY (strategy_name, date)
        );

        CREATE INDEX IF NOT EXISTS idx_trades_mint     ON trades(mint);
        CREATE INDEX IF NOT EXISTS idx_snapshots_mint  ON snapshots(mint);
        CREATE INDEX IF NOT EXISTS idx_tokens_score    ON tokens(score);
    """)
    conn.close()
    log.info("Database initialised ✓")


# ════════════════════════════════════════════════════════
# STRATEGY ENGINE  (21 strategies)
# ════════════════════════════════════════════════════════
def evaluate_strategies(token: dict, trades: list) -> dict:
    """Evaluate all 21 strategies against a token's trade window."""

    created_at = token.get("created_at", time.time())
    dev_wallet  = token.get("developer", "")
    symbol      = token.get("symbol", "")
    sol_px      = CONFIG["SOL_PRICE_USD"]

    buys         = [t for t in trades if t["is_buy"]]
    sells        = [t for t in trades if not t["is_buy"]]
    unique_buys  = set(t["wallet"] for t in buys)
    unique_sells = set(t["wallet"] for t in sells)

    def age(t):             return t["timestamp"] - created_at
    def buys_in(sec):       return [t for t in buys  if age(t) <= sec]
    def wallets_in(sec):    return set(t["wallet"] for t in buys_in(sec))

    s = {}   # results dict

    # ── POSITIVE SIGNALS ────────────────────────────────

    # 1 · Whale Sniffer
    whale_w = set(t["wallet"] for t in buys_in(30) if t["sol_amount"] >= 1.0)
    s["whale_sniffer"] = dict(
        label="🐋 Whale Sniffer", positive=True,
        fired=len(whale_w) >= 2, value=len(whale_w),
        detail=f"{len(whale_w)} wallets ≥1 SOL in first 30s"
    )

    # 2 · Micro Bot Swarm
    micro_w = set(t["wallet"] for t in buys_in(5) if t["sol_amount"] < 0.02)
    s["micro_bot_swarm"] = dict(
        label="🤖 Micro Bot Swarm", positive=True,
        fired=len(micro_w) >= 20, value=len(micro_w),
        detail=f"{len(micro_w)} micro-wallets in first 5s"
    )

    # 3 · Holder Velocity
    elapsed = max(min(time.time(), created_at + 120) - created_at, 1)
    vel = len(unique_buys) / (elapsed / 60)
    s["holder_velocity"] = dict(
        label="📈 Holder Velocity", positive=True,
        fired=vel >= 10, value=round(vel, 1),
        detail=f"{round(vel,1)} unique wallets/min"
    )

    # 4 · Volume Consistency (buys spread across 10s windows)
    windows = defaultdict(list)
    for t in buys:
        windows[int(age(t) / 10)].append(t)
    active_w = sum(1 for v in windows.values() if v)
    s["volume_consistency"] = dict(
        label="📊 Volume Consistency", positive=True,
        fired=active_w >= 4, value=active_w,
        detail=f"Activity in {active_w} of 12 time windows"
    )

    # 5 · Dev Hands Off
    dev_sold = any(t["wallet"] == dev_wallet and not t["is_buy"] for t in trades) if dev_wallet else False
    s["dev_hands_off"] = dict(
        label="🔒 Dev Hands Off", positive=True,
        fired=not dev_sold, value=int(not dev_sold),
        detail="Dev holding" if not dev_sold else "Dev sold ⚠️"
    )

    # 6 · First Buy Size
    first_sol = buys[0]["sol_amount"] if buys else 0
    s["first_buy_size"] = dict(
        label="💰 Strong First Buy", positive=True,
        fired=first_sol >= 0.5, value=round(first_sol, 3),
        detail=f"First buy: {round(first_sol,3)} SOL"
    )

    # 7 · Buy/Sell Ratio
    total_tx  = len(trades)
    buy_ratio = len(buys) / max(total_tx, 1)
    s["buy_sell_ratio"] = dict(
        label="⚖️  Buy/Sell Ratio", positive=True,
        fired=buy_ratio >= 0.80, value=round(buy_ratio, 2),
        detail=f"{round(buy_ratio*100)}% buy transactions"
    )

    # 8 · Time to 50 Holders
    seen, wallet_times = set(), []
    for t in sorted(buys, key=lambda x: x["timestamp"]):
        if t["wallet"] not in seen:
            seen.add(t["wallet"])
            wallet_times.append(t["timestamp"])
    if len(wallet_times) >= 50:
        t50 = wallet_times[49] - created_at
        s["time_to_50_holders"] = dict(
            label="⚡ Fast to 50 Holders", positive=True,
            fired=t50 <= 90, value=round(t50, 1),
            detail=f"50 holders in {round(t50,1)}s"
        )
    else:
        s["time_to_50_holders"] = dict(
            label="⚡ Fast to 50 Holders", positive=True,
            fired=False, value=len(wallet_times),
            detail=f"Only {len(wallet_times)} holders in watch window"
        )

    # 9 · Repeat Buyers
    buy_counts = defaultdict(int)
    for t in buys: buy_counts[t["wallet"]] += 1
    repeat = sum(1 for c in buy_counts.values() if c >= 2)
    s["repeat_buyers"] = dict(
        label="🔄 Repeat Buyers", positive=True,
        fired=repeat >= 3, value=repeat,
        detail=f"{repeat} wallets bought 2+ times"
    )

    # 10 · Strong Early Volume
    early_vol     = sum(t["sol_amount"] for t in buys_in(30))
    early_vol_usd = early_vol * sol_px
    s["early_volume"] = dict(
        label="💥 Strong Early Volume", positive=True,
        fired=early_vol >= 3.0, value=round(early_vol, 2),
        detail=f"{round(early_vol,2)} SOL (${int(early_vol_usd):,}) in first 30s"
    )

    # 11 · Has Twitter
    has_tw = bool(token.get("twitter"))
    s["has_twitter"] = dict(
        label="🐦 Has Twitter", positive=True,
        fired=has_tw, value=int(has_tw),
        detail="Twitter linked" if has_tw else "No Twitter"
    )

    # 12 · Has Telegram
    has_tg = bool(token.get("telegram"))
    s["has_telegram"] = dict(
        label="📱 Has Telegram", positive=True,
        fired=has_tg, value=int(has_tg),
        detail="Telegram linked" if has_tg else "No Telegram"
    )

    # 13 · Peak Launch Time (13–21 UTC = US daytime)
    hour = datetime.utcfromtimestamp(created_at).hour
    peak = hour in range(13, 22)
    s["peak_launch_time"] = dict(
        label="⏰ Peak Launch Time", positive=True,
        fired=peak, value=hour,
        detail=f"Launched {hour:02d}:xx UTC ({'peak' if peak else 'off-peak'})"
    )

    # 14 · Short Ticker (2–4 chars)
    short = 2 <= len(symbol) <= 4
    s["short_ticker"] = dict(
        label="🏷️  Short Ticker", positive=True,
        fired=short, value=len(symbol),
        detail=f"${symbol} ({len(symbol)} chars)"
    )

    # 15 · Dev Strong Initial Buy
    init_buy = token.get("initial_buy_sol", 0)
    s["strong_initial_buy"] = dict(
        label="🚀 Dev Strong Init Buy", positive=True,
        fired=init_buy >= 0.5, value=round(init_buy, 3),
        detail=f"Dev initial: {round(init_buy,3)} SOL"
    )

    # ── NEGATIVE / RED-FLAG SIGNALS ──────────────────────

    # 16 · Early Dev Dump
    s["early_dev_dump"] = dict(
        label="💀 Early Dev Dump", positive=False,
        fired=dev_sold, value=int(dev_sold),
        detail="Dev sold in watch window" if dev_sold else "Dev held"
    )

    # 17 · Single Wallet Dominance (>25% of buy volume)
    if buys:
        total_vol = sum(t["sol_amount"] for t in buys) or 0.001
        w_vols = defaultdict(float)
        for t in buys: w_vols[t["wallet"]] += t["sol_amount"]
        dom = max(w_vols.values()) / total_vol
        s["single_wallet_dominance"] = dict(
            label="👁️  Wallet Dominance", positive=False,
            fired=dom >= 0.25, value=round(dom*100, 1),
            detail=f"Top wallet: {round(dom*100,1)}% of buy volume"
        )
    else:
        s["single_wallet_dominance"] = dict(
            label="👁️  Wallet Dominance", positive=False,
            fired=False, value=0, detail="No buy data"
        )

    # 18 · Wash Trading (wallet both buys & sells)
    wash = unique_buys & unique_sells
    s["wash_trading"] = dict(
        label="🔃 Wash Trading", positive=False,
        fired=len(wash) >= 2, value=len(wash),
        detail=f"{len(wash)} wallets both bought & sold"
    )

    # 19 · No Socials
    no_soc = not has_tw and not has_tg
    s["no_socials"] = dict(
        label="👻 No Socials", positive=False,
        fired=no_soc, value=int(no_soc),
        detail="No Twitter or Telegram" if no_soc else "Has socials"
    )

    # 20 · Bundle Detector (5+ buys in first 1 second)
    first_sec = [t for t in buys if age(t) <= 1.0]
    bundle    = len(first_sec) >= 5
    s["bundle_detector"] = dict(
        label="📦 Bundle Detected", positive=False,
        fired=bundle, value=len(first_sec),
        detail=f"{len(first_sec)} buys in first 1s (likely bundled)"
    )

    # 21 · Early Sell Pressure (>30% of txns are sells)
    sell_pct = len(sells) / max(total_tx, 1)
    s["early_sell_pressure"] = dict(
        label="📉 Early Sell Pressure", positive=False,
        fired=sell_pct >= 0.30, value=round(sell_pct*100, 1),
        detail=f"{round(sell_pct*100,1)}% of transactions are sells"
    )

    return s


def compute_score(strategies: dict) -> int:
    """Composite score 0–100 using weighted strategies."""
    weights   = _load_weights()
    pos_score = 0.0
    pos_max   = 0.0
    neg_pen   = 0.0

    for name, s in strategies.items():
        w = weights.get(name, 1.0)
        if s["positive"]:
            pos_max += w
            if s["fired"]:
                pos_score += w
        elif s["fired"]:
            neg_pen += w * 12   # −12 pts per fired negative

    raw = (pos_score / max(pos_max, 1)) * 100
    return max(0, min(100, round(raw - neg_pen)))


def _load_weights() -> dict:
    try:
        conn = get_db()
        c    = conn.cursor()
        c.execute("""
            SELECT strategy_name, weight FROM strategy_performance
            WHERE date = (SELECT MAX(date) FROM strategy_performance)
        """)
        rows = c.fetchall()
        conn.close()
        if rows:
            return {r["strategy_name"]: r["weight"] for r in rows}
    except Exception:
        pass
    return {}


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

def color_for_score(score):
    if score >= CONFIG["MIN_SCORE_HIGHLIGHT"]: return GREEN
    if score >= 40:                            return YELLOW
    return RED

def display_token_result(token: dict, strategies: dict, score: int, trades: list):
    buys         = [t for t in trades if t["is_buy"]]
    sells        = [t for t in trades if not t["is_buy"]]
    unique_buyers = len(set(t["wallet"] for t in buys))
    latest_mc_sol = trades[-1].get("market_cap_sol", 0) if trades else 0
    latest_mc_usd = latest_mc_sol * CONFIG["SOL_PRICE_USD"]
    total_vol_sol = sum(t["sol_amount"] for t in buys)

    c = color_for_score(score)
    bar = "█" * (score // 10) + "░" * (10 - score // 10)

    pos_fired = [s for s in strategies.values() if s["positive"]  and s["fired"]]
    neg_fired = [s for s in strategies.values() if not s["positive"] and s["fired"]]

    soc = []
    if token.get("twitter"):  soc.append("🐦")
    if token.get("telegram"): soc.append("📱")

    print(f"\n{c}{'═'*62}{RESET}")
    print(f"{c}{BOLD}  ${token.get('symbol','???'):<8}{RESET}  {token.get('name','Unknown')[:28]:<28}  {DIM}{token.get('mint','')[:10]}...{RESET}")
    print(f"  Score: {c}{BOLD}{score:3d}/100{RESET}  {c}[{bar}]{RESET}   MC: {BOLD}${latest_mc_usd:>8,.0f}{RESET}  {' '.join(soc)}")
    print(f"  {DIM}Buys:{len(buys)}  Sells:{len(sells)}  Wallets:{unique_buyers}  Vol:{round(total_vol_sol,2)} SOL  Watch:{len(trades)} trades{RESET}")

    if pos_fired:
        chunks = [pos_fired[i:i+3] for i in range(0, len(pos_fired), 3)]
        for chunk in chunks:
            print(f"  {GREEN}✅{RESET} {f'  {DIM}│{RESET}  '.join(s['label'] for s in chunk)}")
    if neg_fired:
        print(f"  {RED}❌{RESET} {f'  {DIM}│{RESET}  '.join(s['label'] for s in neg_fired)}")

    print(f"{c}{'═'*62}{RESET}")


def display_stats_header():
    """Print running stats periodically."""
    conn = get_db()
    c    = conn.cursor()
    c.execute("SELECT COUNT(*) as n FROM tokens")
    total = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) as n FROM tokens WHERE watch_complete=1")
    complete = c.fetchone()["n"]
    c.execute("""
        SELECT COUNT(*) as n FROM tokens t
        JOIN snapshots s ON t.mint=s.mint
        WHERE s.market_cap_usd >= ?
    """, (CONFIG["WINNER_MC_THRESHOLD_USD"],))
    winners = c.fetchone()["n"]
    conn.close()

    print(f"\n{CYAN}{'─'*62}")
    print(f"  📊 STATS  │  Seen: {total}  │  Scored: {complete}  │  Winners (>{CONFIG['WINNER_MC_THRESHOLD_USD']//1000}k): {winners}")
    print(f"{'─'*62}{RESET}\n")


# ════════════════════════════════════════════════════════
# DB PERSISTENCE
# ════════════════════════════════════════════════════════
def save_token_initial(token: dict):
    conn = get_db()
    conn.execute("""
        INSERT OR IGNORE INTO tokens
            (mint, name, symbol, developer, created_at, twitter, telegram, initial_buy_sol)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        token["mint"], token["name"], token["symbol"],
        token["developer"], int(token["created_at"]),
        token["twitter"], token["telegram"], token["initial_buy_sol"]
    ))
    conn.commit()
    conn.close()


def save_token_result(token: dict, trades: list, strategies: dict, score: int):
    conn = get_db()
    c    = conn.cursor()

    # Save trades
    for t in trades:
        mc_sol = t.get("market_cap_sol", 0)
        c.execute("""
            INSERT OR IGNORE INTO trades
                (mint, wallet, sol_amount, token_amount, is_buy, timestamp, market_cap_sol, market_cap_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            token["mint"],
            t.get("wallet", ""),
            t.get("sol_amount", 0),
            t.get("token_amount", 0),
            1 if t.get("is_buy") else 0,
            t.get("timestamp", time.time()),
            mc_sol,
            mc_sol * CONFIG["SOL_PRICE_USD"]
        ))

    buys  = [t for t in trades if t["is_buy"]]
    stats = {
        "unique_buyers": len(set(t["wallet"] for t in buys)),
        "total_buys":    len(buys),
        "total_sells":   len(trades) - len(buys),
        "total_trades":  len(trades),
        "total_vol_sol": round(sum(t["sol_amount"] for t in buys), 4),
    }

    # Serialize minimal strategy data
    strat_save = {
        k: {"fired": v["fired"], "value": v["value"], "positive": v["positive"]}
        for k, v in strategies.items()
    }

    c.execute("""
        UPDATE tokens
        SET score=?, strategies_json=?, stats_json=?, watch_complete=1
        WHERE mint=?
    """, (score, json.dumps(strat_save), json.dumps(stats), token["mint"]))

    conn.commit()
    conn.close()


# ════════════════════════════════════════════════════════
# ACTIVE WATCH STORE
# ════════════════════════════════════════════════════════
active_watches: Dict[str, dict] = {}   # mint → {token, trades}


async def watch_token(mint: str, token: dict, send_fn):
    """Deep-watch a token for WATCH_DEPTH_SECONDS then score it."""
    active_watches[mint] = {"token": token, "trades": []}

    try:
        await send_fn(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
    except Exception as e:
        log.warning(f"Subscribe failed {mint[:8]}: {e}")
        active_watches.pop(mint, None)
        return

    await asyncio.sleep(CONFIG["WATCH_DEPTH_SECONDS"])

    data       = active_watches.pop(mint, {})
    trades     = data.get("trades", [])
    strategies = evaluate_strategies(token, trades)
    score      = compute_score(strategies)

    save_token_result(token, trades, strategies, score)
    display_token_result(token, strategies, score, trades)

    try:
        await send_fn(json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]}))
    except Exception:
        pass


# ════════════════════════════════════════════════════════
# PERIODIC MC CHECKER
# ════════════════════════════════════════════════════════
async def periodic_checker():
    """Poll MC outcomes at fixed intervals after launch."""
    checks = list(zip(CONFIG["PERIODIC_CHECKS_SEC"], CONFIG["PERIODIC_LABELS"]))

    while True:
        await asyncio.sleep(60)
        now  = time.time()
        conn = get_db()
        c    = conn.cursor()

        c.execute("SELECT mint, created_at, symbol FROM tokens WHERE watch_complete=1")
        tokens = c.fetchall()

        for row in tokens:
            mint       = row["mint"]
            created_at = row["created_at"]
            sym        = row["symbol"]

            for offset, label in checks:
                target = created_at + offset
                if not (target <= now <= target + 90):
                    continue

                c.execute("SELECT id FROM snapshots WHERE mint=? AND label=?", (mint, label))
                if c.fetchone():
                    continue

                c.execute(
                    "SELECT market_cap_usd FROM trades WHERE mint=? ORDER BY timestamp DESC LIMIT 1",
                    (mint,)
                )
                tr = c.fetchone()
                if tr and tr["market_cap_usd"]:
                    mc = tr["market_cap_usd"]
                    c.execute(
                        "INSERT INTO snapshots (mint, timestamp, market_cap_usd, label) VALUES (?,?,?,?)",
                        (mint, int(now), mc, label)
                    )
                    conn.commit()

                    if mc >= CONFIG["WINNER_MC_THRESHOLD_USD"]:
                        log.info(f"{GREEN}🏆 WINNER [{label}]{RESET}  ${sym}  MC=${mc:,.0f}")

        conn.close()


# ════════════════════════════════════════════════════════
# NIGHTLY OPTIMISER
# ════════════════════════════════════════════════════════
async def nightly_optimiser():
    while True:
        now      = datetime.utcnow()
        to_mid   = ((23 - now.hour) * 3600 + (59 - now.minute) * 60 + (60 - now.second))
        log.info(f"[Optimiser] Next run in {to_mid//3600}h {(to_mid%3600)//60}m")
        await asyncio.sleep(to_mid)
        _run_optimiser()


def _run_optimiser():
    log.info("[Optimiser] Running strategy performance analysis...")
    conn = get_db()
    c    = conn.cursor()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    c.execute("""
        SELECT t.mint, t.strategies_json, MAX(s.market_cap_usd) as peak_mc
        FROM tokens t
        JOIN snapshots s ON t.mint = s.mint
        WHERE t.watch_complete=1 AND t.strategies_json IS NOT NULL
        GROUP BY t.mint
    """)
    rows = c.fetchall()

    if len(rows) < 10:
        log.info("[Optimiser] Not enough data yet (need 10+ completed tokens with snapshots).")
        conn.close()
        return

    stats = defaultdict(lambda: {"total": 0, "wins": 0})
    win_threshold = CONFIG["WINNER_MC_THRESHOLD_USD"]

    for row in rows:
        try:
            strats    = json.loads(row["strategies_json"])
            is_winner = (row["peak_mc"] or 0) >= win_threshold
        except Exception:
            continue
        for name, s in strats.items():
            if s["fired"]:
                stats[name]["total"] += 1
                if is_winner:
                    stats[name]["wins"] += 1

    print(f"\n{CYAN}{'═'*58}")
    print(f"  🧠 OPTIMISER RESULTS  ({today})")
    print(f"{'─'*58}")
    print(f"  {'Strategy':<30} {'Sigs':>5} {'Wins':>5} {'Win%':>6} {'Weight':>7}")
    print(f"{'─'*58}{RESET}")

    for name, stat in sorted(stats.items(), key=lambda x: x[1]["wins"]/max(x[1]["total"],1), reverse=True):
        if stat["total"] < 5:
            continue
        wr     = stat["wins"] / stat["total"]
        weight = max(0.2, min(2.0, 0.2 + wr * 1.6))
        bar    = GREEN if wr > 0.5 else (YELLOW if wr > 0.3 else RED)
        print(f"  {bar}{name:<30}{RESET} {stat['total']:>5} {stat['wins']:>5} {wr*100:>5.1f}% {weight:>7.2f}")
        c.execute("""
            INSERT OR REPLACE INTO strategy_performance
                (strategy_name, date, total_signals, wins, win_rate, weight)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (name, today, stat["total"], stat["wins"], round(wr, 4), weight))

    conn.commit()
    conn.close()
    print(f"{CYAN}{'═'*58}{RESET}")
    log.info("[Optimiser] Weights updated ✓")


# ════════════════════════════════════════════════════════
# TEST / SIMULATION MODE
# ════════════════════════════════════════════════════════
async def run_simulation():
    """Simulate token launches to test all logic without a live WS connection."""
    import random, string

    log.info(f"{YELLOW}⚡ SIMULATION MODE — generating fake tokens{RESET}")

    scenarios = [
        {   # High quality token
            "symbol": "MOON", "name": "Moon Token", "twitter": "x.com/moon", "telegram": "t.me/moon",
            "initial_buy_sol": 1.2,
            "trades_config": {"whale_count": 3, "micro_count": 0, "total_buys": 80,
                              "total_sells": 8, "spread_windows": 10, "dev_sells": False}
        },
        {   # Bot swarm token
            "symbol": "SWARM", "name": "Swarm Bot", "twitter": "", "telegram": "t.me/swarm",
            "initial_buy_sol": 0.1,
            "trades_config": {"whale_count": 0, "micro_count": 25, "total_buys": 60,
                              "total_sells": 5, "spread_windows": 2, "dev_sells": False}
        },
        {   # Rug / bundle
            "symbol": "RUG", "name": "Totally Safe", "twitter": "", "telegram": "",
            "initial_buy_sol": 0.05,
            "trades_config": {"whale_count": 0, "micro_count": 0, "total_buys": 15,
                              "total_sells": 12, "spread_windows": 1, "dev_sells": True,
                              "bundle": True}
        },
        {   # Mid token
            "symbol": "MID", "name": "Average Launch", "twitter": "x.com/mid", "telegram": "",
            "initial_buy_sol": 0.3,
            "trades_config": {"whale_count": 1, "micro_count": 5, "total_buys": 35,
                              "total_sells": 6, "spread_windows": 5, "dev_sells": False}
        },
    ]

    for i, scenario in enumerate(scenarios):
        now = time.time()
        mint = "SIM" + "".join(random.choices(string.ascii_uppercase + string.digits, k=40))
        cfg  = scenario["trades_config"]

        token = {
            "mint":           mint,
            "name":           scenario["name"],
            "symbol":         scenario["symbol"],
            "developer":      "DEV" + "x" * 40,
            "created_at":     now,
            "twitter":        scenario["twitter"],
            "telegram":       scenario["telegram"],
            "initial_buy_sol": scenario["initial_buy_sol"],
        }

        log.info(f"🆕 [SIM {i+1}] ${scenario['symbol']} — {scenario['name']}")
        save_token_initial(token)

        # Build realistic trade list
        trades = []

        # Bundle buys (first second)
        if cfg.get("bundle"):
            for _ in range(7):
                trades.append({"wallet": f"BND{random.randint(1,5):03d}" + "x"*39,
                               "sol_amount": 0.5, "token_amount": 500000,
                               "is_buy": True, "timestamp": now + 0.2 + random.random()*0.5,
                               "market_cap_sol": 5})

        # Whale buys (first 30s)
        for j in range(cfg["whale_count"]):
            trades.append({"wallet": f"WHL{j:03d}" + "x"*39,
                           "sol_amount": 1.2 + random.random(), "token_amount": 800000,
                           "is_buy": True, "timestamp": now + 5 + j * 8,
                           "market_cap_sol": 10 + j * 5})

        # Micro buys (first 5s)
        for j in range(cfg["micro_count"]):
            trades.append({"wallet": f"MCR{j:03d}" + "x"*39,
                           "sol_amount": 0.01 + random.random() * 0.01, "token_amount": 5000,
                           "is_buy": True, "timestamp": now + random.random() * 4,
                           "market_cap_sol": 3})

        # Regular buys spread across windows
        spread = cfg["spread_windows"]
        for j in range(cfg["total_buys"]):
            window_sec = (j / max(cfg["total_buys"], 1)) * (spread / 12) * 120
            trades.append({"wallet": f"REG{j:03d}" + "x"*39,
                           "sol_amount": 0.05 + random.random() * 0.3, "token_amount": 50000,
                           "is_buy": True, "timestamp": now + window_sec + random.random() * 5,
                           "market_cap_sol": 5 + j * 0.5})

        # Sells
        for j in range(cfg["total_sells"]):
            trades.append({"wallet": f"SLL{j:03d}" + "x"*39,
                           "sol_amount": 0.1, "token_amount": 100000,
                           "is_buy": False, "timestamp": now + 60 + j * 5,
                           "market_cap_sol": 40})

        # Dev dump
        if cfg["dev_sells"]:
            trades.append({"wallet": token["developer"],
                           "sol_amount": 2.0, "token_amount": 2000000,
                           "is_buy": False, "timestamp": now + 30,
                           "market_cap_sol": 15})

        trades.sort(key=lambda x: x["timestamp"])

        strategies = evaluate_strategies(token, trades)
        score      = compute_score(strategies)

        save_token_result(token, trades, strategies, score)
        display_token_result(token, strategies, score, trades)

        await asyncio.sleep(0.5)

    print(f"\n{GREEN}✅ Simulation complete — {len(scenarios)} tokens processed{RESET}")
    print(f"   Database: {CONFIG['DB_PATH']}")
    print(f"   Scores: check tracker.db  │  Run live with TEST_MODE=False\n")


# ════════════════════════════════════════════════════════
# MAIN WEBSOCKET LOOP
# ════════════════════════════════════════════════════════
async def run_live():
    reconnect_delay = 5

    while True:
        try:
            async with websockets.connect(
                CONFIG["WS_URL"],
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
                max_size=2**23,
            ) as ws:
                log.info("✅ Connected to PumpPortal WebSocket")
                reconnect_delay = 5

                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                log.info("📡 Subscribed to new token stream\n")

                last_stats = time.time()
                msg_count  = 0

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    msg_count += 1
                    if time.time() - last_stats > 300:
                        display_stats_header()
                        last_stats = time.time()

                    tx_type = data.get("txType", "")
                    mint    = data.get("mint",   "")
                    if not mint:
                        continue

                    # ── New token ──────────────────────────
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

                        soc = []
                        if token["twitter"]:  soc.append("🐦")
                        if token["telegram"]: soc.append("📱")
                        log.info(f"🆕 ${token['symbol']:<8}  {token['name'][:30]}  {''.join(soc)}")

                        save_token_initial(token)

                        if len(active_watches) < CONFIG["MAX_ACTIVE_WATCHES"]:
                            asyncio.create_task(watch_token(mint, token, ws.send))
                        else:
                            log.warning(f"Max watches ({CONFIG['MAX_ACTIVE_WATCHES']}) reached — skipping {mint[:8]}")

                    # ── Trade event ────────────────────────
                    elif tx_type in ("buy", "sell"):
                        if mint in active_watches:
                            active_watches[mint]["trades"].append({
                                "wallet":        data.get("traderPublicKey", ""),
                                "sol_amount":    data.get("solAmount",       0),
                                "token_amount":  data.get("tokenAmount",     0),
                                "is_buy":        tx_type == "buy",
                                "timestamp":     time.time(),
                                "market_cap_sol": data.get("marketCapSol",  0),
                            })

        except ConnectionClosed as e:
            log.warning(f"Disconnected ({e}). Reconnect in {reconnect_delay}s...")
        except Exception as e:
            log.error(f"Error: {e}. Reconnect in {reconnect_delay}s...")

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)


# ════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════
async def main():
    init_db()
    log.info("🚀 PumpFun Tracker")
    log.info(f"   Watch window:      {CONFIG['WATCH_DEPTH_SECONDS']}s")
    log.info(f"   Winner threshold:  ${CONFIG['WINNER_MC_THRESHOLD_USD']:,}")
    log.info(f"   Strategies:        21")
    log.info(f"   Mode:              {'SIMULATION' if CONFIG['TEST_MODE'] else 'LIVE'}")

    # Start dashboard web server
    try:
        from dashboard import start as start_dashboard
        asyncio.create_task(start_dashboard())
        port = int(os.environ.get("PORT", 8080))
        log.info(f"   Dashboard:         http://localhost:{port}")
    except ImportError:
        log.warning("dashboard.py not found — running without web UI")

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
        log.info("Tracker stopped.")
