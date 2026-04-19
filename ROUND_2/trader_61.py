# trader.py — IMC Prosperity 4, Round 2 (11k Target – Claude Fixes + ChatGPT Alpha)
import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order

class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    LIMIT = 80

    # --- Pepper (Claude baseline + Drift Acceleration) ---
    PEPPER_SLOPE = 0.001
    PEPPER_BUY_TOL = 8
    ENDGAME_START = 90_000
    SCALP_RESERVE = 6
    SCALP_DIP = 8
    SCALP_EXIT = 5
    SCALP_SIZE = 10

    # ChatGPT Drift Acceleration
    PEPPER_BASE_SIZE = 74            # Core position (LIMIT - SCALP_RESERVE)
    PEPPER_DRIFT_WINDOW = 500        # Ticks for slope calculation
    PEPPER_DRIFT_BOOST = 1.2         # Increase position by 20% when accelerating

    # --- Osmium (Claude‑fixed) ---
    OSM_FAIR_FALLBACK = 10_000
    OSM_EMA_ALPHA = 0.02
    OSM_L1_SIZE = 19
    OSM_L2_SIZE = 14
    OSM_L3_SIZE = 10
    OSM_MR_THRESH = 8
    OSM_MR_MAX_QTY = 24
    OSM_SKEW_FACTOR = 0.06
    OSM_AGGRESSIVE_BUY_THRESH = 9_998
    OSM_AGGRESSIVE_SELL_THRESH = 10_004

    # Claude fixes
    OSM_SELL_MIN_PRICE = 10_005          # Raise floor – don't sell cheap
    OSM_BUY_MAX_PRICE = 10_001           # Cap – don't chase expensive
    OSM_SELL_COOLDOWN_TICKS = 500        # Prevent flooding
    OSM_MAX_SELL_QTY_PER_WINDOW = 20     # Cap units per window
    OSM_BUY_QTY_CAP = 10                 # Max units per buy

    # --- Timing ---
    ROUND_DAYS = 3
    MAX_TS = 99_900
    NEW_DAY_THRESH = 10_000

    def __init__(self):
        # Pepper state
        self.pepper_prev_mid = 0.0
        self.scalp_entry = 0.0
        self.scalp_active = False
        self.scalp_units = 0
        self.pepper_mid_history = []
        # Osmium state
        self.last_osm_sell_tick = -9999
        self.osm_sell_window_qty = 0
        self.osm_sell_window_start = 0

    def bid(self) -> int:
        return 6_500

    def _reset_daily_state(self):
        self.pepper_prev_mid = 0.0
        self.scalp_active = False
        self.scalp_units = 0
        self.pepper_mid_history = []
        self.last_osm_sell_tick = -9999
        self.osm_sell_window_qty = 0
        self.osm_sell_window_start = 0

    # ---------- Pepper (with Drift Acceleration) ----------
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
            # --- Drift Acceleration (ChatGPT Alpha) ---
            self.pepper_mid_history.append(mid)
            if len(self.pepper_mid_history) > self.PEPPER_DRIFT_WINDOW:
                self.pepper_mid_history.pop(0)

            drift_boost = 1.0
            if len(self.pepper_mid_history) == self.PEPPER_DRIFT_WINDOW:
                short_slope = (mid - self.pepper_mid_history[0]) / self.PEPPER_DRIFT_WINDOW
                avg_slope = self.PEPPER_SLOPE
                if short_slope > avg_slope * 1.5:  # Accelerating
                    drift_boost = self.PEPPER_DRIFT_BOOST

            target_core = int(self.PEPPER_BASE_SIZE * drift_boost)
            target_core = min(target_core, self.LIMIT - self.SCALP_RESERVE)

            # Core accumulation
            main_cap = target_core - pos
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

            # Scalp (unchanged)
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
            # Endgame unwind (unchanged)
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

    # ---------- Osmium (Claude‑Fixed) ----------
    def _osmium_orders(self, state: TradingState, data: dict) -> List[Order]:
        od = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []
        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        if best_bid is None or best_ask is None:
            return orders

        raw_mid = (best_bid + best_ask) / 2.0
        current_spread = best_ask - best_bid
        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_FALLBACK))
        ema = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = round(ema)

        buy_cap = self.LIMIT - pos
        sell_cap = self.LIMIT + pos
        ts = state.timestamp

        # --- Aggressive Buying (with price cap) ---
        if best_ask is not None and best_ask <= self.OSM_AGGRESSIVE_BUY_THRESH:
            if best_ask <= self.OSM_BUY_MAX_PRICE and buy_cap > 0:
                vol = min(self.OSM_BUY_QTY_CAP, buy_cap, self._ask_vol(od, best_ask))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, best_ask, vol))
                    buy_cap -= vol

        # --- Aggressive Selling (with cooldown + price floor + window cap) ---
        if best_bid is not None and best_bid >= self.OSM_AGGRESSIVE_SELL_THRESH:
            # Reset window if cooldown expired
            if ts - self.osm_sell_window_start > self.OSM_SELL_COOLDOWN_TICKS:
                self.osm_sell_window_qty = 0
                self.osm_sell_window_start = ts

            can_sell = True
            if ts - self.last_osm_sell_tick < self.OSM_SELL_COOLDOWN_TICKS:
                can_sell = False
            if best_bid < self.OSM_SELL_MIN_PRICE:
                can_sell = False
            if self.osm_sell_window_qty >= self.OSM_MAX_SELL_QTY_PER_WINDOW:
                can_sell = False

            if can_sell and sell_cap > 0:
                vol = min(self.OSM_BUY_QTY_CAP, sell_cap, self._bid_vol(od, best_bid))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, best_bid, -vol))
                    sell_cap -= vol
                    self.last_osm_sell_tick = ts
                    self.osm_sell_window_qty += vol

        # --- Mean Reversion Safety Net (also subject to caps) ---
        if od.sell_orders and buy_cap > 0:
            for ask_px in sorted(od.sell_orders.keys()):
                if ask_px > fair - self.OSM_MR_THRESH:
                    break
                if ask_px > self.OSM_BUY_MAX_PRICE:
                    continue
                vol = min(self.OSM_BUY_QTY_CAP, buy_cap, self._ask_vol(od, ask_px))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, ask_px, vol))
                    buy_cap -= vol

        if od.buy_orders and sell_cap > 0:
            # Reset window if cooldown expired (for MR sells too)
            if ts - self.osm_sell_window_start > self.OSM_SELL_COOLDOWN_TICKS:
                self.osm_sell_window_qty = 0
                self.osm_sell_window_start = ts

            for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                if bid_px < fair + self.OSM_MR_THRESH:
                    break
                if bid_px < self.OSM_SELL_MIN_PRICE:
                    continue
                if ts - self.last_osm_sell_tick < self.OSM_SELL_COOLDOWN_TICKS:
                    continue
                if self.osm_sell_window_qty >= self.OSM_MAX_SELL_QTY_PER_WINDOW:
                    break
                vol = min(self.OSM_BUY_QTY_CAP, sell_cap, self._bid_vol(od, bid_px))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, bid_px, -vol))
                    sell_cap -= vol
                    self.last_osm_sell_tick = ts
                    self.osm_sell_window_qty += vol

        # --- Passive Market Making (with inventory skew) ---
        skew = int(round(0.1 * pos))
        if buy_cap > 0:
            bid_px = fair - 6 - skew
            qty = min(8, buy_cap)
            orders.append(Order(self.OSMIUM, bid_px, qty))
        if sell_cap > 0:
            ask_px = fair + 6 - skew
            qty = min(8, sell_cap)
            orders.append(Order(self.OSMIUM, ask_px, -qty))

        return orders

    def _ask_vol(self, od: OrderDepth, price: int) -> int:
        return abs(od.sell_orders.get(price, 0))

    def _bid_vol(self, od: OrderDepth, price: int) -> int:
        return od.buy_orders.get(price, 0)

    # ---------- Main Entry ----------
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        t = state.timestamp
        if hasattr(self, 'prev_ts'):
            if self.prev_ts > self.NEW_DAY_THRESH and t < self.NEW_DAY_THRESH:
                self._reset_daily_state()
        self.prev_ts = t

        result: Dict[str, List[Order]] = {}
        for product in [self.PEPPER, self.OSMIUM]:
            od = state.order_depths.get(product)
            if od is None:
                continue
            pos = state.position.get(product, 0)
            if product == self.PEPPER:
                # Pepper needs fair value
                if "pepper_anchor" not in state.traderData:
                    anchor = self._pepper_anchor(state)
                else:
                    anchor = json.loads(state.traderData).get("pepper_anchor", self._pepper_anchor(state))
                fair = anchor + self.PEPPER_SLOPE * t
                is_last_day = (getattr(self, 'day', 0) >= self.ROUND_DAYS - 1)
                is_endgame = is_last_day and (t >= self.ENDGAME_START)
                data = json.loads(state.traderData) if state.traderData else {}
                result[product] = self._pepper_orders(state, t, fair, is_endgame, data)
            else:
                data = json.loads(state.traderData) if state.traderData else {}
                result[product] = self._osmium_orders(state, data)

        # Persist minimal state
        trader_data = json.dumps({"pepper_anchor": getattr(self, 'pepper_anchor', 12000)})
        return result, 0, trader_data