# trader_v3.py — IMC Prosperity 4, Round 2
# Diagnosis from log 297069 (Day 1 result: Pepper=7292, Osmium=994, Total=8286):
#
# PEPPER  ✓ Perfect — buys 80 units in first 300 ticks, rides full uptrend.
#           No changes needed; it is already at theoretical maximum for day 1.
#
# OSMIUM  ✗ Only 994 profit. Root cause identified from log:
#   1. Our bid at fair-5 ≈ 9,995 is BEHIND the market's best_bid (≥9,997)
#      61.7% of the time → sellers cross the market and hit THEIR best bid,
#      not ours. We miss all that buy-side volume.
#   2. Our ask at fair+3 ≈ 10,003 is already the best ask in the book
#      (market ask ≈ 10,009-10,018) → sell side is fine.
#   3. Two dead periods (ts 27k-30.5k, 36.7k-38k) where nobody traded at all;
#      cannot fix those, they are a market-structure constraint.
#
# FIX: "Penny" the market bid → quote at best_bid+1 so we are always the
#      best bid in the book. Sellers crossing the spread hit us first.
#      Keep ask at fair+3 (already inside market). Round-trip spread
#      captured drops from 8 → 5 ticks but filled volume roughly doubles,
#      net effect: +500-1,000 Osmium profit → total target ≥ 9,000.

import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order


