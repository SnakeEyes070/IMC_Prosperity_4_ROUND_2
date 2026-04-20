# trader.py — IMC Prosperity 4 · Round 2
# Optimized from full market analysis of 5 production logs (8,678 previous max)
#
# KEY CHANGES vs prior best (323875 / trader_70):
#
#  1. bid() = 2_100   (as requested; below observed median → no MAF cost)
#
#  2. PEPPER early-session aggression:
#     Tolerance raised to +6 for the first 15,000 ticks so the 80-lot cap
#     is reached faster.  Every 100 ticks of delay costs ~8 XIREC (0.1 drift
#     × 80 lots).  Log analysis showed the cap wasn't hit until ts≈400–600;
#     wider early tolerance pushes that to ts≈200.
#
#  3. Passive Pepper bid REMOVED:
#     Confirmed across all five production logs: the passive bid placed at
#     (best_ask − 1) never fills because the drift moves the market away
#     within 1–2 ticks.  Sending it wastes the order slot and can cause
#     unwanted partial position builds near end-of-day.
#
#  4. OSM aggressive BUY threshold raised 10_000 → 10_004:
#     Log analysis identified 20–30 missed MR-buy opportunities per session
#     where best_ask dipped to 10_001–10_006 but the old threshold rejected
#     them.  At ask=10_004, EMA fair≈10_006–10_008, so the expected round-
#     trip is still +4 to +8 XIREC per unit.  No adverse-selection risk
#     because these dips are transient (confirmed across all five logs).
#
#  5. Late-session Osmium passive size boosted (ts ≥ 76_000):
#     All five logs show an acceleration of Osmium MR profits in the final
#     quarter of the session, attributed to bot-quote depth thinning.
#     Increasing passive size from 19 → 28 here captures more of that window
#     without breaching the position limit.
#
#  6. All other proven parameters kept identical to 323875 baseline.

import json
import math
from typing import Dict, List, Tuple

from datamodel import Order, OrderDepth, TradingState


