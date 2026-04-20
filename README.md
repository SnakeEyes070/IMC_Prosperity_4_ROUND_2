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

## 📁 Repository Structure
.
├── trader.py # Final optimized submission (8,678 peak)
├── trader_68.py # Frozen control baseline (8,643 logic)
├── trader_69.py # Active‑log optimized variant
├── round2_replay.py # Local replay harness
├── round2_replay_lib.py # Backtesting library
├── round2_ablation.py # Parameter sweep runner
├── round2_bid_calculator.py # MAF bid game‑theory calculator
├── logs/ # Activity logs from top runs
│ ├── 323875.log # 8,678 peak run
│ ├── 321787.log # 8,412 run (bad seed)
│ └── 333175.log # 8,593 run (mid seed)
└── README.md # This file

text

## 🚀 How to Run

This code is designed for the **IMC Prosperity 4** environment. To use it:

1. Copy the contents of `trader.py` into the Prosperity web editor.
2. Ensure the `datamodel` import is available (provided by the platform).
3. Submit the code for Round 2 evaluation.

### Local Backtesting (Optional)

A Rust‑based backtester was used during development. To run it locally:

```bash
# Install the backtester
cargo install rust_backtester --locked

# Run against Round 2 sample data
rust_backtester --trader trader.py --dataset round2
💰 Market Access Fee (MAF)
The bid() method returns 2,100 XIRECs. This is a low‑risk, asymmetric bet:

If the bid is below the median, you pay nothing and keep all profit.

If the bid is in the top 50%, you gain access to 25% extra quotes for a small fee.

The extra flow is estimated to add ~2,000–3,000 raw XIRECs in final scoring, making the expected value positive.

🔬 Key Learnings
Simplicity wins: Every attempt to add complex features (dynamic offsets, laddered aggression, drift guards) reduced PnL.

Seed variance is real: 93% of the PnL gap between runs was driven by the simulation's counterparty bots, not strategy parameters.

Forensic analysis is the edge: Tick‑by‑tick log analysis (using Claude and local backtesting) revealed structural leaks that parameter sweeps alone could not find.

📫 Contact
GitHub: @SnakeEyes070

Competition: IMC Prosperity 4 – Round 2 Finalist

Built with relentless iteration, forensic log analysis, and a bit of game theory. 🚀

text

Once you create this file, add it to your repository:

```bash
git add README.md
git commit -m "Add project README"
git push -u origin master:main



*Note: Actual live PnL varies by ±100–150 XIRECs due to the platform's randomized 80% quote subset and counterparty bot behavior.* 

