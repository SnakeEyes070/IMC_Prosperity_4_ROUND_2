# trader.py — IMC Prosperity 4, Round 2 (12k Target – Data‑Driven Final)
import json
import math
from typing import Dict, List, Tuple
from datamodel import Order, OrderDepth, TradingState

class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    LIMIT = 80

    # --- Pepper (8,678 peak – unchanged, seed‑stable) ---
    PEPPER_SLOPE = 0.001
    PEPPER_BUY_TOL = 4
    ENDGAME_START = 92_000
    SCALP_RESERVE = 0
    SCALP_DIP = 8
    SCALP_EXIT = 5
    SCALP_SIZE = 10

    # --- Osmium (Completely Rebuilt – Eliminate Adverse Fills) ---
    # Static fair value – avoids EMA noise and lag
    OSM_FAIR = 10_003

    # Passive offsets: wider to stay outside the market's natural spread
    # Bid = 9_997, Ask = 10_009 – inside wide regime, avoids crossing
    OSM_PASSIVE_OFFSET = 6

    # Aggressive buy: only when ask is genuinely dislocated (fair – 4 = 9_999)
    # Captures the 5 tight‑spread dislocations from the log
    OSM_AGG_BUY_THRESHOLD = 4

    # NO aggressive sells – log proves they were the primary adverse fill source

    # Inventory skew: when long, shift quotes down to offload; when short, shift up
    OSM_USE_SKEW = True
    OSM_SKEW_PER_UNIT = 0.1

    # Imbalance filter: only quote when order book is balanced
    OSM_IMBALANCE_THRESH = 0.15

    # Passive size: keep moderate to avoid over‑exposure
    OSM_PASSIVE_SIZE = 15

    # --- Timing ---
    ROUND_DAYS = 3
    MAX_TS = 99_900
    NEW_DAY_THRESH = 10_000

    # --- MAF Bid – Calibrated for 12k Target ---
    # Extra flow adds ~2,500–3,500 XIRECs raw. Bid 5,500 clears median with margin.
    def bid(self) -> int:
        return 5_500

    # ------------------------------------------------------------------
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data = json.loads(state.traderData) if state.traderData else {}
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
        is_last_day = day >= self.ROUND_DAYS - 1
        is_endgame = is_last_day and ts >= self.ENDGAME_START

        orders: Dict[str, List[Order]] = {}

        if self.PEPPER in state.order_depths:
            pepper_orders = self._pepper_orders(state, pepper_fair, is_endgame, data)
            if pepper_orders:
                orders[self.PEPPER] = pepper_orders

        if self.OSMIUM in state.order_depths:
            osmium_orders = self._osmium_orders(state)
            if osmium_orders:
                orders[self.OSMIUM] = osmium_orders

        data["last_ts"] = ts
        return orders, 0, json.dumps(data)

    # ---------- Pepper (unchanged) ----------
    def _pepper_anchor(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    def _pepper_orders(self, state: TradingState, fair: float,
                       is_endgame: bool, data: dict) -> List[Order]:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        pos = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else fair

        if not is_endgame:
            main_cap = max(0, self.LIMIT - self.SCALP_RESERVE - pos)
            if main_cap > 0 and od.sell_orders:
                for ask_px in sorted(od.sell_orders.keys()):
                    if main_cap <= 0:
                        break
                    if ask_px <= fair + self.PEPPER_BUY_TOL:
                        vol = min(main_cap, -od.sell_orders[ask_px])
                        if vol > 0:
                            orders.append(Order(self.PEPPER, ask_px, vol))
                            main_cap -= vol
                if main_cap > 0:
                    passive_bid = min(od.sell_orders.keys()) - 1
                    orders.append(Order(self.PEPPER, passive_bid, main_cap))

            if "pepper_recent_high" not in data:
                data["pepper_recent_high"] = mid
            else:
                data["pepper_recent_high"] = max(data["pepper_recent_high"], mid)

            scalp_entry = data.get("pepper_scalp_entry")
            if scalp_entry is None and best_ask is not None:
                trigger = data["pepper_recent_high"] - self.SCALP_DIP
                if best_ask < trigger:
                    remaining = self.LIMIT - pos
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
            if pos > 0 and od.buy_orders:
                ts = state.timestamp
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
                if state.timestamp >= self.MAX_TS - 100:
                    leftover = pos - (to_sell - remaining)
                    if leftover > 0 and od.buy_orders:
                        orders.append(Order(self.PEPPER, max(od.buy_orders.keys()), -leftover))

        return orders

    # ---------- Osmium (Rebuilt for 12k) ----------
    def _osmium_orders(self, state: TradingState) -> List[Order]:
        od = state.order_depths.get(self.OSMIUM)
        if not od:
            return []
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None

        # Order book imbalance
        bid_vol = sum(od.buy_orders.values())
        ask_vol = sum(abs(v) for v in od.sell_orders.values())
        total_vol = bid_vol + ask_vol
        imbalance = (bid_vol - ask_vol) / total_vol if total_vol > 0 else 0

        buy_cap = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # Aggressive buy: only at genuine dislocations
        if best_ask is not None and best_ask <= self.OSM_FAIR - self.OSM_AGG_BUY_THRESHOLD:
            if buy_cap > 0 and imbalance > -self.OSM_IMBALANCE_THRESH:
                vol = min(buy_cap, -od.sell_orders.get(best_ask, 0))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, best_ask, vol))
                    buy_cap -= vol

        # Inventory skew
        skew = int(round(self.OSM_SKEW_PER_UNIT * pos)) if self.OSM_USE_SKEW else 0

        # Passive bid – only when imbalance is favorable
        if buy_cap > 0 and imbalance > -self.OSM_IMBALANCE_THRESH:
            bid_px = self.OSM_FAIR - self.OSM_PASSIVE_OFFSET - skew
            qty = min(self.OSM_PASSIVE_SIZE, buy_cap)
            orders.append(Order(self.OSMIUM, bid_px, qty))
            buy_cap -= qty

        # Passive ask – NO aggressive sells, only passive
        if sell_cap > 0 and imbalance < self.OSM_IMBALANCE_THRESH:
            ask_px = self.OSM_FAIR + self.OSM_PASSIVE_OFFSET - skew
            qty = min(self.OSM_PASSIVE_SIZE, sell_cap)
            orders.append(Order(self.OSMIUM, ask_px, -qty))
            sell_cap -= qty

        return orders