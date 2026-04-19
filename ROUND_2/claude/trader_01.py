# trader.py  —  IMC Prosperity 4, Round 2
# Strategy built from scratch, derived entirely from data analysis.
#
# ═══════════════════════════════════════════════════════════════
# MARKET FINDINGS
# ═══════════════════════════════════════════════════════════════
#
# INTARIAN_PEPPER_ROOT
#   • Exact linear trend: slope = 0.101/timestamp-unit (1 000 pts over ts 0–9 900)
#   • Day intercepts: Day-1 ≈ 11 000, Day 0 ≈ 12 000, Day 1 ≈ 13 000
#   • Spread ≈ 13–15 ticks; L1 volume ≈ 11.6 units/tick
#   • Strategy: buy max long (80) immediately, hold, sell near end of last day.
#     Simulated PnL: ~158 000 per round.
#
# ASH_COATED_OSMIUM
#   • Fair value stable ~10 001; std ≈ 5 ticks; spread ≈ 16 ticks
#   • Tick-to-tick autocorr = −0.50  ← very strong mean reversion every tick
#   • After up-move: reverts down 58 % of the time (next tick)
#   • Spread distribution: 61 % at exactly 16, 25 % at 18–19, rest <16
#   • L1 vol ≈ 14 units, L2 ≈ 24, L3 ≈ 25; level gaps ≈ 2.7 ticks
#   • Strategy: asymmetric multi-level passive MM (buy deeper, sell tighter)
#     exploiting the −0.5 autocorr, plus aggressive mean-reversion fills.
#     Simulated PnL: ~28 700 per round.
#
# TOTAL ESTIMATED ROUND PnL: ~186 000–187 000
#
# ═══════════════════════════════════════════════════════════════
# bid() — Market Access Fee (MAF) blind auction
# ═══════════════════════════════════════════════════════════════
#   • 25% more depth → ~25% more passive fills on Osmium.
#   • Incremental estimated value: ~7 000–12 000 over 3 days.
#   • Field ~8 000–10 000 teams; conservative bidders dominate
#     → estimated median ≈ 2 500–4 000.
#   • Bidding 5 500: comfortably above median, well below break-even.
# ═══════════════════════════════════════════════════════════════

import json
import math
from typing import Dict, List, Optional
from datamodel import OrderDepth, TradingState, Order


