# trader.py – IMC Prosperity 4, Round 3 (Corrected Return Signature)
import json
import math
from typing import Dict, List, Tuple, Optional
from datamodel import OrderDepth, TradingState, Order

# ---------- Manual Normal CDF ----------
def norm_cdf(x: float) -> float:
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = 1.0 if x >= 0 else -1.0
    x = abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x)
    return 0.5 * (1.0 + sign * y)

def black_scholes_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)

def delta(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)

class Trader:
    # --- Round 2 Proven Parameters (8,434 Peak) ---
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    LIMIT = 80

    PEPPER_SLOPE = 0.001
    PEPPER_BUY_TOL = 12
    ENDGAME_START = 94_000

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

    ROUND_DAYS = 3
    MAX_TS = 99_900
    NEW_DAY_THRESH = 10_000

    # --- Options Parameters (Start Simple) ---
    UNDERLYING = "VOLCANIC_ROCK"
    OPTION_PREFIX = "VOLCANIC_ROCK_VOUCHER_"
    OPTION_STRIKES = [9500, 9750, 10000, 10250, 10500]
    OPTION_SIZE = 5
    ARBITRAGE_THRESHOLD = 10
    IMPLIED_VOL = 0.20
    RISK_FREE_RATE = 0.0
    TIME_TO_EXPIRY = 1.0 / 252

    def bid(self) -> int:
        return 6_500

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        # ✅ ALWAYS initialize orders to an empty dict
        orders: Dict[str, List[Order]] = {}
        data: dict = {}
        conversions = 0

        try:
            # Load persistent state
            if state.traderData:
                try:
                    data = json.loads(state.traderData)
                except:
                    data = {}
            else:
                data = {}

            ts = state.timestamp
            prev_ts = data.get("last_ts", -1)
            day = data.get("day", 0)

            # Day rollover
            if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
                day += 1
                data["day"] = day
                data.pop("pepper_anchor", None)
                data.pop("pepper_recent_high", None)
                data.pop("pepper_scalp_entry", None)

            # Pepper anchor
            if "pepper_anchor" not in data:
                data["pepper_anchor"] = self._pepper_anchor(state)

            pepper_fair = data["pepper_anchor"] + self.PEPPER_SLOPE * ts
            is_last_day = (day >= self.ROUND_DAYS - 1)
            is_endgame = is_last_day and (ts >= self.ENDGAME_START)

            # --- Pepper Orders ---
            if self.PEPPER in state.order_depths:
                pepper_ords = self._pepper_orders(state, ts, pepper_fair, is_endgame, data)
                if pepper_ords:
                    orders[self.PEPPER] = pepper_ords

            # --- Osmium Orders ---
            if self.OSMIUM in state.order_depths:
                osmium_ords = self._osmium_orders(state, data)
                if osmium_ords:
                    orders[self.OSMIUM] = osmium_ords
                 
            # --- Options Arbitrage (Minimalist) ---
            if self.UNDERLYING in state.order_depths:
                ud = state.order_depths[self.UNDERLYING]
                if ud.buy_orders and ud.sell_orders:
                    S = (max(ud.buy_orders.keys()) + min(ud.sell_orders.keys())) / 2.0
                    for strike in self.OPTION_STRIKES:
                        symbol = f"{self.OPTION_PREFIX}{strike}"
                        if symbol not in state.order_depths:
                            continue
                        od = state.order_depths[symbol]
                        if not od.buy_orders or not od.sell_orders:
                            continue
                        best_bid = max(od.buy_orders.keys())
                        best_ask = min(od.sell_orders.keys())
                        theo = black_scholes_call(S, strike, self.TIME_TO_EXPIRY, self.RISK_FREE_RATE, self.IMPLIED_VOL)
                        d = delta(S, strike, self.TIME_TO_EXPIRY, self.RISK_FREE_RATE, self.IMPLIED_VOL)

                        if best_ask < theo - self.ARBITRAGE_THRESHOLD:
                            vol = min(self.OPTION_SIZE, -od.sell_orders[best_ask])
                            if vol > 0:
                                orders.setdefault(symbol, []).append(Order(symbol, best_ask, vol))
                                hedge_qty = int(vol * d)
                                if hedge_qty > 0:
                                    orders.setdefault(self.UNDERLYING, []).append(Order(self.UNDERLYING, best_bid, -hedge_qty))
                        elif best_bid > theo + self.ARBITRAGE_THRESHOLD:
                            vol = min(self.OPTION_SIZE, od.buy_orders[best_bid])
                            if vol > 0:
                                orders.setdefault(symbol, []).append(Order(symbol, best_bid, -vol))
                                hedge_qty = int(vol * d)
                                if hedge_qty > 0:
                                    orders.setdefault(self.UNDERLYING, []).append(Order(self.UNDERLYING, best_ask, hedge_qty))

            data["last_ts"] = ts

        except Exception as e:
            # If anything fails, we still return valid empty orders to avoid crash
            print(f"Trader error: {e}")
        finally:
            # ✅ GUARANTEED 3‑tuple return
            trader_data = json.dumps(data)
            return orders, conversions, trader_data

    # ---------- Pepper Methods (Unchanged from 8,434) ----------
    def _pepper_anchor(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER)
        if od and od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od and od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    def _pepper_orders(self, state: TradingState, ts: int, fair: float, is_endgame: bool, data: dict) -> List[Order]:
        od = state.order_depths.get(self.PEPPER)
        if not od:
            return []
        pos = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []
        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else fair

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
                if buy_cap > 0 and od.sell_orders:
                    best_ask = min(od.sell_orders.keys())
                    orders.append(Order(self.PEPPER, best_ask, buy_cap))

            # Scalp
            if "pepper_recent_high" not in data:
                data["pepper_recent_high"] = mid
            else:
                data["pepper_recent_high"] = max(data["pepper_recent_high"], mid)
            scalp_entry = data.get("pepper_scalp_entry")
            if scalp_entry is None and best_ask and best_ask < data["pepper_recent_high"] - 8:
                remaining = self.LIMIT - pos
                if remaining > 0:
                    vol = min(10, remaining, -od.sell_orders.get(best_ask, 0))
                    if vol > 0:
                        orders.append(Order(self.PEPPER, best_ask, vol))
                        data["pepper_scalp_entry"] = best_ask
            if scalp_entry is not None and best_bid and best_bid > scalp_entry + 5:
                vol = min(10, od.buy_orders.get(best_bid, 0))
                if vol > 0:
                    orders.append(Order(self.PEPPER, best_bid, -vol))
                    data["pepper_scalp_entry"] = None
                    data["pepper_recent_high"] = mid
        else:
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

    # ---------- Osmium Methods (Unchanged from 8,434) ----------
    def _osmium_orders(self, state: TradingState, data: dict) -> List[Order]:
        od = state.order_depths.get(self.OSMIUM)
        if not od:
            return []
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []
        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        if best_bid is None or best_ask is None:
            return orders

        raw_mid = (best_bid + best_ask) / 2.0
        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_FALLBACK))
        ema = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = round(ema)

        buy_cap = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

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

        # Original MR
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

        skew = int(max(-6, min(6, pos * self.OSM_SKEW_FACTOR)))
        spread_factor = (best_ask - best_bid) / 16.0
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