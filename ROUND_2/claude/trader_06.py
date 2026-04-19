from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict

PEPPER = "INTARIAN_PEPPER_ROOT"
OSMIUM = "ASH_COATED_OSMIUM"
LIMIT  = 80

# ── Pepper ───────────────────────────────────────────────────────────────────
P_BUY_TOL          = 5
P_ENDGAME_START    = 90000
P_MAIN_CAP         = 74      # reserve 6 units for scalp headroom
P_SELL_SLOW        = 4       # units/tick when mid still rising
P_SELL_FAST        = 16      # units/tick when mid falling
P_SCALP_DROP       = 5
P_SCALP_EXIT       = 3
P_SCALP_SIZE       = 6
P_AGG_OPEN_END     = 300     # aggressive market-order window at open

# ── Osmium ───────────────────────────────────────────────────────────────────
OSM_PASSIVE_OFFSET  = 6
OSM_PASSIVE_SIZE    = 8
OSM_NARROW_OFFSET   = 3
OSM_NARROW_THRESH   = 10
OSM_AGG_BUY_L1      = 10000
OSM_AGG_BUY_L2      = 9999
OSM_AGG_BUY_SIZE_L1 = 10
OSM_AGG_BUY_SIZE_L2 = 5
OSM_AGG_SELL_L1     = 10003
OSM_AGG_SELL_L2     = 10004
OSM_AGG_SELL_SIZE_L1= 10
OSM_AGG_SELL_SIZE_L2= 5
OSM_SKEW_PER_UNIT   = 0.10   # ticks of skew per unit of position
OSM_DRIFT_WINDOW    = 5      # ticks to measure mid drift
OSM_DRIFT_THRESH    = 4.0    # mid-point move that suppresses one side


