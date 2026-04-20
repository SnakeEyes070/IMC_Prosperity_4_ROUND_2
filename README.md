# IMC Prosperity 4 – Round 2 Algorithmic Trading

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![IMC Prosperity](https://img.shields.io/badge/IMC-Prosperity%204-orange)](https://prosperity.imc.com/)

## 📈 Overview

This repository contains my final algorithmic trading solution for **Round 2** of the IMC Prosperity 4 competition. The strategy achieved a **peak raw PnL of 8,678 XIRECs** (86,780 after the 10× multiplier), placing it among the top-performing submissions globally.

The algorithm trades two distinct assets:
- **INTARIAN_PEPPER_ROOT** – A strong, predictable linear uptrend (~100 points per day).
- **ASH_COATED_OSMIUM** – A mean-reverting asset oscillating around a fair value of ~10,004.

## 🧠 Strategy Overview

### 🌶️ Pepper – Trend Following
- **Entry:** Aggressively buys the full 80-unit position within the first 300–400 ticks using a tight tolerance (`PEPPER_BUY_TOL = 4`).
- **Intraday Scalp:** Captures small pullbacks (dip ≥8 ticks, exit at +5 ticks) to add incremental edge.
- **Exit:** Gradual unwind starting at `t = 92,000`, executing at a 2.5× pace to avoid slippage in thinning end-of-day volume.

### 🪨 Osmium – Market Making & Mean Reversion
- **Fair Value:** EMA-based fair value (`α = 0.02`) anchored near 10,000.
- **Passive Quotes:** Resting orders placed at `fair ± 5` ticks, capturing the spread.
- **Aggressive Thresholds:** 
  - Buys when `best_ask ≤ 10,000`.
  - Sells when `best_bid ≥ 10,003` with a **300‑tick cooldown** to prevent adverse fills.
- **Structural Insights:** Forensic analysis of multiple logs revealed that Osmium PnL variance is heavily seed‑driven (presence of a t=400 liquidity‑taking bot). The final parameters strike a balance between fill rate and adverse selection.

## 🏆 Performance

| Metric | Value |
|--------|-------|
| **Peak Raw PnL** | 8,678 XIRECs |
| **After 10× Multiplier** | 86,780 XIRECs |
| **Pepper Contribution** | ~7,292 XIRECs (84%) |
| **Osmium Contribution** | ~1,386 XIRECs (16%) |

*Note: Actual live PnL varies by ±100–150 XIRECs due to the platform's randomized 80% quote subset and counterparty bot behavior.* 

