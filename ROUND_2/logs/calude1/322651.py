# trader.py — IMC Prosperity 4, Round 2
# Forensic-optimized from 8,643 XIREC run analysis
# Target: 9,100+ XIRECs
#
# Change log vs prior version (8,643 run):
#   [FIX]  OSM_SKEW_FACTOR was defined but never applied → now live
#   [TUNE] PEPPER_BUY_TOL         5    → 3    (log: 1.16 tick/unit overpay at entry)
#   [TUNE] ENDGAME_START          90k  → 83k  (log: peak mid 13,105 at t≈92.5k; bids elevated from t=83k)
#   [TUNE] OSM_AGGRESSIVE_SELL_THRESH 10,003 → 10,002 (log: 12 ticks at bid=10,002 missed, ~120 XIRECs)
#   [TUNE] OSM_AGGRESSIVE_BUY_THRESH  10,000 → 9,998  (log: only 2 ticks at ask≤9,998, 10,000 lifts at no edge)
#   [TUNE] OSM_PASSIVE_BID_OFFSET     6    → 5    (log: actual fills cluster at mid-5, not mid-6)
#   [TUNE] OSM_PASSIVE_ASK_OFFSET     6    → 5    (log: symmetric; tighter ask lifts fill rate ~15%)
#   [TUNE] OSM_PASSIVE_SIZE           19   → 22   (freed inventory headroom from tighter offsets)
#   [NEW]  Scalp runs on a reserved 10-unit budget separate from main 70-unit trend position
#          → main position no longer blocks scalp signals at full capacity
#   [NEW]  Endgame sell multiplier 2.5× → 3.5× (faster drawdown into t=83k–90k peak bids)
#   [NEW]  Endgame final sweep triggers at MAX_TS-200 instead of -100 (extra safety tick)
#   [NEW]  Pepper anchor uses VWAP of first two price levels, not just min ask

import json
import math
from typing import Dict, List, Tuple
from datamodel import OrderDepth, TradingState, Order


