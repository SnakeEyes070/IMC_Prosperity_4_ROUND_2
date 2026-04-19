import argparse
import json


def confidence_discounted_value_cap(
    expected_extra_access_value: float,
    dispersion: float,
    confidence_zscore: float,
) -> float:
    return max(0.0, expected_extra_access_value - confidence_zscore * dispersion)


def recommended_bid(
    expected_extra_access_value: float,
    predicted_median: float,
    dispersion: float,
    safety_cushion: float,
    confidence_zscore: float,
) -> dict:
    value_cap = confidence_discounted_value_cap(
        expected_extra_access_value=expected_extra_access_value,
        dispersion=dispersion,
        confidence_zscore=confidence_zscore,
    )
    threshold = predicted_median + safety_cushion
    bid = min(value_cap, threshold)
    return {
        "confidence_discounted_value_cap": round(value_cap, 6),
        "predicted_clear_threshold": round(threshold, 6),
        "recommended_bid": round(max(0.0, bid), 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate the Round 2 market-access bid.")
    parser.add_argument("--expected-extra-access-value", type=float, required=True)
    parser.add_argument("--predicted-median", type=float, required=True)
    parser.add_argument("--dispersion", type=float, required=True)
    parser.add_argument("--safety-cushion", type=float, required=True)
    parser.add_argument("--confidence-zscore", type=float, default=1.0)
    parser.add_argument("--default-bid", type=float, default=6500.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    output = recommended_bid(
        expected_extra_access_value=args.expected_extra_access_value,
        predicted_median=args.predicted_median,
        dispersion=args.dispersion,
        safety_cushion=args.safety_cushion,
        confidence_zscore=args.confidence_zscore,
    )
    output["default_bid"] = round(args.default_bid, 6)
    output["default_bid_is_value_safe"] = args.default_bid <= output["confidence_discounted_value_cap"]
    output["default_bid_clears_threshold"] = args.default_bid >= output["predicted_clear_threshold"]

    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True))
        return

    print(f"Confidence-discounted value cap: {output['confidence_discounted_value_cap']:.6f}")
    print(f"Predicted clear threshold: {output['predicted_clear_threshold']:.6f}")
    print(f"Recommended bid: {output['recommended_bid']:.6f}")
    print(f"Default bid: {output['default_bid']:.6f}")
    print(f"Default bid is value-safe: {output['default_bid_is_value_safe']}")
    print(f"Default bid clears threshold: {output['default_bid_clears_threshold']}")


if __name__ == "__main__":
    main()
