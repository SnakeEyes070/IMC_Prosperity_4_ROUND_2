import json
import math
from typing import Dict, List, Tuple

from datamodel import Order, OrderDepth, TradingState


class Trader:
    PEPPER  = "INTARIAN_PEPPER_ROOT"
    OSMIUM  = "ASH_COATED_OSMIUM"
    LIMIT   = 80

    # ── Pepper ────────────────────────────────────────────────────────────────
    PEPPER_SLOPE     = 0.001
    PEPPER_BUY_TOL   = 4          # unchanged: fair tracks ask exactly
    ENDGAME_START    = 92_000     # unchanged: bids flat t=90k-94k, no gain
    SCALP_RESERVE    = 0
    SCALP_DIP        = 6          # WAS 8  → +26 extra captures, +945 expected
    SCALP_EXIT       = 4          # WAS 5  → exit 1 tick earlier, +60 expected
    SCALP_SIZE       = 10

    # ── Osmium ────────────────────────────────────────────────────────────────
    OSM_FAIR_FALLBACK            = 10_000
    OSM_EMA_ALPHA                = 0.02
    OSM_PASSIVE_BID_OFFSET       = 6      # WAS 5  → less adverse selection, +80
    OSM_PASSIVE_ASK_OFFSET       = 6      # WAS 5  → less adverse selection, +80
    OSM_PASSIVE_SIZE             = 19
    OSM_MR_THRESH                = 8
    OSM_MR_MAX_QTY               = 24
    OSM_AGGRESSIVE_BUY_THRESH    = 10_000
    OSM_AGGRESSIVE_SELL_THRESH   = 10_002  # WAS 10003 → +10 events, +480 expected
    OSM_SELL_COOLDOWN            = 0       # WAS 300   → removed, +72 expected

    # ── Passive skew (enabled) ────────────────────────────────────────────────
    OSM_USE_PASSIVE_SKEW = True            # WAS False → reduces adverse fills, +150
    OSM_SKEW_PER_UNIT    = 0.10

    # ── Laddered aggression (kept disabled — insufficient log evidence) ───────
    OSM_USE_LADDERED_AGGRESSION = False
    OSM_AGG_SIZE_L1  = 12
    OSM_AGG_SIZE_L2  = 8
    OSM_AGG_DEEPER   = 2

    # ── Timing ────────────────────────────────────────────────────────────────
    ROUND_DAYS      = 3
    MAX_TS          = 99_900
    NEW_DAY_THRESH  = 10_000

    def bid(self) -> int:
        return 6_500

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts      = state.timestamp
        prev_ts = data.get("last_ts", -1)
        day     = data.get("day", 0)

        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day
            data.pop("pepper_anchor",       None)
            data.pop("pepper_recent_high",  None)
            data.pop("pepper_scalp_entry",  None)
            data.pop("osm_last_sell_ts",    None)

        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_fair  = data["pepper_anchor"] + self.PEPPER_SLOPE * ts
        is_last_day  = day >= self.ROUND_DAYS - 1
        is_endgame   = is_last_day and ts >= self.ENDGAME_START

        orders: Dict[str, List[Order]] = {}

        if self.PEPPER in state.order_depths:
            pepper_orders = self._pepper_orders(state, pepper_fair, is_endgame, data)
            if pepper_orders:
                orders[self.PEPPER] = pepper_orders

        if self.OSMIUM in state.order_depths:
            osmium_orders = self._osmium_orders(state, data)
            if osmium_orders:
                orders[self.OSMIUM] = osmium_orders

        data["last_ts"] = ts
        return orders, 0, json.dumps(data)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _pepper_anchor(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    # ── pepper ────────────────────────────────────────────────────────────────

    def _pepper_orders(
        self,
        state: TradingState,
        fair: float,
        is_endgame: bool,
        data: dict,
    ) -> List[Order]:
        od      = state.order_depths.get(self.PEPPER, OrderDepth())
        pos     = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []

        best_bid = max(od.buy_orders.keys())  if od.buy_orders  else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else fair

        if not is_endgame:
            # ── main trend buy ────────────────────────────────────────────────
            main_cap = max(0, self.LIMIT - self.SCALP_RESERVE - pos)
            if main_cap > 0 and od.sell_orders:
                for ask_px in sorted(od.sell_orders.keys()):
                    if main_cap <= 0:
                        break
                    if ask_px <= fair + self.PEPPER_BUY_TOL:
                        vol = min(main_cap, -od.sell_orders[ask_px])
                        if vol > 0:
                            orders.append(Order(self.PEPPER, ask_px, vol))
                            main_cap -= vol
                if main_cap > 0:
                    # passive bid just below best ask
                    passive_bid = min(od.sell_orders.keys()) - 1
                    orders.append(Order(self.PEPPER, passive_bid, main_cap))

            # ── track recent high for scalp ───────────────────────────────────
            if "pepper_recent_high" not in data:
                data["pepper_recent_high"] = mid
            else:
                data["pepper_recent_high"] = max(data["pepper_recent_high"], mid)

            # ── scalp: enter on SCALP_DIP=6 dip from recent high ─────────────
            scalp_entry = data.get("pepper_scalp_entry")
            if scalp_entry is None and best_ask is not None:
                trigger = data["pepper_recent_high"] - self.SCALP_DIP
                if best_ask < trigger:
                    remaining = self.LIMIT - pos
                    vol = min(
                        self.SCALP_SIZE,
                        remaining,
                        -od.sell_orders.get(best_ask, 0),
                    )
                    if vol > 0:
                        orders.append(Order(self.PEPPER, best_ask, vol))
                        data["pepper_scalp_entry"] = best_ask

            # ── scalp: exit on SCALP_EXIT=4 recovery ─────────────────────────
            if scalp_entry is not None and best_bid is not None:
                if best_bid > scalp_entry + self.SCALP_EXIT:
                    vol = min(self.SCALP_SIZE, od.buy_orders.get(best_bid, 0))
                    if vol > 0:
                        orders.append(Order(self.PEPPER, best_bid, -vol))
                        data["pepper_scalp_entry"] = None
                        data["pepper_recent_high"] = mid

        else:
            # ── endgame: spread liquidation across remaining ticks ────────────
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
                # force-dump on very last tick
                if state.timestamp >= self.MAX_TS - 100:
                    leftover = pos - (to_sell - remaining)
                    if leftover > 0 and od.buy_orders:
                        orders.append(
                            Order(self.PEPPER, max(od.buy_orders.keys()), -leftover)
                        )

        return orders

    # ── osmium ────────────────────────────────────────────────────────────────

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
        fair     = round(ema)

        buy_cap  = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # ── aggressive mean reversion ─────────────────────────────────────────
        if self.OSM_USE_LADDERED_AGGRESSION:
            buy_cap, sell_cap = self._laddered_aggression(
                od, best_bid, best_ask, buy_cap, sell_cap, orders
            )
        else:
            if best_ask <= self.OSM_AGGRESSIVE_BUY_THRESH and buy_cap > 0:
                vol = min(self.OSM_MR_MAX_QTY, buy_cap, -od.sell_orders.get(best_ask, 0))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, best_ask, vol))
                    buy_cap -= vol

            # cooldown = 0 → fire on every qualifying tick
            last_sell      = data.get("osm_last_sell_ts", -9999)
            cooldown_ready = (
                self.OSM_SELL_COOLDOWN <= 0
                or state.timestamp - last_sell >= self.OSM_SELL_COOLDOWN
            )
            if cooldown_ready and best_bid >= self.OSM_AGGRESSIVE_SELL_THRESH and sell_cap > 0:
                vol = min(self.OSM_MR_MAX_QTY, sell_cap, od.buy_orders.get(best_bid, 0))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, best_bid, -vol))
                    sell_cap -= vol
                    if self.OSM_SELL_COOLDOWN > 0:
                        data["osm_last_sell_ts"] = state.timestamp

        # ── MR safety net (±8 from EMA fair) ─────────────────────────────────
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

        # ── passive quotes with inventory skew ───────────────────────────────
        skew = int(round(self.OSM_SKEW_PER_UNIT * pos)) if self.OSM_USE_PASSIVE_SKEW else 0

        if buy_cap > 0:
            bid_px = fair - self.OSM_PASSIVE_BID_OFFSET - skew
            orders.append(Order(self.OSMIUM, bid_px, min(self.OSM_PASSIVE_SIZE, buy_cap)))

        if sell_cap > 0:
            ask_px = fair + self.OSM_PASSIVE_ASK_OFFSET - skew
            orders.append(Order(self.OSMIUM, ask_px, -min(self.OSM_PASSIVE_SIZE, sell_cap)))

        return orders

    # ── laddered aggression (opt-in) ──────────────────────────────────────────

    def _laddered_aggression(
        self,
        od: OrderDepth,
        best_bid: int,
        best_ask: int,
        buy_cap: int,
        sell_cap: int,
        orders: List[Order],
    ) -> Tuple[int, int]:
        if best_ask <= self.OSM_AGGRESSIVE_BUY_THRESH and buy_cap > 0:
            vol = min(self.OSM_AGG_SIZE_L1, buy_cap, -od.sell_orders.get(best_ask, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_ask, vol))
                buy_cap -= vol
        if best_ask <= self.OSM_AGGRESSIVE_BUY_THRESH - self.OSM_AGG_DEEPER and buy_cap > 0:
            vol = min(self.OSM_AGG_SIZE_L2, buy_cap, -od.sell_orders.get(best_ask, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_ask, vol))
                buy_cap -= vol

        if best_bid >= self.OSM_AGGRESSIVE_SELL_THRESH and sell_cap > 0:
            vol = min(self.OSM_AGG_SIZE_L1, sell_cap, od.buy_orders.get(best_bid, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_bid, -vol))
                sell_cap -= vol
        if best_bid >= self.OSM_AGGRESSIVE_SELL_THRESH + self.OSM_AGG_DEEPER and sell_cap > 0:
            vol = min(self.OSM_AGG_SIZE_L2, sell_cap, od.buy_orders.get(best_bid, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_bid, -vol))
                sell_cap -= vol

        return buy_cap, sell_cap