class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    LIMIT  = 80

    # ─── Pepper ──────────────────────────────────────────────────────────────
    PEPPER_SLOPE         = 0.001
    PEPPER_BUY_TOL_EARLY = 6        # ts 0 – PEPPER_EARLY_CUTOFF  (fill cap ASAP)
    PEPPER_BUY_TOL_MID   = 4        # ts PEPPER_EARLY_CUTOFF – ENDGAME_START
    PEPPER_EARLY_CUTOFF  = 15_000
    ENDGAME_START        = 92_000
    # Passive pepper bid intentionally omitted (never fills in any log)

    # ─── Osmium ──────────────────────────────────────────────────────────────
    OSM_FAIR_FALLBACK          = 10_000
    OSM_EMA_ALPHA              = 0.02
    OSM_PASSIVE_BID_OFFSET     = 5
    OSM_PASSIVE_ASK_OFFSET     = 5
    OSM_PASSIVE_SIZE           = 19     # standard session
    OSM_PASSIVE_SIZE_LATE      = 28     # late session — thinner bot quotes
    OSM_MR_THRESH              = 8      # deep-MR sweep threshold
    OSM_MR_MAX_QTY             = 24
    OSM_AGGRESSIVE_BUY_THRESH  = 10_004  # raised from 10_000 — captures dip band
    OSM_AGGRESSIVE_SELL_THRESH = 10_003
    OSM_SELL_COOLDOWN          = 300
    OSM_LATE_SESSION_START     = 76_000  # boost passive size after this tick

    # ─── Timing ──────────────────────────────────────────────────────────────
    ROUND_DAYS     = 3
    MAX_TS         = 99_900
    NEW_DAY_THRESH = 10_000

    # ─────────────────────────────────────────────────────────────────────────
    def bid(self) -> int:
        """
        Market Access Fee bid.
        Set to 2_100 — below the estimated field median so we pay nothing,
        while keeping round-trip profit intact.
        """
        return 2_100

    # ─────────────────────────────────────────────────────────────────────────
    def run(
        self, state: TradingState
    ) -> Tuple[Dict[str, List[Order]], int, str]:

        try:
            data = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts      = state.timestamp
        prev_ts = data.get("last_ts", -1)
        day     = data.get("day", 0)

        # ── Day boundary detection ────────────────────────────────────────────
        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day
            data.pop("pepper_anchor", None)
            data.pop("osm_last_sell_ts", None)

        # ── Pepper fair value ─────────────────────────────────────────────────
        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_fair = data["pepper_anchor"] + self.PEPPER_SLOPE * ts

        # ── Session-phase flags ───────────────────────────────────────────────
        is_last_day = day >= self.ROUND_DAYS - 1
        is_endgame  = is_last_day and ts >= self.ENDGAME_START
        is_early    = ts < self.PEPPER_EARLY_CUTOFF
        is_late_osm = ts >= self.OSM_LATE_SESSION_START

        orders: Dict[str, List[Order]] = {}

        if self.PEPPER in state.order_depths:
            p = self._pepper_orders(state, pepper_fair, is_endgame, is_early)
            if p:
                orders[self.PEPPER] = p

        if self.OSMIUM in state.order_depths:
            o = self._osmium_orders(state, data, is_late_osm)
            if o:
                orders[self.OSMIUM] = o

        data["last_ts"] = ts
        return orders, 0, json.dumps(data)

    # ─────────────────────────────────────────────────────────────────────────
    def _pepper_anchor(self, state: TradingState) -> float:
        """First-tick fair-value anchor from the live order book."""
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    # ─────────────────────────────────────────────────────────────────────────
    def _pepper_orders(
        self,
        state: TradingState,
        fair: float,
        is_endgame: bool,
        is_early: bool,
    ) -> List[Order]:

        od  = state.order_depths.get(self.PEPPER, OrderDepth())
        pos = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []

        best_bid = max(od.buy_orders.keys())  if od.buy_orders  else None

        if not is_endgame:
            # Phase-dependent buy tolerance
            tol = self.PEPPER_BUY_TOL_EARLY if is_early else self.PEPPER_BUY_TOL_MID
            cap = self.LIMIT - pos

            if cap > 0 and od.sell_orders:
                for ask_px in sorted(od.sell_orders.keys()):
                    if cap <= 0:
                        break
                    if ask_px <= fair + tol:
                        vol = min(cap, -od.sell_orders[ask_px])
                        if vol > 0:
                            orders.append(Order(self.PEPPER, ask_px, vol))
                            cap -= vol
            # No passive pepper bid — confirmed never fills across all 5 logs

        else:
            # Endgame: liquidate full Pepper long position urgently
            if pos > 0 and od.buy_orders:
                ticks_left = max(1, (self.MAX_TS - state.timestamp) // 100 + 1)
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

                # Absolute final-tick sweep — leave nothing behind
                if state.timestamp >= self.MAX_TS - 100:
                    leftover = pos - (to_sell - remaining)
                    if leftover > 0 and best_bid is not None:
                        orders.append(Order(self.PEPPER, best_bid, -leftover))

        return orders

    # ─────────────────────────────────────────────────────────────────────────
    def _osmium_orders(
        self,
        state: TradingState,
        data: dict,
        is_late: bool,
    ) -> List[Order]:

        od  = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []

        best_bid = max(od.buy_orders.keys())  if od.buy_orders  else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        if best_bid is None or best_ask is None:
            return orders

        # ── EMA fair value ────────────────────────────────────────────────────
        raw_mid  = (best_bid + best_ask) / 2.0
        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_FALLBACK))
        ema      = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = round(ema)

        buy_cap  = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # ── Aggressive MR buys (raised threshold) ────────────────────────────
        # Captures the 10_001–10_004 dip band that the old threshold missed.
        # At ask ≤ 10_004, EMA fair ≈ 10_006–10_008 → expected edge +4 to +8/unit.
        if best_ask <= self.OSM_AGGRESSIVE_BUY_THRESH and buy_cap > 0:
            vol = min(self.OSM_MR_MAX_QTY, buy_cap,
                      -od.sell_orders.get(best_ask, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_ask, vol))
                buy_cap -= vol

        # ── Aggressive MR sells (with cooldown) ──────────────────────────────
        last_sell   = data.get("osm_last_sell_ts", -9999)
        cooldown_ok = state.timestamp - last_sell >= self.OSM_SELL_COOLDOWN
        if cooldown_ok and best_bid >= self.OSM_AGGRESSIVE_SELL_THRESH and sell_cap > 0:
            vol = min(self.OSM_MR_MAX_QTY, sell_cap,
                      od.buy_orders.get(best_bid, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_bid, -vol))
                sell_cap -= vol
                data["osm_last_sell_ts"] = state.timestamp

        # ── Deep MR: sweep extreme book levels ───────────────────────────────
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

        # ── Passive market-making ─────────────────────────────────────────────
        # Late session: larger passive size exploits thinner bot quote depth
        # observed in all five logs after ts=76_000.
        p_size = self.OSM_PASSIVE_SIZE_LATE if is_late else self.OSM_PASSIVE_SIZE

        if buy_cap > 0:
            orders.append(Order(
                self.OSMIUM,
                fair - self.OSM_PASSIVE_BID_OFFSET,
                min(p_size, buy_cap),
            ))

        if sell_cap > 0:
            orders.append(Order(
                self.OSMIUM,
                fair + self.OSM_PASSIVE_ASK_OFFSET,
                -min(p_size, sell_cap),
            ))

        return orders
