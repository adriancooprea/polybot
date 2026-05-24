# polybot

A Claude-powered copy-trading bot for [Polymarket](https://polymarket.com).

## Thesis

Most people lose on prediction markets because they trade on opinion. This bot
has no opinions. It finds wallets that have *already* proven they beat the
market over hundreds of trades, watches what they do in real time, and only
acts when several of them independently agree on the same position inside a
short window.

## How it works

1. **Ingest** — pull historical Polymarket trade data and build a local store.
2. **Rank** — score every wallet by win rate and realized profit over time;
   keep only those that consistently beat the market across a large sample.
3. **Monitor** — watch the top-ranked wallets for new positions in real time.
4. **Signal** — fire only when *N* top wallets enter the same position within a
   30-minute window. Claude evaluates the signal for sanity before execution.
5. **Execute** — place the trade. Hard cap of **10 trades/day**. The discipline
   is the edge; overtrading is how accounts die.

## Quick start

One command on Linux/macOS — bootstraps a virtualenv, installs deps, runs:

```bash
git clone git@github.com:adriancooprea/polybot.git
cd polybot
./run.sh download    # 1. fetch the 86M-trade dataset (~GBs, into data/)
./run.sh rank        # 2. rank wallets -> data/top_wallets.csv
./run.sh monitor     # 3. watch top wallets, print consensus signals
./run.sh             # full bot: monitor -> Claude vote -> trade -> exit
./run.sh dashboard   # Matrix terminal dashboard
```

First run copies `.env.example` → `.env`. Add your `ANTHROPIC_API_KEY` and
Polymarket keys there. Trading defaults to **dry-run** (`DRY_RUN=true`); set it
to `false` only when you mean it. `touch data/KILL` is the kill-switch.

## Stack

- Python 3.12+
- Anthropic SDK (Claude) for the decision layer
- Polymarket data + trading APIs

## Cost target

Under ~$30/month to run.

## Disclaimer

For educational and research purposes. Trading prediction markets carries real
financial risk. Past wallet performance does not guarantee future results.
Nothing here is financial advice. You are responsible for legal/regulatory
compliance in your jurisdiction.
