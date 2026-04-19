# trader.py — IMC Prosperity 4, Round 2 (Stable Baseline + Micro‑Price)
# Reverts to original 8,199 data‑derived strategy, adds only volume‑weighted fair value.

import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order


class Trader:

    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"

    LIMIT = 80

    # ── Pepper (original data‑derived) ────────────────────────────
    PEPPER_SLOPE   = 0.101
    PEPPER_BUY_TOL = 12
    ENDGAME_START  = 9_600

    # ── Osmium EMA (original) ─────────────────────────────────────
    OSM_FAIR_INIT  = 10_001
    OSM_EMA_ALPHA  = 0.08

    # ── Osmium asymmetric levels (original) ───────────────────────
    OSM_LEVELS = [
        (1, 1, 30),
        (2, 1, 25),
        (3, 1, 20),
        (4, 2, 15),
        (6, 2, 10),
        (8, 3,  8),
    ]

    # ── Osmium aggressive mean reversion (original) ───────────────
    OSM_MR_THRESH = 1
    OSM_MR_SIZE   = 20

    # ── Day‑detection ─────────────────────────────────────────────
    ROUND_DAYS     = 3
    MAX_TS         = 9_900
    NEW_DAY_THRESH = 1_000

    # ──────────────────────────────────────────────────────────────
    def bid(self) -> int:
        return 5_500

    # ──────────────────────────────────────────────────────────────
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts      = state.timestamp
        prev_ts = data.get("prev_ts", -1)
        day     = data.get("day", 0)

        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"]    = day
            data["anchor"] = None

        if data.get("anchor") is None:
            data["anchor"] = self._first_ask(state)

        pepper_fair = data["anchor"] + self.PEPPER_SLOPE * ts

        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame  = is_last_day and (ts >= self.ENDGAME_START)

        orders: Dict[str, List[Order]] = {}

        pep = self._pepper_orders(state, ts, pepper_fair, is_endgame)
        if pep:
            orders[self.PEPPER] = pep

        osm = self._osmium_orders(state, data)
        if osm:
            orders[self.OSMIUM] = osm

        data["prev_ts"] = ts
        conversions = 0
        trader_data = json.dumps(data)
        return orders, conversions, trader_data

    # ──────────────────────────────────────────────────────────────
    def _first_ask(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    # ──────────────────────────────────────────────────────────────
    # PEPPER: original trend‑follow logic (unchanged)
    # ──────────────────────────────────────────────────────────────
    def _pepper_orders(self, state: TradingState, ts: int,
                       fair: float, is_endgame: bool) -> List[Order]:
        od  = state.order_depths.get(self.PEPPER, OrderDepth())
        pos = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []

        if not is_endgame:
            budget = self.LIMIT - pos
            if budget <= 0 or not od.sell_orders:
                return orders

            for ask_px in sorted(od.sell_orders.keys()):
                if budget <= 0:
                    break
                if ask_px <= fair + self.PEPPER_BUY_TOL:
                    vol = min(budget, -od.sell_orders[ask_px])
                    if vol > 0:
                        orders.append(Order(self.PEPPER, ask_px, vol))
                        budget -= vol

            if budget > 0 and od.sell_orders:
                best_ask = min(od.sell_orders.keys())
                orders.append(Order(self.PEPPER, best_ask, budget))

        else:
            if pos <= 0 or not od.buy_orders:
                return orders

            ticks_remaining = max(1, (self.MAX_TS - ts) // 100 + 1)
            to_sell = min(pos, math.ceil(pos / ticks_remaining) * 2)
            left    = to_sell

            for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                if left <= 0:
                    break
                vol = min(left, od.buy_orders[bid_px])
                if vol > 0:
                    orders.append(Order(self.PEPPER, bid_px, -vol))
                    left -= vol

            if ts >= self.MAX_TS - 100 and pos > 0:
                residual = pos - (to_sell - left)
                if residual > 0 and od.buy_orders:
                    orders.append(Order(self.PEPPER, max(od.buy_orders.keys()), -residual))

        return orders

    # ──────────────────────────────────────────────────────────────
    # OSMIUM: original asymmetric MM + micro‑price fair value
    # ──────────────────────────────────────────────────────────────
    def _osmium_orders(self, state: TradingState, data: dict) -> List[Order]:
        od  = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []

        best_bid: Optional[int] = max(od.buy_orders.keys())  if od.buy_orders  else None
        best_ask: Optional[int] = min(od.sell_orders.keys()) if od.sell_orders else None

        if best_bid is None and best_ask is None:
            return orders

        # === MICRO‑PRICE FAIR VALUE (ONLY UPGRADE) ===
        if best_bid is not None and best_ask is not None:
            bv = od.buy_orders[best_bid]
            av = -od.sell_orders[best_ask]
            if bv + av > 0:
                raw_mid = (best_bid * av + best_ask * bv) / (bv + av)
            else:
                raw_mid = (best_bid + best_ask) / 2.0
        elif best_bid is not None:
            raw_mid = best_bid + 8.0
        else:
            raw_mid = best_ask - 8.0
        # === END MICRO‑PRICE ===

        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_INIT))
        ema      = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = int(round(ema))

        buy_cap  = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # Aggressive fills
        if best_ask is not None and best_ask <= fair - self.OSM_MR_THRESH and buy_cap > 0:
            vol = min(self.OSM_MR_SIZE, buy_cap, -od.sell_orders[best_ask])
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_ask, vol))
                buy_cap -= vol

        if best_bid is not None and best_bid >= fair + self.OSM_MR_THRESH and sell_cap > 0:
            vol = min(self.OSM_MR_SIZE, sell_cap, od.buy_orders[best_bid])
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_bid, -vol))
                sell_cap -= vol

        # Passive multi‑level asymmetric quotes
        inv_ratio = pos / self.LIMIT

        for (b_off, a_off, base_sz) in self.OSM_LEVELS:
            if buy_cap <= 0 and sell_cap <= 0:
                break

            our_bid = fair - b_off
            our_ask = fair + a_off

            if our_bid >= our_ask:
                our_bid = fair - 1
                our_ask = fair + 1
            if our_bid <= 0:
                our_bid = 1

            buy_sz  = max(1, round(base_sz * (1.0 - max(0.0,  inv_ratio) * 0.7)))
            sell_sz = max(1, round(base_sz * (1.0 - max(0.0, -inv_ratio) * 0.7)))

            if best_ask is not None and best_ask <= our_bid and buy_cap > 0:
                vol = min(buy_sz, buy_cap, -od.sell_orders.get(best_ask, 0))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, our_bid, vol))
                    buy_cap -= vol

            if best_bid is not None and best_bid >= our_ask and sell_cap > 0:
                vol = min(sell_sz, sell_cap, od.buy_orders.get(best_bid, 0))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, our_ask, -vol))
                    sell_cap -= vol

        return orders