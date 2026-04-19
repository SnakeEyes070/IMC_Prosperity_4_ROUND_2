# trader.py — IMC Prosperity 4, Round 2 (Brute Force)
# No EMAs, no filters — pure trend capture + tight market making.

import json
import math
from typing import Dict, List, Tuple
from datamodel import OrderDepth, TradingState, Order


class Trader:

    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"

    LIMIT = 80

    # ── Pepper: buy everything, sell at the end ───────────────────
    ENDGAME_START = 9_700

    # ── Osmium: fixed tight quotes around 10,000 ─────────────────
    OSM_BID = 9_998
    OSM_ASK = 10_002

    # ── Day tracking ─────────────────────────────────────────────
    MAX_TS         = 9_900
    NEW_DAY_THRESH = 1_000

    # ──────────────────────────────────────────────────────────────
    def bid(self) -> int:
        return 6_000

    # ──────────────────────────────────────────────────────────────
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts      = state.timestamp
        prev_ts = data.get("prev_ts", -1)
        day     = data.get("day", 0)

        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day

        is_last_day = (day >= 2)  # 0-indexed: days 0,1,2
        is_endgame  = is_last_day and (ts >= self.ENDGAME_START)

        orders: Dict[str, List[Order]] = {}

        # ── PEPPER ─────────────────────────────────────────────────
        if self.PEPPER in state.order_depths:
            od = state.order_depths[self.PEPPER]
            pos = state.position.get(self.PEPPER, 0)
            orders[self.PEPPER] = []

            if not is_endgame:
                # Buy everything available until limit 80
                budget = self.LIMIT - pos
                if budget > 0 and od.sell_orders:
                    for ask_px in sorted(od.sell_orders.keys()):
                        if budget <= 0:
                            break
                        vol = min(budget, -od.sell_orders[ask_px])
                        if vol > 0:
                            orders[self.PEPPER].append(Order(self.PEPPER, ask_px, vol))
                            budget -= vol
            else:
                # Endgame: dump everything at best bid
                if pos > 0 and od.buy_orders:
                    best_bid = max(od.buy_orders.keys())
                    vol = min(pos, od.buy_orders[best_bid])
                    if vol > 0:
                        orders[self.PEPPER].append(Order(self.PEPPER, best_bid, -vol))

        # ── OSMIUM ─────────────────────────────────────────────────
        if self.OSMIUM in state.order_depths:
            od = state.order_depths[self.OSMIUM]
            pos = state.position.get(self.OSMIUM, 0)
            orders[self.OSMIUM] = []

            buy_cap  = self.LIMIT - pos
            sell_cap = self.LIMIT + pos

            # Fixed tight quotes
            if buy_cap > 0:
                orders[self.OSMIUM].append(Order(self.OSMIUM, self.OSM_BID, buy_cap))
            if sell_cap > 0:
                orders[self.OSMIUM].append(Order(self.OSMIUM, self.OSM_ASK, -sell_cap))

        data["prev_ts"] = ts
        conversions = 0
        trader_data = json.dumps(data)
        return orders, conversions, trader_data