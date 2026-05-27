#!/usr/bin/env python3
"""Backtest + optimize the consensus copy-trading strategy over a historical window.

MODES:
  fetch   - download & cache top-wallet activity + per-token price history (slow, network)
  run     - run one config over the whole window (uses cache; prints summary)
  sweep   - grid-search params on a TRAIN split, report best out-of-sample on TEST

CAVEATS (don't over-read results):
- EXCLUDES the Claude vote (it uses live web search => lookahead on resolved
  matches). The live bot SKIPs most signals via Claude; this tests the *raw
  signal* + guards + exits. Measures whether the signal itself has edge.
- Entry = CLOB midpoint at signal time (price-history), not an actual fill; no
  slippage/fees. Whale-exodus exit not simulated.
- Optimizing on history overfits; only the TEST (out-of-sample) numbers matter.
"""
from __future__ import annotations

import csv
import datetime as dt
import itertools
import json
import statistics as st
import sys
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

import httpx

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "backtest_cache"
CACHE.mkdir(parents=True, exist_ok=True)

WINDOW_START = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
WINDOW_END = dt.datetime(2026, 5, 15, 23, 59, 59, tzinfo=dt.UTC)
FETCH_FLOOR = int((WINDOW_START - dt.timedelta(days=2)).timestamp())
# train/test split for honest out-of-sample evaluation
SPLIT = dt.datetime(2026, 4, 1, tzinfo=dt.UTC)

DEFAULTS = dict(min_agree=3, window_s=30 * 60, max_chase=0.10,
                tp_pct=0.15, sl_pct=0.30, abs_tp=1.0,
                near_lo=0.05, near_hi=0.95, max_per_day=10,
                entry_lo=0.0, entry_hi=1.0, stake=2.0)

http = httpx.Client(timeout=30.0, follow_redirects=True)


def _get(url, params, tries=6):
    for i in range(tries):
        try:
            r = http.get(url, params=params)
            if r.status_code == 429:
                time.sleep(2 * (i + 1)); continue
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:
            time.sleep(1 + i)
    return None


def load_wallets():
    with (ROOT / "data" / "top_wallets.csv").open() as f:
        return [row["wallet"].lower() for row in csv.DictReader(f)]


def fetch_activity(wallet):
    """Paginate /activity back to FETCH_FLOOR. Cached per wallet+floor."""
    cache = CACHE / f"act_{wallet}_{FETCH_FLOOR}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    out, offset = [], 0
    while offset < 120000:  # deep cap for multi-month windows
        page = _get(f"{DATA_API}/activity",
                    {"user": wallet, "limit": 500, "offset": offset, "sortDirection": "DESC"})
        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        oldest = min(int(e.get("timestamp", 0) or 0) for e in page)
        if oldest < FETCH_FLOOR or len(page) < 500:
            break
        offset += 500
        time.sleep(0.2)
    cache.write_text(json.dumps(out))
    return out


def build_signals(wallets, window_s=30 * 60, min_agree=3):
    """Replicate monitor.py consensus over cached activity."""
    sig_cache = CACHE / f"signals_{FETCH_FLOOR}_{window_s}_{min_agree}.json"
    if sig_cache.exists():
        return json.loads(sig_cache.read_text())
    trades = []
    for w in wallets:
        for e in fetch_activity(w):
            if e.get("type") != "TRADE" or str(e.get("side", "")).upper() != "BUY":
                continue
            trades.append({"ts": int(e.get("timestamp", 0) or 0), "wallet": w,
                           "market": str(e.get("conditionId", "")), "outcome": str(e.get("outcome", "")),
                           "token": str(e.get("asset", "")), "price": float(e.get("price", 0) or 0),
                           "title": str(e.get("title", ""))})
    trades.sort(key=lambda t: t["ts"])
    windows, fired, signals = defaultdict(deque), set(), []
    for t in trades:
        win = windows[(t["market"], t["outcome"])]
        win.append(t)
        while win and win[0]["ts"] < t["ts"] - window_s:
            win.popleft()
        wset = {x["wallet"] for x in win}
        if len(wset) < min_agree:
            continue
        dd = (t["market"], t["outcome"], frozenset(wset))
        if dd in fired:
            continue
        fired.add(dd)
        prices = [x["price"] for x in win if x["price"]]
        signals.append({"ts": t["ts"], "market": t["market"], "outcome": t["outcome"],
                        "token": t["token"], "title": t["title"], "n_wallets": len(wset),
                        "avg_price": round(sum(prices) / len(prices), 4) if prices else 0.0})
    signals = [s for s in signals if WINDOW_START.timestamp() <= s["ts"] <= WINDOW_END.timestamp()]
    sig_cache.write_text(json.dumps(signals))
    return signals


