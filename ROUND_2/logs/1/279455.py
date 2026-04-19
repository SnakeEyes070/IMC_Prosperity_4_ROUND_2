# trader.py — IMC Prosperity 4, Round 2 (Enhanced from 6,294 Round 1 Baseline)
# Products: INTARIAN_PEPPER_ROOT (trend-follow) + ASH_COATED_OSMIUM (dynamic‑spread MM)
# Position limits: 80 each (up from 50)
# New: bid() for Market Access Fee (MAF) blind auction

import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order

class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    
    # ── Position limit increased to 80 for Round 2 ────────────────────────
    LIMIT = 80

    # ── PEPPER PARAMETERS (scaled for limit 80) ──────────────────────────
    PEPPER_SLOPE   = 0.001        # confirmed linear uptrend
    PEPPER_BUY_TOL = 15           # wide tolerance to fill 80 units quickly
    PEPPER_MAX_BUY = 80           # target full long position

    # ── OSMIUM PARAMETERS (dynamic spread capture, scaled sizes) ─────────
    OSM_FAIR_FALLBACK = 10_000
    OSM_EMA_ALPHA     = 0.015
    OSM_L1_SIZE       = 18        # increased from 15 (more capacity)
    OSM_L2_SIZE       = 14        # increased from 12
    OSM_L3_SIZE       = 10        # increased from 8
    OSM_MR_THRESH     = 8         # mean‑reversion threshold
    OSM_MR_MAX_QTY    = 24        # increased from 20
    OSM_SKEW_FACTOR   = 0.06

    # ── TIMING (consistent with Round 1) ─────────────────────────────────
    ROUND_DAYS     = 3
    MAX_TS         = 99_900
    ENDGAME_START  = 96_500       # earlier unwind for smoother exit
    NEW_DAY_THRESH = 10_000

    # ──────────────────────────────────────────────────────────────────────
    # bid() – Market Access Fee for Round 2
    #
    # Rationale:
    #   • 25% more quotes ≈ 25% more passive fill opportunities on Osmium.
    #   • Osmium dynamic MM captures ~6–8 ticks per round trip on ~3–5 units/tick.
    #   • Extra flow could add 15k–30k XIRECs gross; net after fee must be positive.
    #   • Field median expected ~2,500–4,500. Bidding 5,500 clears top 50% with margin.
    #   • Break‑even analysis: even if extra profit is only 10k, fee of 5,500 is +EV.
    # ──────────────────────────────────────────────────────────────────────
    def bid(self) -> int:
        return 5500

    # ──────────────────────────────────────────────────────────────────────
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts      = state.timestamp
        prev_ts = data.get("last_ts", -1)
        day     = data.get("day", 0)

        # Day boundary detection (timestamp reset)
        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day
            data.pop("pepper_anchor", None)

        # Pepper anchor (first tick of each day)
        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_fair = data["pepper_anchor"] + self.PEPPER_SLOPE * ts

        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame  = is_last_day and (ts >= self.ENDGAME_START)

        orders: Dict[str, List[Order]] = {}

        pepper_ords = self._pepper_orders(state, ts, pepper_fair, is_endgame)
        if pepper_ords:
            orders[self.PEPPER] = pepper_ords

        osmium_ords = self._osmium_orders(state, data)
        if osmium_ords:
            orders[self.OSMIUM] = osmium_ords

        data["last_ts"] = ts
        conversions = 0
        trader_data = json.dumps(data)
        return orders, conversions, trader_data

    # ──────────────────────────────────────────────────────────────────────
    # PEPPER – Anchor at day open (best ask)
    # ──────────────────────────────────────────────────────────────────────
    def _pepper_anchor(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    # ──────────────────────────────────────────────────────────────────────
    # PEPPER – Aggressive accumulation + gradual unwind
    # ──────────────────────────────────────────────────────────────────────
    def _pepper_orders(self, state: TradingState, ts: int, fair: float,
                       is_endgame: bool) -> List[Order]:
        od  = state.order_depths.get(self.PEPPER, OrderDepth())
        pos = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []

        if not is_endgame:
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
                # Post passive bid to catch any remaining flow
                if buy_cap > 0 and od.sell_orders:
                    best_ask = min(od.sell_orders.keys())
                    orders.append(Order(self.PEPPER, best_ask, buy_cap))
        else:
            # Gradual unwind: sell into bids over remaining ticks
            if pos > 0 and od.buy_orders:
                ticks_left    = max(1, (self.MAX_TS - ts) // 100 + 1)
                per_tick_sell = math.ceil(pos / ticks_left)
                to_sell       = min(pos, per_tick_sell * 2)
                remaining = to_sell
                for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                    if remaining <= 0:
                        break
                    vol = min(remaining, od.buy_orders[bid_px])
                    if vol > 0:
                        orders.append(Order(self.PEPPER, bid_px, -vol))
                        remaining -= vol
                # Final tick safety: force‑sell any leftover
                if ts >= self.MAX_TS - 100 and pos > 0:
                    leftover = pos - to_sell + remaining
                    if leftover > 0 and od.buy_orders:
                        best_bid = max(od.buy_orders.keys())
                        orders.append(Order(self.PEPPER, best_bid, -leftover))
        return orders

    # ──────────────────────────────────────────────────────────────────────
    # OSMIUM – Dynamic spread market making + mean reversion
    # ──────────────────────────────────────────────────────────────────────
    def _osmium_orders(self, state: TradingState, data: dict) -> List[Order]:
        od  = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []

        best_bid: Optional[int] = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask: Optional[int] = min(od.sell_orders.keys()) if od.sell_orders else None

        if best_bid is not None and best_ask is not None:
            raw_mid = (best_bid + best_ask) / 2.0
            current_spread = best_ask - best_bid
        elif best_bid is not None:
            raw_mid = best_bid + 4
            current_spread = 16
        elif best_ask is not None:
            raw_mid = best_ask - 4
            current_spread = 16
        else:
            raw_mid = float(self.OSM_FAIR_FALLBACK)
            current_spread = 16

        # Slow EMA fair value
        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_FALLBACK))
        ema = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = round(ema)

        buy_cap  = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # Aggressive mean reversion (take mispriced quotes)
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

        # Inventory skew (shift quotes to reduce position)
        skew = int(max(-6, min(6, pos * self.OSM_SKEW_FACTOR)))

        # Dynamic offsets – adapt to current spread
        spread_factor = current_spread / 16.0
        dyn_l1 = max(3, min(5, round(4 * spread_factor)))
        dyn_l2 = max(5, min(9, round(7 * spread_factor)))
        dyn_l3 = max(9, min(13, round(11 * spread_factor)))

        levels = [
            (dyn_l1, self.OSM_L1_SIZE),
            (dyn_l2, self.OSM_L2_SIZE),
            (dyn_l3, self.OSM_L3_SIZE),
        ]

        long_bias = pos / self.LIMIT

        for offset, base_size in levels:
            if buy_cap <= 0 and sell_cap <= 0:
                break

            bid_px = fair - offset - skew
            ask_px = fair + offset - skew

            if bid_px >= ask_px:
                bid_px = fair - 1
                ask_px = fair + 1

            buy_size  = max(1, round(base_size * (1 - max(0,  long_bias) * 0.6)))
            sell_size = max(1, round(base_size * (1 - max(0, -long_bias) * 0.6)))

            if buy_cap > 0 and bid_px > 0:
                vol = min(buy_size, buy_cap)
                orders.append(Order(self.OSMIUM, bid_px, vol))
                buy_cap -= vol

            if sell_cap > 0:
                vol = min(sell_size, sell_cap)
                orders.append(Order(self.OSMIUM, ask_px, -vol))
                sell_cap -= vol

        return orders