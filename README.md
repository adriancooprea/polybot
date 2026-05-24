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
./run.sh download      # 1. fetch the 86M-trade dataset (~GBs, into data/)
./run.sh resolutions   # (optional) ground-truth outcomes for accurate PnL
./run.sh rank          # 2. rank wallets -> data/top_wallets.csv
./run.sh monitor       # 3. watch top wallets, print consensus signals
./run.sh doctor        # check config/readiness before going live
./run.sh               # full bot: monitor -> Claude vote -> trade -> exit
./run.sh dashboard     # Matrix terminal dashboard
```

First run copies `.env.example` → `.env`. Add your `ANTHROPIC_API_KEY` and
Polymarket keys there.

### Going live

Trading defaults to **dry-run** (`DRY_RUN=true`) — orders are logged, never sent.
To arm real trading, in `.env`:

1. Set `POLYMARKET_WALLET_PRIVATE_KEY` and `POLYMARKET_FUNDER` (the address
   holding your USDC), and `POLYMARKET_SIGNATURE_TYPE` (1 = email/Magic, 0 = EOA).
2. Set token allowances on Polymarket first (EOA/MetaMask users).
3. Set `DRY_RUN=false`. Run `./run.sh doctor` to confirm.

Orders route through `py-clob-client` as market FOK orders. Safety rails always
on: `MAX_TRADES_PER_DAY` cap (entries only), and `touch data/KILL` halts all
trading instantly. Open positions persist to `data/open_trades.json` and resume
after a restart.

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
