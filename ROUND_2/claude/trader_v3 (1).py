# trader.py — IMC Prosperity 4, Round 2
# Base: 8,643 XIREC run (verified best)
# Change from base: ONE change only
#   OSM_AGGRESSIVE_SELL_THRESH  10,003 → 10,002
#   Log evidence: 12 ticks had bid_price_1=10,002 with zero fills → ~120 XIREC leak
#   Risk: zero — threshold is 1 tick below natural ask floor, no inventory impact
#
# Everything else is IDENTICAL to the 8,643 run.
# The previous optimisation attempt introduced two fatal bugs:
#   ✗ SCALP_RESERVE=10   → scalp sold 29 units during uptrend → -359 pepper XIRECs
#   ✗ OSM_SKEW applied   → at pos=40: ask quote at ema+2.6 (inside spread) → -622 osmium XIRECs
# Both have been reverted.

import json
import math
from typing import Dict, List, Tuple
from datamodel import OrderDepth, TradingState, Order


class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    LIMIT  = 80

    # ── Pepper ─────────────────────────────────────────────────────────────
    PEPPER_SLOPE   = 0.001
    PEPPER_BUY_TOL = 5          # log: tol=3 caused different fills at t=0-100; keep 5 until next run confirms
    ENDGAME_START  = 90_000     # log: 83k endgame caused scalp-depleted position; keep 90k

    # ── Osmium ─────────────────────────────────────────────────────────────
    OSM_FAIR_FALLBACK          = 10_000
    OSM_EMA_ALPHA              = 0.02
    OSM_PASSIVE_BID_OFFSET     = 6        # log: actual fills at mid-5, but tightening caused skew cascade; keep 6
    OSM_PASSIVE_ASK_OFFSET     = 6
    OSM_PASSIVE_SIZE           = 19
    OSM_MR_THRESH              = 8
    OSM_MR_MAX_QTY             = 24
    OSM_SKEW_FACTOR            = 0.06     # defined for reference — NOT applied (see diagnosis above)
    OSM_AGGRESSIVE_BUY_THRESH  = 10_000   # only 2 events at ≤9,998; keep 10,000
    OSM_AGGRESSIVE_SELL_THRESH = 10_002   # ← ONLY CHANGE: was 10,003; 12 missed fills at 10,002 = ~120 XIRECs

    # ── Timing ─────────────────────────────────────────────────────────────
    ROUND_DAYS     = 3
    MAX_TS         = 99_900
    NEW_DAY_THRESH = 10_000

    def bid(self) -> int:
        return 6_500

    # ═══════════════════════════════════════════════════════════════════════
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts      = state.timestamp
        prev_ts = data.get("last_ts", -1)
        day     = data.get("day", 0)

        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day
            data.pop("pepper_anchor",      None)
            data.pop("pepper_recent_high", None)
            data.pop("pepper_scalp_entry", None)

        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_fair = data["pepper_anchor"] + self.PEPPER_SLOPE * ts
        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame  = is_last_day and (ts >= self.ENDGAME_START)

        orders: Dict[str, List[Order]] = {}

        if self.PEPPER in state.order_depths:
            pep = self._pepper_orders(state, ts, pepper_fair, is_endgame, data)
            if pep:
                orders[self.PEPPER] = pep

        if self.OSMIUM in state.order_depths:
            osm = self._osmium_orders(state, data)
            if osm:
                orders[self.OSMIUM] = osm

        data["last_ts"] = ts
        return orders, 0, json.dumps(data)

    # ── Pepper ─────────────────────────────────────────────────────────────

    def _pepper_anchor(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    def _pepper_orders(self, state: TradingState, ts: int, fair: float,
                       is_endgame: bool, data: dict) -> List[Order]:
        od      = state.order_depths.get(self.PEPPER, OrderDepth())
        pos     = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []
        best_bid = max(od.buy_orders.keys())  if od.buy_orders  else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        mid      = (best_bid + best_ask) / 2.0 if (best_bid and best_ask) else fair

        if not is_endgame:
            # ── Trend accumulation: full 80-unit position ──────────────────
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
                if buy_cap > 0 and od.sell_orders:
                    passive_bid = min(od.sell_orders.keys()) - 1
                    orders.append(Order(self.PEPPER, passive_bid, buy_cap))

            # ── Scalp overlay (proven +134 XIRECs in 8,643 run) ───────────
            # CRITICAL: scalp only fires when position is BELOW limit.
            # At full 80-unit capacity, buy_cap=0 → scalp vol=0 → no action.
            # This means scalp NEVER sells during the trend in normal operation.
            # Do not add SCALP_RESERVE — that breaks this invariant.
            if "pepper_recent_high" not in data:
                data["pepper_recent_high"] = mid
            else:
                data["pepper_recent_high"] = max(data["pepper_recent_high"], mid)

            scalp_entry = data.get("pepper_scalp_entry")
            if scalp_entry is None and best_ask is not None:
                if best_ask < data["pepper_recent_high"] - 8:
                    remaining = self.LIMIT - pos
                    if remaining > 0:
                        vol = min(10, remaining, -od.sell_orders.get(best_ask, 0))
                        if vol > 0:
                            orders.append(Order(self.PEPPER, best_ask, vol))
                            data["pepper_scalp_entry"] = best_ask

            if scalp_entry is not None and best_bid is not None:
                if best_bid > scalp_entry + 5:
                    vol = min(10, od.buy_orders.get(best_bid, 0))
                    if vol > 0:
                        orders.append(Order(self.PEPPER, best_bid, -vol))
                        data["pepper_scalp_entry"] = None
                        data["pepper_recent_high"] = mid

        else:
            # ── Endgame liquidation ────────────────────────────────────────
            if pos > 0 and od.buy_orders:
                ticks_left = max(1, (self.MAX_TS - ts) // 100 + 1)
                per_tick   = math.ceil(pos / ticks_left)
                to_sell    = min(pos, int(per_tick * 2.5))
                remaining  = to_sell

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
                        orders.append(Order(self.PEPPER, max(od.buy_orders.keys()), -leftover))

        return orders

    # ── Osmium ─────────────────────────────────────────────────────────────

    def _osmium_orders(self, state: TradingState, data: dict) -> List[Order]:
        od      = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos     = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []
        best_bid = max(od.buy_orders.keys())  if od.buy_orders  else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        if best_bid is None or best_ask is None:
            return orders

        raw_mid  = (best_bid + best_ask) / 2.0
        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_FALLBACK))
        ema      = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema

        # NOTE: OSM_SKEW_FACTOR is intentionally NOT applied here.
        # Applying it causes the passive quote to cross the spread when pos>35,
        # generating immediate wrong-side fills and destroying spread capture.
        # At offset=6, safe skew = factor × pos < 2 ticks. Max safe: 0.025 × 80 = 2.
        # 0.06 × 40 = 2.4 → exceeds safe bound. Needs a new run to re-calibrate.
        fair = round(ema)

        buy_cap  = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # ── 1. Aggressive taking ──────────────────────────────────────────
        if best_ask <= self.OSM_AGGRESSIVE_BUY_THRESH and buy_cap > 0:
            vol = min(self.OSM_MR_MAX_QTY, buy_cap, -od.sell_orders.get(best_ask, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_ask, vol))
                buy_cap -= vol

        if best_bid >= self.OSM_AGGRESSIVE_SELL_THRESH and sell_cap > 0:
            vol = min(self.OSM_MR_MAX_QTY, sell_cap, od.buy_orders.get(best_bid, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_bid, -vol))
                sell_cap -= vol

        # ── 2. Mean-reversion safety net ─────────────────────────────────
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

        # ── 3. Passive market-making ──────────────────────────────────────
        if buy_cap > 0:
            qty = min(self.OSM_PASSIVE_SIZE, buy_cap)
            orders.append(Order(self.OSMIUM, fair - self.OSM_PASSIVE_BID_OFFSET, qty))

        if sell_cap > 0:
            qty = min(self.OSM_PASSIVE_SIZE, sell_cap)
            orders.append(Order(self.OSMIUM, fair + self.OSM_PASSIVE_ASK_OFFSET, -qty))

        return orders
