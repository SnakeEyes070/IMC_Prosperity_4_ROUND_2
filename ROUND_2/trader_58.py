# trader.py — IMC Prosperity 4, Round 2 (Combined Safe Optimizations)
import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order

class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    LIMIT = 80

    # --- Pepper (Optimized) ---
    PEPPER_SLOPE = 0.001
    PEPPER_BUY_TOL = 6                # Slightly tighter than 8, avoids overpaying
    ENDGAME_START = 90_000            # Earlier: sells into thicker bids
    SCALP_RESERVE = 6                 # Keep 6 units free for pullback scalps

    SCALP_DIP = 8
    SCALP_EXIT = 5
    SCALP_SIZE = 10

    # --- Osmium (Optimized) ---
    OSM_FAIR_FALLBACK = 10_000
    OSM_EMA_ALPHA = 0.02
    OSM_PASSIVE_BID_OFFSET = 6        # Fixed 6 ticks below mid
    OSM_PASSIVE_ASK_OFFSET = 6        # Fixed 6 ticks above mid
    OSM_PASSIVE_SIZE = 19
    OSM_MR_THRESH = 8
    OSM_MR_MAX_QTY = 24
    OSM_SKEW_FACTOR = 0.06
    OSM_AGGRESSIVE_BUY_THRESH = 10_000
    OSM_AGGRESSIVE_SELL_THRESH = 10_003

    # NEW: Laddered aggressive sizes
    OSM_AGG_SIZE_L1 = 12              # Reduced from 24
    OSM_AGG_SIZE_L2 = 8               # Second level (deeper)
    OSM_AGG_DEEPER = 2                # Extra ticks for L2 trigger

    # NEW: Inventory skew (applied on top of passive offset)
    OSM_SKEW_PER_UNIT = 0.1

    # --- Timing ---
    ROUND_DAYS = 3
    MAX_TS = 99_900
    NEW_DAY_THRESH = 10_000

    def bid(self) -> int:
        return 6_500

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}
        ts = state.timestamp
        prev_ts = data.get("last_ts", -1)
        day = data.get("day", 0)

        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day
            data.pop("pepper_anchor", None)
            data.pop("pepper_recent_high", None)
            data.pop("pepper_scalp_entry", None)

        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_fair = data["pepper_anchor"] + self.PEPPER_SLOPE * ts
        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame = is_last_day and (ts >= self.ENDGAME_START)

        orders: Dict[str, List[Order]] = {}

        if self.PEPPER in state.order_depths:
            pepper_ords = self._pepper_orders(state, ts, pepper_fair, is_endgame, data)
            if pepper_ords:
                orders[self.PEPPER] = pepper_ords

        if self.OSMIUM in state.order_depths:
            osmium_ords = self._osmium_orders(state, data)
            if osmium_ords:
                orders[self.OSMIUM] = osmium_ords

        data["last_ts"] = ts
        conversions = 0
        trader_data = json.dumps(data)
        return orders, conversions, trader_data

    # ---------- Pepper ----------
    def _pepper_anchor(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    def _pepper_orders(self, state: TradingState, ts: int, fair: float,
                       is_endgame: bool, data: dict) -> List[Order]:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        pos = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []
        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else fair

        if not is_endgame:
            # Core accumulation (respect scalp reserve)
            main_cap = self.LIMIT - self.SCALP_RESERVE - pos
            if main_cap > 0 and od.sell_orders:
                for ask_px in sorted(od.sell_orders.keys()):
                    if main_cap <= 0:
                        break
                    if ask_px <= fair + self.PEPPER_BUY_TOL:
                        vol = min(main_cap, -od.sell_orders[ask_px])
                        if vol > 0:
                            orders.append(Order(self.PEPPER, ask_px, vol))
                            main_cap -= vol
                if main_cap > 0 and od.sell_orders:
                    best_ask = min(od.sell_orders.keys())
                    passive_bid = best_ask - 1
                    orders.append(Order(self.PEPPER, passive_bid, main_cap))

            # Scalp (proven edge)
            if "pepper_recent_high" not in data:
                data["pepper_recent_high"] = mid
            else:
                data["pepper_recent_high"] = max(data["pepper_recent_high"], mid)
            scalp_entry = data.get("pepper_scalp_entry")
            if scalp_entry is None and best_ask and best_ask < data["pepper_recent_high"] - self.SCALP_DIP:
                remaining = self.LIMIT - pos
                if remaining > 0:
                    vol = min(self.SCALP_SIZE, remaining, -od.sell_orders.get(best_ask, 0))
                    if vol > 0:
                        orders.append(Order(self.PEPPER, best_ask, vol))
                        data["pepper_scalp_entry"] = best_ask
            if scalp_entry is not None and best_bid and best_bid > scalp_entry + self.SCALP_EXIT:
                vol = min(self.SCALP_SIZE, od.buy_orders.get(best_bid, 0))
                if vol > 0:
                    orders.append(Order(self.PEPPER, best_bid, -vol))
                    data["pepper_scalp_entry"] = None
                    data["pepper_recent_high"] = mid
        else:
            # Endgame unwind (unchanged pacing)
            if pos > 0 and od.buy_orders:
                ticks_left = max(1, (self.MAX_TS - ts) // 100 + 1)
                per_tick = math.ceil(pos / ticks_left)
                to_sell = min(pos, int(per_tick * 2.5))
                remaining = to_sell
                for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                    if remaining <= 0:
                        break
                    vol = min(remaining, od.buy_orders[bid_px])
                    if vol > 0:
                        orders.append(Order(self.PEPPER, bid_px, -vol))
                        remaining -= vol
                if ts >= self.MAX_TS - 100 and pos > 0:
                    leftover = pos - (to_sell - remaining)
                    if leftover > 0 and od.buy_orders:
                        best_bid = max(od.buy_orders.keys())
                        orders.append(Order(self.PEPPER, best_bid, -leftover))
        return orders

    # ---------- Osmium ----------
    def _osmium_orders(self, state: TradingState, data: dict) -> List[Order]:
        od = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []
        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        if best_bid is None or best_ask is None:
            return orders

        raw_mid = (best_bid + best_ask) / 2.0
        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_FALLBACK))
        ema = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = round(ema)

        buy_cap = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # --- Aggressive Mean Reversion (Laddered) ---
        if best_ask is not None and best_ask <= self.OSM_AGGRESSIVE_BUY_THRESH:
            # Level 1
            if buy_cap > 0:
                vol = min(self.OSM_AGG_SIZE_L1, buy_cap, self._ask_vol(od, best_ask))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, best_ask, vol))
                    buy_cap -= vol
            # Level 2 (deeper)
            if best_ask <= self.OSM_AGGRESSIVE_BUY_THRESH - self.OSM_AGG_DEEPER and buy_cap > 0:
                vol = min(self.OSM_AGG_SIZE_L2, buy_cap, self._ask_vol(od, best_ask))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, best_ask, vol))
                    buy_cap -= vol

        if best_bid is not None and best_bid >= self.OSM_AGGRESSIVE_SELL_THRESH:
            # Level 1
            if sell_cap > 0:
                vol = min(self.OSM_AGG_SIZE_L1, sell_cap, self._bid_vol(od, best_bid))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, best_bid, -vol))
                    sell_cap -= vol
            # Level 2 (deeper)
            if best_bid >= self.OSM_AGGRESSIVE_SELL_THRESH + self.OSM_AGG_DEEPER and sell_cap > 0:
                vol = min(self.OSM_AGG_SIZE_L2, sell_cap, self._bid_vol(od, best_bid))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, best_bid, -vol))
                    sell_cap -= vol

        # --- Mean Reversion Safety Net ---
        if od.sell_orders and buy_cap > 0:
            for ask_px in sorted(od.sell_orders.keys()):
                if ask_px > fair - self.OSM_MR_THRESH:
                    break
                vol = min(buy_cap, self._ask_vol(od, ask_px), self.OSM_MR_MAX_QTY)
                if vol > 0:
                    orders.append(Order(self.OSMIUM, ask_px, vol))
                    buy_cap -= vol
        if od.buy_orders and sell_cap > 0:
            for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                if bid_px < fair + self.OSM_MR_THRESH:
                    break
                vol = min(sell_cap, self._bid_vol(od, bid_px), self.OSM_MR_MAX_QTY)
                if vol > 0:
                    orders.append(Order(self.OSMIUM, bid_px, -vol))
                    sell_cap -= vol

        # --- Passive Market Making with Inventory Skew ---
        skew = int(round(self.OSM_SKEW_PER_UNIT * pos))
        if buy_cap > 0:
            bid_px = fair - self.OSM_PASSIVE_BID_OFFSET - skew
            qty = min(self.OSM_PASSIVE_SIZE, buy_cap)
            orders.append(Order(self.OSMIUM, bid_px, qty))
            buy_cap -= qty
        if sell_cap > 0:
            ask_px = fair + self.OSM_PASSIVE_ASK_OFFSET - skew
            qty = min(self.OSM_PASSIVE_SIZE, sell_cap)
            orders.append(Order(self.OSMIUM, ask_px, -qty))
            sell_cap -= qty

        return orders

    def _ask_vol(self, od: OrderDepth, price: int) -> int:
        return abs(od.sell_orders.get(price, 0))

    def _bid_vol(self, od: OrderDepth, price: int) -> int:
        return od.buy_orders.get(price, 0)