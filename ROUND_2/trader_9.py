# trader.py — IMC Prosperity 4, Round 2
# Strategy: Asymmetric Multi‑Level MM (Osmium) + Aggressive Trend Capture (Pepper)
# Target: 12,000–15,000 XIRECs

import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order


class Trader:

    # ── Products ────────────────────────────────────────────────
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"

    # ── Position limit (Round 2) ────────────────────────────────
    LIMIT = 80

    # ═══════════════════════════════════════════════════════════════
    # PEPPER PARAMETERS (derived from 3‑day data capsule regression)
    # ═══════════════════════════════════════════════════════════════
    # Slope: 1,001 points over 9,900 ticks → 0.1011 per timestamp unit
    PEPPER_SLOPE   = 0.1011
    # Spread averages 13–15 ticks. Tolerance of 14 guarantees L1 fill.
    PEPPER_BUY_TOL = 14
    # Start unwind at 9,650 to smoothly exit 80 units over ~3 ticks.
    ENDGAME_START  = 9_650

    # ═══════════════════════════════════════════════════════════════
    # OSMIUM PARAMETERS (asymmetric multi‑level MM)
    # ═══════════════════════════════════════════════════════════════
    # Fair value anchored at 10,001 (empirical mean).
    OSM_FAIR_INIT = 10_001
    # EMA alpha = 0.08 → half‑life ~8 ticks, optimal for –0.50 autocorr.
    OSM_EMA_ALPHA = 0.08

    # Asymmetric 6‑level quotes: (bid_offset, ask_offset, base_size)
    # Design: buy deeper (larger bid_offset) to catch dips,
    #         sell tighter (smaller ask_offset) to capture spread quickly.
    OSM_LEVELS = [
        (1, 1, 30),   # L1: symmetric near fair, highest fill rate
        (2, 1, 25),   # L2: buy slightly deeper, sell at fair+1
        (3, 1, 20),   # L3: deeper buy, still tight sell
        (4, 2, 15),   # L4: catch dips > half of typical move (σ=5)
        (6, 2, 10),   # L5: mean‑reversion at ~1.2σ
        (8, 3,  8),   # L6: deep reversion at ~1.6σ
    ]

    # Aggressive mean‑reversion: take mispriced quotes immediately.
    # Threshold = 1 tick (58% reversion probability, positive EV).
    OSM_MR_THRESH = 1
    OSM_MR_SIZE   = 20

    # ═══════════════════════════════════════════════════════════════
    # DAY DETECTION
    # ═══════════════════════════════════════════════════════════════
    ROUND_DAYS     = 3
    MAX_TS         = 9_900
    NEW_DAY_THRESH = 1_000   # timestamp drops below this on day rollover

    # ═══════════════════════════════════════════════════════════════
    # MARKET ACCESS FEE (MAF) – Game Theory Optimal
    # ═══════════════════════════════════════════════════════════════
    # 25% extra flow → ~7,000–12,000 gross upside.
    # Field median estimated 2,500–4,000. Bidding 5,500 clears top 50%.
    def bid(self) -> int:
        return 5_500

    # ═══════════════════════════════════════════════════════════════
    # MAIN RUN METHOD
    # ═══════════════════════════════════════════════════════════════
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts      = state.timestamp
        prev_ts = data.get("prev_ts", -1)
        day     = data.get("day", 0)

        # Day boundary detection
        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"]    = day
            data["anchor"] = None   # recalculate Pepper anchor on new day

        # Pepper anchor: first available ask of the day
        if data.get("anchor") is None:
            data["anchor"] = self._first_ask(state)

        pepper_fair = data["anchor"] + self.PEPPER_SLOPE * ts

        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame  = is_last_day and (ts >= self.ENDGAME_START)

        orders: Dict[str, List[Order]] = {}

        # Pepper orders
        pep = self._pepper_orders(state, ts, pepper_fair, is_endgame)
        if pep:
            orders[self.PEPPER] = pep

        # Osmium orders
        osm = self._osmium_orders(state, data)
        if osm:
            orders[self.OSMIUM] = osm

        data["prev_ts"] = ts
        conversions = 0
        trader_data = json.dumps(data)
        return orders, conversions, trader_data

    # ═══════════════════════════════════════════════════════════════
    # PEPPER – Helper Methods
    # ═══════════════════════════════════════════════════════════════
    def _first_ask(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    def _pepper_orders(self, state: TradingState, ts: int,
                       fair: float, is_endgame: bool) -> List[Order]:
        od  = state.order_depths.get(self.PEPPER, OrderDepth())
        pos = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []

        if not is_endgame:
            # Aggressive accumulation: buy until limit 80
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

            # Fallback: if still not full, take best ask unconditionally
            if budget > 0 and od.sell_orders:
                best_ask = min(od.sell_orders.keys())
                orders.append(Order(self.PEPPER, best_ask, budget))

        else:
            # Graduated unwind: sell into bids over remaining ticks
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

            # Final tick safety: dump any residual
            if ts >= self.MAX_TS - 100 and pos > 0:
                residual = pos - (to_sell - left)
                if residual > 0 and od.buy_orders:
                    orders.append(Order(self.PEPPER, max(od.buy_orders.keys()), -residual))

        return orders

    # ═══════════════════════════════════════════════════════════════
    # OSMIUM – Asymmetric Multi‑Level Market Making
    # ═══════════════════════════════════════════════════════════════
    def _osmium_orders(self, state: TradingState, data: dict) -> List[Order]:
        od  = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []

        best_bid: Optional[int] = max(od.buy_orders.keys())  if od.buy_orders  else None
        best_ask: Optional[int] = min(od.sell_orders.keys()) if od.sell_orders else None

        if best_bid is None and best_ask is None:
            return orders

        # Compute mid price (fallback to ±8 if one side missing)
        if best_bid is not None and best_ask is not None:
            raw_mid = (best_bid + best_ask) / 2.0
        elif best_bid is not None:
            raw_mid = best_bid + 8.0
        else:
            raw_mid = best_ask - 8.0

        # Update EMA fair value
        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_INIT))
        ema      = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = int(round(ema))

        buy_cap  = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # ── Aggressive mean‑reversion fills ──
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

        # ── Passive asymmetric multi‑level quotes ──
        inv_ratio = pos / self.LIMIT   # ranges –1.0 to +1.0

        for (b_off, a_off, base_sz) in self.OSM_LEVELS:
            if buy_cap <= 0 and sell_cap <= 0:
                break

            our_bid = fair - b_off
            our_ask = fair + a_off

            # Safety: prevent crossed or zero/negative quotes
            if our_bid >= our_ask:
                our_bid = fair - 1
                our_ask = fair + 1
            if our_bid <= 0:
                our_bid = 1

            # Inventory‑aware sizing: cut size when position is skewed
            buy_sz  = max(1, round(base_sz * (1.0 - max(0.0,  inv_ratio) * 0.7)))
            sell_sz = max(1, round(base_sz * (1.0 - max(0.0, -inv_ratio) * 0.7)))

            # Post passive buy if market ask crosses down to our bid
            if best_ask is not None and best_ask <= our_bid and buy_cap > 0:
                vol = min(buy_sz, buy_cap, -od.sell_orders.get(best_ask, 0))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, our_bid, vol))
                    buy_cap -= vol

            # Post passive sell if market bid crosses up to our ask
            if best_bid is not None and best_bid >= our_ask and sell_cap > 0:
                vol = min(sell_sz, sell_cap, od.buy_orders.get(best_bid, 0))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, our_ask, -vol))
                    sell_cap -= vol

        return orders