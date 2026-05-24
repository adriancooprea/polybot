# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

`polybot` — a Claude-powered copy-trading bot for Polymarket. It ranks Polymarket
wallets by proven performance, monitors the top ones, and copies positions only
when multiple top wallets agree within a 30-minute window. Hard cap: 10 trades/day.

## Current status (2026-05-24)

Code-complete and verified end-to-end in dry-run against live APIs. What's proven:

- Data layer (Gamma markets, Data API activity/positions, CLOB midpoint) — live,
  field shapes confirmed.
- Dataset: poly_data S3 snapshot is **raw `orderFilled`** (not `processed/trades.csv`);
  spans 2022-11 → 2025-10, ~151M rows, 6.24 GB xz / ~37 GB csv. Ranker consumes
  `orderFilled` directly and was validated on real rows (prices 100% in [0,1]).
- `maker`/`taker` in the dataset == `proxyWallet` in the Data API (same address
  space) — ranker output feeds the monitor correctly.
- Claude vote works (ENTER + SKIP) on the real Opus model.
- Full dry-run pipeline runs: signal → vote → token resolution → risk-gated
  execute → state persistence → exit evaluation.

**Live order submission — WORKING (2026-05-24).** Confirmed by placing a real
`$1` limit order on the live CLOB and cancelling it. Polymarket uses CLOB V2
(legacy `py-clob-client` is dead → `py-clob-client-v2`); new accounts are
EIP-7702 deposit wallets, `signature_type=3` (POLY_1271). The SDK *can't mint* a
deposit-wallet-bound API key (issues #65/#70/#71), but you don't need to — the
account already has one. The two things that make it work:

1. **`POLYMARKET_FUNDER` must be the account's "API wallet"** — the address shown
   on the site as *"for API use only"* (Portfolio → wallet address popover), NOT
   the deposit address you send USDC to. The CLOB credits your deposit to this
   API wallet, and orders set `signer == maker == funder == the API-key address`,
   which the exchange's signer check requires.
2. **Supply the account's existing L2 creds** in `.env`
   (`POLYMARKET_API_KEY/SECRET/PASSPHRASE`). L2 auth uses the EOA address (don't
   override it); the EOA signs, the deposit wallet validates via EIP-1271.

`clob.py` uses these creds directly when present (else falls back to derive).
Flip `DRY_RUN=false` to arm. Daily cap + kill-switch still apply.

### Pick up here (e.g. on a bigger machine)

Full ranking over ~151M rows needs ~60 GB+ free (csv + DuckDB spill). To produce
the ranked list: `./run.sh download && ./run.sh resolutions && ./run.sh rank`,
then copy `data/top_wallets.csv` to wherever the live bot runs.

## Architecture (intended)

Pipeline of independent stages, each runnable in isolation:

- `ingest/`  — pull Polymarket historical + live trade data into local storage.
- `rank/`    — score wallets by win rate and realized profit; produce a ranked list.
- `monitor/` — watch top wallets for new positions in real time.
- `signal/`  — detect agreement (N wallets, same position, 30-min window).
- `decide/`  — Claude evaluates a candidate signal before execution.
- `execute/` — place/cancel trades; enforce the daily trade cap and risk limits.

State flows forward; each stage reads the previous stage's output. Keep stages
decoupled so they can be tested and replayed against recorded data.

## Conventions

- Python 3.12+. Use type hints. Format with `ruff`.
- Never commit secrets. API keys live in `.env` (gitignored); load via env vars.
- All trade-executing code paths must respect the daily cap and a global
  kill-switch. No code path places a trade without passing risk checks.
- Money math: use integers (cents/shares) or `Decimal`, never `float`.
- Prefer recorded/replayable data for tests over live API calls.

## Safety

This bot moves real money. Treat `execute/` as the danger zone:
- Default to dry-run / paper mode. Live trading must be explicitly opted into.
- Confirm before any change that could place real orders.

## Commands

One-command launcher (`run.sh`) bootstraps a venv, installs, and dispatches:

```
./run.sh download      # ingest: fetch the 86M-trade snapshot -> data/
./run.sh resolutions   # fetch ground-truth resolutions -> data/resolutions.csv
./run.sh rank          # rank wallets -> data/top_wallets.csv
./run.sh monitor       # print consensus signals from top wallets
./run.sh doctor        # preflight: config + data readiness
./run.sh               # run the full bot (monitor -> Claude vote -> execute -> exit)
./run.sh dashboard     # Matrix terminal dashboard
```

Tests: `pytest tests/` (offline; covers risk gates + state).

Same commands via the installed entrypoint: `polybot <cmd>`.

Pipeline → module map:
- ingest  → `ingest/download.py`, `ingest/resolutions.py`, schema in `ingest/schema.py`
- rank    → `rank/rank_wallets.py` (DuckDB, out-of-core; `--resolutions` for truth)
- monitor → `monitor/monitor.py` (consensus detection)
- decide  → `decide/consensus.py` (Claude risk-gate vote)
- execute → `execute/executor.py` (caps + kill-switch + live routing),
            `execute/exit_rules.py`
- trade   → `polymarket/client.py` (read), `polymarket/clob.py` (live orders, V2)
- state   → `state.py` (open-trade persistence -> data/open_trades.json)
- bot     → `bot.py` (orchestrator), `cli.py` (CLI)

Kill-switch: `touch data/KILL` halts all trading.
