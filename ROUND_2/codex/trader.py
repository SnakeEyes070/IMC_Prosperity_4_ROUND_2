import json
import math
from typing import Dict, List, Optional, Tuple

from datamodel import Order, OrderDepth, TradingState


class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"

    LIMIT = 80

    ROUND_DAYS = 3
    MAX_TS = 99_900
    NEW_DAY_THRESH = 10_000

    # Pepper: preserve the proven trend-follow core and keep the end-of-round mark.
    PEPPER_SLOPE = 0.001
    PEPPER_BUY_TOL = 15
    ENDGAME_START = 1_000_000

    # Osmium: slightly tighter and larger inner quotes to monetize extra book access.
    OSM_FAIR_FALLBACK = 10_000.0
    OSM_EMA_ALPHA = 0.020
    OSM_MICRO_WEIGHT = 0.62
    OSM_MR_THRESH = 6
    OSM_MR_MAX_QTY = 28
    OSM_SKEW_FACTOR = 0.06
    OSM_IMBALANCE_THRESH = 0.34

    OSM_L1_SIZE = 20
    OSM_L2_SIZE = 16
    OSM_L3_SIZE = 12

    def bid(self) -> int:
        # Slightly cheaper than 5.5k while still targeting access in the top half.
        return 5_000

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data: Dict[str, object] = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts = state.timestamp
        prev_ts = int(data.get("last_ts", -1))
        day = int(data.get("day", 0))

        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day
            data.pop("pepper_anchor", None)

        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_fair = float(data["pepper_anchor"]) + self.PEPPER_SLOPE * ts
        is_last_day = day >= self.ROUND_DAYS - 1
        is_endgame = is_last_day and ts >= self.ENDGAME_START

        orders: Dict[str, List[Order]] = {}

        pepper_orders = self._pepper_orders(state, ts, pepper_fair, is_endgame)
        if pepper_orders:
            orders[self.PEPPER] = pepper_orders

        osmium_orders = self._osmium_orders(state, data)
        if osmium_orders:
            orders[self.OSMIUM] = osmium_orders

        data["last_ts"] = ts
        return orders, 0, json.dumps(data, separators=(",", ":"))

    def _pepper_anchor(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    def _pepper_orders(
        self, state: TradingState, ts: int, fair: float, is_endgame: bool
    ) -> List[Order]:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        pos = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []

        if not is_endgame:
            buy_cap = self.LIMIT - pos
            if buy_cap > 0 and od.sell_orders:
                for ask_px in sorted(od.sell_orders.keys()):
                    if buy_cap <= 0:
                        break
                    if ask_px <= fair + self.PEPPER_BUY_TOL:
                        vol = min(buy_cap, -od.sell_orders[ask_px])
                        if vol > 0:
                            orders.append(Order(self.PEPPER, ask_px, vol))
                            buy_cap -= vol
                if buy_cap > 0 and od.sell_orders:
                    best_ask = min(od.sell_orders.keys())
                    orders.append(Order(self.PEPPER, best_ask, buy_cap))
        else:
            if pos > 0 and od.buy_orders:
                ticks_left = max(1, (self.MAX_TS - ts) // 100 + 1)
                per_tick_sell = math.ceil(pos / ticks_left)
                to_sell = min(pos, per_tick_sell * 2)
                remaining = to_sell

                for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                    if remaining <= 0:
                        break
                    vol = min(remaining, od.buy_orders[bid_px])
                    if vol > 0:
                        orders.append(Order(self.PEPPER, bid_px, -vol))
                        remaining -= vol

                if ts >= self.MAX_TS - 100 and pos > 0:
                    leftover = pos - to_sell + remaining
                    if leftover > 0 and od.buy_orders:
                        best_bid = max(od.buy_orders.keys())
                        orders.append(Order(self.PEPPER, best_bid, -leftover))

        return self._compress_orders(self.PEPPER, orders)

    def _osmium_orders(self, state: TradingState, data: Dict[str, object]) -> List[Order]:
        od = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None

        micro_price, current_spread, imbalance = self._osmium_microprice(od)

        prev_ema = float(data.get("osm_ema", self.OSM_FAIR_FALLBACK))
        ema = prev_ema + self.OSM_EMA_ALPHA * (micro_price - prev_ema)
        data["osm_ema"] = ema
        fair = self.OSM_MICRO_WEIGHT * micro_price + (1.0 - self.OSM_MICRO_WEIGHT) * ema

        buy_cap = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # Aggressive mean reversion
        if od.sell_orders and buy_cap > 0:
            for ask_px in sorted(od.sell_orders.keys()):
                if ask_px > fair - self.OSM_MR_THRESH:
                    break
                vol = min(buy_cap, -od.sell_orders[ask_px], self.OSM_MR_MAX_QTY)
                if vol > 0:
                    orders.append(Order(self.OSMIUM, ask_px, vol))
                    buy_cap -= vol

        if od.buy_orders and sell_cap > 0:
            for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                if bid_px < fair + self.OSM_MR_THRESH:
                    break
                vol = min(sell_cap, od.buy_orders[bid_px], self.OSM_MR_MAX_QTY)
                if vol > 0:
                    orders.append(Order(self.OSMIUM, bid_px, -vol))
                    sell_cap -= vol

        if best_bid is None or best_ask is None:
            return self._compress_orders(self.OSMIUM, orders)

        skew = int(max(-6, min(6, pos * self.OSM_SKEW_FACTOR)))
        spread_factor = current_spread / 16.0
        dyn_l1 = max(3, min(5, round(4 * spread_factor)))
        dyn_l2 = max(5, min(9, round(7 * spread_factor)))
        dyn_l3 = max(9, min(13, round(11 * spread_factor)))

        levels = [
            (dyn_l1, self.OSM_L1_SIZE),
            (dyn_l2, self.OSM_L2_SIZE),
            (dyn_l3, self.OSM_L3_SIZE),
        ]

        long_bias = pos / self.LIMIT
        allow_bids = imbalance < self.OSM_IMBALANCE_THRESH
        allow_asks = imbalance > -self.OSM_IMBALANCE_THRESH

        for offset, base_size in levels:
            if buy_cap <= 0 and sell_cap <= 0:
                break

            bid_px = int(math.floor(fair - offset - skew))
            ask_px = int(math.ceil(fair + offset - skew))

            if bid_px >= ask_px:
                bid_px = int(math.floor(fair - 1))
                ask_px = int(math.ceil(fair + 1))

            buy_size = max(1, round(base_size * (1 - max(0, long_bias) * 0.6)))
            sell_size = max(1, round(base_size * (1 - max(0, -long_bias) * 0.6)))

            if buy_cap > 0 and allow_bids and bid_px > 0:
                vol = min(buy_size, buy_cap)
                orders.append(Order(self.OSMIUM, bid_px, vol))
                buy_cap -= vol

            if sell_cap > 0 and allow_asks:
                vol = min(sell_size, sell_cap)
                orders.append(Order(self.OSMIUM, ask_px, -vol))
                sell_cap -= vol

        return self._compress_orders(self.OSMIUM, orders)

    def _osmium_microprice(self, od: OrderDepth) -> Tuple[float, float, float]:
        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None

        top_bid_levels = []
        top_ask_levels = []

        if od.buy_orders:
            for px in sorted(od.buy_orders.keys(), reverse=True)[:2]:
                vol = od.buy_orders[px]
                if vol > 0:
                    top_bid_levels.append((px, vol))

        if od.sell_orders:
            for px in sorted(od.sell_orders.keys())[:2]:
                vol = -od.sell_orders[px]
                if vol > 0:
                    top_ask_levels.append((px, vol))

        if top_bid_levels and top_ask_levels:
            bid_vwap = self._vwap(top_bid_levels)
            ask_vwap = self._vwap(top_ask_levels)
            bid_vol = sum(vol for _, vol in top_bid_levels)
            ask_vol = sum(vol for _, vol in top_ask_levels)
            total_vol = bid_vol + ask_vol

            if total_vol > 0:
                micro = (ask_vwap * bid_vol + bid_vwap * ask_vol) / total_vol
                imbalance = (bid_vol - ask_vol) / total_vol
            else:
                micro = (best_bid + best_ask) / 2.0
                imbalance = 0.0

            return micro, float(best_ask - best_bid), imbalance

        if best_bid is not None:
            return best_bid + 8.0, 16.0, 0.0
        if best_ask is not None:
            return best_ask - 8.0, 16.0, 0.0
        return self.OSM_FAIR_FALLBACK, 16.0, 0.0

    def _vwap(self, levels: List[Tuple[int, int]]) -> float:
        total = sum(vol for _, vol in levels)
        if total <= 0:
            return 0.0
        return sum(px * vol for px, vol in levels) / total

    def _compress_orders(self, symbol: str, orders: List[Order]) -> List[Order]:
        if not orders:
            return orders

        by_price: Dict[int, int] = {}
        for order in orders:
            by_price[order.price] = by_price.get(order.price, 0) + order.quantity

        out: List[Order] = []
        for price, qty in by_price.items():
            if qty != 0:
                out.append(Order(symbol, price, qty))

        out.sort(key=lambda order: (order.price, order.quantity))
        return out
