import logging
import time
from os import getenv
from typing import Any

from pymongo import MongoClient
from tf2_utils import is_metal, is_pure

from .exceptions import SKUNotFound
from .utils import sku_to_item_data


def has_buy_and_sell_price(data: dict) -> bool:
    return data.get("buy", {}) != {} and data.get("sell", {}) != {}


class Database:
    def __init__(self, database: str) -> None:
        host = getenv("MONGO_HOST", "localhost")
        client = MongoClient(host, 27017)
        db = client[database]

        self.name = database
        self.trades = db["trades"]
        self.items = db["items"]
        self.deals = db["deals"]

        # bot needs key price to work
        if not self.get_item("5021;6"):
            self._add_key_for_first_time()

    def _add_key_for_first_time(self) -> None:
        self.add_item(**sku_to_item_data("5021;6"))

    def has_price(self, sku: str) -> bool:
        data = self.get_item(sku)

        if not data:
            return False

        return has_buy_and_sell_price(data)

    def is_temporarily_priced(self, sku: str) -> bool:
        if is_pure(sku):
            return False

        return self.get_item(sku).get("temporary", False)

    def insert_trade(self, data: dict) -> None:
        self.trades.insert_one(data)
        logging.info("Offer was added to the database")

    def get_trades(
        self, start_index: int, amount: int
    ) -> tuple[list[dict], int, int, int]:
        # sort newest trades first
        all_trades = list(self.trades.find().sort("time_updated", -1))
        total_trades = len(all_trades)
        intended_end_index = start_index + amount
        trades = all_trades[start_index:intended_end_index]
        end_index = start_index + len(trades)

        return {
            "trades": trades,
            "total_trades": total_trades,
            "start_index": start_index,
            "end_index": end_index,
        }

    def get_price(self, sku: str, intent: str) -> tuple[int, float]:
        # metals does not exist in the database, but has value
        if sku == "5002;6":
            return 0, 1.0

        if sku == "5001;6":
            return 0, 0.33

        if sku == "5000;6":
            return 0, 0.11

        item_price = self.get_item(sku)

        # item does not exist in db or does not have a price
        if not item_price or not has_buy_and_sell_price(item_price):
            return 0, 0.0

        price = item_price[intent]
        keys = price.get("keys", 0)
        metal = price.get("metal", 0.0)

        return keys, metal

    def get_skus(self) -> list[str]:
        return [item["sku"] for item in self.items.find()]

    def get_autopriced(self) -> list[dict]:
        return [
            item
            for item in self.items.find({"autoprice": True})
            if item["sku"] != "-100;6"
        ]

    def get_autopriced_skus(self) -> list[str]:
        return [item["sku"] for item in self.get_autopriced()]

    def get_item(self, sku: str) -> dict[str, Any]:
        item = self.items.find_one({"sku": sku})

        if item is None:
            logging.debug(f"{sku} not found in database")
            return {}

        del item["_id"]
        return item

    def get_pricelist(self) -> list[dict]:
        return self.items.find()

    def get_pricelist_count(self) -> int:
        return self.items.count_documents({})

    def get_stock(self, sku: str) -> tuple[int, int]:
        """returns in_stock, max_stock"""
        data = self.get_item(sku)
        return (data.get("in_stock", 0), data.get("max_stock", -1))

    def replace_item(self, data: dict) -> None:
        sku = data["sku"]

        logging.debug(f"Updating {sku} with {data=}")
        self.items.replace_one({"sku": sku}, data)

    def update_stock(self, stock: dict) -> None:
        all_items = self.items.find()

        for item in all_items:
            sku = item["sku"]

            if sku not in stock:
                continue

            in_stock = stock[sku]

            # in_stock is the same, no need to update
            if in_stock == item.get("in_stock", 0):
                continue

            item["in_stock"] = in_stock
            self.replace_item(item)

        logging.info("Updated stock for all items")

    def add_item(
        self,
        sku: str,
        color: str,
        image: str,
        name: str,
        autoprice: bool = True,
        in_stock: int = 0,
        max_stock: int = -1,
        temporary: bool = False,
        buy: dict = {},
        sell: dict = {},
    ) -> None:
        if is_metal(sku):
            logging.warning(f"cannot add metal {sku} to database")
            return

        if sku in self.get_skus():
            logging.warning(f"{sku} already exists in database")
            return

        document = {
            "sku": sku,
            "name": name,
            "buy": buy,
            "sell": sell,
            "autoprice": autoprice,
            "temporary": temporary,  # delete price after we have sold
            "in_stock": in_stock,
            "max_stock": max_stock,
            "color": color,
            "image": image,
        }

        self.items.insert_one(document)
        logging.info(f"Added {sku} to database")

    def update_price(
        self,
        sku: str,
        buy: dict,
        sell: dict,
        autoprice: bool = False,
        max_stock: int = -1,
    ) -> None:
        assert "keys" in buy and "metal" in buy, "Buy price must have keys and metal"
        assert "keys" in sell and "metal" in sell, "Sell price must have keys and metal"

        data = self.get_item(sku)

        if not data:
            raise SKUNotFound(f"{sku} does not exist in database!")

        data["buy"] = buy
        data["sell"] = sell
        data["autoprice"] = autoprice
        data["max_stock"] = max_stock
        data["updated"] = time.time()

        self.items.replace_one({"sku": sku}, data)
        logging.info(f"Updated price for {sku}")

    def update_autoprice(self, data: dict) -> None:
        self.update_price(data["sku"], data["buy"], data["sell"], True)

    def delete_price(self, sku: str) -> None:
        self.items.delete_one({"sku": sku})
        logging.info(f"Removed {sku} from the database")

    def add_deal(self, deal: dict) -> None:
        sku = deal["sku"]

        if self.deals.find_one({"sku": sku}):
            logging.warning(f"Deal {sku} already exists in the database")
            return

        self.deals.insert_one(deal)
        logging.info("Deal was added to the database")

    def get_deals(self) -> list[dict]:
        return list(self.deals.find())

    def get_deal(self, sku: str) -> dict:
        return self.deals.find_one({"sku": sku})

    def update_deal(self, deal: dict) -> None:
        self.deals.replace_one({"sku": deal["sku"]}, deal)

    def delete_deal(self, sku: str) -> None:
        self.deals.delete_one({"sku": sku})
        logging.info("Deal was removed from the database")
