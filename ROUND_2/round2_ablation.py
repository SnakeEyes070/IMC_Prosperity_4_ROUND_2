import argparse
import json
from itertools import product
from pathlib import Path
from typing import Dict, List

from round2_replay_lib import ReplaySummary, run_replay


DEFAULT_TRADER = Path(__file__).resolve().parent / "trader_68.py"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Round 2 ablations centered on trader_68.")
    parser.add_argument(
        "--trader-path",
        default=str(DEFAULT_TRADER),
        help="Path to the base trader module. Defaults to trader_68.py.",
    )
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
        help="Directory containing Round 2 CSV data.",
    )
    parser.add_argument(
        "--market-log",
        default=None,
        help="Optional active log file with activitiesLog/tradeHistory market data.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=3,
        help="How many top Pepper and Osmium candidates to cross in the combined sweep.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional JSON file path for the full ablation results.",
    )
    args = parser.parse_args()

    baseline = run_replay(
        args.trader_path,
        data_dir=args.data_dir,
        days=args.days,
        overrides={},
        market_log=args.market_log,
    )

    pepper_configs = [
        {
            "PEPPER_BUY_TOL": pepper_buy_tol,
            "ENDGAME_START": endgame_start,
            "SCALP_RESERVE": scalp_reserve,
        }
        for pepper_buy_tol, endgame_start, scalp_reserve in product(
            [4, 5, 6],
            [86_000, 88_000, 90_000, 92_000],
            [0, 6],
        )
    ]
    osmium_configs = [
        {
            "OSM_PASSIVE_BID_OFFSET": passive_offset,
            "OSM_PASSIVE_ASK_OFFSET": passive_offset,
            "OSM_AGGRESSIVE_BUY_THRESH": buy_thresh,
            "OSM_AGGRESSIVE_SELL_THRESH": sell_thresh,
            "OSM_EMA_ALPHA": ema_alpha,
            "OSM_SELL_COOLDOWN": cooldown,
        }
        for passive_offset, buy_thresh, sell_thresh, ema_alpha, cooldown in product(
            [5, 6],
            [9_998, 9_999, 10_000],
            [10_002, 10_003, 10_004],
            [0.02, 0.05],
            [0, 300],
        )
    ]

    pepper_results = _evaluate_many(
        args.trader_path, args.data_dir, args.days, pepper_configs, args.market_log
    )
    osmium_results = _evaluate_many(
        args.trader_path, args.data_dir, args.days, osmium_configs, args.market_log
    )

    top_pepper = pepper_results[: args.top_k]
    top_osmium = osmium_results[: args.top_k]
    combined_configs = _unique_configs(
        [
            {**pepper["overrides"], **osmium["overrides"]}
            for pepper in top_pepper
            for osmium in top_osmium
        ]
    )
    combined_results = _evaluate_many(
        args.trader_path, args.data_dir, args.days, combined_configs, args.market_log
    )

    all_runs = [
        {"label": "baseline", "overrides": {}, "summary": baseline},
        *pepper_results,
        *osmium_results,
        *combined_results,
    ]
    best_run = max(all_runs, key=lambda item: item["summary"].aggregate_pnl)

    payload = {
        "baseline": _summary_payload("baseline", {}, baseline),
        "pepper": [_summary_payload(item["label"], item["overrides"], item["summary"]) for item in pepper_results],
        "osmium": [_summary_payload(item["label"], item["overrides"], item["summary"]) for item in osmium_results],
        "combined": [_summary_payload(item["label"], item["overrides"], item["summary"]) for item in combined_results],
        "best": _summary_payload(best_run["label"], best_run["overrides"], best_run["summary"]),
    }

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print(_render_report(baseline, pepper_results, osmium_results, combined_results, best_run))


def _evaluate_many(
    trader_path: str,
    data_dir: str,
    days: List[int],
    configs: List[Dict[str, object]],
    market_log: str,
) -> List[Dict[str, object]]:
    results: List[Dict[str, object]] = []
    for index, overrides in enumerate(configs, start=1):
        summary = run_replay(
            trader_path,
            data_dir=data_dir,
            days=days,
            overrides=overrides,
            market_log=market_log,
        )
        results.append(
            {
                "label": f"run_{index}",
                "overrides": overrides,
                "summary": summary,
            }
        )
    results.sort(key=lambda item: item["summary"].aggregate_pnl, reverse=True)
    return results


def _summary_payload(label: str, overrides: Dict[str, object], summary: ReplaySummary) -> Dict[str, object]:
    return {
        "label": label,
        "overrides": overrides,
        "summary": summary.to_dict(),
    }


def _unique_configs(configs: List[Dict[str, object]]) -> List[Dict[str, object]]:
    seen = set()
    unique: List[Dict[str, object]] = []
    for config in configs:
        key = tuple(sorted(config.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(config)
    return unique


def _render_report(
    baseline: ReplaySummary,
    pepper_results: List[Dict[str, object]],
    osmium_results: List[Dict[str, object]],
    combined_results: List[Dict[str, object]],
    best_run: Dict[str, object],
) -> str:
    lines = [
        f"Baseline aggregate PnL: {baseline.aggregate_pnl:.6f}",
        "",
        "Top Pepper-only candidates:",
    ]
    lines.extend(_render_top_rows(pepper_results))
    lines.extend(["", "Top Osmium-only candidates:"])
    lines.extend(_render_top_rows(osmium_results))
    lines.extend(["", "Top combined candidates:"])
    lines.extend(_render_top_rows(combined_results))
    lines.extend(
        [
            "",
            "Best overall:",
            f"  {best_run['label']}: {best_run['summary'].aggregate_pnl:.6f}",
            f"  overrides={best_run['overrides']}",
        ]
    )
    return "\n".join(lines)


def _render_top_rows(results: List[Dict[str, object]], limit: int = 5) -> List[str]:
    rows: List[str] = []
    for item in results[:limit]:
        summary: ReplaySummary = item["summary"]  # type: ignore[assignment]
        rows.append(
            f"  {item['label']}: pnl={summary.aggregate_pnl:.6f}, overrides={item['overrides']}"
        )
    return rows


if __name__ == "__main__":
    main()
