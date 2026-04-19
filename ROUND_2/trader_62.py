# trader.py — IMC Prosperity 4, Round 2 (11k Target – Fully Adaptive)
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

    PEPPER_BASE_SIZE = 74
    PEPPER_DRIFT_WINDOW = 500
    PEPPER_DRIFT_BOOST = 1.2

    # --- Osmium (Adaptive Upgrades) ---
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

    # Adaptive fair value anchor (sell premium over rolling mid)
    OSM_SELL_PREMIUM = 3                # Sell only if bid >= fair + premium
    OSM_FAIR_WINDOW = 1000              # Ticks for rolling fair value

    # Inventory‑aware throttling
    OSM_INVENTORY_HIGH = 0.7            # >70% of limit → aggressive unwind
    OSM_INVENTORY_LOW = 0.3             # <30% → patient

    # Adverse selection filter (short‑term trend)
    OSM_TREND_WINDOW = 300              # Ticks for short‑term slope
    OSM_TREND_THRESH = 0.002            # Slope threshold for adverse move

    # Claude fixes (kept as fallback / additional gates)
    OSM_BUY_MAX_PRICE = 10_001
    OSM_BUY_QTY_CAP = 10
    OSM_MAX_SELL_QTY_PER_WINDOW = 20

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
        self.osm_mid_history = []        # For rolling fair value & trend
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
        self.osm_mid_history = []
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
            # Drift Acceleration
            self.pepper_mid_history.append(mid)
            if len(self.pepper_mid_history) > self.PEPPER_DRIFT_WINDOW:
                self.pepper_mid_history.pop(0)

            drift_boost = 1.0
            if len(self.pepper_mid_history) == self.PEPPER_DRIFT_WINDOW:
                short_slope = (mid - self.pepper_mid_history[0]) / self.PEPPER_DRIFT_WINDOW
                if short_slope > self.PEPPER_SLOPE * 1.5:
                    drift_boost = self.PEPPER_DRIFT_BOOST

            target_core = int(self.PEPPER_BASE_SIZE * drift_boost)
            target_core = min(target_core, self.LIMIT - self.SCALP_RESERVE)

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

            # Scalp
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

    # ---------- Osmium (Fully Adaptive) ----------
    def _osmium_orders(self, state: TradingState, data: dict) -> List[Order]:
        od = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []
        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        if best_bid is None or best_ask is None:
            return orders

        mid = (best_bid + best_ask) / 2.0
        ts = state.timestamp

        # Update mid history for fair value & trend
        self.osm_mid_history.append(mid)
        if len(self.osm_mid_history) > self.OSM_FAIR_WINDOW:
            self.osm_mid_history.pop(0)

        # Rolling fair value
        if len(self.osm_mid_history) >= 100:
            fair_value = sum(self.osm_mid_history[-100:]) / 100
        else:
            fair_value = mid

        # Short‑term trend (adverse selection filter)
        trend_up = False
        trend_down = False
        if len(self.osm_mid_history) >= self.OSM_TREND_WINDOW:
            slope = (mid - self.osm_mid_history[-self.OSM_TREND_WINDOW]) / self.OSM_TREND_WINDOW
            if slope > self.OSM_TREND_THRESH:
                trend_up = True
            elif slope < -self.OSM_TREND_THRESH:
                trend_down = True

        buy_cap = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # Inventory ratio
        inv_ratio = abs(pos) / self.LIMIT

        # --- Aggressive Buying (with adverse selection & price cap) ---
        if best_ask is not None and best_ask <= self.OSM_AGGRESSIVE_BUY_THRESH:
            if not trend_down:  # Don't catch a falling knife
                if best_ask <= self.OSM_BUY_MAX_PRICE and buy_cap > 0:
                    vol = min(self.OSM_BUY_QTY_CAP, buy_cap, self._ask_vol(od, best_ask))
                    if vol > 0:
                        orders.append(Order(self.OSMIUM, best_ask, vol))
                        buy_cap -= vol

        # --- Aggressive Selling (Adaptive Fair Value Anchor + Inventory Throttling + Adverse Selection) ---
        if best_bid is not None and best_bid >= self.OSM_AGGRESSIVE_SELL_THRESH:
            # Adaptive sell floor: only sell if bid >= fair_value + premium
            if best_bid >= fair_value + self.OSM_SELL_PREMIUM:
                if not trend_up:  # Don't sell into rising strength
                    # Inventory‑aware throttling
                    if inv_ratio > self.OSM_INVENTORY_HIGH:
                        cooldown_required = 100   # aggressive unwind
                    elif inv_ratio < self.OSM_INVENTORY_LOW:
                        cooldown_required = 800   # patient
                    else:
                        cooldown_required = 400

                    if ts - self.last_osm_sell_tick >= cooldown_required:
                        if self.osm_sell_window_qty < self.OSM_MAX_SELL_QTY_PER_WINDOW:
                            vol = min(self.OSM_BUY_QTY_CAP, sell_cap, self._bid_vol(od, best_bid))
                            if vol > 0:
                                orders.append(Order(self.OSMIUM, best_bid, -vol))
                                sell_cap -= vol
                                self.last_osm_sell_tick = ts
                                self.osm_sell_window_qty += vol
                                if self.osm_sell_window_start == 0:
                                    self.osm_sell_window_start = ts
                                # Reset window if needed
                                if ts - self.osm_sell_window_start > 500:
                                    self.osm_sell_window_qty = 0
                                    self.osm_sell_window_start = ts

        # --- Mean Reversion Safety Net (with same adaptive gates) ---
        if od.sell_orders and buy_cap > 0:
            for ask_px in sorted(od.sell_orders.keys()):
                if ask_px > fair_value - self.OSM_MR_THRESH:
                    break
                if trend_down:
                    continue
                if ask_px > self.OSM_BUY_MAX_PRICE:
                    continue
                vol = min(self.OSM_BUY_QTY_CAP, buy_cap, self._ask_vol(od, ask_px))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, ask_px, vol))
                    buy_cap -= vol

        if od.buy_orders and sell_cap > 0:
            for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                if bid_px < fair_value + self.OSM_MR_THRESH:
                    break
                if trend_up:
                    continue
                if bid_px < fair_value + self.OSM_SELL_PREMIUM:
                    continue

                if inv_ratio > self.OSM_INVENTORY_HIGH:
                    cooldown_required = 100
                elif inv_ratio < self.OSM_INVENTORY_LOW:
                    cooldown_required = 800
                else:
                    cooldown_required = 400

                if ts - self.last_osm_sell_tick < cooldown_required:
                    continue
                if self.osm_sell_window_qty >= self.OSM_MAX_SELL_QTY_PER_WINDOW:
                    break

                vol = min(self.OSM_BUY_QTY_CAP, sell_cap, self._bid_vol(od, bid_px))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, bid_px, -vol))
                    sell_cap -= vol
                    self.last_osm_sell_tick = ts
                    self.osm_sell_window_qty += vol
                    if self.osm_sell_window_start == 0:
                        self.osm_sell_window_start = ts
                    if ts - self.osm_sell_window_start > 500:
                        self.osm_sell_window_qty = 0
                        self.osm_sell_window_start = ts

        # --- Passive Market Making (with inventory skew) ---
        skew = int(round(0.1 * pos))
        if buy_cap > 0 and not trend_down:
            bid_px = int(fair_value) - 6 - skew
            qty = min(8, buy_cap)
            orders.append(Order(self.OSMIUM, bid_px, qty))
        if sell_cap > 0 and not trend_up:
            ask_px = int(fair_value) + 6 - skew
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

        # Load persistent data
        data = {}
        if state.traderData:
            try:
                data = json.loads(state.traderData)
            except:
                pass

        # Pepper anchor
        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_fair = data["pepper_anchor"] + self.PEPPER_SLOPE * t
        day = data.get("day", 0)
        if t < 100 and day == 0:
            day = 0
        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame = is_last_day and (t >= self.ENDGAME_START)

        result: Dict[str, List[Order]] = {}
        if self.PEPPER in state.order_depths:
            result[self.PEPPER] = self._pepper_orders(state, t, pepper_fair, is_endgame, data)
        if self.OSMIUM in state.order_depths:
            result[self.OSMIUM] = self._osmium_orders(state, data)

        data["last_ts"] = t
        data["day"] = day
        return result, 0, json.dumps(data)