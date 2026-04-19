import numpy as np
import math
from collections import Counter

def research(x: float) -> float:
    if x <= 0: return 0
    return 200_000 * math.log(1 + x) / math.log(101)

def scale(x: float) -> float:
    return 7.0 * x / 100.0

def speed_multiplier(rank: int, total_players: int) -> float:
    if total_players <= 1:
        return 0.9
    return 0.9 - 0.8 * (rank - 1) / (total_players - 1)

def generate_opponent_allocation(strategy: str) -> tuple[float, float, float]:
    if strategy == "balanced":
        r = np.random.normal(33, 6)
        s = np.random.normal(33, 6)
        sp = np.random.normal(34, 6)
    elif strategy == "research_bias":
        r = np.random.normal(50, 8)
        s = np.random.normal(25, 5)
        sp = np.random.normal(25, 5)
    elif strategy == "speed_averse":
        r = np.random.normal(40, 5)
        s = np.random.normal(40, 5)
        sp = np.random.normal(20, 5)
    elif strategy == "game_theory":
        r = np.random.normal(17, 3)
        s = np.random.normal(48, 3)
        sp = np.random.normal(35, 3)
    else:  # random
        r = np.random.uniform(0, 100)
        s = np.random.uniform(0, 100 - r)
        sp = np.random.uniform(0, 100 - r - s)
    
    total = r + s + sp
    if total > 0:
        r = (r / total) * 100
        s = (s / total) * 100
        sp = (sp / total) * 100
    else:
        r = s = sp = 33.33
    return max(0, r), max(0, s), max(0, sp)

def simulate_pnl(my_alloc: tuple, opponent_allocs: list) -> float:
    r, s, sp = my_alloc
    my_research = research(r)
    my_scale = scale(s)
    all_speeds = [sp] + [opp[2] for opp in opponent_allocs]
    sorted_speeds = sorted(all_speeds, reverse=True)
    rank = 1
    for speed in sorted_speeds:
        if speed > sp:
            rank += 1
        else:
            break
    mult = speed_multiplier(rank, len(all_speeds))
    return my_research * my_scale * mult - 50_000

def monte_carlo_psychology(n_opponents: int = 1000, n_simulations: int = 3000):
    # Realistic distribution based on Discord observations
    strategies = ["balanced", "research_bias", "speed_averse", "game_theory", "random"]
    probs = [0.45, 0.25, 0.15, 0.10, 0.05]
    
    candidates = []
    for r in range(0, 101, 2):
        for s in range(0, 101 - r, 2):
            sp = 100 - r - s
            if sp >= 0:
                candidates.append((r, s, sp))
    
    best_alloc = None
    best_pnl = -np.inf
    
    for alloc in candidates:
        total_pnl = 0.0
        for _ in range(n_simulations):
            opponents = []
            for _ in range(n_opponents):
                strat = np.random.choice(strategies, p=probs)
                opponents.append(generate_opponent_allocation(strat))
            total_pnl += simulate_pnl(alloc, opponents)
        avg = total_pnl / n_simulations
        if avg > best_pnl:
            best_pnl = avg
            best_alloc = alloc
    
    return best_alloc, best_pnl

if __name__ == "__main__":
    best, pnl = monte_carlo_psychology(n_opponents=500, n_simulations=1000)
    print(f"🎯 Optimal (Psychology‑Aware): Research {best[0]:.1f}%, Scale {best[1]:.1f}%, Speed {best[2]:.1f}%")
    print(f"   Expected PnL: {pnl:,.0f} XIRECs")