"""Matrix-themed terminal dashboard.

Green-on-black panels: live positions, whale wallet monitor, trade log, Claude
consensus votes, and a bankroll compounding curve. Reads the executor's trade
log and the Polymarket API; refreshes on a timer.
"""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..config import CONFIG
from ..execute.executor import TRADE_LOG
from ..monitor.monitor import load_top_wallets

MATRIX = "bold green on black"
DIM = "green dim on black"


def _read_trades(limit: int = 200) -> list[dict]:
    if not TRADE_LOG.exists():
        return []
    rows: list[dict] = []
    with TRADE_LOG.open() as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:]


def _bankroll_curve(trades: list[dict], start: float = 200.0, stake: float = 20.0) -> list[float]:
    """Rough equity curve: SELLs realize (current-entry) * stake/entry."""
    equity = [start]
    bal = start
    entries: dict[str, float] = {}
    for t in trades:
        mid = t.get("market_id")
        if t.get("side") == "BUY":
            entries[mid] = t.get("price", 0)
        elif t.get("side") == "SELL" and mid in entries:
            entry = entries.pop(mid) or 1e-9
            bal += stake * (t.get("price", 0) - entry) / entry
            equity.append(round(bal, 2))
    return equity


def _sparkline(values: list[float]) -> str:
    if len(values) < 2:
        return "▁"
    bars = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    return "".join(bars[min(7, int((v - lo) / span * 7))] for v in values)


def _positions_panel(trades: list[dict]) -> Panel:
    open_mkts: dict[str, dict] = {}
    for t in trades:
        if t.get("side") == "BUY":
            open_mkts[t["market_id"]] = t
        elif t.get("side") == "SELL":
            open_mkts.pop(t["market_id"], None)
    tbl = Table(expand=True, style="green", border_style="green")
    tbl.add_column("MARKET"); tbl.add_column("OUT"); tbl.add_column("ENTRY"); tbl.add_column("$")
    for t in list(open_mkts.values())[-8:]:
        tbl.add_row(t["market_id"][:14] + "…", t.get("outcome", ""),
                    f'{t.get("price", 0):.3f}', f'{t.get("size_usd", 0):.0f}')
    return Panel(tbl, title="[bold]LIVE POSITIONS", style=MATRIX, border_style="green")


def _log_panel(trades: list[dict]) -> Panel:
    lines = []
    for t in trades[-12:]:
        ts = t.get("ts", "")[11:19]
        tag = "ENT" if t.get("side") == "BUY" else "EXT"
        lines.append(f'{ts} {tag} {t.get("outcome",""):>4} @{t.get("price",0):.3f} '
                     f'[{t.get("status","")}] {t.get("reason","")}')
    return Panel(Text("\n".join(lines) or "— no trades yet —", style="green"),
                 title="[bold]TRADE LOG", style=MATRIX, border_style="green")


def _whales_panel(wallets: list[str]) -> Panel:
    body = "\n".join(f"◉ {w[:18]}…" for w in wallets[:12]) or "— load top_wallets.csv —"
    return Panel(Text(body, style="green"),
                 title=f"[bold]WHALE MONITOR ({len(wallets)})", style=MATRIX, border_style="green")


def _consensus_panel(trades: list[dict]) -> Panel:
    votes = [t for t in trades if "consensus" in str(t.get("reason", ""))][-6:]
    body = "\n".join(f'✓ {t["market_id"][:16]}… {t.get("reason","")}' for t in votes)
    return Panel(Text(body or "— awaiting consensus —", style="green"),
                 title="[bold]CLAUDE CONSENSUS", style=MATRIX, border_style="green")


def _bankroll_panel(curve: list[float]) -> Panel:
    cur = curve[-1] if curve else 200.0
    start = curve[0] if curve else 200.0
    pct = (cur / start - 1) * 100 if start else 0
    body = Group(
        Text(_sparkline(curve), style="bold green"),
        Text(f"${cur:,.2f}  ({pct:+.1f}%)  from ${start:,.0f}", style="bold green"),
    )
    return Panel(body, title="[bold]BANKROLL", style=MATRIX, border_style="green")


def build(console: Console, wallets: list[str]) -> Layout:
    trades = _read_trades()
    curve = _bankroll_curve(trades)
    layout = Layout()
    layout.split_column(
        Layout(Panel(Text("polybot // POLYMARKET CONSENSUS ENGINE", style="bold green",
                          justify="center"), style=MATRIX, border_style="green"), size=3),
        Layout(name="mid"), Layout(name="bot", size=10))
    layout["mid"].split_row(Layout(_positions_panel(trades)), Layout(_whales_panel(wallets)))
    layout["bot"].split_row(Layout(_log_panel(trades)),
                            Layout(name="r"))
    layout["bot"]["r"].split_column(Layout(_consensus_panel(trades)),
                                    Layout(_bankroll_panel(curve)))
    return layout


def run(refresh_seconds: int = 3) -> None:
    console = Console()
    try:
        wallets = load_top_wallets()
    except FileNotFoundError:
        wallets = []
    with Live(build(console, wallets), console=console, screen=True,
              refresh_per_second=4) as live:
        while True:
            time.sleep(refresh_seconds)
            live.update(build(console, wallets))


if __name__ == "__main__":
    run()
