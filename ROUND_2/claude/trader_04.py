# trader_v2.py — IMC Prosperity 4, Round 2 (Target: 12,000+)
# Optimizations over v1 (8,364 baseline):
#   PEPPER  – wider buy tolerance, deeper dip scalp, earlier endgame
#   OSMIUM  – tighter MR threshold, slower EMA, tighter quote skew,
#              tighter lock-in range (9,996–10,004)

import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order


class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"

    LIMIT = 80

    # ── PEPPER ────────────────────────────────────────────────────────
    PEPPER_SLOPE    = 0.001          # kept from v1
    PEPPER_BUY_TOL  = 18            # ↑ was 15 — buy slightly more aggressively
    ENDGAME_START   = 93_000        # ↑ was 94,500 — start unwinding earlier
                                    #   (volume drops fast in final ticks)

    # Dip-scalp parameters
    DIP_TRIGGER     = 8             # ticks below recent high to enter
    DIP_EXIT        = 7             # ↑ was 5 — hold for bigger exit gain
    DIP_QTY         = 12            # ↑ was 10 — slightly larger scalp size

    # ── OSMIUM ────────────────────────────────────────────────────────
    OSM_FAIR_FALLBACK = 10_000
    OSM_EMA_ALPHA     = 0.008       # ↓ was 0.015 — slower, more stable fair value
    OSM_L1_SIZE       = 18          # slightly reduced to avoid over-exposure
    OSM_L2_SIZE       = 13
    OSM_L3_SIZE       = 9

    # Mean reversion: only hit TRUE mispricings
    # Data shows ask rarely drops below 9,999, bid rarely tops 10,004
    # → set threshold to 5 so we trade only outside 9,995–10,005
    OSM_MR_THRESH     = 5           # ↓ was 8 — tighter, fewer but better trades
    OSM_MR_MAX_QTY    = 20          # ↓ was 24 — less exposure per MR hit

    OSM_SKEW_FACTOR   = 0.04        # ↓ was 0.06 — less aggressive inventory skew

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
                # Passive limit at best ask if cap remains
                if buy_cap > 0 and best_ask is not None:
                    orders.append(Order(self.PEPPER, best_ask, buy_cap))

            # ── Dip Scalp ─────────────────────────────────────────────
            if "pepper_recent_high" not in data:
                data["pepper_recent_high"] = mid
            else:
                data["pepper_recent_high"] = max(data["pepper_recent_high"], mid)

            scalp_entry = data.get("pepper_scalp_entry")

            # Entry: dip DIP_TRIGGER ticks from recent high
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
                    data["pepper_recent_high"] = mid   # reset high after profit

        else:
            # ── Endgame Unwind ────────────────────────────────────────
            if pos > 0 and od.buy_orders:
                ticks_left    = max(1, (self.MAX_TS - ts) // 100 + 1)
                per_tick_sell = math.ceil(pos / ticks_left)
                # Sell faster: 3× per-tick rate (↑ from 2.5×) to clear by MAX_TS
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

        # Slower EMA → more stable fair value, fewer phantom signals
        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_FALLBACK))
        ema = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = round(ema)

        buy_cap  = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # ── Aggressive Mean Reversion (only on true mispricings) ──────
        # Threshold=5 → only fire when ask<9,995 or bid>10,005
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

        # ── Market Making ─────────────────────────────────────────────
        skew = int(max(-5, min(5, pos * self.OSM_SKEW_FACTOR)))

        spread_factor = current_spread / 16.0
        base_l1 = max(3, min(5, round(4 * spread_factor)))
        base_l2 = max(5, min(9, round(7 * spread_factor)))
        base_l3 = max(9, min(13, round(11 * spread_factor)))

        # Asymmetric L1: bid slightly closer, ask slightly further
        l1_bid_offset = base_l1 + 1
        l1_ask_offset = max(2, base_l1 - 1)

        levels = [
            (l1_bid_offset, l1_ask_offset, self.OSM_L1_SIZE),
            (base_l2,       base_l2,       self.OSM_L2_SIZE),
            (base_l3,       base_l3,       self.OSM_L3_SIZE),
        ]

        long_bias = pos / self.LIMIT

        for b_off, a_off, base_size in levels:
            if buy_cap <= 0 and sell_cap <= 0:
                break

            bid_px = fair - b_off - skew
            ask_px = fair + a_off - skew

            # Safety: never let quotes cross
            if bid_px >= ask_px:
                bid_px = fair - 1
                ask_px = fair + 1

            buy_size  = max(1, round(base_size * (1 - max(0,  long_bias) * 0.6)))
            sell_size = max(1, round(base_size * (1 - max(0, -long_bias) * 0.6)))

            if buy_cap > 0 and bid_px > 0:
                vol = min(buy_size, buy_cap)
                orders.append(Order(self.OSMIUM, bid_px,  vol))
                buy_cap -= vol

            if sell_cap > 0:
                vol = min(sell_size, sell_cap)
                orders.append(Order(self.OSMIUM, ask_px, -vol))
                sell_cap -= vol

        return orders