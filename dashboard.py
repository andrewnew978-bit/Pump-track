#!/usr/bin/env python3
"""
PumpTracker Dashboard — FastAPI server
Run via: uvicorn dashboard:app --host 0.0.0.0 --port $PORT
"""

import json, os, time, sqlite3, csv, io
from collections import defaultdict
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

app = FastAPI(title="PumpTracker")

DB_PATH          = os.environ.get("DB_PATH",                  "tracker.db")
WINNER_THRESHOLD = float(os.environ.get("WINNER_MC_THRESHOLD_SOL", "133"))
ENTRY_SCORE      = int(os.environ.get("MIN_SCORE_HIGHLIGHT",        "60"))
BET_SOL          = float(os.environ.get("BET_SIZE_SOL",             "0.1"))

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def to_dict(row): return dict(zip(row.keys(), row))

def hydrate(rows):
    out = []
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else r
        if d.get("strategies_json"):
            d["strategies"] = json.loads(d["strategies_json"])
        if d.get("stats_json"):
            d["stats"] = json.loads(d["stats_json"])
        d.pop("strategies_json", None)
        d.pop("stats_json", None)
        out.append(d)
    return out

# ════════════════════════════════════════════════════════
# API ENDPOINTS
# ════════════════════════════════════════════════════════
@app.get("/api/stats")
def get_stats():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as n FROM tokens"); total = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM tokens WHERE watch_complete=1"); scored = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM tokens WHERE watch_complete=0"); watching = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM tokens WHERE score>=? AND watch_complete=1", (ENTRY_SCORE,)); entries = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM entries WHERE exit_reason LIKE 'TAKE%'"); wins = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM entries WHERE exit_reason='STOP_LOSS'"); losses = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM entries WHERE exit_time IS NULL"); open_trades = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM trades"); trade_count = c.fetchone()["n"]
        c.execute("""SELECT COUNT(DISTINCT t.mint) as n FROM tokens t
                     JOIN snapshots s ON t.mint=s.mint WHERE s.market_cap_sol>=?""", (WINNER_THRESHOLD,))
        winners = c.fetchone()["n"]
        return {"total":total,"scored":scored,"watching":watching,"entries":entries,
                "wins":wins,"losses":losses,"open_trades":open_trades,
                "winners":winners,"trade_count":trade_count,
                "entry_rate":round(entries/max(scored,1)*100,1),
                "win_rate":round(wins/max(entries,1)*100,1),
                "last_updated":int(time.time())}
    finally:
        conn.close()


@app.get("/api/live")
def get_live(limit: int = 80):
    conn = get_db()
    try:
        c   = conn.cursor()
        now = int(time.time())
        c.execute("""
            SELECT t.*,
                COUNT(tr.id) AS live_trade_count,
                MAX(tr.market_cap_sol) AS current_mc_sol,
                MAX(s.market_cap_sol) AS peak_mc_sol,
                (? - t.created_at) AS age_seconds
            FROM tokens t
            LEFT JOIN trades tr ON tr.mint=t.mint
            LEFT JOIN snapshots s ON s.mint=t.mint
            GROUP BY t.mint
            ORDER BY t.created_at DESC LIMIT ?
        """, (now, limit))
        return hydrate(c.fetchall())
    finally:
        conn.close()


@app.get("/api/entries")
def get_entries():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT t.*, MAX(s.market_cap_sol) as peak_mc_sol
            FROM tokens t LEFT JOIN snapshots s ON t.mint=s.mint
            WHERE t.watch_complete=1 AND t.score>=?
            GROUP BY t.mint ORDER BY t.created_at DESC
        """, (ENTRY_SCORE,))
        return hydrate(c.fetchall())
    finally:
        conn.close()


@app.get("/api/tradelog")
def get_tradelog():
    """All entry signals with performance data — the trade log."""
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT e.*, t.symbol, t.name, t.twitter, t.telegram, t.mint as tok_mint,
                   t.strategies_json, t.stats_json, t.score as tok_score
            FROM entries e JOIN tokens t ON e.mint=t.mint
            ORDER BY e.entry_time DESC
        """)
        rows = []
        for r in c.fetchall():
            d = dict(r)
            if d.get("strategies_json"):
                d["strategies"] = json.loads(d["strategies_json"])
            if d.get("stats_json"):
                d["stats"] = json.loads(d["stats_json"])
            d.pop("strategies_json", None)
            d.pop("stats_json", None)
            d["roi"] = round(d["exit_mc_sol"] / d["entry_mc_sol"], 2) if d.get("exit_mc_sol") and d.get("entry_mc_sol") else None
            d["max_roi"] = round(d["max_mc_sol"] / d["entry_mc_sol"], 2) if d.get("max_mc_sol") and d.get("entry_mc_sol") else None
            rows.append(d)
        return rows
    finally:
        conn.close()


@app.get("/api/pumped")
def get_pumped():
    """All tokens that hit the MC threshold — with entry/skip decision and reason."""
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT * FROM (
                SELECT t.*,
                    COALESCE(
                        (SELECT MAX(tr.market_cap_sol) FROM trades tr WHERE tr.mint=t.mint),
                        0
                    ) as peak_mc_sol
                FROM tokens t WHERE t.watch_complete=1
            ) WHERE peak_mc_sol >= ?
            ORDER BY peak_mc_sol DESC
        """, (WINNER_THRESHOLD,))
        rows = hydrate(c.fetchall())
        # Tag each with entry status
        for row in rows:
            c.execute("SELECT id FROM entries WHERE mint=?", (row["mint"],))
            row["was_entered"] = c.fetchone() is not None
        return rows
    finally:
        conn.close()


@app.get("/api/missed")
def get_missed():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT t.*, MAX(s.market_cap_sol) as peak_mc_sol
            FROM tokens t JOIN snapshots s ON t.mint=s.mint
            WHERE s.market_cap_sol>=? AND (t.score<? OR t.score IS NULL)
            GROUP BY t.mint ORDER BY peak_mc_sol DESC
        """, (WINNER_THRESHOLD, ENTRY_SCORE))
        return hydrate(c.fetchall())
    finally:
        conn.close()


@app.get("/api/winners")
def get_winners():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT t.*, MAX(s.market_cap_sol) as peak_mc_sol
            FROM tokens t JOIN snapshots s ON t.mint=s.mint
            WHERE s.market_cap_sol>=? AND t.score>=?
            GROUP BY t.mint ORDER BY peak_mc_sol DESC
        """, (WINNER_THRESHOLD, ENTRY_SCORE))
        return hydrate(c.fetchall())
    finally:
        conn.close()


@app.get("/api/strategies")
def get_strategies():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT * FROM strategy_performance
                     WHERE date=(SELECT MAX(date) FROM strategy_performance)
                     ORDER BY win_rate DESC""")
        perf = [to_dict(r) for r in c.fetchall()]
        if not perf:
            c.execute("""SELECT t.strategies_json, MAX(s.market_cap_sol) as peak_mc
                         FROM tokens t LEFT JOIN snapshots s ON t.mint=s.mint
                         WHERE t.watch_complete=1 AND t.strategies_json IS NOT NULL
                         GROUP BY t.mint""")
            stats = defaultdict(lambda: {"total":0,"wins":0})
            for row in c.fetchall():
                try:
                    strats = json.loads(row["strategies_json"])
                    winner = (row["peak_mc"] or 0) >= WINNER_THRESHOLD
                    for name, s in strats.items():
                        if s.get("fired"):
                            stats[name]["total"] += 1
                            if winner: stats[name]["wins"] += 1
                except: pass
            perf = sorted([{"strategy_name":n,"total_signals":s["total"],"wins":s["wins"],
                            "win_rate":round(s["wins"]/max(s["total"],1),4),"weight":1.0}
                           for n,s in stats.items()], key=lambda x: x["win_rate"], reverse=True)
        return perf
    finally:
        conn.close()


