from trader_68 import Trader as Trader68


class Trader(Trader68):
    PEPPER_BUY_TOL = 4
    ENDGAME_START = 92_000
    SCALP_RESERVE = 0

    OSM_PASSIVE_BID_OFFSET = 5
    OSM_PASSIVE_ASK_OFFSET = 5
    OSM_SELL_COOLDOWN = 300
