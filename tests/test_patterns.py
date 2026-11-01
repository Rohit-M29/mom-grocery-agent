import re

from grocery_agent.commerce import DEFAULT_ORDER_PATTERN

ORDER = re.compile(DEFAULT_ORDER_PATTERN, re.I)


def test_order_tools_are_caught():
    for name in ["checkout", "place_order", "placeOrder", "create_order", "confirm_order", "pay",
                 "pay_order", "cart_checkout"]:
        assert ORDER.search(name), name


def test_read_only_tools_are_not():
    for name in ["search_products", "get_payment_methods", "get_order_history", "view_cart",
                 "add_to_cart", "get_product_details"]:
        assert not ORDER.search(name), name
