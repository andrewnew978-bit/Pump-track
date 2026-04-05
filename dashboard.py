#!/usr/bin/env python3
"""
PumpTracker Dashboard — FastAPI server
Reads from tracker.db and serves a live web dashboard.
Started automatically by tracker.py alongside the WebSocket bot.
"""

import json
import os
import time
import sqlite3
from collections import defaultdict
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="PumpTracker")

DB_PATH          = os.environ.get("DB_PATH",                  "tracker.db")
WINNER_THRESHOLD = int(os.environ.get("WINNER_MC_THRESHOLD_USD", "20000"))
ENTRY_SCORE      = int(os.environ.get("MIN_SCORE_HIGHLIGHT",      "60"))

# ════════════════════════════════════════════════════════
# DB HELPERS
# ════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def to_dict(row):
    return dict(zip(row.keys(), row))

def hydrate(rows):
    """Parse JSON fields and clean up rows for API response."""
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
    c    = conn.cursor()

    c.execute("SELECT COUNT(*) as n FROM tokens")
    total = c.fetchone()["n"]

    c.execute("SELECT COUNT(*) as n FROM tokens WHERE watch_complete=1")
    scored = c.fetchone()["n"]

    c.execute("SELECT COUNT(*) as n FROM tokens WHERE score >= ? AND watch_complete=1", (ENTRY_SCORE,))
    entries = c.fetchone()["n"]

    c.execute("""
        SELECT COUNT(DISTINCT t.mint) as n FROM tokens t
        JOIN snapshots s ON t.mint=s.mint
        WHERE s.market_cap_usd >= ?
    """, (WINNER_THRESHOLD,))
    winners = c.fetchone()["n"]

    c.execute("SELECT COUNT(*) as n FROM tokens WHERE watch_complete=0")
    watching = c.fetchone()["n"]

    c.execute("SELECT COUNT(*) as n FROM trades")
    trade_count = c.fetchone()["n"]

    conn.close()
    return {
        "total":        total,
        "scored":       scored,
        "watching":     watching,
        "entries":      entries,
        "winners":      winners,
        "trade_count":  trade_count,
        "entry_rate":   round(entries / max(scored, 1) * 100, 1),
        "winner_rate":  round(winners / max(entries, 1) * 100, 1),
        "last_updated": int(time.time()),
    }

