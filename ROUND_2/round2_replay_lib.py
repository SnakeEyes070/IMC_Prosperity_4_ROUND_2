from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from datamodel import Listing, Observation, OrderDepth, Trade, TradingState


ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = ROOT / "ROUND_2"
GLOBAL_DAY_ORDER = [-1, 0, 1]
PRODUCTS = ["INTARIAN_PEPPER_ROOT", "ASH_COATED_OSMIUM"]
DENOMINATION = "XIRECS"
TRACKED_PARAMS = [
    "PEPPER_BUY_TOL",
    "ENDGAME_START",
    "SCALP_RESERVE",
    "SCALP_DIP",
    "SCALP_EXIT",
    "SCALP_SIZE",
    "OSM_EMA_ALPHA",
    "OSM_PASSIVE_BID_OFFSET",
    "OSM_PASSIVE_ASK_OFFSET",
    "OSM_AGGRESSIVE_BUY_THRESH",
    "OSM_AGGRESSIVE_SELL_THRESH",
    "OSM_SELL_COOLDOWN",
    "OSM_USE_LADDERED_AGGRESSION",
    "OSM_USE_PASSIVE_SKEW",
    "OSM_AGG_SIZE_L1",
    "OSM_AGG_SIZE_L2",
    "OSM_AGG_DEEPER",
]


@dataclass
class PriceRow:
    day: int
    timestamp: int
    product: str
    bids: List[Tuple[int, int]]
    asks: List[Tuple[int, int]]
    mid_price: float


@dataclass
class PendingOrder:
    product: str
    price: int
    quantity: int


@dataclass
class ProductReplaySummary:
    pnl: float
    position: int
    cash: float
    final_mid: float
    buy_trades: int
    sell_trades: int
    buy_qty: int
    sell_qty: int


@dataclass
class DayReplaySummary:
    day: int
    total_pnl: float
    per_product: Dict[str, ProductReplaySummary]


@dataclass
class ReplaySummary:
    trader_path: str
    bid: int
    days: List[int]
    aggregate_pnl: float
    per_day: List[DayReplaySummary]
    per_product: Dict[str, ProductReplaySummary]
    parameters: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trader_path": self.trader_path,
            "bid": self.bid,
            "days": self.days,
            "aggregate_pnl": self.aggregate_pnl,
            "per_day": [
                {
                    "day": item.day,
                    "total_pnl": item.total_pnl,
                    "per_product": {
                        product: vars(summary) for product, summary in item.per_product.items()
                    },
                }
                for item in self.per_day
            ],
            "per_product": {product: vars(summary) for product, summary in self.per_product.items()},
            "parameters": self.parameters,
        }


def resolve_data_dir(data_dir: Optional[str] = None) -> Path:
    path = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    return path.resolve()


def normalize_days(days: Optional[Sequence[int]]) -> List[int]:
    if not days:
        return list(GLOBAL_DAY_ORDER)
    ordered = sorted(set(int(day) for day in days), key=GLOBAL_DAY_ORDER.index)
    return ordered


def load_price_rows(data_dir: Path, day: int) -> Tuple[List[int], Dict[int, Dict[str, PriceRow]]]:
    path = data_dir / f"prices_round_2_day_{day}.csv"
    by_timestamp: Dict[int, Dict[str, PriceRow]] = {}

    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for raw in reader:
            timestamp = int(raw["timestamp"])
            product = raw["product"]
            bids = _parse_levels(raw, "bid")
            asks = _parse_levels(raw, "ask")
            row = PriceRow(
                day=int(raw["day"]),
                timestamp=timestamp,
                product=product,
                bids=bids,
                asks=asks,
                mid_price=float(raw["mid_price"]),
            )
            by_timestamp.setdefault(timestamp, {})[product] = row

    timestamps = sorted(by_timestamp.keys())
    return timestamps, by_timestamp


def load_trade_rows(data_dir: Path, day: int) -> Dict[int, Dict[str, List[Trade]]]:
    path = data_dir / f"trades_round_2_day_{day}.csv"
    by_timestamp: Dict[int, Dict[str, List[Trade]]] = {}

    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for raw in reader:
            timestamp = int(raw["timestamp"])
            product = raw["symbol"]
            trade = Trade(
                symbol=product,
                price=int(round(float(raw["price"]))),
                quantity=int(raw["quantity"]),
                buyer=raw["buyer"] or None,
                seller=raw["seller"] or None,
                timestamp=timestamp,
            )
            by_timestamp.setdefault(timestamp, {}).setdefault(product, []).append(trade)

    return by_timestamp


