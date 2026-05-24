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
    sub.add_parser("download", help="download the Polymarket trade dataset")
    sub.add_parser("rank", help="rank wallets and write top_wallets.csv")
    sub.add_parser("monitor", help="print consensus signals from top wallets")
    sub.add_parser("run", help="run the full trading bot")
    sub.add_parser("dashboard", help="launch the terminal dashboard")
    args = ap.parse_args()

    if args.cmd == "download":
        from .ingest.download import main as f
    elif args.cmd == "rank":
        from .rank.rank_wallets import main as f
    elif args.cmd == "monitor":
        from .monitor.monitor import main as f
    elif args.cmd == "run":
        from .bot import run as f
    elif args.cmd == "dashboard":
        from .dashboard.dashboard import run as f
    else:  # unreachable; argparse enforces choices
        ap.error("unknown command")
    f()


if __name__ == "__main__":
    main()