def price_history(token):
    cache = CACHE / f"ph_{token}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    data = _get(f"{CLOB_API}/prices-history", {"market": token, "interval": "max", "fidelity": 10})
    hist = (data or {}).get("history", []) if isinstance(data, dict) else []
    cache.write_text(json.dumps(hist))
    time.sleep(0.15)
    return hist


def price_at(hist, ts):
    for p in hist:
        if p["t"] >= ts:
            return float(p["p"])
    return float(hist[-1]["p"]) if hist else None


def simulate(signals, p):
    """Run one parameter set over signals (cache must be warm). Returns trades."""
    per_day, entered, trades = defaultdict(int), set(), []
    for s in sorted(signals, key=lambda x: x["ts"]):
        if not s["token"] or s["token"] in entered:
            continue
        hist = price_history(s["token"])
        entry = price_at(hist, s["ts"]) or s["avg_price"]
        if not entry or entry <= 0:
            continue
        if entry >= p["near_hi"] or entry <= p["near_lo"]:
            continue
        if not (p["entry_lo"] <= entry <= p["entry_hi"]):
            continue
        if s["avg_price"] > 0 and entry > s["avg_price"] * (1 + p["max_chase"]):
            continue
        day = dt.datetime.fromtimestamp(s["ts"], dt.UTC).strftime("%Y-%m-%d")
        if per_day[day] >= p["max_per_day"]:
            continue
        per_day[day] += 1
        entered.add(s["token"])
        # exit walk
        tp, sl = entry * (1 + p["tp_pct"]), entry * (1 - p["sl_pct"])
        exit_px, reason = None, None
        for pt in hist:
            if pt["t"] <= s["ts"]:
                continue
            px = float(pt["p"])
            if p["abs_tp"] < 1.0 and px >= p["abs_tp"]:
                exit_px, reason = px, "abs_tp"; break
            if px >= tp:
                exit_px, reason = px, "take_profit"; break
            if px <= sl:
                exit_px, reason = px, "stop_loss"; break
        if exit_px is None:
            last = float(hist[-1]["p"]) if hist else entry
            exit_px = 1.0 if last >= p["near_hi"] else (0.0 if last <= p["near_lo"] else last)
            reason = "resolution"
        shares = p["stake"] / entry
        trades.append({"day": day, "ts": s["ts"], "title": s["title"][:36], "outcome": s["outcome"],
                       "n_wallets": s["n_wallets"], "entry": round(entry, 3), "exit": round(exit_px, 3),
                       "reason": reason, "pnl": round(shares * exit_px - p["stake"], 3)})
    return trades


def metrics(trades, stake):
    n = len(trades)
    if not n:
        return dict(n=0, pnl=0, roi=0, winrate=0, median=0, ex_top=0)
    pnls = [t["pnl"] for t in trades]
    total = sum(pnls)
    ex_top = total - max(pnls)  # PnL minus the single biggest winner (outlier check)
    return dict(n=n, pnl=round(total, 2), roi=round(total / (n * stake) * 100, 1),
                winrate=round(sum(x > 0 for x in pnls) / n * 100), median=round(st.median(pnls), 3),
                ex_top=round(ex_top, 2))