class Trader:
    # ── Products ──────────────────────────────────────────────────────────────
    PEPPER  = "INTARIAN_PEPPER_ROOT"
    OSMIUM  = "ASH_COATED_OSMIUM"
    LIMIT   = 80

    # ── Pepper ────────────────────────────────────────────────────────────────
    PEPPER_SLOPE    = 0.001        # long-run drift per tick (log-verified: +91 ticks over 99.9k)
    PEPPER_BUY_TOL  = 3            # was 5 → 3; log showed 1.16 tick/unit overpay with tol=5
    SCALP_RESERVE   = 10           # units reserved for scalp; main trend fills to LIMIT-SCALP_RESERVE
    SCALP_DROP_THRESH = 8          # pullback depth to trigger scalp entry (18 events ≥8 ticks in log)
    SCALP_PROFIT_TICKS = 5         # minimum bid move above entry before scalp exit

    # ── Endgame ───────────────────────────────────────────────────────────────
    ENDGAME_START       = 83_000   # was 90k; peak mid (13,105) at t=92.5k, bids elevated from t=83k
    ENDGAME_RATE_MULT   = 3.5      # was 2.5; faster drawdown into the peak-bid window
    ENDGAME_SWEEP_GUARD = 200      # final all-in sweep fires at MAX_TS - this value

    # ── Osmium ────────────────────────────────────────────────────────────────
    OSM_FAIR_FALLBACK           = 10_000
    OSM_EMA_ALPHA               = 0.02        # slow EMA; Osmium mean-reverts around 10,000
    OSM_PASSIVE_BID_OFFSET      = 5           # was 6; log fills clustered at mid-5
    OSM_PASSIVE_ASK_OFFSET      = 5           # was 6; symmetric improvement
    OSM_PASSIVE_SIZE            = 22          # was 19; headroom freed by tighter offsets
    OSM_MR_THRESH               = 8           # mean-reversion buy/sell trigger (ticks from fair)
    OSM_MR_MAX_QTY              = 24
    OSM_SKEW_FACTOR             = 0.06        # inventory skew: was defined but NEVER applied — fixed
    OSM_AGGRESSIVE_BUY_THRESH   = 9_998       # was 10,000; only 2 events at ≤9,998 — all captured
    OSM_AGGRESSIVE_SELL_THRESH  = 10_002      # was 10,003; recovers 12 missed events (~120 XIRECs)

    # ── Timing ────────────────────────────────────────────────────────────────
    ROUND_DAYS      = 3
    MAX_TS          = 99_900
    NEW_DAY_THRESH  = 10_000

    # ── Market Access Fee bid ─────────────────────────────────────────────────
    # Rook-E1 analysis: "you only need to finish in top half — bid close to median"
    # Current value is conservative; raise if other participants are observed bidding higher.
    def bid(self) -> int:
        return 6_500

    # ═════════════════════════════════════════════════════════════════════════
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts       = state.timestamp
        prev_ts  = data.get("last_ts", -1)
        day      = data.get("day", 0)

        # Day boundary detection
        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day
            data.pop("pepper_anchor",       None)
            data.pop("pepper_recent_high",  None)
            data.pop("pepper_scalp_entry",  None)

        # Initialise pepper anchor once per day
        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_fair = data["pepper_anchor"] + self.PEPPER_SLOPE * ts
        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame  = is_last_day and (ts >= self.ENDGAME_START)

        orders: Dict[str, List[Order]] = {}

        if self.PEPPER in state.order_depths:
            pep_ords = self._pepper_orders(state, ts, pepper_fair, is_endgame, data)
            if pep_ords:
                orders[self.PEPPER] = pep_ords

        if self.OSMIUM in state.order_depths:
            osm_ords = self._osmium_orders(state, data)
            if osm_ords:
                orders[self.OSMIUM] = osm_ords

        data["last_ts"] = ts
        return orders, 0, json.dumps(data)

    # ─────────────────────────────────────────────────────────────────────────
    # PEPPER helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _pepper_anchor(self, state: TradingState) -> float:
        """
        VWAP of the two best ask levels at day-open.
        More stable than bare min(ask) — avoids anchoring to a thin outlier.
        Falls back to best bid, then 12,000.
        """
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            sorted_asks = sorted(od.sell_orders.items())          # [(price, neg_vol), ...]
            total_val, total_vol = 0.0, 0
            for px, neg_vol in sorted_asks[:2]:
                vol = -neg_vol
                total_val += px * vol
                total_vol += vol
            return total_val / total_vol if total_vol > 0 else float(sorted_asks[0][0])
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

        # ── Update rolling high ──────────────────────────────────────────────
        if "pepper_recent_high" not in data:
            data["pepper_recent_high"] = mid
        else:
            data["pepper_recent_high"] = max(data["pepper_recent_high"], mid)

        if not is_endgame:
            # ── Main trend accumulation (fills up to LIMIT - SCALP_RESERVE) ──
            trend_cap = (self.LIMIT - self.SCALP_RESERVE) - pos
            if trend_cap > 0 and od.sell_orders:
                for ask_px in sorted(od.sell_orders.keys()):
                    if trend_cap <= 0:
                        break
                    if ask_px <= fair + self.PEPPER_BUY_TOL:
                        vol = min(trend_cap, -od.sell_orders[ask_px])
                        if vol > 0:
                            orders.append(Order(self.PEPPER, ask_px, vol))
                            trend_cap -= vol

                # Passive join: sit 1 tick below best ask
                if trend_cap > 0:
                    passive_bid = min(od.sell_orders.keys()) - 1
                    orders.append(Order(self.PEPPER, passive_bid, trend_cap))

            # ── Scalp logic (operates on the reserved 10-unit budget) ─────────
            # Entry: pullback ≥ SCALP_DROP_THRESH ticks below recent high
            # Exit : best bid rises ≥ SCALP_PROFIT_TICKS above entry price
            scalp_entry = data.get("pepper_scalp_entry")
            scalp_pos   = data.get("pepper_scalp_pos", 0)   # units currently in scalp trade

            if scalp_entry is None:
                # No open scalp trade — look for pullback entry
                if (best_ask is not None and
                        best_ask < data["pepper_recent_high"] - self.SCALP_DROP_THRESH):
                    scalp_budget = self.LIMIT - pos - max(0, trend_cap if trend_cap < 0 else 0)
                    avail = min(self.SCALP_RESERVE, scalp_budget,
                                -od.sell_orders.get(best_ask, 0))
                    if avail > 0:
                        orders.append(Order(self.PEPPER, best_ask, avail))
                        data["pepper_scalp_entry"] = best_ask
                        data["pepper_scalp_pos"]   = avail
            else:
                # Open scalp trade — watch for exit
                if best_bid is not None and best_bid >= scalp_entry + self.SCALP_PROFIT_TICKS:
                    to_exit = min(scalp_pos, od.buy_orders.get(best_bid, 0))
                    if to_exit > 0:
                        orders.append(Order(self.PEPPER, best_bid, -to_exit))
                        data["pepper_scalp_entry"] = None
                        data["pepper_scalp_pos"]   = 0
                        data["pepper_recent_high"] = mid   # reset so next pullback re-anchors

        else:
            # ── Endgame liquidation ───────────────────────────────────────────
            # Cancel any open scalp on entry into endgame
            if data.get("pepper_scalp_entry") is not None:
                data["pepper_scalp_entry"] = None
                data["pepper_scalp_pos"]   = 0

            if pos > 0 and od.buy_orders:
                ticks_left = max(1, (self.MAX_TS - ts) // 100 + 1)
                per_tick   = math.ceil(pos / ticks_left)
                to_sell    = min(pos, int(per_tick * self.ENDGAME_RATE_MULT))
                remaining  = to_sell

                # Walk the bid book best-to-worst
                for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                    if remaining <= 0:
                        break
                    vol = min(remaining, od.buy_orders[bid_px])
                    if vol > 0:
                        orders.append(Order(self.PEPPER, bid_px, -vol))
                        remaining -= vol

                # Final all-in sweep — never carry inventory past deadline
                if ts >= self.MAX_TS - self.ENDGAME_SWEEP_GUARD and pos > 0:
                    leftover = pos - (to_sell - remaining)
                    if leftover > 0:
                        sweep_bid = max(od.buy_orders.keys())
                        orders.append(Order(self.PEPPER, sweep_bid, -leftover))

        return orders

    # ─────────────────────────────────────────────────────────────────────────
    # OSMIUM helpers
    # ─────────────────────────────────────────────────────────────────────────

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

        # ── Inventory skew (BUG FIX: previously defined but never applied) ───
        # When long: shift fair down → passive ask moves closer → faster unwind.
        # When short: shift fair up  → passive bid moves closer → faster restock.
        # Skew is bounded at ±40% of limit so it never inverts the spread.
        skew = self.OSM_SKEW_FACTOR * pos
        skew = max(-self.LIMIT * 0.4, min(self.LIMIT * 0.4, skew))
        fair = round(ema - skew)

        buy_cap  = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # ── 1. Aggressive taking ──────────────────────────────────────────────
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

        # ── 2. Mean-reversion sweep (safety net for dislocations) ─────────────
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

        # ── 3. Passive market-making (primary alpha source) ───────────────────
        # Offsets reduced 6→5: log shows fills cluster at mid±5, not mid±6.
        # fair is already inventory-skewed so quotes naturally lean toward flat.
        if buy_cap > 0:
            bid_px = fair - self.OSM_PASSIVE_BID_OFFSET
            qty    = min(self.OSM_PASSIVE_SIZE, buy_cap)
            orders.append(Order(self.OSMIUM, bid_px, qty))
            buy_cap -= qty

        if sell_cap > 0:
            ask_px = fair + self.OSM_PASSIVE_ASK_OFFSET
            qty    = min(self.OSM_PASSIVE_SIZE, sell_cap)
            orders.append(Order(self.OSMIUM, ask_px, -qty))
            sell_cap -= qty

        # ── 4. Second passive layer (wider quotes, smaller size) ─────────────
        # Catches larger mid moves that skip the primary layer.
        # Only place if there is remaining capacity on both sides.
        if buy_cap > 0:
            bid2_px = fair - self.OSM_PASSIVE_BID_OFFSET - 4
            qty2    = min(10, buy_cap)
            orders.append(Order(self.OSMIUM, bid2_px, qty2))

        if sell_cap > 0:
            ask2_px = fair + self.OSM_PASSIVE_ASK_OFFSET + 4
            qty2    = min(10, sell_cap)
            orders.append(Order(self.OSMIUM, ask2_px, -qty2))

        return orders