# trader.py — IMC Prosperity 4, Round 2 (Error‑Free Fully Optimized)
import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order

class Trader:
    # ---------- Product Names ----------
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    LIMIT = 80

    # ---------- Pepper (Optimized) ----------
    PEPPER_SLOPE = 0.001
    PEPPER_BUY_TOL = 5                # Used after t=300
    ENDGAME_START = 90000
    SCALP_DIP = 5
    SCALP_EXIT = 3
    SCALP_SIZE = 5
    SCALP_RESERVE = 6                 # Keep 6 units free for scalps

    # ---------- Osmium (Optimized) ----------
    OSM_FAIR_FALLBACK = 10000
    OSM_EMA_ALPHA = 0.02
    OSM_PASSIVE_OFFSET = 6
    OSM_PASSIVE_SIZE = 8
    OSM_AGG_BUY_THRESH = 10000
    OSM_AGG_SELL_THRESH = 10003
    OSM_AGG_SIZE_L1 = 10
    OSM_AGG_SIZE_L2 = 5
    OSM_SKEW_PER_UNIT = 0.1
    OSM_DRIFT_WINDOW = 5
    OSM_DRIFT_THRESH = 4

    # ---------- Timing ----------
    ROUND_DAYS = 3
    MAX_TS = 99900
    NEW_DAY_THRESH = 10000

    def __init__(self):
        # Pepper state
        self.pepper_prev_mid = 0.0
        self.scalp_entry = 0.0
        self.scalp_active = False
        self.scalp_units = 0
        # Osmium state
        self.osm_mid_history = []
        self.prev_ts = -1

    def bid(self) -> int:
        return 6500

    def _reset_daily_state(self):
        self.pepper_prev_mid = 0.0
        self.scalp_active = False
        self.scalp_units = 0
        self.osm_mid_history = []

    # ---------- Helpers ----------
    def _best_ask(self, od: OrderDepth):
        return min(od.sell_orders.keys()) if od.sell_orders else None

    def _best_bid(self, od: OrderDepth):
        return max(od.buy_orders.keys()) if od.buy_orders else None

    def _mid(self, od: OrderDepth) -> Optional[float]:
        ba, bb = self._best_ask(od), self._best_bid(od)
        if ba and bb:
            return (ba + bb) / 2.0
        return ba or bb or None

    def _ask_vol(self, od: OrderDepth, price: int) -> int:
        return abs(od.sell_orders.get(price, 0))

    def _bid_vol(self, od: OrderDepth, price: int) -> int:
        return od.buy_orders.get(price, 0)

    # ---------- Pepper (All Optimizations) ----------
    def _trade_pepper(self, od: OrderDepth, pos: int, t: int) -> List[Order]:
        orders: List[Order] = []
        mid = self._mid(od)
        if mid is None:
            return orders

        ba = self._best_ask(od)
        bb = self._best_bid(od)

        # --- Endgame: Momentum‑aware exit ---
        if t >= self.ENDGAME_START:
            if pos > 0 and bb is not None:
                if mid > self.pepper_prev_mid and self.pepper_prev_mid > 0:
                    sell_per_tick = 4
                else:
                    sell_per_tick = 16
                to_sell = min(pos, sell_per_tick)
                for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                    if to_sell <= 0:
                        break
                    vol = min(to_sell, od.buy_orders[bid_px])
                    if vol > 0:
                        orders.append(Order(self.PEPPER, bid_px, -vol))
                        to_sell -= vol
                if t >= 99000 and pos > 0:
                    best_bid = max(od.buy_orders.keys())
                    orders.append(Order(self.PEPPER, best_bid, -pos))
            self.pepper_prev_mid = mid
            return orders

        # --- Aggressive open: market buy first 3 ticks ---
        if t < 300:
            if ba is not None and pos < self.LIMIT:
                qty = min(self.LIMIT - pos, self._ask_vol(od, ba))
                if qty > 0:
                    orders.append(Order(self.PEPPER, ba, qty))
        else:
            main_cap = self.LIMIT - self.SCALP_RESERVE - pos
            if ba is not None and main_cap > 0:
                if ba <= mid + self.PEPPER_BUY_TOL:
                    qty = min(main_cap, self._ask_vol(od, ba))
                    if qty > 0:
                        orders.append(Order(self.PEPPER, ba, qty))

        # --- Scalp exit ---
        if self.scalp_active and bb is not None:
            if mid >= self.scalp_entry + self.SCALP_EXIT:
                exit_qty = min(self.scalp_units, pos)
                if exit_qty > 0:
                    orders.append(Order(self.PEPPER, bb, -exit_qty))
                    self.scalp_active = False
                    self.scalp_units = 0

        # --- Scalp entry (with headroom) ---
        scalp_room = self.LIMIT - pos
        if (not self.scalp_active
                and self.pepper_prev_mid > 0
                and mid <= self.pepper_prev_mid - self.SCALP_DIP
                and ba is not None
                and scalp_room >= self.SCALP_SIZE):
            sq = min(self.SCALP_SIZE, self._ask_vol(od, ba))
            if sq > 0:
                orders.append(Order(self.PEPPER, ba, sq))
                self.scalp_entry = mid
                self.scalp_active = True
                self.scalp_units = sq

        self.pepper_prev_mid = mid
        return orders

    # ---------- Osmium (All Optimizations) ----------
    def _trade_osmium(self, od: OrderDepth, pos: int) -> List[Order]:
        orders: List[Order] = []
        mid = self._mid(od)
        if mid is None:
            return orders

        ba = self._best_ask(od)
        bb = self._best_bid(od)

        self.osm_mid_history.append(mid)
        if len(self.osm_mid_history) > self.OSM_DRIFT_WINDOW:
            self.osm_mid_history.pop(0)

        drift = 0.0
        if len(self.osm_mid_history) == self.OSM_DRIFT_WINDOW:
            drift = mid - self.osm_mid_history[0]

        # --- Aggressive mean reversion (laddered) ---
        if ba is not None and ba <= self.OSM_AGG_BUY_THRESH and pos < self.LIMIT:
            qty = min(self.OSM_AGG_SIZE_L1, self.LIMIT - pos, self._ask_vol(od, ba))
            if qty > 0:
                orders.append(Order(self.OSMIUM, ba, qty))
            if ba <= self.OSM_AGG_BUY_THRESH - 1:
                qty2 = min(self.OSM_AGG_SIZE_L2, self.LIMIT - pos, self._ask_vol(od, ba))
                if qty2 > 0:
                    orders.append(Order(self.OSMIUM, ba, qty2))

        if bb is not None and bb >= self.OSM_AGG_SELL_THRESH and pos > -self.LIMIT:
            qty = min(self.OSM_AGG_SIZE_L1, self.LIMIT + pos, self._bid_vol(od, bb))
            if qty > 0:
                orders.append(Order(self.OSMIUM, bb, -qty))
            if bb >= self.OSM_AGG_SELL_THRESH + 1:
                qty2 = min(self.OSM_AGG_SIZE_L2, self.LIMIT + pos, self._bid_vol(od, bb))
                if qty2 > 0:
                    orders.append(Order(self.OSMIUM, bb, -qty2))

        # --- Inventory skew ---
        skew = int(round(self.OSM_SKEW_PER_UNIT * pos))
        p_bid = int(mid) - self.OSM_PASSIVE_OFFSET - skew
        p_ask = int(mid) + self.OSM_PASSIVE_OFFSET - skew

        buy_room = self.LIMIT - pos
        sell_room = self.LIMIT + pos

        # --- Drift guard ---
        bid_allowed = True
        ask_allowed = True
        if drift < -self.OSM_DRIFT_THRESH:
            bid_allowed = False
        elif drift > self.OSM_DRIFT_THRESH:
            ask_allowed = False

        if buy_room > 0 and bid_allowed:
            q = min(self.OSM_PASSIVE_SIZE, buy_room)
            orders.append(Order(self.OSMIUM, p_bid, q))

        if sell_room > 0 and ask_allowed:
            q = min(self.OSM_PASSIVE_SIZE, sell_room)
            orders.append(Order(self.OSMIUM, p_ask, -q))

        return orders

    # ---------- Main Entry ----------
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        t = state.timestamp
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
                result[product] = self._trade_pepper(od, pos, t)
            else:
                result[product] = self._trade_osmium(od, pos)

        return result, 0, ""