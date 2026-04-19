# trader.py - IMC Prosperity 4, Round 2 (Active-Log Winner)
#
# This version keeps the 315520 control logic from trader_68 and promotes the
# parameter set that won on both replayable active log markets.

from trader_68 import Trader as FrozenControlTrader


class Trader(FrozenControlTrader):
    PEPPER_BUY_TOL = 4
    ENDGAME_START = 92_000
    SCALP_RESERVE = 6
    OSM_PASSIVE_BID_OFFSET = 5
    OSM_PASSIVE_ASK_OFFSET = 5
