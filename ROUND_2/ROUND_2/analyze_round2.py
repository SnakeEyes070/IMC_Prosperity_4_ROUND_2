# analyze_round2.py – Extract optimal parameters from Round 2 data capsule
import pandas as pd
import numpy as np

# Load all three days of price data
files = [
    "prices_round_2_day_-1.csv",
    "prices_round_2_day_1.csv",
    "prices_round_2_day_0.csv"
]

dfs = []
for f in files:
    try:
        df = pd.read_csv(f, delimiter=";")
        dfs.append(df)
        print(f"Loaded {f}: {len(df)} rows")
    except Exception as e:
        print(f"Error loading {f}: {e}")

df = pd.concat(dfs, ignore_index=True)
df['mid_price'] = (df['bid_price_1'] + df['ask_price_1']) / 2

# ============================================================
# PEPPER ANALYSIS
# ============================================================
pepper = df[df['product'] == 'INTARIAN_PEPPER_ROOT'].copy()
pepper = pepper.sort_values('timestamp')

# Compute daily slope (linear regression per day)
pepper['day'] = pepper['timestamp'] // 100000  # Approximate day grouping
slopes = []
for day in pepper['day'].unique():
    day_data = pepper[pepper['day'] == day]
    if len(day_data) > 10:
        x = day_data['timestamp'].values
        y = day_data['mid_price'].values
        slope, _ = np.polyfit(x, y, 1)
        slopes.append(slope)

avg_slope = np.mean(slopes)
print(f"\n📈 PEPPER")
print(f"   Average daily slope: {avg_slope:.6f} per timestamp unit")

# Opening spread (first 10 ticks of each day)
opening_spreads = []
for day in pepper['day'].unique():
    day_data = pepper[pepper['day'] == day].head(10)
    spreads = day_data['ask_price_1'] - day_data['bid_price_1']
    opening_spreads.extend(spreads.dropna().tolist())

avg_opening_spread = np.mean(opening_spreads)
print(f"   Average opening spread: {avg_opening_spread:.2f} ticks")

# Recommended buy tolerance (half of opening spread, plus buffer)
recommended_tolerance = int(avg_opening_spread / 2) + 4
print(f"   Recommended PEPPER_BUY_TOL: {recommended_tolerance}")

# ============================================================
# OSMIUM ANALYSIS
# ============================================================
osmium = df[df['product'] == 'ASH_COATED_OSMIUM'].copy()
osmium = osmium.sort_values('timestamp')

# Spread statistics
osmium['spread'] = osmium['ask_price_1'] - osmium['bid_price_1']
avg_spread = osmium['spread'].mean()
std_spread = osmium['spread'].std()
print(f"\n📊 OSMIUM")
print(f"   Average spread: {avg_spread:.2f} ticks")
print(f"   Spread std dev: {std_spread:.2f} ticks")

# Fair value (mean mid price)
fair_mean = osmium['mid_price'].mean()
fair_std = osmium['mid_price'].std()
print(f"   Fair value mean: {fair_mean:.2f}")
print(f"   Fair value std dev: {fair_std:.2f} ticks")

# Autocorrelation (mean reversion strength)
osmium['returns'] = osmium['mid_price'].diff()
autocorr = osmium['returns'].autocorr(lag=1)
print(f"   Lag-1 autocorrelation: {autocorr:.4f}")

# Optimal EMA alpha (half-life ~8-12 ticks)
# alpha = 1 - exp(ln(0.5) / half_life)
half_life = 10  # ticks
optimal_alpha = 1 - np.exp(np.log(0.5) / half_life)
print(f"   Recommended OSM_EMA_ALPHA: {optimal_alpha:.3f}")

# Mean reversion threshold (based on standard deviation)
# Use ~1.5σ for aggressive but safe threshold
mr_threshold = int(np.ceil(1.5 * fair_std))
print(f"   Recommended OSM_MR_THRESH: {mr_threshold}")

# Optimal quote offsets (half of average spread, minus small safety margin)
half_spread = avg_spread / 2
l1_offset = max(3, int(half_spread - 3))
print(f"   Recommended OSM_L1_OFFSET: {l1_offset}")

# ============================================================
# VOLUME ANALYSIS (for sizing)
# ============================================================
avg_bid_vol = osmium['bid_volume_1'].mean()
avg_ask_vol = osmium['ask_volume_1'].mean()
print(f"\n📦 VOLUME")
print(f"   Average L1 bid volume: {avg_bid_vol:.1f} units")
print(f"   Average L1 ask volume: {avg_ask_vol:.1f} units")

# Recommended passive sizes (aim to capture ~30-40% of typical volume)
l1_size = int(min(avg_bid_vol, avg_ask_vol) * 0.6)
l2_size = int(l1_size * 0.7)
l3_size = int(l1_size * 0.5)
print(f"   Recommended OSM_L1_SIZE: {l1_size}")
print(f"   Recommended OSM_L2_SIZE: {l2_size}")
print(f"   Recommended OSM_L3_SIZE: {l3_size}")

# ============================================================
# ENDGAME TIMING (Pepper unwind)
# ============================================================
# Find when price peaks on final day (day 2)
final_day = pepper[pepper['day'] == pepper['day'].max()]
if len(final_day) > 0:
    peak_idx = final_day['mid_price'].idxmax()
    peak_ts = final_day.loc[peak_idx, 'timestamp']
    max_ts = final_day['timestamp'].max()
    # Start unwind ~300 ticks before max to smooth exit
    recommended_endgame = max_ts - 300
    print(f"\n⏱️ TIMING")
    print(f"   Final day max timestamp: {max_ts}")
    print(f"   Price peak at: {peak_ts}")
    print(f"   Recommended ENDGAME_START: {recommended_endgame}")

# ============================================================
# SUMMARY – OPTIMIZED PARAMETERS
# ============================================================
print("\n" + "=" * 60)
print("🎯 OPTIMIZED PARAMETERS FOR ROUND 2")
print("=" * 60)
print(f"PEPPER_SLOPE      = {avg_slope:.6f}")
print(f"PEPPER_BUY_TOL    = {recommended_tolerance}")
print(f"OSM_EMA_ALPHA     = {optimal_alpha:.3f}")
print(f"OSM_L1_SIZE       = {l1_size}")
print(f"OSM_L2_SIZE       = {l2_size}")
print(f"OSM_L3_SIZE       = {l3_size}")
print(f"OSM_MR_THRESH     = {mr_threshold}")
print(f"ENDGAME_START     = {int(recommended_endgame) if 'recommended_endgame' in locals() else 96500}")