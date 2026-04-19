# datamodel.py - IMC Prosperity Official Data Structures
from typing import Dict, List
import json
from json import JSONEncoder

Time = int
Symbol = str
Product = str
Position = int
UserId = str

class Listing:
    def __init__(self, symbol: Symbol, product: Product, denomination: Product):
        self.symbol = symbol
        self.product = product
        self.denomination = denomination

class OrderDepth:
    def __init__(self):
        self.buy_orders: Dict[int, int] = {}
        self.sell_orders: Dict[int, int] = {}

class Trade:
    def __init__(self, symbol: Symbol, price: int, quantity: int,
                 buyer: UserId = None, seller: UserId = None, timestamp: int = 0):
        self.symbol = symbol
        self.price = price
        self.quantity = quantity
        self.buyer = buyer
        self.seller = seller
        self.timestamp = timestamp

class Order:
    def __init__(self, symbol: Symbol, price: int, quantity: int):
        self.symbol = symbol
        self.price = price
        self.quantity = quantity

class Observation:
    def __init__(self, plainValueObservations=None, conversionObservations=None):
        self.plainValueObservations = plainValueObservations or {}
        self.conversionObservations = conversionObservations or {}

class TradingState:
    def __init__(self, traderData: str, timestamp: Time,
                 listings: Dict[Symbol, Listing],
                 order_depths: Dict[Symbol, OrderDepth],
                 own_trades: Dict[Symbol, List[Trade]],
                 market_trades: Dict[Symbol, List[Trade]],
                 position: Dict[Product, Position],
                 observations: Observation):
        self.traderData = traderData
        self.timestamp = timestamp
        self.listings = listings
        self.order_depths = order_depths
        self.own_trades = own_trades
        self.market_trades = market_trades
        self.position = position
        self.observations = observations

    def toJSON(self):
        return json.dumps(self, default=lambda o: o.__dict__, sort_keys=True)

class ProsperityEncoder(JSONEncoder):
    def default(self, o):
        return o.__dict__