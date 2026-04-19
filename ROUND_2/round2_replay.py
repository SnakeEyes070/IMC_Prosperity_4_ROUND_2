import argparse
import json

from round2_replay_lib import format_summary, parse_override_items, run_replay


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a Round 2 trader against local CSV market data.")
    parser.add_argument("trader_path", help="Path to the trader module to replay.")
    parser.add_argument(
        "--days",
        nargs="*",
        type=int,
        default=None,
        help="Optional day subset from {-1, 0, 1}. Defaults to all three days.",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Directory containing prices_round_2_day_*.csv and trades_round_2_day_*.csv.",
    )
    parser.add_argument(
        "--market-log",
        default=None,
        help="Optional active log file with activitiesLog/tradeHistory market data.",
    )
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        help="Override Trader class attributes, for example --set PEPPER_BUY_TOL=4.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit replay output as JSON instead of a formatted summary.",
    )
    args = parser.parse_args()

    overrides = parse_override_items(args.overrides)
    summary = run_replay(
        args.trader_path,
        data_dir=args.data_dir,
        days=args.days,
        overrides=overrides,
        market_log=args.market_log,
    )

    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    else:
        print(format_summary(summary))


if __name__ == "__main__":
    main()
