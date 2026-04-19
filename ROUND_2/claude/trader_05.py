# trader.py — IMC Prosperity 4, Round 2 (Claude‑Forensic + Critical Fixes)
import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order

PEPPER = "INTARIAN_PEPPER_ROOT"
OSMIUM = "ASH_COATED_OSMIUM"
LIMIT = 80

# Pepper parameters (forensic‑tuned)
P_BUY_TOL = 5
P_ENDGAME_START = 90000
P_SCALP_DROP = 5
P_SCALP_EXIT = 3
P_SCALP_SIZE = 5

# Osmium parameters (forensic‑tuned)
OSM_PASSIVE_OFFSET = 6
OSM_PASSIVE_SIZE = 8
OSM_NARROW_OFFSET = 3
OSM_NARROW_THRESH = 10
OSM_AGG_BUY_THRESH = 10000
OSM_AGG_SELL_THRESH = 10003
OSM_AGG_SIZE = 20

# Timing
MAX_TS = 99900
NEW_DAY_THRESH = 10000

class Trader:
    def __init__(self):
        self.pepper_prev_mid = 0.0
        self.scalp_entry = 0.0
        self.scalp_active = False
        self.scalp_units = 0

    def bid(self) -> int:
        return 6500   # ✅ FIXED: Guarantee extra flow

    def _reset_daily_state(self):
        self.pepper_prev_mid = 0.0
        self.scalp_active = False
        self.scalp_units = 0

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

    def _trade_pepper(self, od: OrderDepth, pos: int, t: int) -> List[Order]:
        orders: List[Order] = []
        mid = self._mid(od)
        if mid is None:
            return orders

        ba = self._best_ask(od)
        bb = self._best_bid(od)

        # Endgame: dynamic unwind (✅ FIXED)
        if t >= P_ENDGAME_START:
            if pos > 0 and bb is not None:
                ticks_left = max(1, (MAX_TS - t) // 100 + 1)
                per_tick = math.ceil(pos / ticks_left)
                to_sell = min(pos, int(per_tick * 2.5))
                remaining = to_sell
                for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                    if remaining <= 0:
                        break
                    vol = min(remaining, od.buy_orders[bid_px])
                    if vol > 0:
                        orders.append(Order(PEPPER, bid_px, -vol))
                        remaining -= vol
                if t >= MAX_TS - 100 and pos > 0:
                    leftover = pos - (to_sell - remaining)
                    if leftover > 0 and od.buy_orders:
                        best_bid = max(od.buy_orders.keys())
                        orders.append(Order(PEPPER, best_bid, -leftover))
            return orders

        # Scalp exit
        if self.scalp_active and bb is not None:
            if mid >= self.scalp_entry + P_SCALP_EXIT:
                exit_qty = min(self.scalp_units, pos)
                if exit_qty > 0:
                    orders.append(Order(PEPPER, bb, -exit_qty))
                    self.scalp_active = False
                    self.scalp_units = 0

        # Main accumulation
        if ba is not None and pos < LIMIT:
            if ba <= mid + P_BUY_TOL:
                qty = min(LIMIT - pos, self._ask_vol(od, ba))
                if qty > 0:
                    orders.append(Order(PEPPER, ba, qty))

        # Scalp entry
        if (not self.scalp_active
                and self.pepper_prev_mid > 0
                and mid <= self.pepper_prev_mid - P_SCALP_DROP
                and ba is not None
                and pos + P_SCALP_SIZE <= LIMIT):
            sq = min(P_SCALP_SIZE, self._ask_vol(od, ba))
            if sq > 0:
                orders.append(Order(PEPPER, ba, sq))
                self.scalp_entry = mid
                self.scalp_active = True
                self.scalp_units = sq

        self.pepper_prev_mid = mid
        return orders

    def _trade_osmium(self, od: OrderDepth, pos: int) -> List[Order]:
        orders: List[Order] = []
        mid = self._mid(od)
        if mid is None:
            return orders

        ba = self._best_ask(od)
        bb = self._best_bid(od)
        spread = (ba - bb) if (ba and bb) else 99

        # Aggressive mean reversion
        if ba is not None and ba <= OSM_AGG_BUY_THRESH and pos < LIMIT:
            qty = min(OSM_AGG_SIZE, LIMIT - pos, self._ask_vol(od, ba))
            if qty > 0:
                orders.append(Order(OSMIUM, ba, qty))

        if bb is not None and bb >= OSM_AGG_SELL_THRESH and pos > -LIMIT:
            qty = min(OSM_AGG_SIZE, LIMIT + pos, self._bid_vol(od, bb))
            if qty > 0:
                orders.append(Order(OSMIUM, bb, -qty))

        # Passive quotes: tighter on narrow spread
        offset = OSM_NARROW_OFFSET if spread <= OSM_NARROW_THRESH else OSM_PASSIVE_OFFSET
        p_bid = int(mid) - offset
        p_ask = int(mid) + offset

        buy_room = LIMIT - pos
        sell_room = LIMIT + pos

        if buy_room > 0:
            q = min(OSM_PASSIVE_SIZE, buy_room)
            orders.append(Order(OSMIUM, p_bid, q))

        if sell_room > 0:
            q = min(OSM_PASSIVE_SIZE, sell_room)
            orders.append(Order(OSMIUM, p_ask, -q))

        return orders

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        # ✅ FIXED: Day rollover detection and state reset
        t = state.timestamp
        if hasattr(self, 'prev_ts'):
            if self.prev_ts > NEW_DAY_THRESH and t < NEW_DAY_THRESH:
                self._reset_daily_state()
        self.prev_ts = t

        result: Dict[str, List[Order]] = {}
        for product in [PEPPER, OSMIUM]:
            od = state.order_depths.get(product)
            if od is None:
                continue
            pos = state.position.get(product, 0)
            if product == PEPPER:
                result[product] = self._trade_pepper(od, pos, t)
            else:
                result[product] = self._trade_osmium(od, pos)

        return result, 0, ""