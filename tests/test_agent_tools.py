"""End-to-end tests of the tool layer against two pretend shops over real MCP connections.

Claude itself is not called here: these drive GroceryAgent._run_tool directly, the
same way the model's tool calls are executed, to prove the order rails hold.
"""

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from grocery_agent.agent import GroceryAgent
from grocery_agent.commerce import Commerce
from grocery_agent.config import Settings
from grocery_agent.store import Store

SERVER = str(Path(__file__).with_name("fake_shop_server.py"))
CONFIGS = [
    {"name": "fakeshop", "label": "FakeShop", "command": sys.executable,
     "args": [SERVER, "fakeshop"], "can_order": True},
    {"name": "othershop", "label": "OtherShop", "command": sys.executable,
     "args": [SERVER, "othershop"], "can_order": False},
]


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")


@asynccontextmanager
async def shops(tmp_path):
    # Opened and closed inside each test: MCP connections must close in the task that opened them.
    settings = Settings(db_path=str(tmp_path / "t.db"), max_order_rupees=500, weekly_cap_rupees=1000)
    store = Store(settings.db_path)
    placed = []

    async def on_order(user, order, result):
        placed.append((user, order, result))

    async with Commerce(CONFIGS) as commerce:
        agent = GroceryAgent(settings, store, commerce, on_order=on_order)
        yield agent, store, placed


def session(turn=1):
    return {"messages": [], "turn": turn, "pending": None}


async def test_tools_from_both_shops_are_exposed(tmp_path):
    async with shops(tmp_path) as env:
        agent, _, _ = env
        names = {t["name"] for t in agent.tools}
        assert {"fakeshop__search_products", "fakeshop__checkout", "othershop__search_products",
                "compare_offers", "propose_order"} <= names
        assert "FakeShop" in agent.system and "price comparison only" in agent.system


async def test_search_works(tmp_path):
    async with shops(tmp_path) as env:
        agent, _, _ = env
        out, err = await agent._run_tool("mom", "doodh", session(), "fakeshop__search_products",
                                         {"query": "milk"})
        assert not err and "Amul Taaza" in out


async def test_compare_only_shop_cannot_add_to_cart(tmp_path):
    async with shops(tmp_path) as env:
        agent, _, _ = env
        out, err = await agent._run_tool("mom", "haan", session(), "othershop__add_to_cart",
                                         {"product_id": "x1"})
        assert err and "comparison only" in out


async def test_checkout_without_proposal_is_blocked(tmp_path):
    async with shops(tmp_path) as env:
        agent, _, placed = env
        out, err = await agent._run_tool("mom", "haan", session(), "fakeshop__checkout", {})
        assert err and "no saved proposal" in out and placed == []


async def test_full_order_flow(tmp_path):
    async with shops(tmp_path) as env:
        agent, store, placed = env
        s = session(turn=1)
        await agent._run_tool("mom", "haan", s, "fakeshop__add_to_cart", {"product_id": "m1", "quantity": 2})
        out, err = await agent._run_tool("mom", "haan", s, "propose_order", {
            "platform": "fakeshop", "total_rupees": 136,
            "items": [{"product": "Amul Taaza", "pack": "1 L", "quantity": 2, "price": 68}]})
        assert not err

        # Same turn: must wait for the user's reply.
        out, err = await agent._run_tool("mom", "haan", s, "fakeshop__checkout", {})
        assert err and "same turn" in out

        # Next turn, but the user asked a question instead of saying yes.
        s["turn"] = 2
        out, err = await agent._run_tool("mom", "kitne ka hua?", s, "fakeshop__checkout", {})
        assert err and "not a clear yes" in out

        # Next turn with a clear yes: the order goes through once.
        s["turn"] = 3
        out, err = await agent._run_tool("mom", "हाँ कर दो", s, "fakeshop__checkout", {})
        assert err and "does not name a payment method" in out  # shop default could be COD
        out, err = await agent._run_tool("mom", "हाँ कर दो", s, "fakeshop__checkout",
                                         {"payment_method": "upi"})
        assert err and "user confirmed wallet" in out
        out, err = await agent._run_tool("mom", "हाँ कर दो", s, "fakeshop__checkout",
                                         {"payment_method": "zepto_cash"})
        assert not err and json.loads(out.split("\n[")[0])["total"] == 136
        assert s["pending"] is None and len(placed) == 1
        assert store.spent_since(3600) == 136

        # A second checkout needs a fresh proposal.
        s["turn"] = 4
        out, err = await agent._run_tool("mom", "haan", s, "fakeshop__checkout", {})
        assert err and "no saved proposal" in out


async def test_proposal_over_limit_is_blocked(tmp_path):
    async with shops(tmp_path) as env:
        agent, _, _ = env
        s = session()
        out, err = await agent._run_tool("mom", "x", s, "propose_order", {
            "platform": "fakeshop", "total_rupees": 900, "items": []})
        assert err and "per-order limit" in out and s["pending"] is None


