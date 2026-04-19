# trader.py — IMC Prosperity 4, Round 2 (Max Profit – Simplicity First)
import json
import math
from typing import Dict, List, Tuple
from datamodel import OrderDepth, TradingState, Order

class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    LIMIT = 80

    # --- Pepper: Pure Trend Capture ---
    PEPPER_SLOPE = 0.001          # 100 pts per day
    PEPPER_BUY_TOL = 10           # Wide enough to fill 80 units fast
    ENDGAME_START = 93_000        # Start unwind early to catch the peak
    UNWIND_PACE = 2.0             # Sell 2× the linear rate

    # --- Osmium: Fixed Ultra‑Tight Range ---
    OSM_FAIR = 10_000
    OSM_BID = 9_998               # Buy 2 ticks below fair
    OSM_ASK = 10_002              # Sell 2 ticks above fair
    OSM_PASSIVE_SIZE = 30         # Large passive size
    OSM_AGGRESSIVE_SIZE = 30      # Large aggressive size

    # --- Timing ---
    ROUND_DAYS = 3
    MAX_TS = 99_900
    NEW_DAY_THRESH = 10_000

    def bid(self) -> int:
        return 8_000               # Guarantee top 50% MAF

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts = state.timestamp
        prev_ts = data.get("last_ts", -1)
        day = data.get("day", 0)

        # Day rollover detection
        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day
            data.pop("pepper_anchor", None)

        # Pepper anchor: first ask of the day
        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_fair = data["pepper_anchor"] + self.PEPPER_SLOPE * ts
        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame = is_last_day and (ts >= self.ENDGAME_START)

        orders: Dict[str, List[Order]] = {}

        # --- Pepper Orders ---
        if self.PEPPER in state.order_depths:
            od = state.order_depths[self.PEPPER]
            pos = state.position.get(self.PEPPER, 0)
            orders[self.PEPPER] = []

            if not is_endgame:
                # Accumulation: buy everything up to limit
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
                    # Fallback passive bid
                    if buy_cap > 0 and od.sell_orders:
                        best_ask = min(od.sell_orders.keys())
                        orders[self.PEPPER].append(Order(self.PEPPER, best_ask - 1, buy_cap))
            else:
                # Endgame: gradual unwind
                if pos > 0 and od.buy_orders:
                    ticks_left = max(1, (self.MAX_TS - ts) // 100 + 1)
                    per_tick = math.ceil(pos / ticks_left)
                    to_sell = min(pos, int(per_tick * self.UNWIND_PACE))
                    remaining = to_sell
                    for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                        if remaining <= 0:
                            break
                        vol = min(remaining, od.buy_orders[bid_px])
                        if vol > 0:
                            orders[self.PEPPER].append(Order(self.PEPPER, bid_px, -vol))
                            remaining -= vol
                    # Final tick safety
                    if ts >= self.MAX_TS - 100 and pos > 0:
                        leftover = pos - (to_sell - remaining)
                        if leftover > 0 and od.buy_orders:
                            best_bid = max(od.buy_orders.keys())
                            orders[self.PEPPER].append(Order(self.PEPPER, best_bid, -leftover))

        # --- Osmium Orders ---
        if self.OSMIUM in state.order_depths:
            od = state.order_depths[self.OSMIUM]
            pos = state.position.get(self.OSMIUM, 0)
            orders[self.OSMIUM] = []

            best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
            best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
            if best_bid is None or best_ask is None:
                pass
            else:
                buy_cap = self.LIMIT - pos
                sell_cap = self.LIMIT + pos

                # Aggressive taking
                if best_ask <= self.OSM_BID and buy_cap > 0:
                    vol = min(self.OSM_AGGRESSIVE_SIZE, buy_cap, -od.sell_orders.get(best_ask, 0))
                    if vol > 0:
                        orders[self.OSMIUM].append(Order(self.OSMIUM, best_ask, vol))
                        buy_cap -= vol
                if best_bid >= self.OSM_ASK and sell_cap > 0:
                    vol = min(self.OSM_AGGRESSIVE_SIZE, sell_cap, od.buy_orders.get(best_bid, 0))
                    if vol > 0:
                        orders[self.OSMIUM].append(Order(self.OSMIUM, best_bid, -vol))
                        sell_cap -= vol

                # Passive market making
                if buy_cap > 0:
                    qty = min(self.OSM_PASSIVE_SIZE, buy_cap)
                    orders[self.OSMIUM].append(Order(self.OSMIUM, self.OSM_BID, qty))
                if sell_cap > 0:
                    qty = min(self.OSM_PASSIVE_SIZE, sell_cap)
                    orders[self.OSMIUM].append(Order(self.OSMIUM, self.OSM_ASK, -qty))

        data["last_ts"] = ts
        conversions = 0
        trader_data = json.dumps(data)
        return orders, conversions, trader_data

    def _pepper_anchor(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER)
        if od and od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od and od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0