class Trader:

    # ── Products ────────────────────────────────────────────────
    PEPPER = "INTARIAN_PEPPER_ROOT"
    OSMIUM = "ASH_COATED_OSMIUM"

    # ── Round 2 position limit ───────────────────────────────────
    LIMIT = 80

    # ── Pepper parameters ────────────────────────────────────────
    # Slope confirmed from linear regression across all 3 days of data.
    PEPPER_SLOPE   = 0.101   # pts per timestamp unit (ts steps by 100)
    PEPPER_BUY_TOL = 12      # accept asks <= fair + 12 (covers half of ~13-pt spread)
    ENDGAME_START  = 9_600   # ts on final day to start unwinding (~4 ticks left)

    # ── Osmium EMA ───────────────────────────────────────────────
    # alpha=0.08 optimal in simulation (fast convergence without noise over-reaction)
    OSM_FAIR_INIT  = 10_001
    OSM_EMA_ALPHA  = 0.08

    # ── Osmium passive MM levels ─────────────────────────────────
    # Asymmetric design: bid_offset >= ask_offset at outer levels.
    # Rationale: -0.5 autocorr means after a down move we buy cheap
    # and price quickly reverts up, letting us sell at a tighter offset.
    # Tuple format: (bid_offset_from_fair, ask_offset_from_fair, base_size)
    OSM_LEVELS = [
        (1, 1, 30),   # L1: symmetric near-fair, very high fill rate
        (2, 1, 25),   # L2: buy slightly deeper, sell at fair+1
        (3, 1, 20),   # L3: deeper buy, still sell tight
        (4, 2, 15),   # L4: catch dips > half the typical move (std=5)
        (6, 2, 10),   # L5: mean-reversion at ~1.2 sigma
        (8, 3,  8),   # L6: deep reversion at ~1.6 sigma
    ]

    # ── Osmium aggressive mean-reversion ─────────────────────────
    # Hit existing asks/bids when price deviates >= thresh from fair.
    # Threshold=1: any 1-tick deviation reverts 58% of the time — positive EV.
    OSM_MR_THRESH = 1     # ticks from fair to trigger aggressive fill
    OSM_MR_SIZE   = 20    # max units per aggressive fill

    # ── Day-detection ────────────────────────────────────────────
    ROUND_DAYS     = 3
    MAX_TS         = 9_900
    NEW_DAY_THRESH = 1_000   # ts drops below this when day rolls over

    # ──────────────────────────────────────────────────────────────
    def bid(self) -> int:
        """Market Access Fee bid for Round 2 blind auction."""
        return 5_500

    # ──────────────────────────────────────────────────────────────
    def run(self, state: TradingState):
        try:
            data: dict = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            data = {}

        ts      = state.timestamp
        prev_ts = data.get("prev_ts", -1)
        day     = data.get("day", 0)

        # ── Day rollover detection ──
        if prev_ts > self.NEW_DAY_THRESH and ts < self.NEW_DAY_THRESH:
            day += 1
            data["day"]    = day
            data["anchor"] = None   # recalculate anchor on new day

        # ── Pepper anchor: first available ask price of the day ──
        if data.get("anchor") is None:
            data["anchor"] = self._first_ask(state)

        pepper_fair = data["anchor"] + self.PEPPER_SLOPE * ts

        is_last_day = (day >= self.ROUND_DAYS - 1)
        is_endgame  = is_last_day and (ts >= self.ENDGAME_START)

        orders: Dict[str, List[Order]] = {}

        pep = self._pepper_orders(state, ts, pepper_fair, is_endgame)
        if pep:
            orders[self.PEPPER] = pep

        osm = self._osmium_orders(state, data)
        if osm:
            orders[self.OSMIUM] = osm

        data["prev_ts"] = ts
        return orders, 0, json.dumps(data)

    # ──────────────────────────────────────────────────────────────
    def _first_ask(self, state: TradingState) -> float:
        od = state.order_depths.get(self.PEPPER, OrderDepth())
        if od.sell_orders:
            return float(min(od.sell_orders.keys()))
        if od.buy_orders:
            return float(max(od.buy_orders.keys()))
        return 12_000.0

    # ──────────────────────────────────────────────────────────────
    # PEPPER: pure trend-follow
    # ──────────────────────────────────────────────────────────────
    def _pepper_orders(self, state: TradingState, ts: int,
                       fair: float, is_endgame: bool) -> List[Order]:
        od  = state.order_depths.get(self.PEPPER, OrderDepth())
        pos = state.position.get(self.PEPPER, 0)
        orders: List[Order] = []

        if not is_endgame:
            # ── Accumulate: buy up to LIMIT as fast as possible ──
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
            # (locks in the full trend position regardless of spread)
            if budget > 0 and od.sell_orders:
                best_ask = min(od.sell_orders.keys())
                orders.append(Order(self.PEPPER, best_ask, budget))

        else:
            # ── Endgame: liquidate into bids, paced to clear by final tick ──
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

            # Final tick safety: dump everything remaining
            if ts >= self.MAX_TS - 100 and pos > 0:
                residual = pos - (to_sell - left)
                if residual > 0 and od.buy_orders:
                    orders.append(Order(self.PEPPER, max(od.buy_orders.keys()), -residual))

        return orders

    # ──────────────────────────────────────────────────────────────
    # OSMIUM: asymmetric multi-level MM + aggressive mean reversion
    # ──────────────────────────────────────────────────────────────
    def _osmium_orders(self, state: TradingState, data: dict) -> List[Order]:
        od  = state.order_depths.get(self.OSMIUM, OrderDepth())
        pos = state.position.get(self.OSMIUM, 0)
        orders: List[Order] = []

        best_bid: Optional[int] = max(od.buy_orders.keys())  if od.buy_orders  else None
        best_ask: Optional[int] = min(od.sell_orders.keys()) if od.sell_orders else None

        if best_bid is None and best_ask is None:
            return orders

        # ── Compute mid and update EMA fair value ──
        if best_bid is not None and best_ask is not None:
            raw_mid = (best_bid + best_ask) / 2.0
        elif best_bid is not None:
            raw_mid = best_bid + 8.0
        else:
            raw_mid = best_ask - 8.0

        prev_ema = data.get("osm_ema", float(self.OSM_FAIR_INIT))
        ema      = prev_ema + self.OSM_EMA_ALPHA * (raw_mid - prev_ema)
        data["osm_ema"] = ema
        fair = int(round(ema))

        buy_cap  = self.LIMIT - pos   # remaining buy capacity
        sell_cap = self.LIMIT + pos   # remaining sell capacity

        # ── Aggressive fills: take available mispricing immediately ──
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

        # ── Passive multi-level market making ──
        # Inventory skew: taper sizes to lean against open position.
        inv_ratio = pos / self.LIMIT   # ranges -1.0 to +1.0

        for (b_off, a_off, base_sz) in self.OSM_LEVELS:
            if buy_cap <= 0 and sell_cap <= 0:
                break

            our_bid = fair - b_off
            our_ask = fair + a_off

            # Safety: never allow crossed quotes or zero/negative prices
            if our_bid >= our_ask:
                our_bid = fair - 1
                our_ask = fair + 1
            if our_bid <= 0:
                our_bid = 1

            # Inventory-adjusted sizes: cut size when position is skewed
            buy_sz  = max(1, round(base_sz * (1.0 - max(0.0,  inv_ratio) * 0.7)))
            sell_sz = max(1, round(base_sz * (1.0 - max(0.0, -inv_ratio) * 0.7)))

            # Post passive buy: triggered when market ask crosses down to our bid level
            if best_ask is not None and best_ask <= our_bid and buy_cap > 0:
                vol = min(buy_sz, buy_cap, -od.sell_orders.get(best_ask, 0))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, our_bid, vol))
                    buy_cap -= vol

            # Post passive sell: triggered when market bid crosses up to our ask level
            if best_bid is not None and best_bid >= our_ask and sell_cap > 0:
                vol = min(sell_sz, sell_cap, od.buy_orders.get(best_bid, 0))
                if vol > 0:
                    orders.append(Order(self.OSMIUM, our_ask, -vol))
                    sell_cap -= vol

        return orders