class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"

    LIMIT = 80

    # ── PEPPER ────────────────────────────────────────────────────────
    PEPPER_SLOPE   = 0.001
    PEPPER_BUY_TOL = 18            # buy up to fair+18 (generous, price trends up)
    ENDGAME_START  = 93_000        # last-day unwind start (unused on days 1-2)

    # Dip-scalp (rides mean-reversion within the trend)
    DIP_TRIGGER    = 8             # entry: price dips 8+ ticks below recent high
    DIP_EXIT       = 7             # exit: price recovers 7+ ticks above entry
    DIP_QTY        = 12            # scalp lot size

    # ── OSMIUM ────────────────────────────────────────────────────────
    OSM_FAIR_FALLBACK = 10_000
    OSM_EMA_ALPHA     = 0.008      # slow EMA → stable fair value
    OSM_MR_THRESH     = 5          # aggressive MR only on true mispricings
    OSM_MR_MAX_QTY    = 20

    # Market-making sizes (slightly larger to capture more when market is live)
    OSM_L1_SIZE    = 20
    OSM_L2_SIZE    = 15
    OSM_L3_SIZE    = 10

    # Inventory skew (lighter, was over-correcting)
    OSM_SKEW_FACTOR = 0.03

    # ── TIMING ────────────────────────────────────────────────────────
    ROUND_DAYS     = 3
    MAX_TS         = 99_900
    NEW_DAY_THRESH = 10_000

    # ─────────────────────────────────────────────────────────────────
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts      = state.timestamp
        prev_ts = data.get("last_ts", -1)
        day     = data.get("day", 0)

        # Day rollover detection
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

        pepper_ords = self._pepper_orders(state, ts, pepper_fair, is_endgame, data)
        if pepper_ords:
            orders[self.PEPPER] = pepper_ords

        osmium_ords = self._osmium_orders(state, data)
        if osmium_ords:
            orders[self.OSMIUM] = osmium_ords

        data["last_ts"] = ts
        return orders, 0, json.dumps(data)

    # ─────────────────────────────────────────────────────────────────
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

        best_bid = max(od.buy_orders.keys())  if od.buy_orders  else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        mid = ((best_bid + best_ask) / 2.0
               if best_bid is not None and best_ask is not None else fair)

        if not is_endgame:
            # ── Core Accumulation ─────────────────────────────────────
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
                # Passive join at best ask for remainder (gets filled next tick)
                if buy_cap > 0 and best_ask is not None:
                    # Only join if price is within tolerance
                    if best_ask <= fair + self.PEPPER_BUY_TOL + 2:
                        orders.append(Order(self.PEPPER, best_ask, buy_cap))

            # ── Dip Scalp ─────────────────────────────────────────────
            if "pepper_recent_high" not in data:
                data["pepper_recent_high"] = mid
            else:
                data["pepper_recent_high"] = max(data["pepper_recent_high"], mid)

            scalp_entry = data.get("pepper_scalp_entry")

            # Entry: price dips DIP_TRIGGER ticks from recent high
            if (scalp_entry is None
                    and best_ask is not None
                    and best_ask < data["pepper_recent_high"] - self.DIP_TRIGGER):
                remaining_cap = self.LIMIT - pos
                if remaining_cap > 0:
                    avail = -od.sell_orders.get(best_ask, 0)
                    vol   = min(self.DIP_QTY, remaining_cap, avail)
                    if vol > 0:
                        orders.append(Order(self.PEPPER, best_ask, vol))
                        data["pepper_scalp_entry"] = best_ask

            # Exit: price recovers DIP_EXIT ticks above entry
            if (scalp_entry is not None
                    and best_bid is not None
                    and best_bid > scalp_entry + self.DIP_EXIT):
                avail = od.buy_orders.get(best_bid, 0)
                vol   = min(self.DIP_QTY, avail)
                if vol > 0:
                    orders.append(Order(self.PEPPER, best_bid, -vol))
                    data["pepper_scalp_entry"] = None
                    data["pepper_recent_high"] = mid

        else:
            # ── Endgame Unwind (last day only) ────────────────────────
            if pos > 0 and od.buy_orders:
                ticks_left    = max(1, (self.MAX_TS - ts) // 100 + 1)
                per_tick_sell = math.ceil(pos / ticks_left)
                to_sell   = min(pos, int(per_tick_sell * 3.0))
                remaining = to_sell
                for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                    if remaining <= 0:
                        break
                    vol = min(remaining, od.buy_orders[bid_px])
                    if vol > 0:
                        orders.append(Order(self.PEPPER, bid_px, -vol))
                        remaining -= vol
                # Emergency flush in final tick
                if ts >= self.MAX_TS - 100 and pos > 0:
                    leftover = pos - to_sell + remaining
                    if leftover > 0 and od.buy_orders:
                        orders.append(Order(
                            self.PEPPER, max(od.buy_orders.keys()), -leftover))

        return orders

    # ─────────────────────────────────────────────────────────────────
    def _osmium_orders(self, state: TradingState, data: dict) -> List[Order]:
        od  = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []

        best_bid: Optional[int] = max(od.buy_orders.keys())  if od.buy_orders  else None
        best_ask: Optional[int] = min(od.sell_orders.keys()) if od.sell_orders else None

        if best_bid is not None and best_ask is not None:
            raw_mid        = (best_bid + best_ask) / 2.0
            current_spread = best_ask - best_bid
        elif best_bid is not None:
            raw_mid, current_spread = best_bid + 4, 16
        elif best_ask is not None:
            raw_mid, current_spread = best_ask - 4, 16
        else:
            raw_mid, current_spread = float(self.OSM_FAIR_FALLBACK), 16

        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_FALLBACK))
        ema = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = round(ema)

        buy_cap  = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # ── Aggressive Mean Reversion (true mispricings only) ─────────
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

        # ── Market Making — "Penny the Market" strategy ───────────────
        #
        # KEY FIX (from log analysis):
        #   Our old L1 bid at fair-5 ≈ 9,995 was BEHIND the market best_bid
        #   (≥9,997) 61.7% of the time → sellers crossed the spread and hit
        #   the market, not us → zero buy-side fills.
        #
        #   Our L1 ask at fair+3 ≈ 10,003 was already INSIDE the market
        #   (market ask ≈ 10,009-10,018) → sell-side was competitive. ✓
        #
        #   New strategy: penny the market on the bid side (best_bid+1) so
        #   we are always the best bid. Keep the competitive ask. Result:
        #   spread captured per round-trip drops from 8→5 ticks, but
        #   filled volume ~doubles → net Osmium profit target ≥ 1,500.

        skew = int(max(-4, min(4, pos * self.OSM_SKEW_FACTOR)))

        if best_bid is not None and best_ask is not None and current_spread >= 4:
            # ── Penny the bid: be the best bid in the book ────────────
            l1_bid_px = best_bid + 1 - skew
            # ── Keep ask inside market spread (already competitive) ───
            # Ask at fair+3 is typically 6-8 ticks inside market ask
            l1_ask_px = fair + 3 - skew

            # Safety: never let bid ≥ ask, and never quote above/below fair
            l1_bid_px = min(l1_bid_px, fair - 1)   # don't buy above fair
            l1_ask_px = max(l1_ask_px, fair + 1)   # don't sell below fair
            if l1_bid_px >= l1_ask_px:
                l1_bid_px = fair - 1
                l1_ask_px = fair + 1

            long_bias = pos / self.LIMIT
            buy_size  = max(1, round(self.OSM_L1_SIZE * (1 - max(0,  long_bias) * 0.6)))
            sell_size = max(1, round(self.OSM_L1_SIZE * (1 - max(0, -long_bias) * 0.6)))

            if buy_cap > 0 and l1_bid_px > 0:
                vol = min(buy_size, buy_cap)
                orders.append(Order(self.OSMIUM, l1_bid_px,  vol))
                buy_cap -= vol

            if sell_cap > 0:
                vol = min(sell_size, sell_cap)
                orders.append(Order(self.OSMIUM, l1_ask_px, -vol))
                sell_cap -= vol

            # ── L2 / L3: deeper passive quotes (fallback liquidity) ───
            spread_factor = current_spread / 16.0
            base_l2 = max(5, min(9, round(7 * spread_factor)))
            base_l3 = max(9, min(13, round(11 * spread_factor)))

            for b_off, a_off, sz in [
                (base_l2, base_l2, self.OSM_L2_SIZE),
                (base_l3, base_l3, self.OSM_L3_SIZE),
            ]:
                if buy_cap <= 0 and sell_cap <= 0:
                    break
                bid_px = fair - b_off - skew
                ask_px = fair + a_off - skew
                if bid_px >= ask_px:
                    bid_px, ask_px = fair - 1, fair + 1

                buy_size_l  = max(1, round(sz * (1 - max(0,  long_bias) * 0.6)))
                sell_size_l = max(1, round(sz * (1 - max(0, -long_bias) * 0.6)))

                if buy_cap > 0 and bid_px > 0:
                    vol = min(buy_size_l, buy_cap)
                    orders.append(Order(self.OSMIUM, bid_px,  vol))
                    buy_cap -= vol

                if sell_cap > 0:
                    vol = min(sell_size_l, sell_cap)
                    orders.append(Order(self.OSMIUM, ask_px, -vol))
                    sell_cap -= vol

        else:
            # ── Fallback: spread is too tight or no book — quote at fair ±1
            if buy_cap > 0:
                orders.append(Order(self.OSMIUM, fair - 1,  min(self.OSM_L1_SIZE, buy_cap)))
            if sell_cap > 0:
                orders.append(Order(self.OSMIUM, fair + 1, -min(self.OSM_L1_SIZE, sell_cap)))

        return orders