def load_log_market(
    log_path: Path,
) -> Dict[int, Tuple[List[int], Dict[int, Dict[str, PriceRow]], Dict[int, Dict[str, List[Trade]]]]]:
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    price_rows = _load_price_rows_from_activities_log(payload["activitiesLog"])
    trade_rows = _load_trade_rows_from_history(payload.get("tradeHistory", []))
    return {
        day: (
            sorted(price_rows[day].keys()),
            price_rows[day],
            trade_rows.get(day, {}),
        )
        for day in sorted(price_rows.keys(), key=GLOBAL_DAY_ORDER.index)
    }


def load_trader_class(trader_path: Path):
    trader_path = trader_path.resolve()
    module_name = "round2_trader_" + hashlib.sha1(
        f"{trader_path}:{time.time_ns()}".encode("utf-8")
    ).hexdigest()
    spec = importlib.util.spec_from_file_location(module_name, trader_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import trader from {trader_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "Trader"):
        raise AttributeError(f"{trader_path} does not define Trader")
    return module.Trader


def apply_overrides(trader_cls, overrides: Dict[str, Any]) -> None:
    for key, value in overrides.items():
        if not hasattr(trader_cls, key):
            raise AttributeError(f"{trader_cls.__name__} has no attribute {key}")
        current = getattr(trader_cls, key)
        setattr(trader_cls, key, _coerce_like(current, value))


def collect_parameters(trader_cls) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for key in TRACKED_PARAMS:
        if hasattr(trader_cls, key):
            params[key] = getattr(trader_cls, key)
    return params


def run_replay(
    trader_path: str,
    *,
    data_dir: Optional[str] = None,
    days: Optional[Sequence[int]] = None,
    overrides: Optional[Dict[str, Any]] = None,
    market_log: Optional[str] = None,
) -> ReplaySummary:
    trader_file = Path(trader_path).resolve()
    if market_log is None:
        data_root = resolve_data_dir(data_dir)
        replay_days = normalize_days(days)
        loaded_days = {
            day: (*load_price_rows(data_root, day), load_trade_rows(data_root, day))
            for day in replay_days
        }
    else:
        loaded_days = load_log_market(Path(market_log).resolve())
        available_days = sorted(loaded_days.keys(), key=GLOBAL_DAY_ORDER.index)
        replay_days = normalize_days(days) if days else available_days
        loaded_days = {day: loaded_days[day] for day in replay_days}

    trader_cls = load_trader_class(trader_file)
    if overrides:
        apply_overrides(trader_cls, overrides)

    trader = trader_cls()
    trader_data = _seed_trader_data_for_day("", GLOBAL_DAY_ORDER.index(replay_days[0]))

    day_summaries: List[DayReplaySummary] = []
    aggregate_by_product = _empty_product_stats()

    previous_day_index: Optional[int] = None
    for day in replay_days:
        day_index = GLOBAL_DAY_ORDER.index(day)
        if previous_day_index is not None and day_index != previous_day_index + 1:
            trader_data = _seed_trader_data_for_day(trader_data, day_index)

        day_summary, trader_data = _run_day(
            trader=trader,
            trader_data=trader_data,
            day=day,
            timestamps=loaded_days[day][0],
            price_rows=loaded_days[day][1],
            market_trades_by_timestamp=loaded_days[day][2],
        )
        day_summaries.append(day_summary)
        _accumulate_day_stats(aggregate_by_product, day_summary.per_product)
        previous_day_index = day_index

    aggregate_pnl = round(sum(item.total_pnl for item in day_summaries), 6)
    aggregate_summary = {
        product: ProductReplaySummary(
            pnl=round(stats["pnl"], 6),
            position=int(stats["position"]),
            cash=round(stats["cash"], 6),
            final_mid=round(stats["final_mid"], 6),
            buy_trades=int(stats["buy_trades"]),
            sell_trades=int(stats["sell_trades"]),
            buy_qty=int(stats["buy_qty"]),
            sell_qty=int(stats["sell_qty"]),
        )
        for product, stats in aggregate_by_product.items()
    }

    return ReplaySummary(
        trader_path=str(trader_file),
        bid=int(trader.bid()),
        days=replay_days,
        aggregate_pnl=aggregate_pnl,
        per_day=day_summaries,
        per_product=aggregate_summary,
        parameters=collect_parameters(trader_cls),
    )


def parse_override_items(items: Iterable[str]) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    for item in items:
        key, value = item.split("=", 1)
        overrides[key] = _parse_scalar(value)
    return overrides


def format_summary(summary: ReplaySummary) -> str:
    lines = [
        f"Trader: {summary.trader_path}",
        f"Bid: {summary.bid}",
        f"Days: {', '.join(str(day) for day in summary.days)}",
        f"Aggregate PnL: {summary.aggregate_pnl:.6f}",
        "",
        "Per-day PnL:",
    ]
    for item in summary.per_day:
        lines.append(f"  day {item.day}: {item.total_pnl:.6f}")

    lines.extend(["", "Per-product totals:"])
    for product, product_summary in summary.per_product.items():
        lines.append(
            "  "
            + f"{product}: pnl={product_summary.pnl:.6f}, "
            + f"buy_trades={product_summary.buy_trades}, sell_trades={product_summary.sell_trades}, "
            + f"buy_qty={product_summary.buy_qty}, sell_qty={product_summary.sell_qty}"
        )

    lines.extend(["", "Parameters:"])
    for key in sorted(summary.parameters):
        lines.append(f"  {key}={summary.parameters[key]}")

    return "\n".join(lines)


def _run_day(
    *,
    trader,
    trader_data: str,
    day: int,
    timestamps: List[int],
    price_rows: Dict[int, Dict[str, PriceRow]],
    market_trades_by_timestamp: Dict[int, Dict[str, List[Trade]]],
) -> Tuple[DayReplaySummary, str]:
    positions = {product: 0 for product in PRODUCTS}
    cash = {product: 0.0 for product in PRODUCTS}
    stats = _empty_product_stats()
    pending_orders: List[PendingOrder] = []
    last_mid = {product: 0.0 for product in PRODUCTS}

    for timestamp in timestamps:
        raw_books = {
            product: _raw_book_from_row(price_rows[timestamp][product])
            for product in PRODUCTS
        }
        own_trades = {product: [] for product in PRODUCTS}

        for pending in pending_orders:
            book = raw_books[pending.product]
            fills = _execute_pending_touch_fill(
                quantity=pending.quantity,
                limit_price=pending.price,
                raw_book=book,
            )
            _apply_fills(
                product=pending.product,
                fills=fills,
                timestamp=timestamp,
                positions=positions,
                cash=cash,
                stats=stats,
                trade_bucket=own_trades[pending.product],
            )
        pending_orders = []

        order_depths = {
            product: _order_depth_from_raw_book(raw_books[product]) for product in PRODUCTS
        }
        state = TradingState(
            traderData=trader_data,
            timestamp=timestamp,
            listings=_build_listings(),
            order_depths=order_depths,
            own_trades=own_trades,
            market_trades=_market_trades_for_timestamp(market_trades_by_timestamp, timestamp),
            position=dict(positions),
            observations=Observation(),
        )

        raw_result = trader.run(state)
        if not isinstance(raw_result, tuple) or len(raw_result) != 3:
            raise RuntimeError(f"Unexpected run() return from {type(trader).__name__}: {raw_result!r}")
        orders_by_product, _, trader_data = raw_result

        for product, order_list in orders_by_product.items():
            book = raw_books[product]
            for order in order_list:
                if order.quantity == 0:
                    continue
                leftover, fills = _execute_limit_order(
                    quantity=order.quantity,
                    limit_price=order.price,
                    raw_book=book,
                )
                _apply_fills(
                    product=product,
                    fills=fills,
                    timestamp=timestamp,
                    positions=positions,
                    cash=cash,
                    stats=stats,
                    trade_bucket=None,
                )
                if leftover != 0:
                    pending_orders.append(PendingOrder(product=product, price=order.price, quantity=leftover))

        for product in PRODUCTS:
            last_mid[product] = price_rows[timestamp][product].mid_price

    per_product = {
        product: ProductReplaySummary(
            pnl=round(cash[product] + positions[product] * last_mid[product], 6),
            position=positions[product],
            cash=round(cash[product], 6),
            final_mid=round(last_mid[product], 6),
            buy_trades=stats[product]["buy_trades"],
            sell_trades=stats[product]["sell_trades"],
            buy_qty=stats[product]["buy_qty"],
            sell_qty=stats[product]["sell_qty"],
        )
        for product in PRODUCTS
    }
    total_pnl = round(sum(item.pnl for item in per_product.values()), 6)
    return DayReplaySummary(day=day, total_pnl=total_pnl, per_product=per_product), trader_data


def _execute_limit_order(
    *,
    quantity: int,
    limit_price: int,
    raw_book: Dict[str, Dict[int, int]],
) -> Tuple[int, List[Tuple[str, int, int]]]:
    remaining = abs(quantity)
    side = "buy" if quantity > 0 else "sell"
    fills: List[Tuple[str, int, int]] = []

    if side == "buy":
        for price in sorted(raw_book["asks"].keys()):
            if remaining <= 0 or price > limit_price:
                break
            available = raw_book["asks"][price]
            fill_qty = min(remaining, available)
            if fill_qty <= 0:
                continue
            fills.append((side, price, fill_qty))
            raw_book["asks"][price] -= fill_qty
            if raw_book["asks"][price] <= 0:
                del raw_book["asks"][price]
            remaining -= fill_qty
    else:
        for price in sorted(raw_book["bids"].keys(), reverse=True):
            if remaining <= 0 or price < limit_price:
                break
            available = raw_book["bids"][price]
            fill_qty = min(remaining, available)
            if fill_qty <= 0:
                continue
            fills.append((side, price, fill_qty))
            raw_book["bids"][price] -= fill_qty
            if raw_book["bids"][price] <= 0:
                del raw_book["bids"][price]
            remaining -= fill_qty

    leftover = remaining if quantity > 0 else -remaining
    return leftover, fills


def _apply_fills(
    *,
    product: str,
    fills: List[Tuple[str, int, int]],
    timestamp: int,
    positions: Dict[str, int],
    cash: Dict[str, float],
    stats: Dict[str, Dict[str, float]],
    trade_bucket: Optional[List[Trade]],
) -> None:
    for side, price, quantity in fills:
        if side == "buy":
            positions[product] += quantity
            cash[product] -= price * quantity
            stats[product]["buy_trades"] += 1
            stats[product]["buy_qty"] += quantity
            trade = Trade(
                symbol=product,
                price=price,
                quantity=quantity,
                buyer="SUBMISSION",
                seller=None,
                timestamp=timestamp,
            )
        else:
            positions[product] -= quantity
            cash[product] += price * quantity
            stats[product]["sell_trades"] += 1
            stats[product]["sell_qty"] += quantity
            trade = Trade(
                symbol=product,
                price=price,
                quantity=quantity,
                buyer=None,
                seller="SUBMISSION",
                timestamp=timestamp,
            )
        if trade_bucket is not None:
            trade_bucket.append(trade)


def _raw_book_from_row(row: PriceRow) -> Dict[str, Dict[int, int]]:
    return {
        "bids": {price: volume for price, volume in row.bids},
        "asks": {price: volume for price, volume in row.asks},
    }


def _order_depth_from_raw_book(raw_book: Dict[str, Dict[int, int]]) -> OrderDepth:
    order_depth = OrderDepth()
    order_depth.buy_orders = dict(raw_book["bids"])
    order_depth.sell_orders = {price: -volume for price, volume in raw_book["asks"].items()}
    return order_depth


def _market_trades_for_timestamp(
    market_trades_by_timestamp: Dict[int, Dict[str, List[Trade]]],
    timestamp: int,
) -> Dict[str, List[Trade]]:
    current = market_trades_by_timestamp.get(timestamp, {})
    return {product: list(current.get(product, [])) for product in PRODUCTS}


def _build_listings() -> Dict[str, Listing]:
    return {
        product: Listing(symbol=product, product=product, denomination=DENOMINATION)
        for product in PRODUCTS
    }


def _parse_levels(raw: Dict[str, str], prefix: str) -> List[Tuple[int, int]]:
    levels: List[Tuple[int, int]] = []
    for level in (1, 2, 3):
        price_raw = raw.get(f"{prefix}_price_{level}", "")
        volume_raw = raw.get(f"{prefix}_volume_{level}", "")
        if not price_raw or not volume_raw:
            continue
        levels.append((int(round(float(price_raw))), int(volume_raw)))
    return levels


def _load_price_rows_from_activities_log(text: str) -> Dict[int, Dict[int, Dict[str, PriceRow]]]:
    by_day: Dict[int, Dict[int, Dict[str, PriceRow]]] = {}
    handle = io.StringIO(text.strip())
    reader = csv.DictReader(handle, delimiter=";")
    for raw in reader:
        day = int(raw["day"])
        timestamp = int(raw["timestamp"])
        product = raw["product"]
        row = PriceRow(
            day=day,
            timestamp=timestamp,
            product=product,
            bids=_parse_levels(raw, "bid"),
            asks=_parse_levels(raw, "ask"),
            mid_price=float(raw["mid_price"]),
        )
        by_day.setdefault(day, {}).setdefault(timestamp, {})[product] = row
    return by_day


def _load_trade_rows_from_history(history: List[Dict[str, Any]]) -> Dict[int, Dict[int, Dict[str, List[Trade]]]]:
    by_day: Dict[int, Dict[int, Dict[str, List[Trade]]]] = {}
    if not history:
        return by_day

    # Active log payloads in this workspace contain one day per file.
    inferred_day = 1
    for raw in history:
        timestamp = int(raw["timestamp"])
        product = raw["symbol"]
        trade = Trade(
            symbol=product,
            price=int(round(float(raw["price"]))),
            quantity=int(raw["quantity"]),
            buyer=raw.get("buyer") or None,
            seller=raw.get("seller") or None,
            timestamp=timestamp,
        )
        by_day.setdefault(inferred_day, {}).setdefault(timestamp, {}).setdefault(product, []).append(trade)
    return by_day


def _parse_scalar(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _coerce_like(current: Any, value: Any) -> Any:
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(value)
    if isinstance(current, float):
        return float(value)
    return value


def _execute_pending_touch_fill(
    *,
    quantity: int,
    limit_price: int,
    raw_book: Dict[str, Dict[int, int]],
) -> List[Tuple[str, int, int]]:
    remaining = abs(quantity)
    if remaining <= 0:
        return []

    side = "buy" if quantity > 0 else "sell"
    if side == "buy":
        if not raw_book["asks"]:
            return []
        best_ask = min(raw_book["asks"].keys())
        if best_ask > limit_price:
            return []
        fill_qty = min(remaining, raw_book["asks"][best_ask])
        if fill_qty <= 0:
            return []
        raw_book["asks"][best_ask] -= fill_qty
        if raw_book["asks"][best_ask] <= 0:
            del raw_book["asks"][best_ask]
        return [(side, best_ask, fill_qty)]

    if not raw_book["bids"]:
        return []
    best_bid = max(raw_book["bids"].keys())
    if best_bid < limit_price:
        return []
    fill_qty = min(remaining, raw_book["bids"][best_bid])
    if fill_qty <= 0:
        return []
    raw_book["bids"][best_bid] -= fill_qty
    if raw_book["bids"][best_bid] <= 0:
        del raw_book["bids"][best_bid]
    return [(side, best_bid, fill_qty)]


def _seed_trader_data_for_day(trader_data: str, day_index: int) -> str:
    if day_index <= 0:
        return trader_data
    try:
        data = json.loads(trader_data) if trader_data else {}
    except Exception:
        data = {}
    data["day"] = day_index - 1
    data["last_ts"] = 99_900
    return json.dumps(data)


def _empty_product_stats() -> Dict[str, Dict[str, float]]:
    return {
        product: {
            "pnl": 0.0,
            "position": 0,
            "cash": 0.0,
            "final_mid": 0.0,
            "buy_trades": 0,
            "sell_trades": 0,
            "buy_qty": 0,
            "sell_qty": 0,
        }
        for product in PRODUCTS
    }


def _accumulate_day_stats(
    aggregate: Dict[str, Dict[str, float]],
    day_stats: Dict[str, ProductReplaySummary],
) -> None:
    for product, summary in day_stats.items():
        aggregate[product]["pnl"] += summary.pnl
        aggregate[product]["position"] = summary.position
        aggregate[product]["cash"] += summary.cash
        aggregate[product]["final_mid"] = summary.final_mid
        aggregate[product]["buy_trades"] += summary.buy_trades
        aggregate[product]["sell_trades"] += summary.sell_trades
        aggregate[product]["buy_qty"] += summary.buy_qty
        aggregate[product]["sell_qty"] += summary.sell_qty
