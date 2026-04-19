# trader.py — IMC Prosperity 4, Round 2 (Snake_Eye's Observed Strategy)
# Pepper: Buy at open, sell near close.
# Osmium: Tight range market making (bid 9991-9993, ask 10007-10010).

import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order

class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    
    LIMIT = 80

    # ── PEPPER (Your Observation) ─────────────────────────────────────
    # "Pepper trends up. Buy at start, sell at end."
    PEPPER_SLOPE   = 0.001
    PEPPER_BUY_TOL = 15        # Wide enough to fill 80 units
    ENDGAME_START  = 94_500     # Start selling near the end

    # ── OSMIUM (Your Observation) ─────────────────────────────────────
    # "Osmium ranges. Bid 9991-9993, ask 10007-10010."
    # We'll use the middle of your range: bid 9992, ask 10008.
    OSM_BID_PRICE = 9_992
    OSM_ASK_PRICE = 10_008
    OSM_ORDER_SIZE = 25         # Trade 25 units at a time (safe within limit 80)

    # ── TIMING ────────────────────────────────────────────────────────
    ROUND_DAYS     = 3
    MAX_TS         = 99_900
    NEW_DAY_THRESH = 10_000

    # ──────────────────────────────────────────────────────────────────
    def bid(self) -> int:
        return 6_500

    # ──────────────────────────────────────────────────────────────────
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts      = state.timestamp
        prev_ts = data.get("last_ts", -1)
        day     = data.get("day", 0)

        # Day rollover
        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day
            data.pop("pepper_anchor", None)

        # Pepper anchor (first ask of the day)
        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_fair = data["pepper_anchor"] + self.PEPPER_SLOPE * ts

        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame  = is_last_day and (ts >= self.ENDGAME_START)

        orders: Dict[str, List[Order]] = {}

        # ── PEPPER: Buy at start, sell at end ─────────────────────────
        if self.PEPPER in state.order_depths:
            od = state.order_depths[self.PEPPER]
            pos = state.position.get(self.PEPPER, 0)
            orders[self.PEPPER] = []

            if not is_endgame:
                # Accumulation phase: buy everything available up to limit
                buy_cap = self.LIMIT - pos
                if buy_cap > 0 and od.sell_orders:
                    for ask_px in sorted(od.sell_orders.keys()):
                        if buy_cap <= 0:
                            break
                        if ask_px <= pepper_fair + self.PEPPER_BUY_TOL:
                            vol = min(buy_cap, -od.sell_orders[ask_px])
                            if vol > 0:
                                orders[self.PEPPER].append(Order(self.PEPPER, ask_px, vol))
                                buy_cap -= vol
            else:
                # Endgame: sell everything
                if pos > 0 and od.buy_orders:
                    best_bid = max(od.buy_orders.keys())
                    vol = min(pos, od.buy_orders[best_bid])
                    if vol > 0:
                        orders[self.PEPPER].append(Order(self.PEPPER, best_bid, -vol))

        # ── OSMIUM: Tight range market making ─────────────────────────
        if self.OSMIUM in state.order_depths:
            od = state.order_depths[self.OSMIUM]
            pos = state.position.get(self.OSMIUM, 0)
            orders[self.OSMIUM] = []

            buy_cap  = self.LIMIT - pos
            sell_cap = self.LIMIT + pos

            # Your observed range: bid 9992, ask 10008
            if buy_cap > 0:
                qty = min(self.OSM_ORDER_SIZE, buy_cap)
                orders[self.OSMIUM].append(Order(self.OSMIUM, self.OSM_BID_PRICE, qty))

            if sell_cap > 0:
                qty = min(self.OSM_ORDER_SIZE, sell_cap)
                orders[self.OSMIUM].append(Order(self.OSMIUM, self.OSM_ASK_PRICE, -qty))

        data["last_ts"] = ts
        conversions = 0
        trader_data = json.dumps(data)
        return orders, conversions, trader_data

    # ──────────────────────────────────────────────────────────────────
    def _pepper_anchor(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0