def _print_run(trades, p, label=""):
    m = metrics(trades, p["stake"])
    print(f"\n===== {label} =====")
    print(f"params: agree>={p['min_agree']} chase<{p['max_chase']} TP {p['tp_pct']:.0%} "
          f"SL {p['sl_pct']:.0%} absTP {p['abs_tp']} entry[{p['entry_lo']},{p['entry_hi']}] cap {p['max_per_day']}/d")
    print(f"trades {m['n']} | winrate {m['winrate']}% | PnL ${m['pnl']:+} ({m['roi']:+}%) | "
          f"median ${m['median']:+} | PnL ex-top-winner ${m['ex_top']:+}")
    print("exit reasons:", dict(Counter(t["reason"] for t in trades)))


def mode_fetch(wallets):
    print("fetching activity (deep)…", file=sys.stderr)
    for w in wallets:
        a = fetch_activity(w)
        print(f"  {w[:12]}…: {len(a)} rows", file=sys.stderr)
    sigs = build_signals(wallets, DEFAULTS["window_s"], DEFAULTS["min_agree"])
    print(f"signals in window: {len(sigs)} — warming price-history cache…", file=sys.stderr)
    for i, s in enumerate(sigs):
        if s["token"]:
            price_history(s["token"])
        if i % 25 == 0:
            print(f"  {i}/{len(sigs)} tokens", file=sys.stderr)
    print("fetch complete.", file=sys.stderr)


def mode_run(wallets):
    sigs = build_signals(wallets, DEFAULTS["window_s"], DEFAULTS["min_agree"])
    trades = simulate(sigs, DEFAULTS)
    _print_run(trades, DEFAULTS, f"FULL WINDOW {WINDOW_START:%Y-%m-%d}..{WINDOW_END:%Y-%m-%d}")


def mode_sweep(wallets):
    sigs = build_signals(wallets, DEFAULTS["window_s"], DEFAULTS["min_agree"])
    train = [s for s in sigs if s["ts"] < SPLIT.timestamp()]
    test = [s for s in sigs if s["ts"] >= SPLIT.timestamp()]
    print(f"signals: {len(sigs)} total | train {len(train)} (<{SPLIT:%Y-%m-%d}) | test {len(test)}",
          file=sys.stderr)
    grid = dict(
        tp_pct=[0.10, 0.15, 0.20, 0.25, 0.40],
        sl_pct=[0.10, 0.15, 0.20, 0.30],
        abs_tp=[1.0, 0.90, 0.95],
        max_chase=[0.05, 0.10, 0.20],
        entry_lo=[0.0, 0.10, 0.30],
        entry_hi=[1.0, 0.70, 0.90],
    )
    keys = list(grid)
    best, results = None, []
    for combo in itertools.product(*grid.values()):
        p = dict(DEFAULTS, **dict(zip(keys, combo)))
        tr = simulate(train, p)
        m = metrics(tr, p["stake"])
        if m["n"] < 20:  # need a minimum sample to be meaningful
            continue
        # robust objective: reward ROI but penalize outlier-dependence (ex-top must be positive-ish)
        score = m["roi"] + (m["ex_top"] / (m["n"] * p["stake"]) * 100)
        results.append((score, p, m))
    results.sort(key=lambda x: x[0], reverse=True)
    print("\n===== TOP 5 CONFIGS ON TRAIN (then validated on TEST) =====")
    for score, p, mtr in results[:5]:
        tst = simulate(test, p)
        mte = metrics(tst, p["stake"])
        print(f"\nTP{p['tp_pct']:.0%} SL{p['sl_pct']:.0%} absTP{p['abs_tp']} chase{p['max_chase']} "
              f"entry[{p['entry_lo']},{p['entry_hi']}]")
        print(f"  TRAIN: {mtr['n']}tr {mtr['winrate']}%w ROI{mtr['roi']:+}% ex-top${mtr['ex_top']:+}")
        print(f"  TEST : {mte['n']}tr {mte['winrate']}%w ROI{mte['roi']:+}% ex-top${mte['ex_top']:+}  <-- out-of-sample")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    ws = load_wallets()
    {"fetch": mode_fetch, "run": mode_run, "sweep": mode_sweep}[mode](ws)
