# trader.py — IMC Prosperity 4, Round 2 (纯数据驱动优化)
# Pepper: 开盘买入，持有至结算，不主动平仓
# Osmium: 更宽的被动报价 + 库存偏斜 + 严格不平衡过滤

import json
import math
from typing import Dict, List, Tuple
from datamodel import Order, OrderDepth, TradingState

class Trader:
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"
    LIMIT = 80

    # --- Pepper（数据：7292 PnL，持有至结算）---
    PEPPER_SLOPE = 0.001
    PEPPER_BUY_TOL = 4
    ENDGAME_START = 999_999           # 永不触发平仓 —— 持有至结算
    SCALP_RESERVE = 0
    SCALP_DIP = 6                     # 数据：62次回调≥6
    SCALP_EXIT = 4                    # 更快止盈
    SCALP_SIZE = 10

    # --- Osmium（数据：+4434正成交，-3048负成交 —— 必须减少不利成交）---
    OSM_FAIR_FALLBACK = 10_000
    OSM_EMA_ALPHA = 0.02
    OSM_PASSIVE_BID_OFFSET = 7        # 加宽至7，远离市场自然价差
    OSM_PASSIVE_ASK_OFFSET = 7
    OSM_PASSIVE_SIZE = 19
    OSM_MR_THRESH = 8
    OSM_MR_MAX_QTY = 24
    OSM_AGGRESSIVE_BUY_THRESH = 10_000
    OSM_AGGRESSIVE_SELL_THRESH = 10_002   # 捕获10次错失的有利卖出
    OSM_SELL_COOLDOWN = 0             # 移除冷却，不错过机会

    # 启用库存偏斜 —— 减少被动持仓积累
    OSM_USE_PASSIVE_SKEW = True
    OSM_SKEW_PER_UNIT = 0.1

    # 更严格的不平衡过滤器 —— 只在订单簿平衡时报价
    OSM_IMBALANCE_THRESH = 0.15

    # 保持阶梯式激进为关闭（简化）
    OSM_USE_LADDERED_AGGRESSION = False
    OSM_AGG_SIZE_L1 = 12
    OSM_AGG_SIZE_L2 = 8
    OSM_AGG_DEEPER = 2

    # --- 时间参数 ---
    ROUND_DAYS = 3
    MAX_TS = 99_900
    NEW_DAY_THRESH = 10_000

    def bid(self) -> int:
        return 2_250   # 低风险彩票

    # ------------------------------------------------------------------
    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        try:
            data = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts = state.timestamp
        prev_ts = data.get("last_ts", -1)
        day = data.get("day", 0)

        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"] = day
            data.pop("pepper_anchor", None)
            data.pop("pepper_recent_high", None)
            data.pop("pepper_scalp_entry", None)

        if "pepper_anchor" not in data:
            data["pepper_anchor"] = self._pepper_anchor(state)

        pepper_fair = data["pepper_anchor"] + self.PEPPER_SLOPE * ts
        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame = is_last_day and (ts >= self.ENDGAME_START)   # 永不为真

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

    # ---------- Pepper ----------
    def _pepper_anchor(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    def _pepper_orders(self, state: TradingState, fair: float,
                       is_endgame: bool, data: dict) -> List[Order]:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        pos = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else fair

        # 无平仓阶段 —— 持有至结算
        if not is_endgame:
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
                    passive_bid = min(od.sell_orders.keys()) - 1
                    orders.append(Order(self.PEPPER, passive_bid, main_cap))

            # 短线剥头皮 —— 数据证实有效
            if "pepper_recent_high" not in data:
                data["pepper_recent_high"] = mid
            else:
                data["pepper_recent_high"] = max(data["pepper_recent_high"], mid)

            scalp_entry = data.get("pepper_scalp_entry")
            if scalp_entry is None and best_ask is not None:
                trigger = data["pepper_recent_high"] - self.SCALP_DIP
                if best_ask < trigger:
                    remaining = self.LIMIT - pos
                    vol = min(self.SCALP_SIZE, remaining, -od.sell_orders.get(best_ask, 0))
                    if vol > 0:
                        orders.append(Order(self.PEPPER, best_ask, vol))
                        data["pepper_scalp_entry"] = best_ask

            if scalp_entry is not None and best_bid and best_bid > scalp_entry + self.SCALP_EXIT:
                vol = min(self.SCALP_SIZE, od.buy_orders.get(best_bid, 0))
                if vol > 0:
                    orders.append(Order(self.PEPPER, best_bid, -vol))
                    data["pepper_scalp_entry"] = None
                    data["pepper_recent_high"] = mid

        return orders

    # ---------- Osmium（纯数据驱动减少不利成交）----------
    def _osmium_orders(self, state: TradingState, data: dict) -> List[Order]:
        od = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
        if best_bid is None or best_ask is None:
            return orders

        # 订单簿不平衡
        bid_vol = sum(od.buy_orders.values())
        ask_vol = sum(abs(v) for v in od.sell_orders.values())
        total_vol = bid_vol + ask_vol
        imbalance = (bid_vol - ask_vol) / total_vol if total_vol > 0 else 0

        # 微观价格（成交量加权）
        bv = od.buy_orders[best_bid]
        av = -od.sell_orders[best_ask]
        if bv + av > 0:
            raw_mid = (best_bid * av + best_ask * bv) / (bv + av)
        else:
            raw_mid = (best_bid + best_ask) / 2.0

        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_FALLBACK))
        ema = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = round(ema)

        buy_cap = self.LIMIT - pos
        sell_cap = self.LIMIT + pos

        # 均值回归 —— 仅在不平衡有利时触发
        if od.sell_orders and buy_cap > 0 and imbalance > -self.OSM_IMBALANCE_THRESH:
            for ask_px in sorted(od.sell_orders.keys()):
                if ask_px > fair - self.OSM_MR_THRESH:
                    break
                vol = min(buy_cap, -od.sell_orders[ask_px], self.OSM_MR_MAX_QTY)
                if vol > 0:
                    orders.append(Order(self.OSMIUM, ask_px, vol))
                    buy_cap -= vol

        if od.buy_orders and sell_cap > 0 and imbalance < self.OSM_IMBALANCE_THRESH:
            for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                if bid_px < fair + self.OSM_MR_THRESH:
                    break
                vol = min(sell_cap, od.buy_orders[bid_px], self.OSM_MR_MAX_QTY)
                if vol > 0:
                    orders.append(Order(self.OSMIUM, bid_px, -vol))
                    sell_cap -= vol

        # 激进吃单 —— 严格不平衡把关
        if best_ask <= self.OSM_AGGRESSIVE_BUY_THRESH and buy_cap > 0 and imbalance > -0.25:
            vol = min(self.OSM_MR_MAX_QTY, buy_cap, -od.sell_orders.get(best_ask, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_ask, vol))
                buy_cap -= vol

        if best_bid >= self.OSM_AGGRESSIVE_SELL_THRESH and sell_cap > 0 and imbalance < 0.25:
            vol = min(self.OSM_MR_MAX_QTY, sell_cap, od.buy_orders.get(best_bid, 0))
            if vol > 0:
                orders.append(Order(self.OSMIUM, best_bid, -vol))
                sell_cap -= vol

        # 库存偏斜
        skew = int(round(self.OSM_SKEW_PER_UNIT * pos)) if self.OSM_USE_PASSIVE_SKEW else 0

        # 被动报价 —— 更宽偏移，严格不平衡过滤
        if buy_cap > 0 and imbalance > -self.OSM_IMBALANCE_THRESH:
            bid_px = fair - self.OSM_PASSIVE_BID_OFFSET - skew
            orders.append(Order(self.OSMIUM, bid_px, min(self.OSM_PASSIVE_SIZE, buy_cap)))

        if sell_cap > 0 and imbalance < self.OSM_IMBALANCE_THRESH:
            ask_px = fair + self.OSM_PASSIVE_ASK_OFFSET - skew
            orders.append(Order(self.OSMIUM, ask_px, -min(self.OSM_PASSIVE_SIZE, sell_cap)))

        return orders