async def test_cannot_propose_on_compare_only_shop(tmp_path):
    async with shops(tmp_path) as env:
        agent, _, _ = env
        out, err = await agent._run_tool("mom", "x", session(), "propose_order", {
            "platform": "othershop", "total_rupees": 70, "items": []})
        assert err and "not a platform you can order from" in out


async def _proposed(agent, s, method):
    await agent._run_tool("mom", "haan", s, "fakeshop__add_to_cart", {"product_id": "m1", "quantity": 1})
    return await agent._run_tool("mom", "haan", s, "propose_order", {
        "platform": "fakeshop", "total_rupees": 68, "payment_method": method,
        "items": [{"product": "Amul Taaza", "pack": "1 L", "quantity": 1, "price": 68}]})


async def test_cod_proposal_is_blocked(tmp_path):
    async with shops(tmp_path) as (agent, _, _):
        s = session()
        out, err = await _proposed(agent, s, "cash on delivery")
        assert err and "cash on delivery is not allowed" in out and s["pending"] is None


async def test_cod_at_checkout_is_blocked_even_after_yes(tmp_path):
    async with shops(tmp_path) as (agent, _, placed):
        s = session()
        out, err = await _proposed(agent, s, "zepto cash")
        assert not err
        s["turn"] = 2
        out, err = await agent._run_tool("mom", "haan", s, "fakeshop__checkout", {"payment_method": "cod"})
        assert err and "cash on delivery found" in out and placed == []


async def test_wallet_order_is_paid(tmp_path):
    async with shops(tmp_path) as (agent, store, placed):
        s = session()
        await _proposed(agent, s, "zepto cash")
        s["turn"] = 2
        out, err = await agent._run_tool("mom", "haan", s, "fakeshop__checkout",
                                         {"payment_method": "zepto_cash"})
        assert not err and "confirmed" in out
        assert placed[0][1]["status"] == "paid" and placed[0][1]["payment_links"] == []


async def test_upi_order_sends_payment_link(tmp_path):
    async with shops(tmp_path) as (agent, store, placed):
        s = session()
        await _proposed(agent, s, "upi")
        s["turn"] = 2
        out, err = await agent._run_tool("mom", "haan", s, "fakeshop__checkout", {"payment_method": "upi"})
        assert not err and "payment link was sent" in out
        order = placed[0][1]
        assert order["status"] == "awaiting_payment"
        assert order["payment_links"] == ["https://pay.example.test/upi/fakeshop-1"]
        row = store.db.execute("SELECT status FROM orders").fetchone()
        assert row["status"] == "awaiting_payment"


async def test_cart_change_after_proposal_clears_it(tmp_path):
    async with shops(tmp_path) as (agent, _, placed):
        s = session()
        await _proposed(agent, s, "zepto cash")
        out, err = await agent._run_tool("mom", "x", s, "fakeshop__add_to_cart", {"product_id": "s1"})
        assert "proposal was cleared" in out and s["pending"] is None
        s["turn"] = 2
        out, err = await agent._run_tool("mom", "haan", s, "fakeshop__checkout",
                                         {"payment_method": "zepto_cash"})
        assert err and "no saved proposal" in out and placed == []


async def test_reading_payment_methods_is_allowed(tmp_path):
    async with shops(tmp_path) as (agent, _, _):
        out, err = await agent._run_tool("mom", "x", session(), "fakeshop__get_payment_methods", {})
        assert not err and "zepto_cash_balance" in out


def test_recent_purchases_are_remembered(tmp_path):
    store = Store(str(tmp_path / "r.db"))
    store.record_order("mom", "zepto", 40, [{"product": "Parle-G", "pack": "250 g", "quantity": 2}], "ok")
    store.record_order("mom", "zepto", 98, [{"product": "Amul Taaza", "pack": "1 L", "quantity": 1},
                                            {"product": "Parle-G", "pack": "250 g", "quantity": 1}], "ok")
    store.record_order("someone-else", "zepto", 10, [{"product": "Coke", "pack": "300 ml"}], "ok")
    assert store.recent_products("mom") == ["Amul Taaza 1 L", "Parle-G 250 g"]


async def test_new_conversation_includes_preferences_and_history(tmp_path):
    async with shops(tmp_path) as (agent, store, _):
        store.set_preference("mom", "biscuit", "Parle-G 250 g")
        store.record_order("mom", "fakeshop", 68, [{"product": "Amul Taaza", "pack": "1 L"}], "ok")

        class Stop(Exception):
            pass

        seen = {}

        async def fake_create(**kwargs):
            seen["first"] = kwargs["messages"][0]["content"]
            raise Stop

        agent.client.messages.create = fake_create
        with pytest.raises(Stop):
            await agent.reply("mom", "biscuit chahiye")
        assert '"biscuit": "Parle-G 250 g"' in seen["first"]
        assert "Amul Taaza 1 L" in seen["first"]
        assert "Never pick a product on your own for a vague request" in agent.system
