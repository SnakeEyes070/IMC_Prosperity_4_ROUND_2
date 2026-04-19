import json
import math
from typing import Dict, List, Optional, Tuple

from datamodel import Order, OrderDepth, TradingState


class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"

    POSITION_LIMIT = 80

    ROUND_DAYS = 3
    MAX_TIMESTAMP = 99_900
    DAY_RESET_THRESHOLD = 10_000

    PEPPER_ANCHOR_FALLBACK = 12_000.0
    PEPPER_BASE_SLOPE = 0.001
    PEPPER_MIN_SLOPE = 0.0005
    PEPPER_MAX_SLOPE = 0.0200
    PEPPER_SLOPE_ALPHA = 0.08
    PEPPER_BUY_TOLERANCE = 15
    PEPPER_TRAIL_TOLERANCE = 14
    PEPPER_ENDGAME_START = 98_800
    PEPPER_FORCE_FLATTEN_TS = 99_600
    PEPPER_UNWIND_POST_EDGE = 1
    PEPPER_UNWIND_TAKE_TOLERANCE = 1

    OSMIUM_LONG_RUN_FAIR = 10_000.0
    OSMIUM_FAIR_FALLBACK = 10_000.0
    OSMIUM_EMA_ALPHA = 0.020
    OSMIUM_MICRO_WEIGHT = 0.60
    OSMIUM_EMA_WEIGHT = 0.25
    OSMIUM_LONG_RUN_WEIGHT = 0.15
    OSMIUM_IMBALANCE_SHIFT = 1.8
    OSMIUM_MEAN_REVERT_THRESHOLD = 6.0
    OSMIUM_AGGRESSIVE_CLIP = 28
    OSMIUM_IMBALANCE_CUTOFF = 0.40
    OSMIUM_SKEW_PER_UNIT = 0.10
    OSMIUM_MAX_SKEW = 8.0
    OSMIUM_LEVEL_1_SIZE = 24
    OSMIUM_LEVEL_2_SIZE = 18
    OSMIUM_LEVEL_3_SIZE = 12
    OSMIUM_LEVEL_4_SIZE = 8
    OSMIUM_LEVEL_1_OFFSET = 2
    OSMIUM_LEVEL_2_OFFSET = 4
    OSMIUM_LEVEL_3_OFFSET = 6
    OSMIUM_LEVEL_4_OFFSET = 8
    OSMIUM_MIN_PASSIVE_SCALE = 0.20
    OSMIUM_MAX_PASSIVE_SCALE = 1.80

    def bid(self) -> int:
        # Slightly above the expected median: good odds of getting the extra quotes
        # without paying as much as a max-aggression blind bid.
        return 5_200

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        data = self._load_data(state.traderData)
        ts = state.timestamp
        prev_ts = int(data.get("last_ts", -1))
        day = int(data.get("day", 0))

        if prev_ts > self.DAY_RESET_THRESHOLD and ts < self.DAY_RESET_THRESHOLD:
            day += 1
            data.pop("pepper_anchor", None)
            data.pop("pepper_slope", None)

        data["day"] = day

        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_mid = self._mid_from_depth(state.order_depths.get(self.PEPPER, OrderDepth()))
        pepper_slope = float(data.get("pepper_slope", self.PEPPER_BASE_SLOPE))
        if pepper_mid is not None and ts > 0:
            sampled_slope = (pepper_mid - float(data["pepper_anchor"])) / ts
            sampled_slope = self._clamp(
                self.PEPPER_MIN_SLOPE,
                self.PEPPER_MAX_SLOPE,
                sampled_slope,
            )
            pepper_slope += self.PEPPER_SLOPE_ALPHA * (sampled_slope - pepper_slope)
        data["pepper_slope"] = pepper_slope

        pepper_fair = float(data["pepper_anchor"]) + pepper_slope * ts
        is_endgame = day >= self.ROUND_DAYS - 1 and ts >= self.PEPPER_ENDGAME_START

        result: Dict[str, List[Order]] = {}

        pepper_orders = self._pepper_orders(state, ts, pepper_fair, is_endgame)
        if pepper_orders:
            result[self.PEPPER] = pepper_orders

        osmium_orders = self._osmium_orders(state, data)
        if osmium_orders:
            result[self.OSMIUM] = osmium_orders

        data["last_ts"] = ts
        trader_data = json.dumps(data, separators=(",", ":"))
        return result, 0, trader_data

    def _pepper_anchor(self, state: TradingState) -> float:
        order_depth = state.order_depths.get(self.PEPPER, OrderDepth())
        if order_depth.sell_orders:
            return float(min(order_depth.sell_orders))
        if order_depth.buy_orders:
            return float(max(order_depth.buy_orders))
        return self.PEPPER_ANCHOR_FALLBACK

    def _pepper_orders(
        self, state: TradingState, ts: int, fair: float, is_endgame: bool
    ) -> List[Order]:
        order_depth = state.order_depths.get(self.PEPPER, OrderDepth())
        position = int(state.position.get(self.PEPPER, 0))
        orders: List[Order] = []

        if not is_endgame:
            buy_room = self.POSITION_LIMIT - position
            if buy_room <= 0:
                return orders

            max_take_price = int(math.floor(fair + self.PEPPER_BUY_TOLERANCE))
            for ask_price in sorted(order_depth.sell_orders):
                if buy_room <= 0 or ask_price > max_take_price:
                    break
                ask_volume = -order_depth.sell_orders[ask_price]
                if ask_volume <= 0:
                    continue
                take_qty = min(buy_room, ask_volume)
                orders.append(Order(self.PEPPER, ask_price, take_qty))
                buy_room -= take_qty

            if buy_room > 0:
                best_ask = min(order_depth.sell_orders) if order_depth.sell_orders else None
                trailing_bid = int(math.floor(fair + self.PEPPER_TRAIL_TOLERANCE))
                if best_ask is not None:
                    trailing_bid = min(trailing_bid, best_ask)
                trailing_bid = max(1, trailing_bid)
                orders.append(Order(self.PEPPER, trailing_bid, buy_room))

            return self._compress_orders(self.PEPPER, orders)

        if position <= 0:
            return orders

        total_exit_steps = ((self.MAX_TIMESTAMP - self.PEPPER_ENDGAME_START) // 100) + 1
        steps_left = max(1, ((self.MAX_TIMESTAMP - ts) // 100) + 1)
        desired_position = max(
            0, math.ceil(self.POSITION_LIMIT * (steps_left - 1) / total_exit_steps)
        )
        sell_target = max(0, position - desired_position)
        if ts >= self.PEPPER_FORCE_FLATTEN_TS:
            sell_target = position

        if sell_target <= 0:
            return orders

        best_bid = max(order_depth.buy_orders) if order_depth.buy_orders else None
        best_ask = min(order_depth.sell_orders) if order_depth.sell_orders else None
        live_mid = self._mid_from_depth(order_depth)
        fair_ref = fair if live_mid is None else max(fair, live_mid)

        remaining = sell_target
        take_floor = int(math.floor(fair_ref - self.PEPPER_UNWIND_TAKE_TOLERANCE))
        for bid_price in sorted(order_depth.buy_orders, reverse=True):
            if remaining <= 0:
                break
            if ts < self.PEPPER_FORCE_FLATTEN_TS and bid_price < take_floor:
                break
            bid_volume = order_depth.buy_orders[bid_price]
            if bid_volume <= 0:
                continue
            hit_qty = min(remaining, bid_volume)
            orders.append(Order(self.PEPPER, bid_price, -hit_qty))
            remaining -= hit_qty

        if remaining > 0:
            if ts >= self.PEPPER_FORCE_FLATTEN_TS and best_bid is not None:
                orders.append(Order(self.PEPPER, best_bid, -remaining))
            else:
                post_price = int(math.ceil(fair_ref + self.PEPPER_UNWIND_POST_EDGE))
                if best_bid is not None:
                    post_price = max(post_price, best_bid + 1)
                if best_ask is not None:
                    post_price = min(post_price, best_ask)
                orders.append(Order(self.PEPPER, post_price, -remaining))

        return self._compress_orders(self.PEPPER, orders)

    def _osmium_orders(self, state: TradingState, data: Dict[str, object]) -> List[Order]:
        order_depth = state.order_depths.get(self.OSMIUM, OrderDepth())
        position = int(state.position.get(self.OSMIUM, 0))
        orders: List[Order] = []

        best_bid = max(order_depth.buy_orders) if order_depth.buy_orders else None
        best_ask = min(order_depth.sell_orders) if order_depth.sell_orders else None

        micro_price, current_spread, imbalance = self._osmium_microstructure(order_depth)
        previous_ema = float(data.get("osm_ema", self.OSMIUM_FAIR_FALLBACK))
        ema = previous_ema + self.OSMIUM_EMA_ALPHA * (micro_price - previous_ema)
        data["osm_ema"] = ema

        fair = (
            self.OSMIUM_MICRO_WEIGHT * micro_price
            + self.OSMIUM_EMA_WEIGHT * ema
            + self.OSMIUM_LONG_RUN_WEIGHT * self.OSMIUM_LONG_RUN_FAIR
            + imbalance * self.OSMIUM_IMBALANCE_SHIFT
        )

        buy_room = self.POSITION_LIMIT - position
        sell_room = self.POSITION_LIMIT + position
        inventory_ratio = position / float(self.POSITION_LIMIT)

        buy_buffer = (
            self.OSMIUM_MEAN_REVERT_THRESHOLD
            + max(0.0, inventory_ratio) * 2.0
            - max(0.0, -inventory_ratio) * 2.0
        )
        sell_buffer = (
            self.OSMIUM_MEAN_REVERT_THRESHOLD
            + max(0.0, -inventory_ratio) * 2.0
            - max(0.0, inventory_ratio) * 2.0
        )

        buy_trigger = fair - buy_buffer
        for ask_price in sorted(order_depth.sell_orders):
            if buy_room <= 0 or ask_price > buy_trigger:
                break
            ask_volume = -order_depth.sell_orders[ask_price]
            if ask_volume <= 0:
                continue
            take_qty = min(buy_room, ask_volume, self.OSMIUM_AGGRESSIVE_CLIP)
            orders.append(Order(self.OSMIUM, ask_price, take_qty))
            buy_room -= take_qty

        sell_trigger = fair + sell_buffer
        for bid_price in sorted(order_depth.buy_orders, reverse=True):
            if sell_room <= 0 or bid_price < sell_trigger:
                break
            bid_volume = order_depth.buy_orders[bid_price]
            if bid_volume <= 0:
                continue
            hit_qty = min(sell_room, bid_volume, self.OSMIUM_AGGRESSIVE_CLIP)
            orders.append(Order(self.OSMIUM, bid_price, -hit_qty))
            sell_room -= hit_qty

        if best_bid is None or best_ask is None:
            return self._compress_orders(self.OSMIUM, orders)

        spread_factor = self._clamp(0.80, 1.50, current_spread / 16.0)
        levels = [
            (max(1, int(round(self.OSMIUM_LEVEL_1_OFFSET * spread_factor))), self.OSMIUM_LEVEL_1_SIZE),
            (max(2, int(round(self.OSMIUM_LEVEL_2_OFFSET * spread_factor))), self.OSMIUM_LEVEL_2_SIZE),
            (max(3, int(round(self.OSMIUM_LEVEL_3_OFFSET * spread_factor))), self.OSMIUM_LEVEL_3_SIZE),
            (max(4, int(round(self.OSMIUM_LEVEL_4_OFFSET * spread_factor))), self.OSMIUM_LEVEL_4_SIZE),
        ]

        inventory_skew = self._clamp(
            -self.OSMIUM_MAX_SKEW,
            self.OSMIUM_MAX_SKEW,
            position * self.OSMIUM_SKEW_PER_UNIT,
        )
        center = fair - inventory_skew + imbalance * 1.5

        buy_scale = self._clamp(
            self.OSMIUM_MIN_PASSIVE_SCALE,
            self.OSMIUM_MAX_PASSIVE_SCALE,
            1.0 - 1.15 * inventory_ratio,
        )
        sell_scale = self._clamp(
            self.OSMIUM_MIN_PASSIVE_SCALE,
            self.OSMIUM_MAX_PASSIVE_SCALE,
            1.0 + 1.15 * inventory_ratio,
        )

        if imbalance > self.OSMIUM_IMBALANCE_CUTOFF:
            buy_scale *= 0.40
            sell_scale *= 1.15
        elif imbalance < -self.OSMIUM_IMBALANCE_CUTOFF:
            buy_scale *= 1.15
            sell_scale *= 0.40

        if position >= 68:
            buy_scale = 0.0
        elif position <= -68:
            sell_scale = 0.0

        for offset, base_size in levels:
            if buy_room <= 0 and sell_room <= 0:
                break

            bid_price = int(math.floor(center - offset))
            ask_price = int(math.ceil(center + offset))
            bid_price = min(bid_price, best_ask - 1)
            ask_price = max(ask_price, best_bid + 1)

            if bid_price >= ask_price:
                bid_price = max(1, best_bid)
                ask_price = best_ask
                if bid_price >= ask_price:
                    bid_price = max(1, int(math.floor(center - 1)))
                    ask_price = int(math.ceil(center + 1))

            if buy_room > 0 and buy_scale > 0.0:
                bid_size = min(buy_room, max(1, int(round(base_size * buy_scale))))
                if bid_size > 0 and bid_price > 0:
                    orders.append(Order(self.OSMIUM, bid_price, bid_size))
                    buy_room -= bid_size

            if sell_room > 0 and sell_scale > 0.0:
                ask_size = min(sell_room, max(1, int(round(base_size * sell_scale))))
                if ask_size > 0:
                    orders.append(Order(self.OSMIUM, ask_price, -ask_size))
                    sell_room -= ask_size

        return self._compress_orders(self.OSMIUM, orders)

    def _osmium_microstructure(self, order_depth: OrderDepth) -> Tuple[float, float, float]:
        bid_levels = self._top_levels(order_depth.buy_orders, highest_first=True, limit=2)
        ask_levels = self._top_levels(order_depth.sell_orders, highest_first=False, limit=2)

        best_bid = bid_levels[0][0] if bid_levels else None
        best_ask = ask_levels[0][0] if ask_levels else None

        bid_volume = sum(volume for _, volume in bid_levels)
        ask_volume = sum(volume for _, volume in ask_levels)
        bid_ref = self._vwap(bid_levels) if bid_levels else None
        ask_ref = self._vwap(ask_levels) if ask_levels else None

        if bid_ref is not None and ask_ref is not None:
            total_top_volume = bid_volume + ask_volume
            if total_top_volume > 0:
                micro_price = (
                    ask_ref * bid_volume + bid_ref * ask_volume
                ) / total_top_volume
                imbalance = (bid_volume - ask_volume) / total_top_volume
            else:
                micro_price = (best_bid + best_ask) / 2.0
                imbalance = 0.0
            return micro_price, float(best_ask - best_bid), imbalance

        if best_bid is not None:
            return best_bid + 8.0, 16.0, 0.0
        if best_ask is not None:
            return best_ask - 8.0, 16.0, 0.0
        return self.OSMIUM_FAIR_FALLBACK, 16.0, 0.0

    def _top_levels(
        self, price_map: Dict[int, int], highest_first: bool, limit: int
    ) -> List[Tuple[int, int]]:
        if not price_map:
            return []

        levels: List[Tuple[int, int]] = []
        for price in sorted(price_map, reverse=highest_first):
            raw_volume = price_map[price]
            volume = raw_volume if highest_first else -raw_volume
            if volume <= 0:
                continue
            levels.append((price, volume))
            if len(levels) >= limit:
                break
        return levels

    def _vwap(self, levels: List[Tuple[int, int]]) -> float:
        total_volume = sum(volume for _, volume in levels)
        if total_volume <= 0:
            return 0.0
        return sum(price * volume for price, volume in levels) / total_volume

    def _mid_from_depth(self, order_depth: OrderDepth) -> Optional[float]:
        best_bid = max(order_depth.buy_orders) if order_depth.buy_orders else None
        best_ask = min(order_depth.sell_orders) if order_depth.sell_orders else None
        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / 2.0
        if best_bid is not None:
            return float(best_bid)
        if best_ask is not None:
            return float(best_ask)
        return None

    def _compress_orders(self, symbol: str, orders: List[Order]) -> List[Order]:
        if not orders:
            return orders

        by_price: Dict[int, int] = {}
        for order in orders:
            by_price[order.price] = by_price.get(order.price, 0) + order.quantity

        compressed: List[Order] = []
        for price, quantity in by_price.items():
            if quantity != 0:
                compressed.append(Order(symbol, price, quantity))

        compressed.sort(key=lambda order: (order.price, order.quantity))
        return compressed

    def _load_data(self, trader_data: str) -> Dict[str, object]:
        if not trader_data:
            return {}
        try:
            loaded = json.loads(trader_data)
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass
        return {}

    def _clamp(self, low: float, high: float, value: float) -> float:
        return max(low, min(high, value))