@app.get("/api/score_bands")
def get_score_bands():
    """
    Groups all scored tokens into 10-point brackets and shows win rate per bracket.
    'Winner' = token's peak MC (from trades) hit WINNER_THRESHOLD.
    """
    conn = get_db()
    try:
        c = conn.cursor()
        # Get every scored token with its peak MC from trades
        c.execute("""
            SELECT t.score,
                   COALESCE((SELECT MAX(tr.market_cap_sol) FROM trades tr WHERE tr.mint=t.mint), 0) AS peak_mc
            FROM tokens t
            WHERE t.watch_complete=1 AND t.score IS NOT NULL
        """)
        rows = c.fetchall()
    finally:
        conn.close()

    # Build brackets: 0-9, 10-19, ..., 90-100
    brackets = {}
    for lo in range(0, 100, 10):
        hi = lo + 9 if lo < 90 else 100
        brackets[(lo, hi)] = {"label": f"{lo}–{hi}", "lo": lo, "hi": hi,
                               "total": 0, "winners": 0, "peak_mcs": []}

    for row in rows:
        score    = row["score"] or 0
        peak_mc  = row["peak_mc"] or 0
        lo       = min((score // 10) * 10, 90)
        hi       = lo + 9 if lo < 90 else 100
        b        = brackets[(lo, hi)]
        b["total"] += 1
        b["peak_mcs"].append(peak_mc)
        if peak_mc >= WINNER_THRESHOLD:
            b["winners"] += 1

    result = []
    for (lo, hi), b in sorted(brackets.items()):
        if b["total"] == 0:
            result.append({"label": b["label"], "lo": lo, "hi": hi,
                           "total": 0, "winners": 0, "win_rate": 0,
                           "avg_peak_mc": 0, "best_peak_mc": 0})
            continue
        win_rate     = round(b["winners"] / b["total"] * 100, 1)
        avg_peak     = round(sum(b["peak_mcs"]) / len(b["peak_mcs"]), 1)
        best_peak    = round(max(b["peak_mcs"]), 1)
        result.append({"label": b["label"], "lo": lo, "hi": hi,
                       "total": b["total"], "winners": b["winners"],
                       "win_rate": win_rate, "avg_peak_mc": avg_peak,
                       "best_peak_mc": best_peak})

    return result


@app.get("/api/snapshots/{mint}")
def get_snapshots(mint: str):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM snapshots WHERE mint=? ORDER BY timestamp", (mint,))
        return [to_dict(r) for r in c.fetchall()]
    finally:
        conn.close()


@app.get("/api/market")
def get_market():
    """
    Composite market conditions score (0-100).
    Pulls internal DB signals + external free APIs (CoinGecko, Fear & Greed).
    Gives a TRADE / CAUTION / PAUSE recommendation.
    """
    import urllib.request, urllib.error
    from datetime import datetime as dt

    conn = get_db()
    now  = time.time()
    signals = []

    try:
        c = conn.cursor()

        # ── 1. Launch rate (0-15 pts) ──────────────────────────────────
        c.execute("SELECT COUNT(*) FROM tokens WHERE created_at > ?", (now - 3600,))
        launches_1h = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM tokens WHERE created_at > ? AND created_at <= ?",
                  (now - 7200, now - 3600))
        launches_prev_1h = c.fetchone()[0] or 1
        launch_trend = launches_1h / launches_prev_1h  # >1 = accelerating
        launch_pts = min(15, int(launches_1h / 5))  # 75+ launches/h = max
        signals.append({"name": "Launch Rate", "pts": launch_pts, "max": 15,
                        "value": f"{launches_1h} launches/hr",
                        "detail": f"{'↑ Accelerating' if launch_trend > 1.2 else '↓ Slowing' if launch_trend < 0.8 else '→ Steady'} vs prev hour ({launches_prev_1h})",
                        "status": "good" if launch_pts >= 10 else "warn" if launch_pts >= 5 else "bad"})

        # ── 2. Recent token quality (0-20 pts) ────────────────────────
        c.execute("SELECT AVG(score) FROM tokens WHERE watch_complete=1 AND created_at > ? AND score IS NOT NULL",
                  (now - 7200,))
        avg_score = c.fetchone()[0] or 0
        quality_pts = min(20, int(avg_score / 5))
        signals.append({"name": "Token Quality", "pts": quality_pts, "max": 20,
                        "value": f"Avg score {avg_score:.1f}/100",
                        "detail": f"Average score of tokens launched in last 2h",
                        "status": "good" if avg_score >= 45 else "warn" if avg_score >= 30 else "bad"})

        # ── 3. Recent win rate (0-20 pts) ─────────────────────────────
        c.execute("SELECT COUNT(*) FROM tokens WHERE watch_complete=1 AND created_at > ?", (now - 14400,))
        recent_scored = c.fetchone()[0] or 1
        c.execute("""SELECT COUNT(DISTINCT t.mint) FROM tokens t JOIN snapshots s ON t.mint=s.mint
                     WHERE s.market_cap_sol >= ? AND t.created_at > ?""", (WINNER_THRESHOLD, now - 14400))
        recent_winners = c.fetchone()[0]
        win_rate_pct = recent_winners / recent_scored * 100
        winrate_pts  = min(20, int(win_rate_pct * 2))
        signals.append({"name": "Win Rate (4h)", "pts": winrate_pts, "max": 20,
                        "value": f"{win_rate_pct:.1f}% of recent tokens pumped",
                        "detail": f"{recent_winners} winners out of {recent_scored} scored in last 4h",
                        "status": "good" if win_rate_pct >= 8 else "warn" if win_rate_pct >= 4 else "bad"})

        # ── 4. Buy pressure across all recent trades (0-15 pts) ───────
        c.execute("SELECT AVG(is_buy) FROM trades WHERE timestamp > ?", (now - 3600,))
        buy_ratio = c.fetchone()[0] or 0.5
        buy_pts = min(15, int((buy_ratio - 0.5) * 60)) if buy_ratio > 0.5 else 0
        signals.append({"name": "Buy Pressure", "pts": buy_pts, "max": 15,
                        "value": f"{buy_ratio*100:.0f}% of trades are buys",
                        "detail": "Measured across all tokens in last hour",
                        "status": "good" if buy_ratio >= 0.70 else "warn" if buy_ratio >= 0.60 else "bad"})

        # ── 5. Time of day (0-10 pts) ─────────────────────────────────
        hour_utc = dt.utcnow().hour
        if 13 <= hour_utc <= 21:
            time_pts, time_status, time_desc = 10, "good", "Peak US hours (13-21 UTC)"
        elif 10 <= hour_utc <= 23:
            time_pts, time_status, time_desc = 6, "warn", "Moderate hours (10-23 UTC)"
        else:
            time_pts, time_status, time_desc = 2, "bad", "Off-peak hours (late night / early morning UTC)"
        signals.append({"name": "Time of Day", "pts": time_pts, "max": 10,
                        "value": f"{hour_utc:02d}:xx UTC",
                        "detail": time_desc,
                        "status": time_status})

        # ── 6. Graduation rate (0-10 pts) ─────────────────────────────
        c.execute("SELECT COUNT(*) FROM graduations WHERE graduated_at > ?", (now - 14400,))
        grads_4h = c.fetchone()[0]
        grad_pts = min(10, grads_4h * 2)
        signals.append({"name": "Graduation Rate", "pts": grad_pts, "max": 10,
                        "value": f"{grads_4h} graduations in 4h",
                        "detail": "Tokens completing bonding curve → bullish signal for overall activity",
                        "status": "good" if grads_4h >= 3 else "warn" if grads_4h >= 1 else "bad"})

    finally:
        conn.close()

    # ── External: SOL price (CoinGecko free) ──────────────────────────
    sol_pts = 5  # neutral default
    sol_data = {}
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd&include_24h_change=true&include_1h_vol=true"
        req = urllib.request.Request(url, headers={"User-Agent": "PumpTracker/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            cg = json.loads(r.read())["solana"]
            sol_price  = cg.get("usd", 0)
            sol_change = cg.get("usd_24h_change", 0)
            sol_data   = {"price": sol_price, "change_24h": round(sol_change, 2)}
            if sol_change >= 5:    sol_pts, sol_status = 10, "good"
            elif sol_change >= 1:  sol_pts, sol_status =  8, "good"
            elif sol_change >= -2: sol_pts, sol_status =  5, "warn"
            elif sol_change >= -5: sol_pts, sol_status =  2, "bad"
            else:                  sol_pts, sol_status =  0, "bad"
            signals.append({"name": "SOL Price Trend", "pts": sol_pts, "max": 10,
                            "value": f"${sol_price:,.2f}  ({sol_change:+.1f}% 24h)",
                            "detail": "SOL pumping = higher risk appetite across the market",
                            "status": sol_status})
    except Exception:
        signals.append({"name": "SOL Price Trend", "pts": sol_pts, "max": 10,
                        "value": "Unavailable", "detail": "CoinGecko request failed",
                        "status": "warn"})

    # ── External: Crypto Fear & Greed Index ───────────────────────────
    fng_pts = 5
    try:
        req = urllib.request.Request("https://api.alternative.me/fng/",
                                     headers={"User-Agent": "PumpTracker/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            fng   = json.loads(r.read())["data"][0]
            fng_v = int(fng["value"])
            fng_l = fng["value_classification"]
            if fng_v >= 75:    fng_pts, fng_status = 10, "good"
            elif fng_v >= 55:  fng_pts, fng_status =  8, "good"
            elif fng_v >= 40:  fng_pts, fng_status =  5, "warn"
            elif fng_v >= 25:  fng_pts, fng_status =  2, "bad"
            else:              fng_pts, fng_status =  0, "bad"
            signals.append({"name": "Fear & Greed Index", "pts": fng_pts, "max": 10,
                            "value": f"{fng_v}/100 — {fng_l}",
                            "detail": "High greed = retail FOMO active, memecoins outperform",
                            "status": fng_status})
    except Exception:
        signals.append({"name": "Fear & Greed Index", "pts": fng_pts, "max": 10,
                        "value": "Unavailable", "detail": "alternative.me request failed",
                        "status": "warn"})

    total   = sum(s["pts"] for s in signals)
    max_pts = sum(s["max"] for s in signals)
    pct     = round(total / max_pts * 100)

    if pct >= 65:   rec, rec_color = "TRADE",  "green"
    elif pct >= 40: rec, rec_color = "CAUTION","amber"
    else:           rec, rec_color = "PAUSE",  "red"

    return {
        "score": pct, "total_pts": total, "max_pts": max_pts,
        "recommendation": rec, "rec_color": rec_color,
        "signals": signals, "sol": sol_data,
        "timestamp": int(now),
    }


@app.get("/api/portfolio")
def get_portfolio():
    """Simulate 9 exit strategies across all real entries and compare P&L."""
    bet = BET_SOL
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT e.mint, e.entry_mc_sol, e.exit_mc_sol, e.max_mc_sol,
                            e.exit_reason, e.hold_seconds, t.symbol, t.name
                     FROM entries e JOIN tokens t ON e.mint=t.mint""")
        entries = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()

    STRATS = [
        {"id":"bot_actual",     "name":"Bot's Actual Exits",           "desc":"Exactly what the bot did"},
        {"id":"full_1_5x",      "name":"All Out at 1.5x",              "desc":"100% exit at 1.5x, bot logic otherwise"},
        {"id":"full_2x",        "name":"All Out at 2x",                "desc":"100% exit at 2x, bot logic otherwise"},
        {"id":"full_3x",        "name":"All Out at 3x",                "desc":"100% exit at 3x, bot logic otherwise"},
        {"id":"half_1_5x_bot",  "name":"50% @ 1.5x / 50% Bot",        "desc":"Lock half at 1.5x, let bot run the rest"},
        {"id":"half_2x_bot",    "name":"50% @ 2x / 50% Bot",          "desc":"Lock half at 2x, let bot run the rest"},
        {"id":"half_1_5x_peak", "name":"50% @ 1.5x / 50% at Peak",    "desc":"Half at 1.5x, sell other half at highest MC seen"},
        {"id":"thirds",         "name":"33% @ 1.5x / 33% @ 2x / 34% Bot","desc":"Three tranches — locks profit, lets rest run"},
        {"id":"peak_ideal",     "name":"Always at Peak (theoretical)", "desc":"Perfect exits — what if you always sold at max MC"},
    ]

    def sim(e, sid):
        emc = e["entry_mc_sol"] or 0.001
        xmc = e.get("exit_mc_sol") or emc
        mmc = max(e.get("max_mc_sol") or emc, xmc)
        r   = lambda mc: mc / emc
        b   = bet
        if   sid == "bot_actual":     g = b * r(xmc)
        elif sid == "peak_ideal":     g = b * r(mmc)
        elif sid == "full_1_5x":      g = b*1.5         if r(mmc)>=1.5 else b*r(xmc)
        elif sid == "full_2x":        g = b*2.0         if r(mmc)>=2.0 else b*r(xmc)
        elif sid == "full_3x":        g = b*3.0         if r(mmc)>=3.0 else b*r(xmc)
        elif sid == "half_1_5x_bot":  g = 0.5*b*1.5+0.5*b*r(xmc)  if r(mmc)>=1.5 else b*r(xmc)
        elif sid == "half_2x_bot":    g = 0.5*b*2.0+0.5*b*r(xmc)  if r(mmc)>=2.0 else b*r(xmc)
        elif sid == "half_1_5x_peak": g = 0.5*b*1.5+0.5*b*r(mmc)  if r(mmc)>=1.5 else b*r(xmc)
        elif sid == "thirds":
            if   r(mmc)>=2.0: g = 0.33*b*1.5+0.33*b*2.0+0.34*b*r(xmc)
            elif r(mmc)>=1.5: g = 0.33*b*1.5+0.67*b*r(xmc)
            else:             g = b*r(xmc)
        else: g = b*r(xmc)
        return round(g - b, 5)

    results = []
    for st in STRATS:
        tpnls = [{"mint":e["mint"],"symbol":e["symbol"],"pnl":sim(e,st["id"]),"roi":round(1+sim(e,st["id"])/bet,3)} for e in entries]
        wins   = [t for t in tpnls if t["pnl"] > 0]
        losses = [t for t in tpnls if t["pnl"] <= 0]
        tot    = round(sum(t["pnl"] for t in tpnls), 4)
        results.append({**st,
            "total_pnl":   tot,
            "avg_pnl":     round(tot/max(len(tpnls),1), 5),
            "win_count":   len(wins), "loss_count": len(losses),
            "win_rate":    round(len(wins)/max(len(tpnls),1)*100, 1),
            "best_trade":  round(max((t["pnl"] for t in tpnls), default=0), 4),
            "worst_trade": round(min((t["pnl"] for t in tpnls), default=0), 4),
            "trades":      sorted(tpnls, key=lambda x: x["pnl"], reverse=True),
        })

    results.sort(key=lambda x: x["total_pnl"], reverse=True)
    return {"bet_sol":bet, "total_trades":len(entries),
            "total_deployed":round(len(entries)*bet,4), "strategies":results}


@app.get("/api/ai_export")
def ai_export():
    """
    Generates a comprehensive markdown document containing all trade and signal data
    for AI strategy analysis. Includes every data point that could inform strategy improvement.
    """
    conn = get_db()
    now  = time.time()
    try:
        c = conn.cursor()

        # ── Summary stats ─────────────────────────────────
        c.execute("SELECT COUNT(*) FROM tokens WHERE watch_complete=1"); total_scored = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM entries"); total_entries = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM entries WHERE exit_reason LIKE 'TAKE%'"); total_wins = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM entries WHERE exit_reason='STOP_LOSS'"); total_losses = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM graduations"); total_grads = c.fetchone()[0]

        # ── All entries with full detail ──────────────────
        c.execute("""
            SELECT e.mint, e.entry_time, e.entry_mc_sol, e.exit_time, e.exit_mc_sol,
                   e.exit_reason, e.exit_detail, e.max_mc_sol, e.hold_seconds, e.score,
                   t.symbol, t.name, t.developer, t.twitter, t.telegram,
                   t.initial_buy_sol, t.created_at, t.strategies_json, t.stats_json
            FROM entries e JOIN tokens t ON e.mint=t.mint
            ORDER BY e.entry_time DESC
        """)
        entries = [dict(r) for r in c.fetchall()]

        # ── Missed winners (pumped but not entered) ───────
        c.execute("""
            SELECT t.mint, t.symbol, t.name, t.score, t.developer,
                   t.twitter, t.telegram, t.initial_buy_sol, t.created_at,
                   t.strategies_json, t.stats_json,
                   MAX(s.market_cap_sol) as peak_mc
            FROM tokens t JOIN snapshots s ON t.mint=s.mint
            WHERE s.market_cap_sol >= ? AND (t.score < ? OR t.score IS NULL)
            GROUP BY t.mint ORDER BY peak_mc DESC
        """, (WINNER_THRESHOLD, ENTRY_SCORE))
        missed = [dict(r) for r in c.fetchall()]

        # ── Score band win rates ───────────────────────────
        c.execute("""
            SELECT t.score,
                   COALESCE((SELECT MAX(tr.market_cap_sol) FROM trades tr WHERE tr.mint=t.mint),0) as peak_mc
            FROM tokens t WHERE t.watch_complete=1 AND t.score IS NOT NULL
        """)
        band_rows = c.fetchall()

        # ── Strategy win rates ────────────────────────────
        c.execute("""SELECT * FROM strategy_performance
                     WHERE date=(SELECT MAX(date) FROM strategy_performance)
                     ORDER BY win_rate DESC""")
        strat_perf = [dict(r) for r in c.fetchall()]

        # ── Dev wallet hall of fame/shame ─────────────────
        c.execute("""
            SELECT wallet, COUNT(*) as launches, SUM(is_winner) as wins,
                   SUM(is_rug) as rugs, AVG(peak_mc_sol) as avg_peak
            FROM dev_history
            GROUP BY wallet HAVING launches >= 2
            ORDER BY wins DESC LIMIT 20
        """)
        dev_stats = [dict(r) for r in c.fetchall()]

    finally:
        conn.close()

    # ── Build score bands ─────────────────────────────────
    bands = defaultdict(lambda: {"total": 0, "winners": 0})
    for row in band_rows:
        lo = min((row["score"] // 10) * 10, 90)
        bands[lo]["total"] += 1
        if row["peak_mc"] >= WINNER_THRESHOLD:
            bands[lo]["winners"] += 1

    def fmt_strats(strats_json):
        """Format strategy signals as readable lines."""
        if not strats_json:
            return "  No strategy data"
        try:
            strats = json.loads(strats_json)
        except Exception:
            return "  Parse error"
        lines = []
        for k, v in strats.items():
            status = "✅ FIRED" if v.get("fired") else "❌ miss"
            kind   = "positive" if v.get("positive") else "NEGATIVE"
            label  = v.get("label", k)
            detail = v.get("detail", "")
            lines.append(f"  [{status}] [{kind:8}] {label}: {detail}")
        return "\n".join(lines)

    def fmt_stats(stats_json):
        if not stats_json:
            return ""
        try:
            s = json.loads(stats_json)
            return (f"  Buys:{s.get('total_buys','?')}  Sells:{s.get('total_sells','?')}  "
                    f"Unique wallets:{s.get('unique_buyers','?')}  Vol:{s.get('total_vol_sol','?')} SOL")
        except Exception:
            return ""

    lines = []
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))

    # ════════════════════════════════════════════════
    lines.append(f"# PumpFun Tracker — Full AI Strategy Assessment Export")
    lines.append(f"Generated: {ts}")
    lines.append(f"Threshold for 'winner': {WINNER_THRESHOLD} SOL peak MC (~$20k)")
    lines.append(f"Entry score threshold: {ENTRY_SCORE}/100")
    lines.append(f"Bet size per trade: {BET_SOL} SOL")
    lines.append("")

    # ── OVERVIEW ─────────────────────────────────────
    lines.append("## OVERVIEW")
    lines.append(f"- Total tokens scored: {total_scored}")
    lines.append(f"- Total entry signals fired: {total_entries}")
    win_rate = round(total_wins/max(total_entries,1)*100,1)
    lines.append(f"- Wins (take profit): {total_wins}  ({win_rate}% of entries)")
    lines.append(f"- Losses (stop loss): {total_losses}")
    lines.append(f"- Token graduations to Raydium: {total_grads}")
    lines.append("")

    # ── SCORE BAND WIN RATES ──────────────────────────
    lines.append("## SCORE BAND WIN RATES")
    lines.append("Score range | Tokens | Winners | Win rate | Notes")
    lines.append("------------|--------|---------|----------|------")
    for lo in range(0, 100, 10):
        hi  = lo + 9 if lo < 90 else 100
        b   = bands[lo]
        wr  = round(b["winners"]/max(b["total"],1)*100,1)
        note = " << entry threshold" if lo == 60 else ""
        note += " (low sample)" if b["total"] < 3 else ""
        lines.append(f"{lo:2d}–{hi:3d}       | {b['total']:6d} | {b['winners']:7d} | {wr:7.1f}% |{note}")
    lines.append("")

    # ── STRATEGY PERFORMANCE ──────────────────────────
    lines.append("## INDIVIDUAL STRATEGY WIN RATES")
    lines.append("(From nightly optimiser — how often each strategy signal correlates with winners)")
    lines.append("")
    if strat_perf:
        lines.append(f"{'Strategy':<35} {'Type':<9} {'Signals':>7} {'Wins':>5} {'Win%':>6} {'Weight':>7}")
        lines.append("-" * 70)
        for sp in strat_perf:
            kind = "NEGATIVE" if sp["strategy_name"] in (
                "early_dev_dump","single_wallet_dominance","wash_trading",
                "no_socials","bundle_detector","early_sell_pressure","known_rugger") else "positive"
            lines.append(f"{sp['strategy_name']:<35} {kind:<9} {sp['total_signals']:>7} "
                         f"{sp['wins']:>5} {sp['win_rate']*100:>6.1f}% {sp['weight']:>7.2f}")
    else:
        lines.append("No strategy performance data yet — needs nightly optimiser to run (requires 10+ tokens with snapshot data).")
    lines.append("")

    # ── DEV WALLET HISTORY ────────────────────────────
    if dev_stats:
        lines.append("## DEV WALLET HISTORY (repeat launchers, ≥2 launches)")
        lines.append(f"{'Wallet':<46} {'Launches':>8} {'Wins':>5} {'Rugs':>5} {'Avg Peak MC':>12}")
        lines.append("-" * 80)
        for d in dev_stats:
            lines.append(f"{d['wallet']:<46} {d['launches']:>8} {d['wins'] or 0:>5} "
                         f"{d['rugs'] or 0:>5} {d['avg_peak'] or 0:>11.1f} SOL")
        lines.append("")

    # ── ENTERED TRADES ────────────────────────────────
    lines.append("## ENTERED TRADES — FULL DETAIL")
    lines.append(f"Total: {len(entries)}")
    lines.append("")
    for e in entries:
        roi     = round(e["exit_mc_sol"]/e["entry_mc_sol"],2) if e.get("exit_mc_sol") and e.get("entry_mc_sol") else None
        max_roi = round(e["max_mc_sol"]/e["entry_mc_sol"],2)  if e.get("max_mc_sol")  and e.get("entry_mc_sol") else None
        outcome = e.get("exit_reason") or "OPEN"
        entry_t = time.strftime("%Y-%m-%d %H:%M", time.gmtime(e["entry_time"])) if e.get("entry_time") else "?"
        exit_t  = time.strftime("%Y-%m-%d %H:%M", time.gmtime(e["exit_time"]))  if e.get("exit_time")  else "still open"
        hold    = f"{e['hold_seconds']//60}m" if e.get("hold_seconds") else "open"

        lines.append(f"### ${e.get('symbol','?')} — {e.get('name','?')}")
        lines.append(f"Mint:         {e['mint']}")
        lines.append(f"Score:        {e.get('score','?')}/100")
        lines.append(f"Has Twitter:  {'Yes' if e.get('twitter') else 'No'}  |  Has Telegram: {'Yes' if e.get('telegram') else 'No'}")
        lines.append(f"Dev init buy: {e.get('initial_buy_sol', 0):.3f} SOL")
        lines.append(f"Entry time:   {entry_t}")
        lines.append(f"Entry MC:     {e.get('entry_mc_sol', 0):.2f} SOL")
        lines.append(f"Exit time:    {exit_t}")
        lines.append(f"Exit MC:      {e.get('exit_mc_sol', 0) or '—'}{' SOL' if e.get('exit_mc_sol') else ''}")
        lines.append(f"Exit reason:  {outcome}")
        if e.get("exit_detail"):
            lines.append(f"Exit detail:  {e['exit_detail']}")
        lines.append(f"Max MC seen:  {e.get('max_mc_sol', 0):.2f} SOL  (best possible: {max_roi}x)")
        lines.append(f"Actual ROI:   {roi}x" if roi else "Actual ROI:  open")
        lines.append(f"Hold time:    {hold}")
        lines.append(f"Trade stats:  {fmt_stats(e.get('stats_json',''))}")
        lines.append("Strategy signals:")
        lines.append(fmt_strats(e.get("strategies_json", "")))
        lines.append("")

    # ── MISSED WINNERS ────────────────────────────────
    lines.append("## MISSED WINNERS — FULL DETAIL")
    lines.append(f"Tokens that pumped above {WINNER_THRESHOLD} SOL but scored below {ENTRY_SCORE}: {len(missed)}")
    lines.append("These are the most valuable for improving entry strategy.")
    lines.append("")
    for m in missed:
        peak   = m.get("peak_mc", 0)
        score  = m.get("score") or 0
        gap    = ENTRY_SCORE - score
        launch = time.strftime("%Y-%m-%d %H:%M", time.gmtime(m["created_at"])) if m.get("created_at") else "?"

        lines.append(f"### ${m.get('symbol','?')} — {m.get('name','?')}")
        lines.append(f"Mint:         {m['mint']}")
        lines.append(f"Score:        {score}/100  (missed entry by {gap} points)")
        lines.append(f"Peak MC:      {peak:.1f} SOL  ({peak/WINNER_THRESHOLD:.1f}x above threshold)")
        lines.append(f"Launched:     {launch}")
        lines.append(f"Has Twitter:  {'Yes' if m.get('twitter') else 'No'}  |  Has Telegram: {'Yes' if m.get('telegram') else 'No'}")
        lines.append(f"Dev init buy: {m.get('initial_buy_sol', 0):.3f} SOL")
        lines.append(f"Trade stats:  {fmt_stats(m.get('stats_json',''))}")
        lines.append("Strategy signals (what fired, what didn't — why we missed it):")
        lines.append(fmt_strats(m.get("strategies_json", "")))
        lines.append("")

    # ── AI PROMPTING GUIDE ────────────────────────────
    lines.append("## HOW TO USE THIS DOCUMENT WITH AI")
    lines.append("""
Paste this document into an AI model (Claude, GPT-4, etc.) with a prompt like:

> "You are a quantitative analyst reviewing a pump.fun memecoin signal system.
> The data above shows all trades entered, all missed winners, strategy signal win rates,
> and score band performance. Please:
> 1. Identify which strategies have the highest win rate and should be weighted more heavily
> 2. Identify strategies that appear to be hurting performance (high fire rate, low win rate)
> 3. Look at the missed winners — what signals DID fire vs what didn't — 
>    what threshold adjustments would have caught more of them?
> 4. Look at losing trades — what signals should have flagged them as risky?
> 5. Suggest an optimal entry score threshold based on the score band data
> 6. Suggest any new signal ideas based on patterns you notice in the data"

The more data you accumulate (days/weeks of running), the more useful this export becomes.
""")

    content = "\n".join(lines)
    buf = io.BytesIO(content.encode("utf-8"))
    fname = f"pump_strategy_export_{time.strftime('%Y%m%d_%H%M')}.md"
    return StreamingResponse(buf, media_type="text/markdown",
                             headers={"Content-Disposition": f"attachment; filename={fname}"})


# ════════════════════════════════════════════════════════
# DASHBOARD HTML
# ════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>PUMP TRACKER</title>
<link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@300;400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#07080f;--bg2:#0c0e1a;--bg3:#131525;--border:#1a1e30;--border2:#242840;
  --green:#00ff88;--cyan:#00cfff;--amber:#ffbb00;--red:#ff3355;--purple:#a78bfa;
  --text:#b8bcc8;--dim:#4a5068;--bright:#e8eaf0;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'Fira Code',monospace;font-size:13px;overflow-x:hidden}

/* HEADER */
.header{background:var(--bg2);border-bottom:1px solid var(--border);
  padding:0 16px;display:flex;align-items:center;justify-content:space-between;
  height:52px;position:sticky;top:0;z-index:100;gap:12px}
.logo{font-family:'Space Mono',monospace;font-size:14px;font-weight:700;
  color:var(--green);letter-spacing:2px;display:flex;align-items:center;gap:8px;white-space:nowrap}
.live-dot{width:7px;height:7px;background:var(--green);border-radius:50%;
  box-shadow:0 0 6px var(--green);animation:blink 1.4s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

.header-stats{display:flex;gap:16px;align-items:center;overflow-x:auto;-webkit-overflow-scrolling:touch}
.hstat{text-align:center;flex-shrink:0}
.hstat-v{font-family:'Space Mono',monospace;font-size:15px;font-weight:700;color:var(--cyan)}
.hstat-l{font-size:8px;letter-spacing:1px;text-transform:uppercase;color:var(--dim)}

.refresh-btn{background:none;border:1px solid var(--border2);color:var(--dim);
  padding:5px 10px;cursor:pointer;font-family:'Fira Code',monospace;font-size:11px;
  border-radius:2px;transition:.15s;white-space:nowrap;min-height:32px}
.refresh-btn:hover{border-color:var(--green);color:var(--green)}

/* TABS */
.tabs{background:var(--bg2);border-bottom:1px solid var(--border);
  padding:0 8px;display:flex;gap:0;overflow-x:auto;-webkit-overflow-scrolling:touch;
  scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{padding:10px 12px;cursor:pointer;font-family:'Space Mono',monospace;font-size:9px;
  letter-spacing:1px;text-transform:uppercase;color:var(--dim);
  border-bottom:2px solid transparent;transition:.15s;white-space:nowrap;min-height:40px;
  display:flex;align-items:center;gap:4px}
.tab:hover{color:var(--text)}
.tab.active{color:var(--green);border-bottom-color:var(--green)}
.tab-badge{display:inline-block;background:var(--bg3);color:var(--dim);
  font-size:8px;padding:1px 4px;border-radius:6px}
.tab.active .tab-badge{background:rgba(0,255,136,.15);color:var(--green)}

/* CONTENT */
.content{padding:12px;max-width:1400px;margin:0 auto}
.page{display:none}
.page.active{display:block}

/* SECTION TITLE */
.section-title{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:2px;
  text-transform:uppercase;color:var(--dim);margin-bottom:10px;
  padding-bottom:6px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:4px}

/* TOKEN CARDS */
.card-grid{display:grid;grid-template-columns:1fr;gap:6px}
.token-card{background:var(--bg2);border:1px solid var(--border);border-radius:4px;
  padding:11px 12px;border-left:3px solid var(--border);transition:border-color .15s}
.token-card.entry{border-left-color:var(--green)}
.token-card.skip{border-left-color:var(--dim)}
.token-card.missed{border-left-color:var(--amber)}
.token-card.entered-win{border-left-color:var(--green)}
.token-card.entered-loss{border-left-color:var(--red)}
.token-card.entered-open{border-left-color:var(--cyan)}

/* WATCHING CARD */
.token-card.watching{border-left-color:var(--cyan);animation:watchglow 2s ease-in-out infinite}
@keyframes watchglow{0%,100%{box-shadow:none}50%{box-shadow:inset 0 0 16px rgba(0,207,255,.04)}}

.card-top{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}
.sym{font-family:'Space Mono',monospace;font-size:14px;font-weight:700;color:var(--bright);min-width:70px}
.tok-name{color:var(--dim);flex:1;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:80px;max-width:200px}
.score-num{font-family:'Space Mono',monospace;font-weight:700;font-size:18px;min-width:44px;text-align:right}
.score-num.high{color:var(--green)}.score-num.mid{color:var(--amber)}
.score-num.low{color:var(--red)}.score-num.zero{color:var(--dim)}

.badge{font-size:8px;font-weight:700;letter-spacing:1px;padding:2px 6px;border-radius:2px;
  font-family:'Space Mono',monospace;white-space:nowrap}
.badge.enter{background:rgba(0,255,136,.1);color:var(--green);border:1px solid rgba(0,255,136,.25)}
.badge.skip{background:rgba(74,80,104,.1);color:var(--dim);border:1px solid rgba(74,80,104,.25)}
.badge.watching{background:rgba(0,207,255,.1);color:var(--cyan);border:1px solid rgba(0,207,255,.25);animation:blink 1.4s ease-in-out infinite}
.badge.win{background:rgba(0,255,136,.1);color:var(--green);border:1px solid rgba(0,255,136,.25)}
.badge.loss{background:rgba(255,51,85,.1);color:var(--red);border:1px solid rgba(255,51,85,.25)}
.badge.open{background:rgba(0,207,255,.1);color:var(--cyan);border:1px solid rgba(0,207,255,.25)}
.badge.pumped{background:rgba(255,187,0,.1);color:var(--amber);border:1px solid rgba(255,187,0,.25)}

.countdown{font-family:'Space Mono',monospace;font-size:13px;font-weight:700;color:var(--cyan)}
.countdown.urgent{color:var(--amber)}

.watch-progress-bg{flex:1;height:2px;background:var(--border2);border-radius:1px;overflow:hidden}
.watch-progress-fill{height:100%;border-radius:1px;background:var(--cyan);transition:width .5s linear}

.score-bar-row{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.score-bar-bg{flex:1;height:2px;background:var(--border2);border-radius:1px;overflow:hidden}
.score-bar-fill{height:100%;border-radius:1px;transition:width .4s ease}

.tok-stats{display:flex;flex-wrap:wrap;gap:8px 14px;font-size:11px;color:var(--dim);margin-bottom:7px}
.tok-stats .hl{color:var(--cyan)}.tok-stats .hl-g{color:var(--green)}.tok-stats .hl-a{color:var(--amber)}

/* CONTRACT ADDRESS */
.mint-row{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.mint-addr{font-size:10px;color:var(--dim);cursor:pointer;font-family:'Space Mono',monospace;
  letter-spacing:.5px;transition:color .15s;user-select:all;word-break:break-all}
.mint-addr:hover{color:var(--cyan)}
.copy-btn{font-size:8px;color:var(--dim);cursor:pointer;padding:2px 6px;border:1px solid var(--border2);
  border-radius:2px;transition:.15s;white-space:nowrap;font-family:'Space Mono',monospace}
.copy-btn:hover{border-color:var(--cyan);color:var(--cyan)}
.copy-btn.copied{border-color:var(--green);color:var(--green)}

/* STRATEGY TAGS */
.stags{display:flex;flex-wrap:wrap;gap:3px}
.stag{font-size:9px;padding:2px 6px;border-radius:2px;white-space:nowrap}
.stag.pos{background:rgba(0,255,136,.07);color:rgba(0,255,136,.85);border:1px solid rgba(0,255,136,.15)}
.stag.neg{background:rgba(255,51,85,.07);color:rgba(255,51,85,.85);border:1px solid rgba(255,51,85,.15)}

/* PEAK MISS */
.peak-mc-big{font-family:'Space Mono',monospace;font-size:16px;font-weight:700;color:var(--amber)}

/* STRATEGY TABLE */
.strat-list{display:flex;flex-direction:column;gap:1px;overflow-x:auto}
.strat-row{display:flex;align-items:center;gap:10px;padding:9px 12px;
  background:var(--bg2);border:1px solid var(--border);border-radius:3px;min-width:560px}
.strat-rank{font-family:'Space Mono',monospace;font-size:10px;color:var(--dim);min-width:20px;text-align:right}
.strat-name{flex:1;font-size:11px;min-width:120px}
.strat-type{font-size:8px;letter-spacing:1px;padding:2px 5px;border-radius:2px;font-family:'Space Mono',monospace;min-width:60px;text-align:center}
.strat-type.pos{color:rgba(0,255,136,.7);background:rgba(0,255,136,.06);border:1px solid rgba(0,255,136,.15)}
.strat-type.neg{color:rgba(255,51,85,.7);background:rgba(255,51,85,.06);border:1px solid rgba(255,51,85,.15)}
.strat-signals{font-size:10px;color:var(--dim);min-width:55px;text-align:right}
.strat-wins{font-size:10px;color:var(--cyan);min-width:35px;text-align:right}
.strat-wr{font-family:'Space Mono',monospace;font-size:12px;font-weight:700;min-width:44px;text-align:right}
.strat-bar-wrap{width:80px;flex-shrink:0}
.strat-bar-bg{height:3px;background:var(--border2);border-radius:2px;overflow:hidden}
.strat-bar-fill{height:100%;border-radius:2px;transition:width .4s}
.strat-weight{font-size:10px;color:var(--purple);min-width:38px;text-align:right}

/* TRADE LOG */
.trade-row{background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:10px 12px;margin-bottom:5px;border-left:3px solid var(--border)}
.trade-row.win{border-left-color:var(--green)}
.trade-row.loss{border-left-color:var(--red)}
.trade-row.open{border-left-color:var(--cyan)}
.trade-row.expired{border-left-color:var(--dim)}
.roi-num{font-family:'Space Mono',monospace;font-size:16px;font-weight:700}
.roi-num.win{color:var(--green)}.roi-num.loss{color:var(--red)}.roi-num.open{color:var(--cyan)}
.trade-meta{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:11px;color:var(--dim);margin-top:5px}
.trade-meta .hl{color:var(--bright)}
.trade-meta .hl-g{color:var(--green)}.trade-meta .hl-a{color:var(--amber)}.trade-meta .hl-r{color:var(--red)}

/* PUMPERS */
.pumper-card{background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:10px 12px;margin-bottom:5px}
.pumper-card.entered{border-left:3px solid var(--green)}
.pumper-card.skipped{border-left:3px solid var(--amber)}

/* SUMMARY BAR */
.summary-bar{display:flex;flex-wrap:wrap;gap:8px;padding:10px 12px;background:var(--bg3);
  border:1px solid var(--border);border-radius:4px;margin-bottom:12px;font-size:11px}
.sbar-item{display:flex;flex-direction:column;align-items:center;min-width:60px}
.sbar-v{font-family:'Space Mono',monospace;font-size:14px;font-weight:700}
.sbar-l{font-size:8px;color:var(--dim);letter-spacing:1px;text-transform:uppercase;margin-top:1px}

/* EMPTY */
.empty{text-align:center;padding:50px 20px;color:var(--dim);font-family:'Space Mono',monospace}
.empty .icon{font-size:32px;margin-bottom:10px;opacity:.4}
.empty .title{font-size:12px;letter-spacing:1px;margin-bottom:5px}
.empty .sub{font-size:10px;opacity:.6}

/* ANALYSIS BOX */
.analysis-box{background:var(--bg2);border:1px solid var(--border);border-left:3px solid var(--purple);
  border-radius:3px;padding:12px;margin-bottom:12px}
.analysis-title{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:2px;
  text-transform:uppercase;color:var(--purple);margin-bottom:6px}
.insight{display:flex;gap:6px;margin-bottom:4px;font-size:11px}
.insight-dot{color:var(--purple);flex-shrink:0}

/* SCORE BANDS */
.band-table{width:100%;border-collapse:collapse}
.band-row{display:grid;grid-template-columns:70px 60px 60px 60px 1fr 90px 90px;
  align-items:center;gap:10px;padding:9px 12px;
  background:var(--bg2);border:1px solid var(--border);border-radius:3px;margin-bottom:3px;transition:border-color .15s}
.band-row:hover{border-color:var(--border2)}
.band-row.best{border-left:3px solid var(--green)}
.band-row.header{background:none;border-color:transparent;
  font-size:8px;letter-spacing:1px;text-transform:uppercase;color:var(--dim);padding:4px 12px;margin-bottom:2px}
.band-label{font-family:'Space Mono',monospace;font-size:13px;font-weight:700;color:var(--bright)}
.band-total{font-size:11px;color:var(--dim)}
.band-wins{font-size:11px;color:var(--green)}
.band-wr{font-family:'Space Mono',monospace;font-size:14px;font-weight:700}
.band-bar-wrap{position:relative;height:6px;background:var(--border2);border-radius:3px;overflow:hidden}
.band-bar-fill{height:100%;border-radius:3px;transition:width .6s ease}
.band-avg{font-size:11px;color:var(--cyan);text-align:right}
.band-best{font-size:11px;color:var(--amber);text-align:right}
.band-insight{background:var(--bg3);border:1px solid var(--border);border-left:3px solid var(--green);
  border-radius:3px;padding:10px 14px;margin-bottom:14px;font-size:12px;line-height:1.7}
.band-insight b{color:var(--green)}

/* MARKET CONDITIONS */
.market-score-big{font-family:'Space Mono',monospace;font-size:64px;font-weight:700;line-height:1;text-align:center;padding:20px 0 4px}
.market-rec{font-family:'Space Mono',monospace;font-size:16px;letter-spacing:4px;text-align:center;margin-bottom:20px;font-weight:700}
.market-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px}
@media(min-width:700px){.market-grid{grid-template-columns:1fr 1fr 1fr}}
.signal-card{background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:10px 12px;border-left:3px solid var(--border)}
.signal-card.good{border-left-color:var(--green)}
.signal-card.warn{border-left-color:var(--amber)}
.signal-card.bad {border-left-color:var(--red)}
.sig-name{font-size:10px;letter-spacing:1px;text-transform:uppercase;color:var(--dim);margin-bottom:4px}
.sig-value{font-family:'Space Mono',monospace;font-size:13px;font-weight:700;color:var(--bright);margin-bottom:3px}
.sig-detail{font-size:10px;color:var(--dim);line-height:1.4}
.sig-pts{font-family:'Space Mono',monospace;font-size:11px;float:right;color:var(--cyan)}
.market-bar-wrap{height:8px;background:var(--border2);border-radius:4px;overflow:hidden;margin:10px 0}
.market-bar-fill{height:100%;border-radius:4px;transition:width .8s ease}

/* PORTFOLIO */
.port-strat{background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:10px 12px;
  margin-bottom:5px;border-left:3px solid var(--border);cursor:pointer;transition:border-color .15s}
.port-strat:hover{border-color:var(--border2)}
.port-strat.best{border-left-color:var(--green)}
.port-strat.worst{border-left-color:var(--red)}
.port-top{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.port-name{flex:1;font-size:12px;font-weight:600;color:var(--bright);min-width:140px}
.port-pnl{font-family:'Space Mono',monospace;font-size:15px;font-weight:700;min-width:80px;text-align:right}
.port-pnl.pos{color:var(--green)}.port-pnl.neg{color:var(--red)}.port-pnl.zero{color:var(--dim)}
.port-meta{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:10px;color:var(--dim);margin-top:5px}
.port-meta .hl{color:var(--cyan)}
.port-desc{font-size:10px;color:var(--dim);margin-top:3px}
.port-bar-wrap{height:3px;background:var(--border2);border-radius:2px;overflow:hidden;margin-top:7px}
.port-bar-fill{height:100%;border-radius:2px}

/* RESPONSIVE */
@media(min-width:700px){
  .card-grid{grid-template-columns:1fr 1fr}
  .content{padding:16px 20px}
  .sym{font-size:15px}
  .hstat-v{font-size:16px}
}
@media(min-width:1100px){
  .card-grid{grid-template-columns:1fr 1fr 1fr}
}
@media(max-width:480px){
  .header{height:auto;padding:8px 12px;flex-wrap:wrap}
  .header-stats{gap:10px}
  .hstat-v{font-size:13px}
  .tab{padding:8px 10px;font-size:8px}
  .tok-name{max-width:120px}
}

::-webkit-scrollbar{width:3px;height:3px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
</style>
</head>
<body>

<div class="header">
  <div class="logo"><div class="live-dot"></div>PUMP</div>
  <div class="header-stats">
    <div class="hstat"><div class="hstat-v" id="h-watching" style="color:var(--cyan)">—</div><div class="hstat-l">Live</div></div>
    <div class="hstat"><div class="hstat-v" id="h-scored">—</div><div class="hstat-l">Scored</div></div>
    <div class="hstat"><div class="hstat-v" id="h-entries" style="color:var(--green)">—</div><div class="hstat-l">Entered</div></div>
    <div class="hstat"><div class="hstat-v" id="h-wins" style="color:var(--green)">—</div><div class="hstat-l">Wins</div></div>
    <div class="hstat"><div class="hstat-v" id="h-losses" style="color:var(--red)">—</div><div class="hstat-l">Losses</div></div>
    <div class="hstat"><div class="hstat-v" id="h-open" style="color:var(--cyan)">—</div><div class="hstat-l">Open</div></div>
    <div class="hstat"><div class="hstat-v" id="h-total">—</div><div class="hstat-l">Total</div></div>
  </div>
  <button class="refresh-btn" onclick="refreshAll()">↻</button>
  <a href="/api/ai_export" class="refresh-btn" style="text-decoration:none;display:flex;align-items:center">🤖 Export</a>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('live',this)">Live <span class="tab-badge" id="tb-live">0</span></div>
  <div class="tab" onclick="switchTab('tradelog',this)">Trade Log <span class="tab-badge" id="tb-trades">0</span></div>
  <div class="tab" onclick="switchTab('pumped',this)">Pumpers <span class="tab-badge" id="tb-pumped">0</span></div>
  <div class="tab" onclick="switchTab('entries',this)">Entries <span class="tab-badge" id="tb-entries">0</span></div>
  <div class="tab" onclick="switchTab('missed',this)">Missed <span class="tab-badge" id="tb-missed">0</span></div>
  <div class="tab" onclick="switchTab('winners',this)">Wins <span class="tab-badge" id="tb-winners">0</span></div>
  <div class="tab" onclick="switchTab('strategies',this)">Strategy <span class="tab-badge" id="tb-strats">0</span></div>
  <div class="tab" onclick="switchTab('scorebands',this)">Score Bands <span class="tab-badge" id="tb-bands">—</span></div>
  <div class="tab" onclick="switchTab('portfolio',this)">Portfolio <span class="tab-badge" id="tb-port">—</span></div>
  <div class="tab" onclick="switchTab('market',this)">Market <span class="tab-badge" id="tb-mkt">—</span></div>
</div>

<div class="content">
  <div id="page-live"       class="page active"></div>
  <div id="page-tradelog"   class="page"></div>
  <div id="page-pumped"     class="page"></div>
  <div id="page-entries"    class="page"></div>
  <div id="page-missed"     class="page"></div>
  <div id="page-winners"    class="page"></div>
  <div id="page-strategies" class="page"></div>
  <div id="page-scorebands" class="page"></div>
  <div id="page-portfolio"  class="page"></div>
  <div id="page-market"     class="page"></div>
</div>

<script>
const WATCH_WINDOW = 120;

// ── UTILS ──────────────────────────────────────────────
const fmtSol  = v => v != null && v > 0 ? (+v).toFixed(1)+' SOL' : '—';
const fmtRoi  = v => v != null ? v.toFixed(2)+'x' : '—';
const fmtDur  = s => {
  if(!s) return '—';
  const m = Math.floor(s/60), h = Math.floor(m/60);
  return h > 0 ? `${h}h ${m%60}m` : `${m}m`;
};
const timeAgo = ts => {
  if(!ts) return '—';
  const s = Math.floor(Date.now()/1000 - ts);
  if(s < 60)   return s+'s ago';
  if(s < 3600) return Math.floor(s/60)+'m ago';
  return Math.floor(s/3600)+'h ago';
};
const fmtTime = ts => ts ? new Date(ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}) : '—';
const fmtDate = ts => ts ? new Date(ts*1000).toLocaleDateString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
const scoreClass = s => s>=60?'high':s>=40?'mid':s>0?'low':'zero';
const barColor   = s => s>=60?'var(--green)':s>=40?'var(--amber)':'var(--red)';

const STRAT_LABELS = {
  whale_sniffer:'🐋 Whale Sniffer',micro_bot_swarm:'🤖 Micro Swarm',
  holder_velocity:'📈 Holder Vel',volume_consistency:'📊 Vol Consistency',
  dev_hands_off:'🔒 Dev Held',first_buy_size:'💰 First Buy',
  buy_sell_ratio:'⚖️ B/S Ratio',time_to_50_holders:'⚡ 50 Holders',
  repeat_buyers:'🔄 Repeat Buys',early_volume:'💥 Early Vol',
  has_twitter:'🐦 Twitter',has_telegram:'📱 Telegram',
  peak_launch_time:'⏰ Peak Time',short_ticker:'🏷️ Short Ticker',
  strong_initial_buy:'🚀 Dev Init Buy',
  early_dev_dump:'💀 Dev Dump',single_wallet_dominance:'👁️ Wallet Dom',
  wash_trading:'🔃 Wash Trade',no_socials:'👻 No Socials',
  bundle_detector:'📦 Bundle',early_sell_pressure:'📉 Sell Pressure',
};
const stratLabel = n => STRAT_LABELS[n] || n.replace(/_/g,' ');
const NEG_STRATS = new Set(['early_dev_dump','single_wallet_dominance','wash_trading',
                             'no_socials','bundle_detector','early_sell_pressure']);

// ── COPY CONTRACT ───────────────────────────────────────
function copyMint(mint, btnId) {
  navigator.clipboard.writeText(mint).catch(()=>{
    const ta = document.createElement('textarea');
    ta.value = mint; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy');
    document.body.removeChild(ta);
  });
  const btn = document.getElementById(btnId);
  if(btn){ btn.textContent='✓ COPIED'; btn.classList.add('copied');
    setTimeout(()=>{ btn.textContent='COPY'; btn.classList.remove('copied'); }, 1500); }
}

function mintHtml(mint) {
  if(!mint) return '';
  const id = 'cp-'+mint.slice(0,8);
  return `<div class="mint-row">
    <span class="mint-addr" onclick="copyMint('${mint}','${id}')" title="Click to copy">${mint.slice(0,20)}…${mint.slice(-8)}</span>
    <span class="copy-btn" id="${id}" onclick="copyMint('${mint}','${id}')">COPY</span>
  </div>`;
}

// ── WATCHING CARD ───────────────────────────────────────
function buildWatchingCard(tok) {
  const age      = Math.floor(Date.now()/1000 - (tok.created_at||0));
  const secsLeft = Math.max(0, WATCH_WINDOW - age);
  const pct      = Math.min(100, age/WATCH_WINDOW*100);
  const cdCls    = secsLeft<=20?'urgent':'';
  const socials  = (tok.twitter?'🐦':'')+' '+(tok.telegram?'📱':'');
  const mintId   = (tok.mint||'').slice(0,8);
  return `
  <div class="token-card watching" data-mint="${tok.mint}" data-created="${tok.created_at}">
    <div class="card-top">
      <div class="sym">$${tok.symbol||'???'}</div>
      <div class="tok-name">${tok.name||'Unknown'} ${socials}</div>
      <span class="badge watching">WATCHING</span>
      <div class="countdown ${cdCls}" id="cd-${mintId}">${secsLeft}s</div>
    </div>
    <div class="score-bar-row">
      <div class="watch-progress-bg">
        <div class="watch-progress-fill" id="wp-${mintId}" style="width:${pct}%"></div>
      </div>
    </div>
    <div class="tok-stats">
      <span>Trades: <span class="hl">${tok.live_trade_count||0}</span></span>
      <span>MC: <span class="hl">${fmtSol(tok.current_mc_sol)}</span></span>
      <span>Init: <span class="hl">${tok.initial_buy_sol?(+tok.initial_buy_sol).toFixed(2)+' SOL':'—'}</span></span>
      <span>Age: <span class="hl">${timeAgo(tok.created_at)}</span></span>
    </div>
    ${mintHtml(tok.mint)}
  </div>`;
}

// ── SCORED CARD ─────────────────────────────────────────
function buildCard(tok, mode='live') {
  const score    = tok.score ?? 0;
  const isEntry  = score >= 60;
  const strats   = tok.strategies || {};
  const stats    = tok.stats || {};
  const peakMc   = tok.peak_mc_sol || tok.current_mc_sol;
  const posFired = Object.entries(strats).filter(([,v])=> v.positive && v.fired).map(([k])=>k);
  const negFired = Object.entries(strats).filter(([,v])=>!v.positive && v.fired).map(([k])=>k);
  const socials  = (tok.twitter?'🐦':'')+' '+(tok.telegram?'📱':'');
  const cardCls  = mode==='missed'?'missed':isEntry?'entry':'skip';
  const badge    = isEntry?'<span class="badge enter">ENTRY</span>':'<span class="badge skip">SKIP</span>';
  const posHtml  = posFired.map(k=>`<span class="stag pos">${stratLabel(k)}</span>`).join('');
  const negHtml  = negFired.map(k=>`<span class="stag neg">${stratLabel(k)}</span>`).join('');
  const peakHtml = peakMc && mode==='missed'
    ? `<div style="margin-top:7px;padding-top:7px;border-top:1px solid var(--border)">
         <span class="peak-mc-big">${fmtSol(peakMc)}</span>
         <span style="font-size:10px;color:var(--dim);margin-left:6px">peak — MISSED</span></div>` : '';
  const snapHtml = peakMc && mode!=='missed'
    ? `<div style="margin-top:7px;padding-top:7px;border-top:1px solid var(--border);font-size:11px;color:var(--dim)">
         Peak MC: <span style="color:var(--${peakMc>=133?'green':'cyan'})">${fmtSol(peakMc)}</span></div>` : '';
  return `
  <div class="token-card ${cardCls}">
    <div class="card-top">
      <div class="sym">$${tok.symbol||'???'}</div>
      <div class="tok-name">${tok.name||'Unknown'} ${socials}</div>
      ${badge}
      <div class="score-num ${scoreClass(score)}">${score}</div>
    </div>
    <div class="score-bar-row">
      <div class="score-bar-bg"><div class="score-bar-fill" style="width:${score}%;background:${barColor(score)}"></div></div>
    </div>
    <div class="tok-stats">
      <span>B: <span class="hl">${stats.total_buys??'—'}</span></span>
      <span>S: <span class="hl">${stats.total_sells??'—'}</span></span>
      <span>Vol: <span class="hl">${fmtSol(stats.total_vol_sol)}</span></span>
      <span>Age: <span class="hl">${timeAgo(tok.created_at)}</span></span>
    </div>
    ${mintHtml(tok.mint)}
    <div class="stags">${posHtml}${negHtml}</div>
    ${peakHtml}${snapHtml}
  </div>`;
}

// ── TRADE LOG CARD ──────────────────────────────────────
function buildTradeCard(entry) {
  const isOpen   = !entry.exit_reason;
  const isWin    = entry.exit_reason?.startsWith('TAKE');
  const isLoss   = entry.exit_reason === 'STOP_LOSS';
  const isExpire = entry.exit_reason === 'TIME_LIMIT';
  const roi      = entry.roi;
  const maxRoi   = entry.max_roi;
  const cls      = isOpen?'open':isWin?'win':isLoss?'loss':'expired';
  const roiCls   = isOpen?'open':isWin?'win':'loss';
  const badgeTxt = isOpen?'OPEN':isWin?`WIN ${fmtRoi(roi)}`:isLoss?`LOSS ${fmtRoi(roi)}`:isExpire?'EXPIRED':entry.exit_reason||'?';
  const badgeCls = isOpen?'open':isWin?'win':isLoss?'loss':'skip';
  const socials  = (entry.twitter?'🐦':'')+' '+(entry.telegram?'📱':'');
  return `
  <div class="trade-row ${cls}">
    <div class="card-top">
      <div class="sym">$${entry.symbol||'???'}</div>
      <div class="tok-name">${entry.name||'Unknown'} ${socials}</div>
      <span class="badge ${badgeCls}">${badgeTxt}</span>
      <div class="roi-num ${roiCls}">${isOpen?fmtSol(entry.entry_mc_sol):fmtRoi(roi)}</div>
    </div>
    <div class="trade-meta">
      <span>Score: <span class="hl">${entry.score??'—'}</span></span>
      <span>Entry MC: <span class="hl-a">${fmtSol(entry.entry_mc_sol)}</span></span>
      ${entry.exit_mc_sol?`<span>Exit MC: <span class="${isWin?'hl-g':'hl-r'}">${fmtSol(entry.exit_mc_sol)}</span></span>`:''}
      <span>Max MC: <span class="hl-g">${fmtSol(entry.max_mc_sol)}</span></span>
      ${maxRoi?`<span>Best: <span class="hl-g">${fmtRoi(maxRoi)}</span></span>`:''}
      <span>Hold: <span class="hl">${fmtDur(entry.hold_seconds || (isOpen?(Date.now()/1000-entry.entry_time):null))}</span></span>
      <span>Entered: <span class="hl">${fmtDate(entry.entry_time)}</span></span>
      ${entry.exit_time?`<span>Exited: <span class="hl">${fmtDate(entry.exit_time)}</span></span>`:''}
    </div>
    ${mintHtml(entry.mint)}
  </div>`;
}

// ── PUMPER CARD ─────────────────────────────────────────
function buildPumperCard(tok) {
  const entered  = tok.was_entered;
  const score    = tok.score ?? 0;
  const strats   = tok.strategies || {};
  const negFired = Object.entries(strats).filter(([,v])=>!v.positive && v.fired).map(([k])=>k);
  const posFired = Object.entries(strats).filter(([,v])=> v.positive && v.fired).map(([k])=>k);
  const cls      = entered?'entered':'skipped';
  const socials  = (tok.twitter?'🐦':'')+' '+(tok.telegram?'📱':'');
  const skipReason = !entered && negFired.length
    ? `<div style="margin-top:6px;font-size:10px;color:var(--dim)">Killed by: ${negFired.map(k=>`<span style="color:var(--red)">${stratLabel(k)}</span>`).join(', ')}</div>` : '';
  return `
  <div class="pumper-card ${cls}">
    <div class="card-top">
      <div class="sym">$${tok.symbol||'???'}</div>
      <div class="tok-name">${tok.name||'Unknown'} ${socials}</div>
      <span class="badge ${entered?'win':'pumped'}">${entered?'ENTERED':'SKIPPED'}</span>
      <div class="score-num ${scoreClass(score)}">${score}</div>
    </div>
    <div class="tok-stats">
      <span>Peak MC: <span class="hl-a">${fmtSol(tok.peak_mc_sol)}</span></span>
      <span>Age: <span class="hl">${timeAgo(tok.created_at)}</span></span>
    </div>
    ${mintHtml(tok.mint)}
    <div class="stags">
      ${posFired.map(k=>`<span class="stag pos">${stratLabel(k)}</span>`).join('')}
      ${negFired.map(k=>`<span class="stag neg">${stratLabel(k)}</span>`).join('')}
    </div>
    ${skipReason}
  </div>`;
}

// ── COUNTDOWN TICKER (every second, no fetch) ───────────
setInterval(() => {
  const now = Math.floor(Date.now() / 1000);
  document.querySelectorAll('.token-card.watching').forEach(card => {
    const created  = parseInt(card.dataset.created, 10);
    const mintId   = (card.dataset.mint||'').slice(0,8);
    const secsLeft = Math.max(0, WATCH_WINDOW - (now - created));
    const pct      = Math.min(100, (now-created)/WATCH_WINDOW*100);
    const cd = document.getElementById('cd-'+mintId);
    const wp = document.getElementById('wp-'+mintId);
    if(cd){ cd.textContent = secsLeft>0?secsLeft+'s':'scoring...'; cd.className='countdown '+(secsLeft<=20?'urgent':''); }
    if(wp) wp.style.width = pct+'%';
  });
}, 1000);

// ── FETCH + RENDER ──────────────────────────────────────
async function fetchAndRender(endpoint, pageId, renderer) {
  try {
    const r = await fetch(endpoint);
    const d = await r.json();
    document.getElementById(pageId).innerHTML = renderer(d);
    return d;
  } catch(e) {
    document.getElementById(pageId).innerHTML =
      `<div class="empty"><div class="icon">⚡</div><div class="title">ERROR</div><div class="sub">${e.message}</div></div>`;
  }
}

// ── RENDERERS ───────────────────────────────────────────
function renderLive(tokens) {
  if(!tokens.length) return `<div class="empty"><div class="icon">📡</div><div class="title">WAITING</div><div class="sub">Coins appear instantly as they launch</div></div>`;
  document.getElementById('tb-live').textContent = tokens.length;
  const watching = tokens.filter(t=>t.watch_complete===0);
  const scored   = tokens.filter(t=>t.watch_complete===1);
  let html = '';
  if(watching.length) {
    html += `<div class="section-title"><span>⚡ Watching Now (${watching.length})</span><span>5s refresh</span></div>
    <div class="card-grid">${watching.map(buildWatchingCard).join('')}</div>`;
  }
  if(scored.length) {
    html += `<div class="section-title" style="margin-top:${watching.length?'14px':'0'}">
      <span>Recent Scored (${scored.length})</span><span>${new Date().toLocaleTimeString()}</span></div>
    <div class="card-grid">${scored.map(t=>buildCard(t,'live')).join('')}</div>`;
  }
  return html;
}

function renderTradelog(entries) {
  document.getElementById('tb-trades').textContent = entries.length;
  if(!entries.length) return `<div class="empty"><div class="icon">📋</div><div class="title">NO TRADES YET</div><div class="sub">Entry signals (score >= 60) appear here</div></div>`;
  const open   = entries.filter(e=>!e.exit_reason);
  const wins   = entries.filter(e=>e.exit_reason?.startsWith('TAKE'));
  const losses = entries.filter(e=>e.exit_reason==='STOP_LOSS');
  const expired= entries.filter(e=>e.exit_reason==='TIME_LIMIT');
  const avgRoi = wins.length ? (wins.reduce((a,e)=>a+(e.roi||1),0)/wins.length).toFixed(2) : '—';
  const summary = `
  <div class="summary-bar">
    <div class="sbar-item"><div class="sbar-v" style="color:var(--cyan)">${open.length}</div><div class="sbar-l">Open</div></div>
    <div class="sbar-item"><div class="sbar-v" style="color:var(--green)">${wins.length}</div><div class="sbar-l">Wins</div></div>
    <div class="sbar-item"><div class="sbar-v" style="color:var(--red)">${losses.length}</div><div class="sbar-l">Losses</div></div>
    <div class="sbar-item"><div class="sbar-v" style="color:var(--dim)">${expired.length}</div><div class="sbar-l">Expired</div></div>
    <div class="sbar-item"><div class="sbar-v" style="color:var(--green)">${avgRoi}x</div><div class="sbar-l">Avg Win</div></div>
    <div class="sbar-item"><div class="sbar-v">${wins.length?Math.round(wins.length/(wins.length+losses.length)*100)+'%':'—'}</div><div class="sbar-l">Win Rate</div></div>
  </div>`;
  return summary + entries.map(buildTradeCard).join('');
}

function renderPumped(tokens) {
  document.getElementById('tb-pumped').textContent = tokens.length;
  if(!tokens.length) return `<div class="empty"><div class="icon">🚀</div><div class="title">NONE YET</div><div class="sub">All coins that hit ${133} SOL+ will appear here</div></div>`;
  const entered = tokens.filter(t=>t.was_entered);
  const skipped = tokens.filter(t=>!t.was_entered);
  return `
  <div class="section-title">
    <span>All Coins That Pumped (${tokens.length})</span>
    <span>Entered: ${entered.length} / Missed: ${skipped.length}</span>
  </div>
  ${tokens.length ? buildMissedAnalysis(skipped) : ''}
  <div class="card-grid">${tokens.map(buildPumperCard).join('')}</div>`;
}

function renderEntries(tokens) {
  document.getElementById('tb-entries').textContent = tokens.length;
  if(!tokens.length) return `<div class="empty"><div class="icon">🎯</div><div class="title">NO ENTRIES YET</div><div class="sub">Tokens scoring >= 60 appear here</div></div>`;
  const confirmed = tokens.filter(t=>t.peak_mc_sol>=133);
  return `<div class="section-title"><span>${tokens.length} Entry Signals</span><span>${confirmed.length} hit 133+ SOL</span></div>
  <div class="card-grid">${tokens.map(t=>buildCard(t,'entry')).join('')}</div>`;
}

function renderMissed(tokens) {
  document.getElementById('tb-missed').textContent = tokens.length;
  if(!tokens.length) return `<div class="empty"><div class="icon">✅</div><div class="title">NO MISSED WINNERS</div></div>`;
  return `<div class="section-title"><span>${tokens.length} Missed Winners</span><span style="color:var(--amber)">Study these</span></div>
  ${buildMissedAnalysis(tokens)}<div class="card-grid">${tokens.map(t=>buildCard(t,'missed')).join('')}</div>`;
}

function renderWinners(tokens) {
  document.getElementById('tb-winners').textContent = tokens.length;
  if(!tokens.length) return `<div class="empty"><div class="icon">🏆</div><div class="title">NO CONFIRMED WINS YET</div></div>`;
  return `<div class="section-title"><span>${tokens.length} Confirmed Wins</span></div>
  <div class="card-grid">${tokens.map(t=>buildCard(t,'winners')).join('')}</div>`;
}

function renderStrategies(strats) {
  document.getElementById('tb-strats').textContent = strats.length;
  if(!strats.length) return `<div class="empty"><div class="icon">🧠</div><div class="title">NO DATA YET</div><div class="sub">Strategy stats populate after snapshot data collected</div></div>`;
  const header = `<div class="section-title">
    <span>${strats.length} Strategies — by Win Rate</span>
    <span style="color:var(--dim)">Win = >133 SOL MC</span></div>
  <div style="overflow-x:auto">
  <div style="min-width:560px">
  <div class="strat-row" style="background:none;border-color:transparent;padding:4px 12px">
    <div style="width:20px"></div>
    <div style="flex:1;font-size:8px;letter-spacing:1px;text-transform:uppercase;color:var(--dim)">Strategy</div>
    <div style="width:60px"></div>
    <div style="min-width:55px;text-align:right;font-size:8px;color:var(--dim)">Signals</div>
    <div style="min-width:35px;text-align:right;font-size:8px;color:var(--dim)">Wins</div>
    <div style="min-width:44px;text-align:right;font-size:8px;color:var(--dim)">Win%</div>
    <div style="width:80px;font-size:8px;color:var(--dim)">Bar</div>
    <div style="min-width:38px;text-align:right;font-size:8px;color:var(--dim)">Weight</div>
  </div>`;
  const rows = strats.filter(s=>s.total_signals>=1).map((s,i)=>{
    const wr  = s.win_rate??0, pct=Math.round(wr*100);
    const col = pct>=50?'var(--green)':pct>=30?'var(--amber)':'var(--red)';
    const neg = NEG_STRATS.has(s.strategy_name);
    const wBg = s.weight>1.2?'var(--green)':s.weight<0.8?'var(--red)':'var(--cyan)';
    return `<div class="strat-row">
      <div class="strat-rank">${i+1}</div>
      <div class="strat-name">${stratLabel(s.strategy_name)}</div>
      <span class="strat-type ${neg?'neg':'pos'}">${neg?'NEG':'POS'}</span>
      <div class="strat-signals">${s.total_signals}</div>
      <div class="strat-wins">${s.wins}</div>
      <div class="strat-wr" style="color:${col}">${pct}%</div>
      <div class="strat-bar-wrap"><div class="strat-bar-bg">
        <div class="strat-bar-fill" style="width:${pct}%;background:${col}"></div></div></div>
      <div class="strat-weight" style="color:${wBg}">${(s.weight??1).toFixed(2)}</div>
    </div>`;
  }).join('');
  return header + `<div class="strat-list">${rows}</div></div></div>`;
}

function buildMissedAnalysis(tokens) {
  if(!tokens.length) return '';
  const sc = {};
  tokens.forEach(tok => {
    const strats = tok.strategies||{};
    Object.entries(strats).forEach(([k,v])=>{ if(v.fired) sc[k]=(sc[k]||0)+1; });
  });
  const top = Object.entries(sc).sort((a,b)=>b[1]-a[1]).slice(0,5)
    .map(([k,n])=>`${stratLabel(k)} (${n}/${tokens.length})`);
  return `<div class="analysis-box"><div class="analysis-title">🧠 Pattern — What did missed pumpers have?</div>
    <div>${top.map(s=>`<div class="insight"><span class="insight-dot">▸</span><span style="font-size:11px">${s}</span></div>`).join('')}</div>
  </div>`;
}

// ── SCORE BANDS ─────────────────────────────────────────
function renderScoreBands(bands) {
  document.getElementById('tb-bands').textContent = bands.filter(b=>b.total>0).length;
  const withData = bands.filter(b => b.total > 0);
  if (!withData.length) return `<div class="empty"><div class="icon">📊</div>
    <div class="title">NO DATA YET</div>
    <div class="sub">Score band win rates appear after tokens are scored and snapshot data is collected</div></div>`;

  // Find the best performing band (highest win rate with meaningful sample)
  const meaningful = withData.filter(b => b.total >= 3);
  const best = meaningful.length ? meaningful.reduce((a,b) => b.win_rate > a.win_rate ? b : a) : null;
  const maxWr = Math.max(...withData.map(b => b.win_rate), 1);

  // Generate insight text
  let insight = '';
  if (best) {
    const entryBands = withData.filter(b => b.lo >= 60 && b.total >= 3);
    const skipBands  = withData.filter(b => b.lo < 60  && b.total >= 3);
    const entryWr    = entryBands.length ? (entryBands.reduce((s,b)=>s+b.winners,0) / entryBands.reduce((s,b)=>s+b.total,0) * 100).toFixed(1) : null;
    const skipWr     = skipBands.length  ? (skipBands.reduce((s,b)=>s+b.winners,0)  / skipBands.reduce((s,b)=>s+b.total,0)  * 100).toFixed(1) : null;

    insight = `<div class="band-insight">
      🏆 <b>Best bracket: Score ${best.label}</b> — ${best.win_rate}% win rate (${best.winners}/${best.total} tokens hit 133+ SOL)
      ${entryWr ? `<br>📈 Tokens we <b>entered</b> (score ≥60) win <b>${entryWr}%</b> of the time` : ''}
      ${skipWr  ? `<br>⏭ Tokens we <b>skipped</b> (score &lt;60) only win <b>${skipWr}%</b> of the time` : ''}
      <br><span style="color:var(--dim);font-size:10px">Winner = token peak MC hit ${WINNER_THRESHOLD} SOL. Brackets with &lt;3 tokens shown greyed out.</span>
    </div>`;
  }

  const header = `<div class="band-row header">
    <div>Score</div><div>Tokens</div><div style="color:var(--green)">Winners</div>
    <div>Win%</div><div>Win Rate Bar</div><div>Avg Peak MC</div><div>Best Peak</div>
  </div>`;

  const rows = bands.map(b => {
    const isEmpty  = b.total === 0;
    const isBest   = best && b.label === best.label;
    const isEntry  = b.lo >= 60;
    const wr       = b.win_rate;
    const col      = wr >= 60 ? 'var(--green)' : wr >= 35 ? 'var(--amber)' : wr >= 15 ? 'var(--cyan)' : 'var(--dim)';
    const barPct   = maxWr > 0 ? (wr / maxWr * 100).toFixed(1) : 0;
    const dimStyle = (isEmpty || b.total < 3) ? 'opacity:0.35' : '';

    return `<div class="band-row${isBest?' best':''}" style="${dimStyle}">
      <div class="band-label" style="color:${isEntry?'var(--green)':'var(--text)'}">${b.label}</div>
      <div class="band-total">${b.total || '—'}</div>
      <div class="band-wins">${b.winners || 0}</div>
      <div class="band-wr" style="color:${col}">${b.total >= 3 ? wr+'%' : b.total > 0 ? '~'+wr+'%' : '—'}</div>
      <div class="band-bar-wrap">
        <div class="band-bar-fill" style="width:${barPct}%;background:${col}"></div>
      </div>
      <div class="band-avg">${b.avg_peak_mc > 0 ? b.avg_peak_mc.toFixed(1)+' SOL' : '—'}</div>
      <div class="band-best">${b.best_peak_mc > 0 ? b.best_peak_mc.toFixed(1)+' SOL' : '—'}</div>
    </div>`;
  }).join('');

  const total  = withData.reduce((s,b)=>s+b.total,0);
  const totWin = withData.reduce((s,b)=>s+b.winners,0);

  return `
  <div class="section-title">
    <span>Score Band Win Rates</span>
    <span style="color:var(--dim)">${total} scored · ${totWin} winners · threshold ${WINNER_THRESHOLD} SOL</span>
  </div>
  ${insight}
  ${header}
  ${rows}
  <div style="margin-top:10px;font-size:10px;color:var(--dim);padding:0 4px">
    ▸ Brackets with fewer than 3 tokens are shown dimmed — not enough data to be reliable.<br>
    ▸ "Winner" = token's peak trade MC hit the ${WINNER_THRESHOLD} SOL threshold (~$20k).<br>
    ▸ Green rows = brackets above the entry threshold (score ≥60).
  </div>`;
}

const WINNER_THRESHOLD = 133;

// ── PORTFOLIO RENDERER ──────────────────────────────────
function renderPortfolio(data) {
  document.getElementById('tb-port').textContent = data.strategies?.length ?? 0;
  if (!data.total_trades) return `<div class="empty"><div class="icon">💼</div>
    <div class="title">NO TRADES YET</div>
    <div class="sub">Portfolio simulation populates once entry signals have been logged</div></div>`;

  const best  = data.strategies[0];
  const worst = data.strategies[data.strategies.length - 1];
  const maxPnl = Math.abs(best.total_pnl || 0.001);

  const summary = `
  <div class="summary-bar" style="margin-bottom:14px">
    <div class="sbar-item"><div class="sbar-v">${data.total_trades}</div><div class="sbar-l">Trades</div></div>
    <div class="sbar-item"><div class="sbar-v">${data.bet_sol} SOL</div><div class="sbar-l">Bet Size</div></div>
    <div class="sbar-item"><div class="sbar-v">${data.total_deployed} SOL</div><div class="sbar-l">Deployed</div></div>
    <div class="sbar-item"><div class="sbar-v" style="color:var(--green)">${best.name.split(' ')[0]}…</div><div class="sbar-l">Best Strategy</div></div>
    <div class="sbar-item"><div class="sbar-v" style="color:${best.total_pnl>=0?'var(--green)':'var(--red)'}">
      ${best.total_pnl>=0?'+':''}${best.total_pnl.toFixed(3)} SOL</div><div class="sbar-l">Best P&L</div></div>
  </div>
  <div class="section-title"><span>9 Exit Strategies Compared</span>
    <span style="color:var(--dim)">Simulated across all ${data.total_trades} real entries · ${data.bet_sol} SOL per trade</span></div>`;

  const rows = data.strategies.map((s, i) => {
    const isB  = i === 0;
    const isW  = i === data.strategies.length - 1;
    const pos  = s.total_pnl >= 0;
    const pct  = Math.abs(s.total_pnl) / maxPnl * 100;
    const col  = pos ? 'var(--green)' : 'var(--red)';
    return `
    <div class="port-strat${isB?' best':isW?' worst':''}">
      <div class="port-top">
        <div class="port-name">${isB?'🏆 ':''}${isW?'📉 ':''}${s.name}</div>
        <div class="port-pnl ${pos?'pos':'neg'}">${pos?'+':''}${s.total_pnl.toFixed(3)} SOL</div>
      </div>
      <div class="port-bar-wrap">
        <div class="port-bar-fill" style="width:${pct}%;background:${col}"></div>
      </div>
      <div class="port-meta">
        <span>Wins: <span class="hl">${s.win_count}</span></span>
        <span>Losses: <span class="hl">${s.loss_count}</span></span>
        <span>Win rate: <span class="hl">${s.win_rate}%</span></span>
        <span>Avg/trade: <span class="hl">${s.avg_pnl>=0?'+':''}${s.avg_pnl.toFixed(4)} SOL</span></span>
        <span>Best: <span class="hl">+${s.best_trade.toFixed(3)}</span></span>
        <span>Worst: <span class="hl">${s.worst_trade.toFixed(3)}</span></span>
      </div>
      <div class="port-desc">${s.desc}</div>
    </div>`;
  }).join('');

  return summary + rows + `
  <div style="margin-top:12px;font-size:10px;color:var(--dim);padding:0 4px;line-height:1.7">
    ▸ All figures are simulated from real entry/exit MC data — not actual on-chain trades.<br>
    ▸ "Peak Ideal" shows theoretical max if you always sold at highest MC seen — useful as an upside benchmark.<br>
    ▸ Strategies using the bot's actual exit logic inherit TP/SL/time exits from tracker.py CONFIG.
  </div>`;
}

// ── MARKET CONDITIONS RENDERER ──────────────────────────
function renderMarket(d) {
  document.getElementById('tb-mkt').textContent = d.score ?? '—';
  const col     = d.rec_color==='green'?'var(--green)':d.rec_color==='amber'?'var(--amber)':'var(--red)';
  const barCol  = d.score>=65?'var(--green)':d.score>=40?'var(--amber)':'var(--red)';
  const updated = d.timestamp ? new Date(d.timestamp*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}) : '—';

  const scoreBlock = `
  <div style="text-align:center;padding:20px 0 8px">
    <div style="font-family:'Space Mono',monospace;font-size:56px;font-weight:700;color:${col};line-height:1">${d.score}</div>
    <div style="font-size:11px;color:var(--dim);margin:2px 0 6px">/ 100</div>
    <div style="font-family:'Space Mono',monospace;font-size:15px;font-weight:700;letter-spacing:4px;color:${col}">${d.recommendation}</div>
    <div style="font-size:10px;color:var(--dim);margin-top:4px">Updated ${updated}</div>
  </div>
  <div class="market-bar-wrap">
    <div class="market-bar-fill" style="width:${d.score}%;background:${barCol}"></div>
  </div>`;

  const sigCards = (d.signals||[]).map(sig => {
    const c = sig.status==='good'?'var(--green)':sig.status==='warn'?'var(--amber)':'var(--red)';
    return `<div class="signal-card ${sig.status}">
      <div class="sig-name">${sig.name} <span class="sig-pts" style="color:${c}">${sig.pts}/${sig.max}</span></div>
      <div class="sig-value">${sig.value}</div>
      <div class="sig-detail">${sig.detail}</div>
    </div>`;
  }).join('');

  const guidance = d.score>=65
    ? `<div class="band-insight" style="border-left-color:var(--green)">
        ✅ <b>Conditions look good for trading.</b> Multiple positive signals active.
        Watch for any rapid score drops which could indicate conditions turning.</div>`
    : d.score>=40
    ? `<div class="band-insight" style="border-left-color:var(--amber)">
        ⚠️ <b>Mixed conditions — trade with reduced size or be more selective.</b>
        Only enter the highest-scoring tokens (70+) until conditions improve.</div>`
    : `<div class="band-insight" style="border-left-color:var(--red)">
        🔴 <b>Poor conditions — consider pausing entries.</b>
        Most tokens launched in bearish/low-activity markets underperform significantly.</div>`;

  return `<div class="section-title"><span>Market Conditions Score</span>
    <span style="color:var(--dim)">Refreshes every 5s · external APIs polled on each load</span></div>
  ${guidance}${scoreBlock}
  <div class="market-grid" style="margin-top:14px">${sigCards}</div>
  <div style="margin-top:8px;font-size:10px;color:var(--dim);line-height:1.7">
    ▸ Internal signals (launch rate, quality, win rate, buy pressure, graduations) use live DB data.<br>
    ▸ SOL price from CoinGecko free tier · Fear & Greed from alternative.me — both may occasionally be unavailable.<br>
    ▸ This score is advisory only — use it alongside your own judgement.
  </div>`;
}

// ── STATS ───────────────────────────────────────────────
async function loadStats() {
  try {
    const d = await (await fetch('/api/stats')).json();
    document.getElementById('h-total').textContent    = d.total?.toLocaleString()?? '—';
    document.getElementById('h-watching').textContent = d.watching??'—';
    document.getElementById('h-scored').textContent   = d.scored?.toLocaleString()?? '—';
    document.getElementById('h-entries').textContent  = d.entries?.toLocaleString()?? '—';
    document.getElementById('h-wins').textContent     = d.wins??'—';
    document.getElementById('h-losses').textContent   = d.losses??'—';
    document.getElementById('h-open').textContent     = d.open_trades??'—';
  } catch(e){}
}

// ── TABS ────────────────────────────────────────────────
let activeTab = 'live';
const tabLoaders = {
  live:       ()=>fetchAndRender('/api/live',       'page-live',       renderLive),
  tradelog:   ()=>fetchAndRender('/api/tradelog',   'page-tradelog',   renderTradelog),
  pumped:     ()=>fetchAndRender('/api/pumped',     'page-pumped',     renderPumped),
  entries:    ()=>fetchAndRender('/api/entries',    'page-entries',    renderEntries),
  missed:     ()=>fetchAndRender('/api/missed',     'page-missed',     renderMissed),
  winners:    ()=>fetchAndRender('/api/winners',    'page-winners',    renderWinners),
  strategies: ()=>fetchAndRender('/api/strategies', 'page-strategies', renderStrategies),
  scorebands: ()=>fetchAndRender('/api/score_bands','page-scorebands', renderScoreBands),
  portfolio:  ()=>fetchAndRender('/api/portfolio',  'page-portfolio',  renderPortfolio),
  market:     ()=>fetchAndRender('/api/market',     'page-market',     renderMarket),
};

function switchTab(name, el) {
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('page-'+name).classList.add('active');
  activeTab = name;
  tabLoaders[name]?.();
}

function refreshAll() {
  loadStats();
  tabLoaders[activeTab]?.();
}

// ── INIT ─────────────────────────────────────────────────
loadStats();
tabLoaders['live']();
setInterval(()=>{ loadStats(); tabLoaders[activeTab]?.(); }, 5000);
</script>
</body>
</html>"""

@app.get("/")
def index():
    return HTMLResponse(HTML)

async def start():
    import uvicorn
    port   = int(os.environ.get("PORT", 8080))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
