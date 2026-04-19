# trader.py — IMC Prosperity 4, Round 2 (Claude‑Optimized)
# Forensic analysis applied to 8,434 baseline.

import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order

class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    
    LIMIT = 80

    # ── PEPPER (Claude‑optimized) ─────────────────────────────────────
    PEPPER_SLOPE   = 0.001
    PEPPER_BUY_TOL = 10           # Restrict to best ask only (avoid overpaying)
    ENDGAME_START  = 94_000      # Catch the peak window (Claude recommendation)

    # ── OSMIUM (Claude‑optimized) ─────────────────────────────────────
    OSM_FAIR_FALLBACK = 10_000
    OSM_EMA_ALPHA     = 0.02
    OSM_PASSIVE_BID_OFFSET = 3   # Fixed tight offset (was dynamic)
    OSM_PASSIVE_ASK_OFFSET = 3   # New: passive ask at fair+2
    OSM_PASSIVE_SIZE   = 21      # Keep baseline size
    OSM_AGGRESSIVE_BUY_THRESH  = 9_998  # Raised per Claude
    OSM_AGGRESSIVE_SELL_THRESH = 10_004  # Lowered per Claude
    OSM_AGGRESSIVE_SIZE = 26      # Keep baseline
    OSM_MR_THRESH     = 8         # Keep original MR as safety net
    OSM_MR_MAX_QTY    = 24
    OSM_SKEW_FACTOR   = 0.06

    # ── TIMING ────────────────────────────────────────────────────────
    ROUND_DAYS     = 3
    MAX_TS         = 99_900
    NEW_DAY_THRESH = 10_000

    # ──────────────────────────────────────────────────────────────────
    def bid(self) -> int:
        return 6_500

    # ──────────────────────────────────────────────────────────────────
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
            data.pop("pepper_anchor", None)
            data.pop("pepper_recent_high", None)
            data.pop("pepper_scalp_entry", None)

        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_fair = data["pepper_anchor"] + self.PEPPER_SLOPE * ts

        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame  = is_last_day and (ts >= self.ENDGAME_START)

        orders: Dict[str, List[Order]] = {}

        pepper_ords = self._pepper_orders(state, ts, pepper_fair, is_endgame, data)
        if pepper_ords:
            orders[self.PEPPER] = pepper_ords

        osmium_ords = self._osmium_orders(state, data)
        if osmium_ords:
            orders[self.OSMIUM] = osmium_ords

        data["last_ts"] = ts
        conversions = 0
        trader_data = json.dumps(data)
        return orders, conversions, trader_data

    # ──────────────────────────────────────────────────────────────────
    def _pepper_anchor(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    def _pepper_orders(self, state: TradingState, ts: int, fair: float,
                       is_endgame: bool, data: dict) -> List[Order]:
        od  = state.order_depths.get(self.PEPPER, OrderDepth())
        pos = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else fair

        if not is_endgame:
            buy_cap = self.LIMIT - pos
            if buy_cap > 0 and od.sell_orders:
                # Only take the best ask if within tight tolerance
                if best_ask and best_ask <= fair + self.PEPPER_BUY_TOL:
                    vol = min(buy_cap, -od.sell_orders[best_ask])
                    if vol > 0:
                        orders.append(Order(self.PEPPER, best_ask, vol))
                        buy_cap -= vol
                # If still need more, post a passive bid at best_ask - 1
                if buy_cap > 0 and best_ask:
                    orders.append(Order(self.PEPPER, best_ask - 1, buy_cap))

            # ─── SCALP (unchanged) ───────────────────────────────────────
            if "pepper_recent_high" not in data:
                data["pepper_recent_high"] = mid
            else:
                data["pepper_recent_high"] = max(data["pepper_recent_high"], mid)

            scalp_entry = data.get("pepper_scalp_entry")
            if scalp_entry is None and best_ask and best_ask < data["pepper_recent_high"] - 8:
                remaining_cap = self.LIMIT - pos
                if remaining_cap > 0:
                    vol = min(10, remaining_cap, -od.sell_orders.get(best_ask, 0))
                    if vol > 0:
                        orders.append(Order(self.PEPPER, best_ask, vol))
                        data["pepper_scalp_entry"] = best_ask

            if scalp_entry is not None and best_bid and best_bid > scalp_entry + 5:
                vol = min(10, od.buy_orders.get(best_bid, 0))
                if vol > 0:
                    orders.append(Order(self.PEPPER, best_bid, -vol))
                    data["pepper_scalp_entry"] = None
                    data["pepper_recent_high"] = mid

        else:
            # Endgame unwind (unchanged)
            if pos > 0 and od.buy_orders:
                ticks_left    = max(1, (self.MAX_TS - ts) // 100 + 1)
                per_tick_sell = math.ceil(pos / ticks_left)
                to_sell       = min(pos, int(per_tick_sell * 2.5))
                remaining = to_sell
                for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                    if remaining <= 0:
                        break
                    vol = min(remaining, od.buy_orders[bid_px])
                    if vol > 0:
                        orders.append(Order(self.PEPPER, bid_px, -vol))
                        remaining -= vol
                if ts >= self.MAX_TS - 100 and pos > 0:
                    leftover = pos - to_sell + remaining
                    if leftover > 0 and od.buy_orders:
                        best_bid = max(od.buy_orders.keys())
                        orders.append(Order(self.PEPPER, best_bid, -leftover))

        return orders

    # ──────────────────────────────────────────────────────────────────
    def _osmium_orders(self, state: TradingState, data: dict) -> List[Order]:
        od  = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []

        best_bid: Optional[int] = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask: Optional[int] = min(od.sell_orders.keys()) if od.sell_orders else None

        if best_bid is None or best_ask is None:
            return orders

        raw_mid = (best_bid + best_ask) / 2.0
        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_FALLBACK))
        ema = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = round(ema)

        buy_cap  = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # Aggressive taking (Claude‑optimized thresholds)
        if best_ask <= self.OSM_AGGRESSIVE_BUY_THRESH and buy_cap > 0:
            vol = min(self.OSM_AGGRESSIVE_SIZE, buy_cap, -od.sell_orders.get(best_ask, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_ask, vol))
                buy_cap -= vol

        if best_bid >= self.OSM_AGGRESSIVE_SELL_THRESH and sell_cap > 0:
            vol = min(self.OSM_AGGRESSIVE_SIZE, sell_cap, od.buy_orders.get(best_bid, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_bid, -vol))
                sell_cap -= vol

        # Passive market making (Claude‑optimized: fixed tight offsets)
        if buy_cap > 0:
            bid_px = fair - self.OSM_PASSIVE_BID_OFFSET
            qty = min(self.OSM_PASSIVE_SIZE, buy_cap)
            orders.append(Order(self.OSMIUM, bid_px, qty))
            buy_cap -= qty

        if sell_cap > 0:
            ask_px = fair + self.OSM_PASSIVE_ASK_OFFSET
            qty = min(self.OSM_PASSIVE_SIZE, sell_cap)
            orders.append(Order(self.OSMIUM, ask_px, -qty))
            sell_cap -= qty

        # Original mean reversion (safety net, kept but likely less active)
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

        return orders