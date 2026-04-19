# trader.py — IMC Prosperity 4, Round 2 (Dual Swing + Market Making)
import json
import math
from typing import Dict, List, Optional, Tuple
from datamodel import OrderDepth, TradingState, Order

class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    LIMIT = 80

    # --- Pepper (Swing + Trend) ---
    PEPPER_SLOPE = 0.001
    PEPPER_BUY_TOL = 8
    ENDGAME_START = 94_000

    PEPPER_SWING_DIP = 12          # Sell partial if price drops 12+ ticks from peak
    PEPPER_SWING_REBOUND = 8       # Buy back if price rises 8+ ticks from dip
    PEPPER_SWING_SIZE = 20
    PEPPER_CORE = 60

    # --- Osmium (Market Making + Swing) ---
    OSM_FAIR_FALLBACK = 10_000
    OSM_EMA_ALPHA = 0.02
    OSM_L1_SIZE = 19
    OSM_L2_SIZE = 14
    OSM_L3_SIZE = 10
    OSM_MR_THRESH = 8
    OSM_MR_MAX_QTY = 24
    OSM_SKEW_FACTOR = 0.06
    OSM_AGGRESSIVE_BUY_THRESH = 9_996
    OSM_AGGRESSIVE_SELL_THRESH = 10_004

    # Osmium swing parameters (mean‑reversion swings)
    OSM_SWING_DEVIATION = 12       # Enter swing if price deviates 12+ ticks from fair
    OSM_SWING_TARGET = 6           # Exit when price reverts within 6 ticks of fair
    OSM_SWING_SIZE = 25            # Units per swing
    OSM_CORE_MM_SIZE = 19          # Base passive size (kept separate)

    # --- Timing ---
    ROUND_DAYS = 3
    MAX_TS = 99_900
    NEW_DAY_THRESH = 10_000

    def bid(self) -> int:
        return 6_500

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}
        ts = state.timestamp
        prev_ts = data.get("last_ts", -1)
        day = data.get("day", 0)

        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day
            # Clear Pepper state
            data.pop("pepper_anchor", None)
            data.pop("pepper_peak", None)
            data.pop("pepper_dip", None)
            data.pop("pepper_swing_active", None)
            # Clear Osmium swing state
            data.pop("osm_swing_direction", None)
            data.pop("osm_swing_entry", None)

        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_fair = data["pepper_anchor"] + self.PEPPER_SLOPE * ts
        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame = is_last_day and (ts >= self.ENDGAME_START)

        orders: Dict[str, List[Order]] = {}

        if self.PEPPER in state.order_depths:
            pepper_ords = self._pepper_orders(state, ts, pepper_fair, is_endgame, data)
            if pepper_ords:
                orders[self.PEPPER] = pepper_ords

        if self.OSMIUM in state.order_depths:
            osmium_ords = self._osmium_orders(state, data)
            if osmium_ords:
                orders[self.OSMIUM] = osmium_ords

        data["last_ts"] = ts
        conversions = 0
        trader_data = json.dumps(data)
        return orders, conversions, trader_data

    # ---------- Pepper (Swing + Core) ----------
    def _pepper_anchor(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    def _pepper_orders(self, state: TradingState, ts: int, fair: float,
                       is_endgame: bool, data: dict) -> List[Order]:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        pos = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []
        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else fair

        if not is_endgame:
            # Track peak
            if "pepper_peak" not in data:
                data["pepper_peak"] = mid
            else:
                data["pepper_peak"] = max(data["pepper_peak"], mid)

            swing_active = data.get("pepper_swing_active", False)
            dip_price = data.get("pepper_dip", None)

            # --- Pepper Swing Logic ---
            if not swing_active:
                if mid < data["pepper_peak"] - self.PEPPER_SWING_DIP:
                    sellable = max(0, pos - self.PEPPER_CORE)
                    if sellable > 0 and best_bid:
                        vol = min(self.PEPPER_SWING_SIZE, sellable, od.buy_orders.get(best_bid, 0))
                        if vol > 0:
                            orders.append(Order(self.PEPPER, best_bid, -vol))
                            pos -= vol
                            data["pepper_swing_active"] = True
                            data["pepper_dip"] = mid
            else:
                if mid < dip_price:
                    data["pepper_dip"] = mid
                    dip_price = mid
                if mid > dip_price + self.PEPPER_SWING_REBOUND:
                    buy_cap = self.LIMIT - pos
                    if buy_cap > 0 and best_ask:
                        vol = min(self.PEPPER_SWING_SIZE, buy_cap, -od.sell_orders.get(best_ask, 0))
                        if vol > 0:
                            orders.append(Order(self.PEPPER, best_ask, vol))
                            pos += vol
                            data["pepper_swing_active"] = False
                            data.pop("pepper_dip", None)
                            data["pepper_peak"] = mid

            # --- Core Accumulation ---
            buy_cap = max(0, self.PEPPER_CORE - pos)
            if buy_cap > 0 and od.sell_orders:
                for ask_px in sorted(od.sell_orders.keys()):
                    if buy_cap <= 0:
                        break
                    if ask_px <= fair + self.PEPPER_BUY_TOL:
                        vol = min(buy_cap, -od.sell_orders[ask_px])
                        if vol > 0:
                            orders.append(Order(self.PEPPER, ask_px, vol))
                            buy_cap -= vol
                if buy_cap > 0 and od.sell_orders:
                    best_ask = min(od.sell_orders.keys())
                    passive_bid = best_ask - 1
                    orders.append(Order(self.PEPPER, passive_bid, buy_cap))

            # Top up to full limit if extra capacity
            extra_cap = self.LIMIT - pos
            if extra_cap > 0 and od.sell_orders:
                best_ask = min(od.sell_orders.keys())
                if best_ask <= fair + self.PEPPER_BUY_TOL:
                    vol = min(extra_cap, -od.sell_orders[best_ask])
                    if vol > 0:
                        orders.append(Order(self.PEPPER, best_ask, vol))

        else:
            # Endgame unwind
            if pos > 0 and od.buy_orders:
                ticks_left = max(1, (self.MAX_TS - ts) // 100 + 1)
                per_tick = math.ceil(pos / ticks_left)
                to_sell = min(pos, int(per_tick * 2.5))
                remaining = to_sell
                for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                    if remaining <= 0:
                        break
                    vol = min(remaining, od.buy_orders[bid_px])
                    if vol > 0:
                        orders.append(Order(self.PEPPER, bid_px, -vol))
                        remaining -= vol
                if ts >= self.MAX_TS - 100 and pos > 0:
                    leftover = pos - (to_sell - remaining)
                    if leftover > 0 and od.buy_orders:
                        best_bid = max(od.buy_orders.keys())
                        orders.append(Order(self.PEPPER, best_bid, -leftover))
        return orders

    # ---------- Osmium (Market Making + Swing) ----------
    def _osmium_orders(self, state: TradingState, data: dict) -> List[Order]:
        od = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []
        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        if best_bid is None or best_ask is None:
            return orders

        raw_mid = (best_bid + best_ask) / 2.0
        current_spread = best_ask - best_bid
        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_FALLBACK))
        ema = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = round(ema)

        buy_cap = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # --- Osmium Swing Logic (Mean‑Reversion) ---
        swing_dir = data.get("osm_swing_direction", None)  # 'long' or 'short'
        swing_entry = data.get("osm_swing_entry", None)

        if swing_dir is None:
            # Look for entry: price deviates significantly from fair
            if best_ask < fair - self.OSM_SWING_DEVIATION and buy_cap > 0:
                vol = min(self.OSM_SWING_SIZE, buy_cap, -od.sell_orders.get(best_ask, 0))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, best_ask, vol))
                    buy_cap -= vol
                    data["osm_swing_direction"] = "long"
                    data["osm_swing_entry"] = best_ask
            elif best_bid > fair + self.OSM_SWING_DEVIATION and sell_cap > 0:
                vol = min(self.OSM_SWING_SIZE, sell_cap, od.buy_orders.get(best_bid, 0))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, best_bid, -vol))
                    sell_cap -= vol
                    data["osm_swing_direction"] = "short"
                    data["osm_swing_entry"] = best_bid
        else:
            # Manage open swing: exit when price reverts toward fair
            if swing_dir == "long":
                if best_bid > swing_entry + self.OSM_SWING_TARGET:
                    vol = min(self.OSM_SWING_SIZE, od.buy_orders.get(best_bid, 0))
                    if vol > 0:
                        orders.append(Order(self.OSMIUM, best_bid, -vol))
                        data.pop("osm_swing_direction", None)
                        data.pop("osm_swing_entry", None)
            else:  # short
                if best_ask < swing_entry - self.OSM_SWING_TARGET:
                    vol = min(self.OSM_SWING_SIZE, -od.sell_orders.get(best_ask, 0))
                    if vol > 0:
                        orders.append(Order(self.OSMIUM, best_ask, vol))
                        data.pop("osm_swing_direction", None)
                        data.pop("osm_swing_entry", None)

        # --- Aggressive Taking (original tight thresholds) ---
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

        # --- Mean Reversion Safety Net ---
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

        # --- Passive Market Making (asymmetric lite) ---
        skew = int(max(-6, min(6, pos * self.OSM_SKEW_FACTOR)))
        spread_factor = current_spread / 16.0
        base_l1 = max(3, min(5, round(4 * spread_factor)))
        base_l2 = max(5, min(9, round(7 * spread_factor)))
        base_l3 = max(9, min(13, round(11 * spread_factor)))
        l1_bid_offset = base_l1 + 1
        l1_ask_offset = max(2, base_l1 - 1)

        levels = [
            (l1_bid_offset, l1_ask_offset, self.OSM_L1_SIZE),
            (base_l2, base_l2, self.OSM_L2_SIZE),
            (base_l3, base_l3, self.OSM_L3_SIZE),
        ]
        long_bias = pos / self.LIMIT
        for b_off, a_off, base_sz in levels:
            if buy_cap <= 0 and sell_cap <= 0:
                break
            bid_px = fair - b_off - skew
            ask_px = fair + a_off - skew
            if bid_px >= ask_px:
                bid_px = fair - 1
                ask_px = fair + 1
            buy_sz = max(1, round(base_sz * (1 - max(0, long_bias) * 0.6)))
            sell_sz = max(1, round(base_sz * (1 - max(0, -long_bias) * 0.6)))
            if buy_cap > 0 and bid_px > 0:
                vol = min(buy_sz, buy_cap)
                orders.append(Order(self.OSMIUM, bid_px, vol))
                buy_cap -= vol
            if sell_cap > 0:
                vol = min(sell_sz, sell_cap)
                orders.append(Order(self.OSMIUM, ask_px, -vol))
                sell_cap -= vol
        return orders