class Trader:

    def __init__(self):
        # pepper state
        self._p_prev_mid: float      = 0.0
        self._scalp_active: bool     = False
        self._scalp_entry: float     = 0.0
        self._scalp_units: int       = 0

        # osmium state
        self._osm_mid_history: List[float] = []

    def bid(self) -> int:
        return 1

    # ── generic helpers ───────────────────────────────────────────────────────

    def _ba(self, od: OrderDepth):
        return min(od.sell_orders) if od.sell_orders else None

    def _bb(self, od: OrderDepth):
        return max(od.buy_orders) if od.buy_orders else None

    def _mid(self, od: OrderDepth):
        ba, bb = self._ba(od), self._bb(od)
        if ba and bb: return (ba + bb) / 2.0
        if ba:        return float(ba)
        if bb:        return float(bb)
        return None

    def _avol(self, od, p): return abs(od.sell_orders.get(p, 0))
    def _bvol(self, od, p): return od.buy_orders.get(p, 0)

    # ── pepper ───────────────────────────────────────────────────────────────

    def _trade_pepper(self, od: OrderDepth, pos: int, t: int) -> List[Order]:
        orders: List[Order] = []
        mid = self._mid(od)
        if mid is None:
            return orders

        ba, bb = self._ba(od), self._bb(od)
        prev   = self._p_prev_mid

        # ── endgame: momentum-aware liquidation ──────────────────────────────
        if t >= P_ENDGAME_START:
            if pos > 0 and bb is not None:
                if t >= 99000:
                    orders.append(Order(PEPPER, bb - 1, -pos))
                else:
                    rising     = (mid > prev) if prev > 0 else True
                    sell_qty   = P_SELL_SLOW if rising else P_SELL_FAST
                    sell_qty   = min(pos, sell_qty)
                    orders.append(Order(PEPPER, bb, -sell_qty))
            self._p_prev_mid = mid
            return orders

        # ── scalp: exit check ────────────────────────────────────────────────
        if self._scalp_active and bb is not None:
            if mid >= self._scalp_entry + P_SCALP_EXIT:
                qty = min(self._scalp_units, pos)
                if qty > 0:
                    orders.append(Order(PEPPER, bb, -qty))
                self._scalp_active = False
                self._scalp_units  = 0

        # ── aggressive open (first 3 ticks, fill fast) ───────────────────────
        if t < P_AGG_OPEN_END:
            if ba is not None and pos < LIMIT:
                qty = min(LIMIT - pos, self._avol(od, ba))
                if qty > 0:
                    orders.append(Order(PEPPER, ba, qty))
            self._p_prev_mid = mid
            return orders

        # ── normal buy: disciplined tolerance ────────────────────────────────
        if ba is not None and pos < P_MAIN_CAP:
            if ba <= mid + P_BUY_TOL:
                qty = min(P_MAIN_CAP - pos, self._avol(od, ba))
                if qty > 0:
                    orders.append(Order(PEPPER, ba, qty))

        # ── pullback scalp: enter ─────────────────────────────────────────────
        scalp_room = LIMIT - pos
        if (not self._scalp_active
                and prev > 0
                and mid <= prev - P_SCALP_DROP
                and ba is not None
                and scalp_room >= P_SCALP_SIZE):
            sq = min(P_SCALP_SIZE, self._avol(od, ba), scalp_room)
            if sq > 0:
                orders.append(Order(PEPPER, ba, sq))
                self._scalp_entry  = mid
                self._scalp_active = True
                self._scalp_units  = sq

        self._p_prev_mid = mid
        return orders

    # ── osmium ────────────────────────────────────────────────────────────────

    def _trade_osmium(self, od: OrderDepth, pos: int) -> List[Order]:
        orders: List[Order] = []
        mid = self._mid(od)
        if mid is None:
            return orders

        ba, bb = self._ba(od), self._bb(od)
        spread = (ba - bb) if (ba and bb) else 99

        # ── track mid drift ──────────────────────────────────────────────────
        self._osm_mid_history.append(mid)
        if len(self._osm_mid_history) > OSM_DRIFT_WINDOW:
            self._osm_mid_history.pop(0)
        drift = mid - self._osm_mid_history[0] if len(self._osm_mid_history) >= 2 else 0.0

        # ── aggressive mean reversion: laddered fills ────────────────────────
        if ba is not None:
            if ba <= OSM_AGG_BUY_L1 and pos < LIMIT:
                qty = min(OSM_AGG_BUY_SIZE_L1, LIMIT - pos, self._avol(od, ba))
                if qty > 0:
                    orders.append(Order(OSMIUM, ba, qty))
            if ba <= OSM_AGG_BUY_L2 and pos < LIMIT:
                qty = min(OSM_AGG_BUY_SIZE_L2, LIMIT - pos, self._avol(od, ba))
                if qty > 0:
                    orders.append(Order(OSMIUM, ba, qty))

        if bb is not None:
            if bb >= OSM_AGG_SELL_L1 and pos > -LIMIT:
                qty = min(OSM_AGG_SELL_SIZE_L1, LIMIT + pos, self._bvol(od, bb))
                if qty > 0:
                    orders.append(Order(OSMIUM, bb, -qty))
            if bb >= OSM_AGG_SELL_L2 and pos > -LIMIT:
                qty = min(OSM_AGG_SELL_SIZE_L2, LIMIT + pos, self._bvol(od, bb))
                if qty > 0:
                    orders.append(Order(OSMIUM, bb, -qty))

        # ── inventory skew: shift quotes toward mean reversion ───────────────
        skew   = OSM_SKEW_PER_UNIT * pos
        offset = OSM_NARROW_OFFSET if spread <= OSM_NARROW_THRESH else OSM_PASSIVE_OFFSET
        p_bid  = round(mid - offset - skew)
        p_ask  = round(mid + offset - skew)

        buy_room  = LIMIT - pos
        sell_room = LIMIT + pos

        # ── drift guard: suppress one side when mid trending ─────────────────
        suppress_bid = drift < -OSM_DRIFT_THRESH   # mid falling → don't buy passively
        suppress_ask = drift >  OSM_DRIFT_THRESH   # mid rising  → don't sell passively

        if buy_room > 0 and not suppress_bid:
            q = min(OSM_PASSIVE_SIZE, buy_room)
            orders.append(Order(OSMIUM, p_bid, q))

        if sell_room > 0 and not suppress_ask:
            q = min(OSM_PASSIVE_SIZE, sell_room)
            orders.append(Order(OSMIUM, p_ask, -q))

        return orders

    # ── main ─────────────────────────────────────────────────────────────────

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}
        t = state.timestamp

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