@app.get("/api/live")
def get_live(limit: int = 60):
    conn = get_db()
    c    = conn.cursor()
    c.execute("""
        SELECT t.*, MAX(s.market_cap_usd) as peak_mc
        FROM tokens t
        LEFT JOIN snapshots s ON t.mint=s.mint
        WHERE t.watch_complete=1
        GROUP BY t.mint
        ORDER BY t.created_at DESC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return hydrate(rows)

@app.get("/api/entries")
def get_entries():
    conn = get_db()
    c    = conn.cursor()
    c.execute("""
        SELECT t.*, MAX(s.market_cap_usd) as peak_mc
        FROM tokens t
        LEFT JOIN snapshots s ON t.mint=s.mint
        WHERE t.watch_complete=1 AND t.score >= ?
        GROUP BY t.mint
        ORDER BY t.created_at DESC
    """, (ENTRY_SCORE,))
    rows = c.fetchall()
    conn.close()
    return hydrate(rows)

@app.get("/api/missed")
def get_missed():
    """Winners the system would have skipped — most valuable for strategy tuning."""
    conn = get_db()
    c    = conn.cursor()
    c.execute("""
        SELECT t.*, MAX(s.market_cap_usd) as peak_mc
        FROM tokens t
        JOIN snapshots s ON t.mint=s.mint
        WHERE s.market_cap_usd >= ? AND (t.score < ? OR t.score IS NULL)
        GROUP BY t.mint
        ORDER BY peak_mc DESC
    """, (WINNER_THRESHOLD, ENTRY_SCORE))
    rows = c.fetchall()
    conn.close()
    return hydrate(rows)

@app.get("/api/winners")
def get_winners():
    """Winners the system correctly flagged."""
    conn = get_db()
    c    = conn.cursor()
    c.execute("""
        SELECT t.*, MAX(s.market_cap_usd) as peak_mc
        FROM tokens t
        JOIN snapshots s ON t.mint=s.mint
        WHERE s.market_cap_usd >= ? AND t.score >= ?
        GROUP BY t.mint
        ORDER BY peak_mc DESC
    """, (WINNER_THRESHOLD, ENTRY_SCORE))
    rows = c.fetchall()
    conn.close()
    return hydrate(rows)

@app.get("/api/strategies")
def get_strategies():
    conn = get_db()
    c    = conn.cursor()

    # Try performance table first (populated by nightly optimiser)
    c.execute("""
        SELECT * FROM strategy_performance
        WHERE date = (SELECT MAX(date) FROM strategy_performance)
        ORDER BY win_rate DESC
    """)
    perf = [to_dict(r) for r in c.fetchall()]

    if not perf:
        # Calculate live from raw data
        c.execute("""
            SELECT t.strategies_json, MAX(s.market_cap_usd) as peak_mc
            FROM tokens t
            LEFT JOIN snapshots s ON t.mint=s.mint
            WHERE t.watch_complete=1 AND t.strategies_json IS NOT NULL
            GROUP BY t.mint
        """)
        rows  = c.fetchall()
        stats = defaultdict(lambda: {"total": 0, "wins": 0})
        for row in rows:
            try:
                strats    = json.loads(row["strategies_json"])
                is_winner = (row["peak_mc"] or 0) >= WINNER_THRESHOLD
                for name, s in strats.items():
                    if s.get("fired"):
                        stats[name]["total"] += 1
                        if is_winner:
                            stats[name]["wins"] += 1
            except Exception:
                pass

        perf = sorted([
            {
                "strategy_name":  name,
                "total_signals":  stat["total"],
                "wins":           stat["wins"],
                "win_rate":       round(stat["wins"] / max(stat["total"], 1), 4),
                "weight":         1.0,
            }
            for name, stat in stats.items()
        ], key=lambda x: x["win_rate"], reverse=True)

    conn.close()
    return perf

@app.get("/api/snapshots/{mint}")
def get_snapshots(mint: str):
    conn = get_db()
    c    = conn.cursor()
    c.execute("SELECT * FROM snapshots WHERE mint=? ORDER BY timestamp", (mint,))
    rows = [to_dict(r) for r in c.fetchall()]
    conn.close()
    return rows

# ════════════════════════════════════════════════════════
# HTML DASHBOARD
# ════════════════════════════════════════════════════════
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PUMP TRACKER</title>
<link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@300;400;500;600;700&family=Space+Mono:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>
:root {
  --bg:      #07080f;
  --bg2:     #0c0e1a;
  --bg3:     #131525;
  --border:  #1a1e30;
  --border2: #242840;
  --green:   #00ff88;
  --cyan:    #00cfff;
  --amber:   #ffbb00;
  --red:     #ff3355;
  --purple:  #a78bfa;
  --text:    #b8bcc8;
  --dim:     #4a5068;
  --bright:  #e8eaf0;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'Fira Code',monospace;font-size:13px;overflow-x:hidden}

/* ── HEADER ───────────────────────────────── */
.header{
  background:var(--bg2);border-bottom:1px solid var(--border);
  padding:0 24px;display:flex;align-items:center;
  justify-content:space-between;height:56px;position:sticky;top:0;z-index:100;
}
.logo{
  font-family:'Space Mono',monospace;font-size:16px;font-weight:700;
  color:var(--green);letter-spacing:3px;display:flex;align-items:center;gap:10px;
}
.live-dot{
  width:8px;height:8px;background:var(--green);border-radius:50%;
  box-shadow:0 0 8px var(--green);animation:blink 1.4s ease-in-out infinite;
}
@keyframes blink{0%,100%{opacity:1;box-shadow:0 0 8px var(--green)}50%{opacity:.3;box-shadow:none}}

.header-stats{display:flex;gap:28px;align-items:center}
.hstat{text-align:center}
.hstat-v{font-family:'Space Mono',monospace;font-size:17px;font-weight:700;color:var(--cyan)}
.hstat-l{font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim);margin-top:1px}

.refresh-btn{
  background:none;border:1px solid var(--border2);color:var(--dim);
  padding:5px 12px;cursor:pointer;font-family:'Fira Code',monospace;
  font-size:11px;border-radius:2px;transition:.15s;letter-spacing:.5px;
}
.refresh-btn:hover{border-color:var(--green);color:var(--green)}

/* ── TABS ─────────────────────────────────── */
.tabs{
  background:var(--bg2);border-bottom:1px solid var(--border);
  padding:0 24px;display:flex;gap:2px;
}
.tab{
  padding:11px 18px;cursor:pointer;
  font-family:'Space Mono',monospace;font-size:10px;letter-spacing:1.5px;
  text-transform:uppercase;color:var(--dim);
  border-bottom:2px solid transparent;transition:.15s;
}
.tab:hover{color:var(--text)}
.tab.active{color:var(--green);border-bottom-color:var(--green)}
.tab-badge{
  display:inline-block;background:var(--bg3);color:var(--dim);
  font-size:9px;padding:1px 5px;border-radius:8px;margin-left:5px;
}
.tab.active .tab-badge{background:rgba(0,255,136,.15);color:var(--green)}

/* ── CONTENT ──────────────────────────────── */
.content{padding:20px 24px;max-width:1400px}
.page{display:none}
.page.active{display:block}
.last-update{font-size:10px;color:var(--dim);text-align:right;margin-bottom:14px;letter-spacing:.5px}

/* ── SECTION TITLE ────────────────────────── */
.section-title{
  font-family:'Space Mono',monospace;font-size:10px;letter-spacing:2px;
  text-transform:uppercase;color:var(--dim);margin-bottom:14px;
  padding-bottom:8px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
}

/* ── TOKEN CARDS ──────────────────────────── */
.token-card{
  background:var(--bg2);border:1px solid var(--border);border-radius:3px;
  padding:13px 16px;margin-bottom:7px;
  border-left:3px solid var(--border);transition:border-color .15s;
}
.token-card:hover{border-color:var(--border2)}
.token-card.entry{border-left-color:var(--green)}
.token-card.skip{border-left-color:var(--dim)}
.token-card.missed{border-left-color:var(--amber)}

.card-top{display:flex;align-items:center;gap:12px;margin-bottom:7px}
.sym{
  font-family:'Space Mono',monospace;font-size:15px;font-weight:700;
  color:var(--bright);min-width:82px;
}
.tok-name{color:var(--dim);flex:1;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:240px}
.score-num{
  font-family:'Space Mono',monospace;font-weight:700;font-size:20px;
  min-width:54px;text-align:right;
}
.score-num.high{color:var(--green)}
.score-num.mid{color:var(--amber)}
.score-num.low{color:var(--red)}
.score-num.zero{color:var(--dim)}

.badge{
  font-size:9px;font-weight:700;letter-spacing:1px;padding:3px 8px;border-radius:2px;
  font-family:'Space Mono',monospace;
}
.badge.enter{background:rgba(0,255,136,.1);color:var(--green);border:1px solid rgba(0,255,136,.25)}
.badge.skip{background:rgba(74,80,104,.1);color:var(--dim);border:1px solid rgba(74,80,104,.25)}

/* score bar */
.score-bar-row{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.score-bar-bg{flex:1;height:2px;background:var(--border2);border-radius:1px;overflow:hidden}
.score-bar-fill{height:100%;border-radius:1px;transition:width .4s ease}

/* stats row */
.tok-stats{display:flex;flex-wrap:wrap;gap:14px;font-size:11px;color:var(--dim);margin-bottom:8px}
.tok-stats .hl{color:var(--cyan)}
.tok-stats .hl-g{color:var(--green)}
.tok-stats .hl-a{color:var(--amber)}

/* strategy tags */
.stags{display:flex;flex-wrap:wrap;gap:4px}
.stag{
  font-size:10px;padding:2px 7px;border-radius:2px;letter-spacing:.3px;
  white-space:nowrap;
}
.stag.pos{background:rgba(0,255,136,.07);color:rgba(0,255,136,.85);border:1px solid rgba(0,255,136,.18)}
.stag.neg{background:rgba(255,51,85,.07);color:rgba(255,51,85,.85);border:1px solid rgba(255,51,85,.18)}

/* MC snapshots */
.snaps{display:flex;gap:14px;margin-top:9px;padding-top:9px;border-top:1px solid var(--border)}
.snap{text-align:center}
.snap-l{font-size:9px;color:var(--dim);letter-spacing:.5px;text-transform:uppercase}
.snap-v{font-size:12px;font-weight:600;color:var(--cyan)}
.snap-v.win{color:var(--green)}

/* peak MC on missed */
.peak-mc-big{
  font-family:'Space Mono',monospace;font-size:18px;font-weight:700;color:var(--amber);
}

/* ── STRATEGY TABLE ───────────────────────── */
.strat-list{display:flex;flex-direction:column;gap:1px}
.strat-row{
  display:flex;align-items:center;gap:14px;padding:10px 14px;
  background:var(--bg2);border:1px solid var(--border);border-radius:3px;
  transition:border-color .15s;
}
.strat-row:hover{border-color:var(--border2)}
.strat-rank{
  font-family:'Space Mono',monospace;font-size:11px;color:var(--dim);
  min-width:24px;text-align:right;
}
.strat-name{flex:1;font-size:12px}
.strat-type{
  font-size:9px;letter-spacing:1px;padding:2px 6px;border-radius:2px;
  font-family:'Space Mono',monospace;
}
.strat-type.pos{color:rgba(0,255,136,.7);background:rgba(0,255,136,.06);border:1px solid rgba(0,255,136,.15)}
.strat-type.neg{color:rgba(255,51,85,.7); background:rgba(255,51,85,.06); border:1px solid rgba(255,51,85,.15)}
.strat-signals{font-size:11px;color:var(--dim);min-width:60px;text-align:right}
.strat-wins{font-size:11px;color:var(--cyan);min-width:40px;text-align:right}
.strat-wr{
  font-family:'Space Mono',monospace;font-size:13px;font-weight:700;
  min-width:52px;text-align:right;
}
.strat-bar-wrap{width:120px}
.strat-bar-bg{height:4px;background:var(--border2);border-radius:2px;overflow:hidden}
.strat-bar-fill{height:100%;border-radius:2px;transition:width .4s}
.strat-weight{font-size:11px;color:var(--purple);min-width:44px;text-align:right}

/* ── EMPTY STATE ──────────────────────────── */
.empty{
  text-align:center;padding:70px 20px;color:var(--dim);
  font-family:'Space Mono',monospace;
}
.empty .icon{font-size:36px;margin-bottom:12px;opacity:.4}
.empty .title{font-size:13px;letter-spacing:1px;margin-bottom:6px}
.empty .sub{font-size:11px;opacity:.6}

/* ── GRID LAYOUT FOR WIDER SCREENS ───────── */
@media(min-width:900px){
  .card-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}
  .card-grid .token-card{margin-bottom:0}
}

/* ── ANALYSIS BOX ─────────────────────────── */
.analysis-box{
  background:var(--bg2);border:1px solid var(--border);border-left:3px solid var(--purple);
  border-radius:3px;padding:14px 16px;margin-bottom:16px;
}
.analysis-title{
  font-family:'Space Mono',monospace;font-size:10px;letter-spacing:2px;
  text-transform:uppercase;color:var(--purple);margin-bottom:8px;
}
.analysis-text{font-size:12px;color:var(--text);line-height:1.7}
.insight{display:flex;gap:8px;margin-bottom:5px;font-size:12px}
.insight-dot{color:var(--purple);flex-shrink:0}

/* scrollbar */
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
</style>
</head>
<body>

<div class="header">
  <div class="logo">
    <div class="live-dot"></div>
    PUMP TRACKER
  </div>
  <div class="header-stats">
    <div class="hstat"><div class="hstat-v" id="h-total">—</div><div class="hstat-l">Seen</div></div>
    <div class="hstat"><div class="hstat-v" id="h-watching">—</div><div class="hstat-l">Watching</div></div>
    <div class="hstat"><div class="hstat-v" id="h-scored">—</div><div class="hstat-l">Scored</div></div>
    <div class="hstat"><div class="hstat-v" id="h-entries" style="color:var(--green)">—</div><div class="hstat-l">Entries</div></div>
    <div class="hstat"><div class="hstat-v" id="h-winners" style="color:var(--amber)">—</div><div class="hstat-l">Winners</div></div>
    <div class="hstat"><div class="hstat-v" id="h-trades">—</div><div class="hstat-l">Trades</div></div>
  </div>
  <button class="refresh-btn" onclick="refreshAll()">↻ REFRESH</button>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('live',this)">Live Feed <span class="tab-badge" id="tb-live">0</span></div>
  <div class="tab" onclick="switchTab('entries',this)">Entries <span class="tab-badge" id="tb-entries">0</span></div>
  <div class="tab" onclick="switchTab('missed',this)">Missed Winners <span class="tab-badge" id="tb-missed">0</span></div>
  <div class="tab" onclick="switchTab('winners',this)">Confirmed Wins <span class="tab-badge" id="tb-winners">0</span></div>
  <div class="tab" onclick="switchTab('strategies',this)">Strategies <span class="tab-badge" id="tb-strats">0</span></div>
</div>

<div class="content">
  <div id="page-live"       class="page active"></div>
  <div id="page-entries"    class="page"></div>
  <div id="page-missed"     class="page"></div>
  <div id="page-winners"    class="page"></div>
  <div id="page-strategies" class="page"></div>
</div>

<script>
// ── UTILS ──────────────────────────────────────────────
const fmt$ = v => v >= 1e6 ? `$${(v/1e6).toFixed(2)}M` : v >= 1e3 ? `$${(v/1e3).toFixed(1)}k` : `$${Math.round(v||0).toLocaleString()}`;
const fmtSol = v => v ? `${(+v).toFixed(2)} SOL` : '—';
const timeAgo = ts => {
  if(!ts) return '—';
  const s = Math.floor(Date.now()/1000 - ts);
  if(s < 60)   return `${s}s ago`;
  if(s < 3600) return `${Math.floor(s/60)}m ago`;
  return `${Math.floor(s/3600)}h ago`;
};
const scoreColor = s => s >= 60 ? 'var(--green)' : s >= 40 ? 'var(--amber)' : s > 0 ? 'var(--red)' : 'var(--dim)';
const scoreClass = s => s >= 60 ? 'high' : s >= 40 ? 'mid' : s > 0 ? 'low' : 'zero';
const barColor   = s => s >= 60 ? 'var(--green)' : s >= 40 ? 'var(--amber)' : 'var(--red)';

function stratLabel(name) {
  const MAP = {
    whale_sniffer:'🐋 Whale Sniffer', micro_bot_swarm:'🤖 Micro Bot Swarm',
    holder_velocity:'📈 Holder Velocity', volume_consistency:'📊 Vol Consistency',
    dev_hands_off:'🔒 Dev Hands Off', first_buy_size:'💰 First Buy Size',
    buy_sell_ratio:'⚖️ Buy/Sell Ratio', time_to_50_holders:'⚡ 50 Holders',
    repeat_buyers:'🔄 Repeat Buyers', early_volume:'💥 Early Volume',
    has_twitter:'🐦 Twitter', has_telegram:'📱 Telegram',
    peak_launch_time:'⏰ Peak Time', short_ticker:'🏷️ Short Ticker',
    strong_initial_buy:'🚀 Dev Init Buy',
    early_dev_dump:'💀 Dev Dump', single_wallet_dominance:'👁️ Wallet Dom',
    wash_trading:'🔃 Wash Trading', no_socials:'👻 No Socials',
    bundle_detector:'📦 Bundle', early_sell_pressure:'📉 Sell Pressure',
  };
  return MAP[name] || name.replace(/_/g,' ');
}

// ── CARD BUILDER ───────────────────────────────────────
function buildCard(tok, mode='live') {
  const score   = tok.score ?? 0;
  const isEntry = score >= 60;
  const strats  = tok.strategies || {};
  const stats   = tok.stats || {};
  const peakMc  = tok.peak_mc;
  const hasPeak = peakMc && peakMc > 0;

  const posFired = Object.entries(strats).filter(([,v])=> v.positive && v.fired).map(([k])=>k);
  const negFired = Object.entries(strats).filter(([,v])=>!v.positive && v.fired).map(([k])=>k);

  const socials = [tok.twitter?'🐦':'', tok.telegram?'📱':''].filter(Boolean).join(' ');

  const cardClass = mode==='missed' ? 'missed' : isEntry ? 'entry' : 'skip';
  const badgeHtml = isEntry
    ? '<span class="badge enter">ENTRY</span>'
    : '<span class="badge skip">SKIP</span>';

  const posTagsHtml = posFired.map(k=>`<span class="stag pos">${stratLabel(k)}</span>`).join('');
  const negTagsHtml = negFired.map(k=>`<span class="stag neg">${stratLabel(k)}</span>`).join('');

  const peakHtml = hasPeak && mode==='missed'
    ? `<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border)">
         <span class="peak-mc-big">${fmt$(peakMc)}</span>
         <span style="font-size:11px;color:var(--dim);margin-left:8px">peak MC — we would have MISSED this</span>
       </div>` : '';

  const snapHtml = hasPeak && mode!=='missed'
    ? `<div class="snaps">
         <div class="snap"><div class="snap-l">Peak</div><div class="snap-v ${peakMc>=20000?'win':''}">${fmt$(peakMc)}</div></div>
       </div>` : '';

  return `
  <div class="token-card ${cardClass}">
    <div class="card-top">
      <div class="sym">$${tok.symbol||'???'}</div>
      <div class="tok-name">${tok.name||'Unknown'} ${socials}</div>
      ${badgeHtml}
      <div class="score-num ${scoreClass(score)}">${score}</div>
    </div>
    <div class="score-bar-row">
      <div class="score-bar-bg">
        <div class="score-bar-fill" style="width:${score}%;background:${barColor(score)}"></div>
      </div>
    </div>
    <div class="tok-stats">
      <span>Buys: <span class="hl">${stats.total_buys??'—'}</span></span>
      <span>Sells: <span class="hl">${stats.total_sells??'—'}</span></span>
      <span>Wallets: <span class="hl">${stats.unique_buyers??'—'}</span></span>
      <span>Vol: <span class="hl">${fmtSol(stats.total_vol_sol)}</span></span>
      <span>Launched: <span class="hl">${timeAgo(tok.created_at)}</span></span>
    </div>
    <div class="stags">${posTagsHtml}${negTagsHtml}</div>
    ${peakHtml}${snapHtml}
  </div>`;
}

// ── STRATEGY ROW ───────────────────────────────────────
function buildStratRow(s, rank) {
  const wr   = s.win_rate ?? 0;
  const pct  = Math.round(wr * 100);
  const col  = pct >= 50 ? 'var(--green)' : pct >= 30 ? 'var(--amber)' : 'var(--red)';
  const wrCls = pct >= 50 ? 'high' : pct >= 30 ? 'mid' : 'low';
  const isNeg = ['early_dev_dump','single_wallet_dominance','wash_trading',
                 'no_socials','bundle_detector','early_sell_pressure'].includes(s.strategy_name);
  const typeHtml = isNeg
    ? '<span class="strat-type neg">NEGATIVE</span>'
    : '<span class="strat-type pos">POSITIVE</span>';
  const weightBg = s.weight > 1.2 ? 'var(--green)' : s.weight < 0.8 ? 'var(--red)' : 'var(--cyan)';

  return `
  <div class="strat-row">
    <div class="strat-rank">${rank}</div>
    <div class="strat-name">${stratLabel(s.strategy_name)}</div>
    ${typeHtml}
    <div class="strat-signals">${s.total_signals} signals</div>
    <div class="strat-wins">${s.wins} wins</div>
    <div class="strat-wr" style="color:${col}">${pct}%</div>
    <div class="strat-bar-wrap">
      <div class="strat-bar-bg">
        <div class="strat-bar-fill" style="width:${pct}%;background:${col}"></div>
      </div>
    </div>
    <div class="strat-weight" style="color:${weightBg}">w:${(s.weight??1).toFixed(2)}</div>
  </div>`;
}

// ── MISSED ANALYSIS ────────────────────────────────────
function buildMissedAnalysis(tokens) {
  if(!tokens.length) return '';

  // Count which strategies the missed winners fired
  const stratCounts = {};
  tokens.forEach(tok => {
    const strats = tok.strategies || {};
    Object.entries(strats).forEach(([k,v]) => {
      if(v.fired) stratCounts[k] = (stratCounts[k]||0) + 1;
    });
  });

  const topStrats = Object.entries(stratCounts)
    .sort((a,b)=>b[1]-a[1]).slice(0,5)
    .map(([k,n])=>`${stratLabel(k)} (${n}/${tokens.length} missed winners)`);

  const insights = topStrats.length ? topStrats.map(s=>
    `<div class="insight"><span class="insight-dot">▸</span><span>${s}</span></div>`
  ).join('') : '<div class="insight"><span class="insight-dot">▸</span><span>No pattern data yet — need more snapshot data</span></div>';

  return `
  <div class="analysis-box">
    <div class="analysis-title">🧠 Pattern Analysis — What did missed winners have in common?</div>
    <div class="analysis-text">
      ${insights}
    </div>
  </div>`;
}

// ── FETCH + RENDER ─────────────────────────────────────
const cache = {};

async function fetchAndRender(endpoint, pageId, renderer) {
  try {
    const r = await fetch(endpoint);
    const d = await r.json();
    cache[endpoint] = d;
    document.getElementById(pageId).innerHTML = renderer(d);
  } catch(e) {
    document.getElementById(pageId).innerHTML =
      `<div class="empty"><div class="icon">⚡</div><div class="title">CONNECTION ERROR</div><div class="sub">${e.message}</div></div>`;
  }
}

function renderLive(tokens) {
  if(!tokens.length) return `<div class="empty"><div class="icon">📡</div><div class="title">WAITING FOR DATA</div><div class="sub">Tokens will appear as they are scored</div></div>`;
  document.getElementById('tb-live').textContent = tokens.length;
  return `<div class="section-title"><span>Last ${tokens.length} Scored Tokens</span><span style="color:var(--dim);font-size:10px">${new Date().toLocaleTimeString()}</span></div>
    <div class="card-grid">${tokens.map(t=>buildCard(t,'live')).join('')}</div>`;
}

function renderEntries(tokens) {
  document.getElementById('tb-entries').textContent = tokens.length;
  if(!tokens.length) return `<div class="empty"><div class="icon">🎯</div><div class="title">NO ENTRIES YET</div><div class="sub">Tokens scoring ≥60 will appear here</div></div>`;
  const entered  = tokens.filter(t=>t.peak_mc && t.peak_mc >= 20000);
  const winPct   = Math.round(entered.length / tokens.length * 100);
  const summary  = `<div class="section-title"><span>${tokens.length} Entry Signals</span><span>${entered.length} confirmed wins (${winPct}% hit ${'>'}20k)</span></div>`;
  return summary + `<div class="card-grid">${tokens.map(t=>buildCard(t,'entry')).join('')}</div>`;
}

function renderMissed(tokens) {
  document.getElementById('tb-missed').textContent = tokens.length;
  if(!tokens.length) return `<div class="empty"><div class="icon">✅</div><div class="title">NO MISSED WINNERS</div><div class="sub">Tokens that pumped but we skipped will appear here.<br>This page is only useful after collecting snapshot data (10m–6h checks)</div></div>`;
  const analysis = buildMissedAnalysis(tokens);
  const title    = `<div class="section-title"><span>${tokens.length} Tokens We Would Have Missed</span><span style="color:var(--amber)">Study these to improve strategies</span></div>`;
  return title + analysis + `<div class="card-grid">${tokens.map(t=>buildCard(t,'missed')).join('')}</div>`;
}

function renderWinners(tokens) {
  document.getElementById('tb-winners').textContent = tokens.length;
  if(!tokens.length) return `<div class="empty"><div class="icon">🏆</div><div class="title">NO CONFIRMED WINS YET</div><div class="sub">Tokens we flagged that hit 20k+ MC will appear here.<br>Requires snapshot data to populate.</div></div>`;
  const title = `<div class="section-title"><span>${tokens.length} Confirmed Wins — Flagged ✓ AND Hit ${'>'}20k MC</span></div>`;
  return title + `<div class="card-grid">${tokens.map(t=>buildCard(t,'winners')).join('')}</div>`;
}

function renderStrategies(strats) {
  document.getElementById('tb-strats').textContent = strats.length;
  if(!strats.length) return `<div class="empty"><div class="icon">🧠</div><div class="title">NO STRATEGY DATA YET</div><div class="sub">Win rates populate after snapshot data is collected (10m–6h checks)</div></div>`;

  const withWinRate = strats.filter(s => s.total_signals >= 1);
  const noData      = strats.filter(s => s.total_signals < 1);

  const header = `
  <div class="section-title">
    <span>${strats.length} Strategies — Ranked by Win Rate</span>
    <span style="color:var(--dim)">Win = token hit ${'>'}$20k MC</span>
  </div>
  <div class="strat-row" style="background:none;border-color:transparent;padding:4px 14px">
    <div style="width:24px"></div>
    <div style="flex:1;font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--dim)">Strategy</div>
    <div style="width:70px"></div>
    <div style="min-width:60px;text-align:right;font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--dim)">Signals</div>
    <div style="min-width:40px;text-align:right;font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--dim)">Wins</div>
    <div style="min-width:52px;text-align:right;font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--dim)">Win%</div>
    <div style="width:120px;font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--dim)">Bar</div>
    <div style="min-width:44px;text-align:right;font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--dim)">Weight</div>
  </div>`;

  const rows = withWinRate.map((s,i)=>buildStratRow(s,i+1)).join('');
  return header + `<div class="strat-list">${rows}</div>`;
}

// ── STATS HEADER ───────────────────────────────────────
async function loadStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    document.getElementById('h-total').textContent    = d.total?.toLocaleString() ?? '—';
    document.getElementById('h-watching').textContent = d.watching ?? '—';
    document.getElementById('h-scored').textContent   = d.scored?.toLocaleString() ?? '—';
    document.getElementById('h-entries').textContent  = d.entries?.toLocaleString() ?? '—';
    document.getElementById('h-winners').textContent  = d.winners ?? '—';
    document.getElementById('h-trades').textContent   = d.trade_count?.toLocaleString() ?? '—';
  } catch(e) {}
}

// ── TAB SWITCHING ──────────────────────────────────────
let activeTab = 'live';
const tabLoaders = {
  live:       ()=>fetchAndRender('/api/live',       'page-live',       renderLive),
  entries:    ()=>fetchAndRender('/api/entries',    'page-entries',    renderEntries),
  missed:     ()=>fetchAndRender('/api/missed',     'page-missed',     renderMissed),
  winners:    ()=>fetchAndRender('/api/winners',    'page-winners',    renderWinners),
  strategies: ()=>fetchAndRender('/api/strategies', 'page-strategies', renderStrategies),
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

// ── INIT + AUTO REFRESH ────────────────────────────────
loadStats();
tabLoaders['live']();
setInterval(()=>{ loadStats(); tabLoaders[activeTab]?.(); }, 10000);
</script>
</body>
</html>"""

# ════════════════════════════════════════════════════════
# SERVE DASHBOARD
# ════════════════════════════════════════════════════════
@app.get("/")
def index():
    return HTMLResponse(HTML)


async def start():
    """Start the dashboard web server. Called by tracker.py."""
    import uvicorn
    port   = int(os.environ.get("PORT", 8080))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
