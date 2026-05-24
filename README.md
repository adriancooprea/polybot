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

## Status

Scaffolding. Implementation steps to follow.

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
