"""polybot command-line interface.

    polybot download    # step 1: fetch the 86M-trade dataset
    polybot rank        # step 2: rank wallets -> data/top_wallets.csv
    polybot monitor     # step 3: watch top wallets, print consensus signals
    polybot run         # full bot: monitor -> Claude vote -> execute -> exit
    polybot dashboard   # step 5: Matrix terminal dashboard
"""

from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser(prog="polybot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    # add_help=False: subcommand flags (e.g. `rank --top 50`, `rank --help`) pass
    # through to the real per-command argparse after re-dispatch below.
    def stub(name: str, help_text: str) -> None:
        sub.add_parser(name, help=help_text, add_help=False)

    stub("download", "download the Polymarket trade dataset")
    stub("resolutions", "fetch ground-truth market resolutions")
    stub("rank", "rank wallets and write top_wallets.csv")
    stub("leaderboard", "fresh wallet ranking from the live profit leaderboard")
    stub("monitor", "print consensus signals from top wallets")
    stub("run", "run the full trading bot")
    stub("dashboard", "launch the terminal dashboard")
    stub("doctor", "preflight check of config and data files")
    stub("report", "analyze recorded trade history (offline)")
    # let subcommands keep their own argparse flags (e.g. rank --top)
    args, rest = ap.parse_known_args()
    import sys
    sys.argv = [f"polybot {args.cmd}", *rest]

    if args.cmd == "download":
        from .ingest.download import main as f
    elif args.cmd == "resolutions":
        from .ingest.resolutions import main as f
    elif args.cmd == "rank":
        from .rank.rank_wallets import main as f
    elif args.cmd == "leaderboard":
        from .rank.leaderboard import main as f
    elif args.cmd == "monitor":
        from .monitor.monitor import main as f
    elif args.cmd == "run":
        from .bot import run as f
    elif args.cmd == "dashboard":
        from .dashboard.dashboard import run as f
    elif args.cmd == "report":
        from .journal import main as f
    elif args.cmd == "doctor":
        f = _doctor
    else:  # unreachable; argparse enforces choices
        ap.error("unknown command")
    f()


def _doctor() -> None:
    """Print a readiness report — what's configured, what's missing."""
    from .config import CONFIG

    def mark(ok: bool) -> str:
        return "✓" if ok else "✗"

    print("polybot doctor\n")
    print(f"  {mark(bool(CONFIG.anthropic_api_key))} ANTHROPIC_API_KEY (Claude vote)")
    print(f"  {mark(CONFIG.top_wallets_csv.exists())} {CONFIG.top_wallets_csv} (run `polybot rank`)")
    print(f"  dry_run={CONFIG.dry_run}  cap={CONFIG.max_trades_per_day}/day  "
          f"stake=${CONFIG.stake_usd}  consensus={CONFIG.min_wallets_agree}")
    if not CONFIG.dry_run:
        print("\n  LIVE TRADING ARMED — checking wallet config:")
        print(f"    {mark(bool(CONFIG.wallet_private_key))} POLYMARKET_WALLET_PRIVATE_KEY")
        print(f"    {mark(bool(CONFIG.funder))} POLYMARKET_FUNDER")
    else:
        print("\n  dry-run: no real orders will be placed (set DRY_RUN=false to arm).")


if __name__ == "__main__":
    main()
