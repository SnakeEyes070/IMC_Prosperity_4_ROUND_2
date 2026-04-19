# trader.py — IMC Prosperity 4, Round 2 (Recalibrated from 8,412)
import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order

class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    LIMIT = 80

    # --- Pepper (recalibrated) ---
    PEPPER_SLOPE = 0.001
    PEPPER_BUY_TOL = 5                # Middle ground: 3 → 5
    ENDGAME_START = 94_000            # Middle ground: 90k → 94k

    # Scalp (re‑enabled – proven +134)
    SCALP_RESERVE = 6
    SCALP_DIP = 8
    SCALP_EXIT = 5
    SCALP_SIZE = 10

    # --- Osmium (keep all proven upgrades) ---
    OSM_FAIR_FALLBACK = 10_004
    OSM_EMA_ALPHA = 0.10
    OSM_PASSIVE_BID_OFFSET = 5
    OSM_PASSIVE_ASK_OFFSET = 5
    OSM_PASSIVE_SIZE = 19
    OSM_MR_THRESH = 8
    OSM_MR_MAX_QTY = 24
    OSM_SKEW_FACTOR = 0.06
    OSM_AGGRESSIVE_BUY_THRESH = 9_998
    OSM_AGGRESSIVE_SELL_THRESH = 10_005
    OSM_SELL_COOLDOWN = 300

    # --- Timing ---
    ROUND_DAYS = 3
    MAX_TS = 99_900
    NEW_DAY_THRESH = 10_000

    def bid(self) -> int:
        return 5_100

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
            data.pop("osm_last_sell_ts", None)

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

            # Scalp (re‑enabled)
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

    # ---------- Osmium (all upgrades kept) ----------
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

        if best_ask <= self.OSM_AGGRESSIVE_BUY_THRESH and buy_cap > 0:
            vol = min(self.OSM_MR_MAX_QTY, buy_cap, -od.sell_orders.get(best_ask, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_ask, vol))
                buy_cap -= vol

        last_sell = data.get("osm_last_sell_ts", -9999)
        if state.timestamp - last_sell >= self.OSM_SELL_COOLDOWN:
            if best_bid >= self.OSM_AGGRESSIVE_SELL_THRESH and sell_cap > 0:
                vol = min(self.OSM_MR_MAX_QTY, sell_cap, od.buy_orders.get(best_bid, 0))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, best_bid, -vol))
                    sell_cap -= vol
                    data["osm_last_sell_ts"] = state.timestamp

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

        skew = -pos * self.OSM_SKEW_FACTOR
        if buy_cap > 0:
            bid_px = round(fair - self.OSM_PASSIVE_BID_OFFSET + skew)
            qty = min(self.OSM_PASSIVE_SIZE, buy_cap)
            orders.append(Order(self.OSMIUM, bid_px, qty))
            buy_cap -= qty
        if sell_cap > 0:
            ask_px = round(fair + self.OSM_PASSIVE_ASK_OFFSET + skew)
            qty = min(self.OSM_PASSIVE_SIZE, sell_cap)
            orders.append(Order(self.OSMIUM, ask_px, -qty))
            sell_cap -= qty

        return orders