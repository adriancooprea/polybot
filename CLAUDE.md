# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

`polybot` — a Claude-powered copy-trading bot for Polymarket. It ranks Polymarket
wallets by proven performance, monitors the top ones, and copies positions only
when multiple top wallets agree within a 30-minute window. Hard cap: 10 trades/day.

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

(To be filled in as the build progresses — test runner, lint, entrypoints.)
