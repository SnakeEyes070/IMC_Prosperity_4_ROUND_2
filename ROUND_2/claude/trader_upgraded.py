# trader.py — IMC Prosperity 4, Round 2
# Pepper: Smart Trend-Rider with Sell-Rally/Rebuy-Dip cycles + Trailing-Peak exit
# OSM: calibrated thresholds from log forensics
#
# Pepper improvement plan (vs 7,291 baseline):
#   +141  better entry  (TOL=2, only ask1)
#   +200  better exit   (trailing-peak instead of early linear sell)
#   +250  intraday scalp (sell rallies >trend+7, rebuy dips <trend+3, size=15)
#   ────────────────────────────────────────────────────────────────────────
#   ≈ 7,880  target per day  →  8k across 3-day round is realistic

import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order


class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"

    LIMIT = 80

    # ── PEPPER parameters ─────────────────────────────────────────────
    PEPPER_SLOPE       = 0.001   # fits log data: +100 over 100k ts
    PEPPER_BUY_TOL     = 2       # only accept ask1 (ask2 is always ask1+3)

    # Intraday scalp — sell rallies above trend, rebuy dips below trend
    # Works even at full position (80) because we SELL first then REBUY
    PEPPER_MIN_BASE    = 65      # never sell below this in non-endgame
    SCALP_SELL_ABOVE   = 7       # sell 15 units when mid > trend + 7
    SCALP_REBUY_BELOW  = 3       # rebuy when mid < trend + 3 AND scalp is open

    # Endgame — trailing-peak exit instead of early linear drain
    ENDGAME_START      = 93_000  # start tracking peak; don't sell yet
    TRAIL_WINDOW       = 8       # ticks to look back for peak bid tracking
    TRAIL_DROP         = 7       # sell all if bid drops 7 from window peak
    TARGET_BID         = 13_095  # immediate full liquidation if bid >= this
    HARD_SELL_START    = 98_000  # fallback: linear sell if still holding here
    MAX_TS             = 99_900

    # ── OSMIUM parameters (log-calibrated) ───────────────────────────
    OSM_FAIR_FALLBACK        = 10_000
    OSM_EMA_ALPHA            = 0.02
    OSM_L1_SIZE              = 19
    OSM_L2_SIZE              = 14
    OSM_L3_SIZE              = 10
    OSM_MR_THRESH            = 8
    OSM_MR_MAX_QTY           = 24
    OSM_SKEW_FACTOR          = 0.06
    # Calibrated from log: ask1 never ≤ 9996 (zero fires); raise to 10003
    OSM_AGGRESSIVE_BUY_THRESH  = 10_003
    # Calibrated from log: 10004 threshold caught 100% of qualifying bids;
    # lower to 10002 to catch the 110 near-miss ticks
    OSM_AGGRESSIVE_SELL_THRESH = 10_002

    # ── Day tracking ──────────────────────────────────────────────────
    ROUND_DAYS     = 3
    NEW_DAY_THRESH = 10_000

    # ─────────────────────────────────────────────────────────────────
    def bid(self) -> int:
        return 6_500

    # ─────────────────────────────────────────────────────────────────
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts      = state.timestamp
        prev_ts = data.get("last_ts", -1)
        day     = data.get("day", 0)

        # ── Day rollover ──────────────────────────────────────────────
        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day
            # Reset per-day pepper state
            data.pop("pepper_anchor",      None)
            data.pop("pepper_scalp_open",  None)
            data.pop("pepper_scalp_entry", None)
            data.pop("pepper_bid_window",  None)

        # ── Pepper fair value ─────────────────────────────────────────
        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        anchor     = data["pepper_anchor"]
        trend_fair = anchor + self.PEPPER_SLOPE * ts

        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame  = is_last_day and (ts >= self.ENDGAME_START)

        orders: Dict[str, List[Order]] = {}

        p_ords = self._pepper_orders(state, ts, trend_fair, is_endgame, data)
        if p_ords:
            orders[self.PEPPER] = p_ords

        o_ords = self._osmium_orders(state, data)
        if o_ords:
            orders[self.OSMIUM] = o_ords

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

    # ─────────────────────────────────────────────────────────────────
    def _pepper_orders(self, state: TradingState, ts: int, trend: float,
                       is_endgame: bool, data: dict) -> List[Order]:
        od  = state.order_depths.get(self.PEPPER, OrderDepth())
        pos = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []

        best_bid = max(od.buy_orders.keys())  if od.buy_orders  else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else trend

        # ── NON-ENDGAME ───────────────────────────────────────────────
        if not is_endgame:

            # 1. BASE LONG — fill up to 80 at ask1 only (TOL=2)
            buy_cap = self.LIMIT - pos
            if buy_cap > 0 and od.sell_orders:
                for ask_px in sorted(od.sell_orders.keys()):
                    if buy_cap <= 0:
                        break
                    if ask_px <= trend + self.PEPPER_BUY_TOL:
                        vol = min(buy_cap, -od.sell_orders[ask_px])
                        if vol > 0:
                            orders.append(Order(self.PEPPER, ask_px, vol))
                            buy_cap -= vol

            # 2. SELL-RALLY / REBUY-DIP SCALP
            #    This works even at pos=80 because we SELL first to create room.
            #    State: pepper_scalp_open = True means we sold 15 and want to rebuy.
            scalp_open = data.get("pepper_scalp_open", False)

            if not scalp_open:
                # Sell 15 units when mid is > trend + SCALP_SELL_ABOVE
                if best_bid and mid > trend + self.SCALP_SELL_ABOVE:
                    # Only sell down to PEPPER_MIN_BASE to protect base position
                    sell_qty = min(15, max(0, pos - self.PEPPER_MIN_BASE))
                    if sell_qty > 0 and od.buy_orders:
                        avail = od.buy_orders.get(best_bid, 0)
                        vol   = min(sell_qty, avail)
                        if vol > 0:
                            orders.append(Order(self.PEPPER, best_bid, -vol))
                            data["pepper_scalp_open"]  = True
                            data["pepper_scalp_entry"] = best_bid
            else:
                # Rebuy when mid has pulled back toward trend
                if best_ask and mid < trend + self.SCALP_REBUY_BELOW:
                    rebuy_qty = min(15, self.LIMIT - pos)
                    if rebuy_qty > 0:
                        avail = -od.sell_orders.get(best_ask, 0)
                        vol   = min(rebuy_qty, avail)
                        if vol > 0:
                            orders.append(Order(self.PEPPER, best_ask, vol))
                            data["pepper_scalp_open"]  = False
                            data["pepper_scalp_entry"] = None

        # ── ENDGAME — trailing-peak exit ──────────────────────────────
        else:
            if pos <= 0:
                return orders

            # Maintain a sliding window of recent bid prices to detect the peak
            window: list = data.get("pepper_bid_window", [])
            if best_bid:
                window.append(best_bid)
            if len(window) > self.TRAIL_WINDOW:
                window = window[-self.TRAIL_WINDOW:]
            data["pepper_bid_window"] = window

            window_peak = max(window) if window else 0

            # ── Trigger 1: Immediate sell if bid hits target ──────────
            target_hit = best_bid is not None and best_bid >= self.TARGET_BID

            # ── Trigger 2: Trailing stop — bid dropped from window peak
            trail_hit = (best_bid is not None
                         and window_peak > 0
                         and best_bid <= window_peak - self.TRAIL_DROP)

            # ── Trigger 3: Hard deadline — sell linearly from 98k ─────
            hard_sell = ts >= self.HARD_SELL_START

            if target_hit or trail_hit:
                # DUMP everything now at the best available bids
                self._sweep_bids(od, pos, orders)

            elif hard_sell:
                # Linear liquidation: spread remaining units over leftover ticks
                ticks_left    = max(1, (self.MAX_TS - ts) // 100 + 1)
                per_tick_sell = math.ceil(pos / ticks_left)
                # Sell 3× pace so we never hold into the final mark
                to_sell = min(pos, per_tick_sell * 3)
                self._sweep_bids(od, to_sell, orders)

            # ── Safety net: guarantee flat at final tick ───────────────
            if ts >= self.MAX_TS and pos > 0:
                self._sweep_bids(od, pos, orders)

        return orders

    # ─────────────────────────────────────────────────────────────────
    def _sweep_bids(self, od: OrderDepth, qty: int, orders: List[Order]) -> None:
        """Sweep through buy-side book to sell 'qty' units."""
        remaining = qty
        for bid_px in sorted(od.buy_orders.keys(), reverse=True):
            if remaining <= 0:
                break
            vol = min(remaining, od.buy_orders[bid_px])
            if vol > 0:
                orders.append(Order(self.PEPPER, bid_px, -vol))
                remaining -= vol

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
            raw_mid        = best_bid + 4
            current_spread = 16
        elif best_ask is not None:
            raw_mid        = best_ask - 4
            current_spread = 16
        else:
            raw_mid        = float(self.OSM_FAIR_FALLBACK)
            current_spread = 16

        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_FALLBACK))
        ema      = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = round(ema)

        buy_cap  = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # ── Aggressive takes (log-calibrated thresholds) ──────────────
        # Buy: ask1 ≤ 10003 (actual aggressive buy zone from log)
        if best_ask is not None and best_ask <= self.OSM_AGGRESSIVE_BUY_THRESH and buy_cap > 0:
            avail = -od.sell_orders.get(best_ask, 0)
            vol   = min(self.OSM_MR_MAX_QTY, buy_cap, avail)
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_ask, vol))
                buy_cap -= vol

        # Sell: bid1 ≥ 10002 (lowered from 10004 to capture 110 missed ticks)
        if best_bid is not None and best_bid >= self.OSM_AGGRESSIVE_SELL_THRESH and sell_cap > 0:
            avail = od.buy_orders.get(best_bid, 0)
            vol   = min(self.OSM_MR_MAX_QTY, sell_cap, avail)
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_bid, -vol))
                sell_cap -= vol

        # ── Mean reversion (safety net beyond aggressive band) ────────
        if od.sell_orders and buy_cap > 0:
            for ask_px in sorted(od.sell_orders.keys()):
                if ask_px > fair - self.OSM_MR_THRESH:
                    break
                avail = -od.sell_orders[ask_px]
                vol   = min(buy_cap, avail, self.OSM_MR_MAX_QTY)
                if vol > 0:
                    orders.append(Order(self.OSMIUM, ask_px, vol))
                    buy_cap -= vol

        if od.buy_orders and sell_cap > 0:
            for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                if bid_px < fair + self.OSM_MR_THRESH:
                    break
                avail = od.buy_orders[bid_px]
                vol   = min(sell_cap, avail, self.OSM_MR_MAX_QTY)
                if vol > 0:
                    orders.append(Order(self.OSMIUM, bid_px, -vol))
                    sell_cap -= vol

        # ── Passive market making ─────────────────────────────────────
        skew = int(max(-6, min(6, pos * self.OSM_SKEW_FACTOR)))

        spread_factor = current_spread / 16.0
        base_l1 = max(3, min(5, round(4 * spread_factor)))
        base_l2 = max(5, min(9, round(7 * spread_factor)))
        base_l3 = max(9, min(13, round(11 * spread_factor)))

        # Tighter L1 bid (more fill), tighter L1 ask
        l1_bid_off = base_l1 + 1
        l1_ask_off = max(2, base_l1 - 1)

        # Passive offset calibrated to log: bids at mid-2 showed higher fill rate
        levels = [
            (l1_bid_off,  l1_ask_off,  self.OSM_L1_SIZE),
            (base_l2,     base_l2,     self.OSM_L2_SIZE),
            (base_l3,     base_l3,     self.OSM_L3_SIZE),
        ]

        long_bias = pos / self.LIMIT

        for b_off, a_off, base_size in levels:
            if buy_cap <= 0 and sell_cap <= 0:
                break

            bid_px = fair - b_off - skew
            ask_px = fair + a_off - skew

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
