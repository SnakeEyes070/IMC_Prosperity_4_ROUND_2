# trader.py — IMC Prosperity 4, Round 2 (Final Optimized)
# This code does three things:
# 1. Trades Pepper (trend up) – buys all 80 units early, sells near the end.
# 2. Trades Osmium (stable around 10,000) – buys low, sells high.
# 3. Bids 7,000 for the Market Access Fee (top 50% get 25% extra quotes).

import json
import math
from typing import Dict, List, Tuple
from datamodel import OrderDepth, TradingState, Order

class Trader:
    # Product names
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    LIMIT = 80                      # Max units we can hold (long or short)

    # --- Pepper settings ---
    PEPPER_SLOPE = 0.001            # Price goes up by 0.001 per timestamp
    PEPPER_BUY_TOL = 8              # Only buy if ask price is within 8 of fair value
    ENDGAME_START = 92_000          # Start selling when timestamp reaches 92,000
    UNWIND_PACE = 2.0               # Sell at 2× the linear rate

    # --- Osmium settings ---
    OSM_FAIR = 10_000               # Osmium always comes back to ~10,000
    OSM_BID = 9_998                 # We'll buy at 9,998 (cheap)
    OSM_ASK = 10_002                # We'll sell at 10,002 (expensive)
    OSM_SIZE = 30                   # Trade 30 units at a time

    # --- Round timing ---
    ROUND_DAYS = 3
    MAX_TS = 99_900                 # Last timestamp of each day
    NEW_DAY_THRESH = 10_000         # Used to detect a new day

    # ========== MARKET ACCESS FEE ==========
    # This is the blind auction for 25% extra quotes.
    # The top 50% of bids win and pay their bid. Losers pay nothing.
    # We bid 7,000 to confidently be in the top half.
    def bid(self) -> int:
        return 7_000

    # ========== MAIN FUNCTION (CALLED EVERY TICK) ==========
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        # Load saved data from previous tick
        try:
            data = json.loads(state.traderData) if state.traderData else {}
        except:
            data = {}

        ts = state.timestamp
        prev_ts = data.get("last_ts", -1)
        day = data.get("day", 0)

        # Detect new day
        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day
            data.pop("pepper_anchor", None)   # Recalculate anchor for new day

        # Set Pepper anchor (first ask price of the day)
        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._get_pepper_anchor(state)

        # Calculate Pepper fair value (anchor + slope * timestamp)
        pepper_fair = data["pepper_anchor"] + self.PEPPER_SLOPE * ts

        # Check if we are in the final endgame phase
        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame = is_last_day and (ts >= self.ENDGAME_START)

        orders = {}

        # --- Pepper orders ---
        if self.PEPPER in state.order_depths:
            pepper_ords = self._pepper_orders(state, ts, pepper_fair, is_endgame)
            if pepper_ords:
                orders[self.PEPPER] = pepper_ords

        # --- Osmium orders ---
        if self.OSMIUM in state.order_depths:
            osmium_ords = self._osmium_orders(state)
            if osmium_ords:
                orders[self.OSMIUM] = osmium_ords

        data["last_ts"] = ts
        trader_data = json.dumps(data)
        return orders, 0, trader_data

    # ---------- Helper: Pepper anchor ----------
    def _get_pepper_anchor(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER)
        if od and od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od and od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0   # fallback

    # ---------- Pepper strategy: buy early, sell late ----------
    def _pepper_orders(self, state: TradingState, ts: int, fair: float, is_endgame: bool) -> List[Order]:
        od = state.order_depths.get(self.PEPPER)
        if not od:
            return []

        pos = state.position.get(self.PEPPER, 0)
        orders = []

        if not is_endgame:
            # Accumulation phase: buy up to LIMIT
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
                # If still not full, place a passive bid
                if buy_cap > 0 and od.sell_orders:
                    best_ask = min(od.sell_orders.keys())
                    orders.append(Order(self.PEPPER, best_ask - 1, buy_cap))
        else:
            # Endgame: sell gradually
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
                        orders.append(Order(self.PEPPER, bid_px, -vol))
                        remaining -= vol
                # Final tick: dump anything left
                if ts >= self.MAX_TS - 100 and pos > 0:
                    leftover = pos - (to_sell - remaining)
                    if leftover > 0 and od.buy_orders:
                        best_bid = max(od.buy_orders.keys())
                        orders.append(Order(self.PEPPER, best_bid, -leftover))
        return orders

    # ---------- Osmium strategy: simple market making ----------
    def _osmium_orders(self, state: TradingState) -> List[Order]:
        od = state.order_depths.get(self.OSMIUM)
        if not od:
            return []

        pos = state.position.get(self.OSMIUM, 0)
        orders = []

        buy_cap = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # Aggressive: if someone is selling cheap, buy it
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        if best_ask and best_ask <= self.OSM_BID and buy_cap > 0:
            vol = min(self.OSM_SIZE, buy_cap, -od.sell_orders.get(best_ask, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_ask, vol))
                buy_cap -= vol

        # Aggressive: if someone is buying expensive, sell to them
        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        if best_bid and best_bid >= self.OSM_ASK and sell_cap > 0:
            vol = min(self.OSM_SIZE, sell_cap, od.buy_orders.get(best_bid, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_bid, -vol))
                sell_cap -= vol

        # Passive: post resting orders to capture the spread
        if buy_cap > 0:
            qty = min(self.OSM_SIZE, buy_cap)
            orders.append(Order(self.OSMIUM, self.OSM_BID, qty))
        if sell_cap > 0:
            qty = min(self.OSM_SIZE, sell_cap)
            orders.append(Order(self.OSMIUM, self.OSM_ASK, -qty))

        return orders