"""A pretend shopping app, served over MCP, for tests and offline demos.

Usage: python tests/fake_shop_server.py <shop-name>
"""

import sys

from mcp.server.mcpserver import MCPServer

SHOP = sys.argv[1] if len(sys.argv) > 1 else "fakeshop"
CATALOG = {
    "fakeshop": [
        {"id": "m1", "name": "Amul Taaza Toned Milk", "pack": "1 L", "price": 68},
        {"id": "m2", "name": "Amul Taaza Toned Milk", "pack": "200 ml", "price": 15},
        {"id": "s1", "name": "Madhur Sugar", "pack": "1 kg", "price": 48},
    ],
    "othershop": [
        {"id": "x1", "name": "Amul Taaza Toned Milk", "pack": "1 L", "price": 70},
        {"id": "x2", "name": "Amul Taaza Toned Milk", "pack": "2 x 500 ml", "price": 66},
    ],
}[SHOP]

mcp = MCPServer(SHOP)
cart: dict[str, int] = {}
orders: list[dict] = []


@mcp.tool()
def search_products(query: str) -> list[dict]:
    """Search the catalogue."""
    words = query.lower().split()
    return [p for p in CATALOG if any(w in p["name"].lower() for w in words)]


@mcp.tool()
def add_to_cart(product_id: str, quantity: int = 1) -> dict:
    """Add a product to the cart."""
    cart[product_id] = cart.get(product_id, 0) + quantity
    return {"cart": cart, "total": _total()}


wallet = {"balance": 500.0}


@mcp.tool()
def get_payment_methods() -> dict:
    """List payment methods and the wallet balance."""
    return {"methods": ["zepto_cash", "upi", "cod"], "zepto_cash_balance": wallet["balance"]}


@mcp.tool()
def checkout(payment_method: str = "cod") -> dict:
    """Place the order for everything in the cart. payment_method: zepto_cash, upi or cod."""
    total = _total()
    order_id = f"{SHOP}-{len(orders) + 1}"
    if payment_method == "zepto_cash":
        if wallet["balance"] < total:
            raise ValueError(f"Insufficient Zepto Cash balance ({wallet['balance']})")
        wallet["balance"] -= total
        order = {"order_id": order_id, "total": total, "status": "confirmed", "paid_by": "zepto_cash"}
    elif payment_method == "upi":
        order = {"order_id": order_id, "total": total, "status": "payment_pending",
                 "payment_link": f"https://pay.example.test/upi/{order_id}"}
    else:
        order = {"order_id": order_id, "total": total, "status": "confirmed", "paid_by": "cod"}
    orders.append(order)
    cart.clear()
    return order


def _total() -> float:
    prices = {p["id"]: p["price"] for p in CATALOG}
    return sum(prices[i] * q for i, q in cart.items())


if __name__ == "__main__":
    mcp.run()
