"""
IMC Prosperity 4 – Round 2 Trader
Products : ASH_COATED_OSMIUM  (mean-reverting, fair value ≈ 10 000)
           INTARIAN_PEPPER_ROOT (linear uptrend ≈ +1 000 per day)
Position limit : 80 for both
"""

import json
from typing import Dict, List, Tuple

from datamodel import Order, OrderDepth, TradingState


class Trader:
    # ── Product names ──────────────────────────────────────────────
    PEPPER  = "INTARIAN_PEPPER_ROOT"
    OSMIUM  = "ASH_COATED_OSMIUM"

    # ── Shared parameters ──────────────────────────────────────────
    POS_LIMIT  = 80          # max absolute position for each product

    # ── Pepper parameters ─────────────────────────────────────────
    PEPPER_SLOPE    = 0.001  # price gain per timestamp unit (~1 000 pts over 99 900 ticks)
    ENDGAME_START   = 96_500 # timestamp at which we start unwinding on the LAST day
    TOTAL_DAYS      = 3      # competition spans 3 days; day index 0-based

    # ── Osmium EMA parameters ─────────────────────────────────────
    OSM_ALPHA       = 0.07   # EMA smoothing factor  (half-life ≈ 10 ticks)
    OSM_FAIR_INIT   = 10_000 # fallback when EMA not yet seeded
    QUOTE_OFFSET    = 2      # passive quote distance from fair value
    AGG_THRESHOLD   = 1      # minimum edge to fill aggressively
    AGG_MAX_VOL     = 20     # max units per aggressive fill

    # ── Osmium one-sided fallback spread ──────────────────────────
    FALLBACK_OFFSET = 8      # used to infer mid when one side is empty

    # ── Market Access Fee bid ─────────────────────────────────────
    MAF_BID = 6_000          # targets top-50 % bracket → 25 % more quotes

    # ──────────────────────────────────────────────────────────────

    def bid(self) -> int:
        """Return MAF bid amount (deducted from final PnL only if accepted)."""
        return self.MAF_BID

    # ──────────────────────────────────────────────────────────────
    def run(
        self, state: TradingState
    ) -> Tuple[Dict[str, List[Order]], int, str]:
        """Main entry point called once per tick."""

        # ── 1. Load persisted state ────────────────────────────────
        try:
            td: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            td = {}

        day:     int   = td.get("day",     0)      # 0-based day counter
        prev_ts: int   = td.get("prev_ts", -1)     # timestamp of last tick
        osm_ema: float = td.get("osm_ema", float(self.OSM_FAIR_INIT))

        # ── 2. Day-boundary detection ──────────────────────────────
        # Timestamps run 0 → 99 900 each day; a reset means a new day.
        ts: int = state.timestamp
        if prev_ts > ts:          # timestamp went backwards → new day
            day += 1
        prev_ts = ts

        # Derived flag: are we on the final day?
        is_final_day: bool = (day >= self.TOTAL_DAYS - 1)

        # ── 3. Collect current positions ──────────────────────────
        pos: Dict[str, int] = {
            self.PEPPER:  state.position.get(self.PEPPER,  0),
            self.OSMIUM:  state.position.get(self.OSMIUM,  0),
        }

        # ── 4. Build order lists ───────────────────────────────────
        orders: Dict[str, List[Order]] = {self.PEPPER: [], self.OSMIUM: []}

        # ═══════════════════════════════════════════════════════════
        # INTARIAN PEPPER ROOT – Trend-ride strategy
        # ═══════════════════════════════════════════════════════════
        pepper_depth: OrderDepth = state.order_depths.get(self.PEPPER, OrderDepth())

        if is_final_day and ts >= self.ENDGAME_START:
            # ── Endgame: unwind all long by selling into best bid ──
            pepper_pos = pos[self.PEPPER]
            if pepper_pos > 0 and pepper_depth.buy_orders:
                best_bid = max(pepper_depth.buy_orders.keys())
                # Sell entire position (capped by available bid volume)
                bid_vol = pepper_depth.buy_orders[best_bid]
                sell_qty = min(pepper_pos, bid_vol)
                if sell_qty > 0:
                    orders[self.PEPPER].append(
                        Order(self.PEPPER, best_bid, -sell_qty)
                    )
        else:
            # ── Accumulation: sweep all asks until position = +80 ──
            pepper_pos = pos[self.PEPPER]
            remaining_buy = self.POS_LIMIT - pepper_pos   # how many more we can buy
            if remaining_buy > 0 and pepper_depth.sell_orders:
                # sell_orders is {price: -volume} (negative volumes)
                for ask_price in sorted(pepper_depth.sell_orders.keys()):
                    if remaining_buy <= 0:
                        break
                    ask_vol = -pepper_depth.sell_orders[ask_price]  # make positive
                    buy_qty = min(remaining_buy, ask_vol)
                    if buy_qty > 0:
                        orders[self.PEPPER].append(
                            Order(self.PEPPER, ask_price, buy_qty)
                        )
                        remaining_buy -= buy_qty

        # ═══════════════════════════════════════════════════════════
        # ASH-COATED OSMIUM – EMA mean-reversion market-making
        # ═══════════════════════════════════════════════════════════
        osm_depth: OrderDepth = state.order_depths.get(self.OSMIUM, OrderDepth())

        # ── a. Compute mid price ───────────────────────────────────
        has_bid = bool(osm_depth.buy_orders)
        has_ask = bool(osm_depth.sell_orders)

        if has_bid and has_ask:
            best_bid_osm = max(osm_depth.buy_orders.keys())
            best_ask_osm = min(osm_depth.sell_orders.keys())
            mid: float = (best_bid_osm + best_ask_osm) / 2.0
        elif has_bid:
            best_bid_osm = max(osm_depth.buy_orders.keys())
            best_ask_osm = None
            mid = best_bid_osm + self.FALLBACK_OFFSET   # infer mid from bid side
        elif has_ask:
            best_ask_osm = min(osm_depth.sell_orders.keys())
            best_bid_osm = None
            mid = best_ask_osm - self.FALLBACK_OFFSET   # infer mid from ask side
        else:
            best_bid_osm = None
            best_ask_osm = None
            mid = osm_ema  # no market data; keep previous EMA

        # ── b. Update EMA fair value ───────────────────────────────
        # ema = prev_ema + alpha * (mid - prev_ema)
        osm_ema = osm_ema + self.OSM_ALPHA * (mid - osm_ema)
        fair: int = round(osm_ema)

        # ── c. Aggressive fills: take mispriced quotes immediately ─
        osm_pos = pos[self.OSMIUM]

        if has_ask and best_ask_osm is not None:
            # Buy aggressively if best ask is below fair − threshold
            if best_ask_osm <= fair - self.AGG_THRESHOLD:
                buy_capacity = self.POS_LIMIT - osm_pos
                ask_vol = -osm_depth.sell_orders[best_ask_osm]
                agg_buy = min(buy_capacity, ask_vol, self.AGG_MAX_VOL)
                if agg_buy > 0:
                    orders[self.OSMIUM].append(
                        Order(self.OSMIUM, best_ask_osm, agg_buy)
                    )
                    osm_pos += agg_buy  # track position change within tick

        if has_bid and best_bid_osm is not None:
            # Sell aggressively if best bid is above fair + threshold
            if best_bid_osm >= fair + self.AGG_THRESHOLD:
                sell_capacity = self.POS_LIMIT + osm_pos  # how much we can still sell
                bid_vol_osm = osm_depth.buy_orders[best_bid_osm]
                agg_sell = min(sell_capacity, bid_vol_osm, self.AGG_MAX_VOL)
                if agg_sell > 0:
                    orders[self.OSMIUM].append(
                        Order(self.OSMIUM, best_bid_osm, -agg_sell)
                    )
                    osm_pos -= agg_sell

        # ── d. Passive quotes around fair value ───────────────────
        # Post a resting buy at fair−2 and a resting sell at fair+2.
        # Size is capped by remaining position capacity.
        passive_buy_price  = fair - self.QUOTE_OFFSET
        passive_sell_price = fair + self.QUOTE_OFFSET

        buy_capacity  = self.POS_LIMIT - osm_pos
        sell_capacity = self.POS_LIMIT + osm_pos   # for short side

        if buy_capacity > 0:
            orders[self.OSMIUM].append(
                Order(self.OSMIUM, passive_buy_price, buy_capacity)
            )
        if sell_capacity > 0:
            orders[self.OSMIUM].append(
                Order(self.OSMIUM, passive_sell_price, -sell_capacity)
            )

        # ── 5. Persist state ───────────────────────────────────────
        new_td = json.dumps({
            "day":     day,
            "prev_ts": prev_ts,
            "osm_ema": osm_ema,
        })

        # conversions = 0 (no conversion products in Round 2)
        return orders, 0, new_td
