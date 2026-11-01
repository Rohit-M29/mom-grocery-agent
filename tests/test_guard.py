import pytest

from grocery_agent.guard import OrderBlocked, check_order_allowed, check_spend, is_affirmative


@pytest.mark.parametrize("text", [
    "haan", "Haan kar do", "हाँ", "हाँ कर दो", "ठीक है", "ho", "होय", "चालेल", "yes please",
    "ok order karo", "bilkul", "हो, करा",
])
def test_yes(text):
    assert is_affirmative(text)


@pytest.mark.parametrize("text", [
    "nahi", "नहीं", "नको", "नाही", "no", "haan nahi", "ruko", "abhi mat karo",
    "kitne ka ho gaya", "kya total hai?", "हो गया?", "", "   ",
    "doodh aur chahiye ek packet atta bhi aur ho sake to shakkar bhi",
])
def test_not_yes(text):
    assert not is_affirmative(text)


PENDING = {"platform": "zepto", "items": [], "total": 200, "turn": 3}


def test_order_needs_proposal():
    with pytest.raises(OrderBlocked, match="no saved proposal"):
        check_order_allowed(platform="zepto", pending=None, current_turn=4, user_text="haan")


def test_order_blocked_in_same_turn():
    with pytest.raises(OrderBlocked, match="same turn"):
        check_order_allowed(platform="zepto", pending=PENDING, current_turn=3, user_text="haan")


def test_order_blocked_without_yes():
    with pytest.raises(OrderBlocked, match="not a clear yes"):
        check_order_allowed(platform="zepto", pending=PENDING, current_turn=4, user_text="kitna hua")


def test_order_blocked_wrong_platform():
    with pytest.raises(OrderBlocked, match="proposal is for zepto"):
        check_order_allowed(platform="instamart", pending=PENDING, current_turn=4, user_text="haan")


def test_order_allowed():
    check_order_allowed(platform="zepto", pending=PENDING, current_turn=4, user_text="हाँ कर दो")


def test_spend_limits():
    check_spend(500, max_order=1500, weekly_cap=5000, spent_this_week=1000)
    with pytest.raises(OrderBlocked, match="per-order limit"):
        check_spend(2000, max_order=1500, weekly_cap=5000, spent_this_week=0)
    with pytest.raises(OrderBlocked, match="weekly limit"):
        check_spend(1000, max_order=1500, weekly_cap=5000, spent_this_week=4500)
    with pytest.raises(OrderBlocked):
        check_spend(0, max_order=1500, weekly_cap=5000, spent_this_week=0)


# --- prepaid only -------------------------------------------------------------
from grocery_agent.guard import check_payment, find_cod, is_cod, payment_kind  # noqa: E402


@pytest.mark.parametrize("text", ["cod", "COD", "Cash on Delivery", "cash_on_delivery",
                                  "pay on delivery", "कैश ऑन डिलीवरी"])
def test_cod_detected(text):
    assert is_cod(text)


@pytest.mark.parametrize("text", ["Zepto Cash", "zepto_cash", "upi", "code", "wallet", "decode"])
def test_not_cod(text):
    assert not is_cod(text)


def test_payment_kinds():
    assert payment_kind("Zepto Cash") == "wallet"
    assert payment_kind("UPI payment link") == "upi"
    assert payment_kind("cash on delivery") is None
    assert payment_kind("card") is None


def test_check_payment():
    assert check_payment("zepto_cash", ["wallet", "upi"]) == "wallet"
    with pytest.raises(OrderBlocked, match="cash on delivery is not allowed"):
        check_payment("COD", ["wallet", "upi"])
    with pytest.raises(OrderBlocked, match="not allowed"):
        check_payment("upi", ["wallet"])
    with pytest.raises(OrderBlocked, match="not allowed"):
        check_payment("credit card", ["wallet", "upi"])


def test_find_cod_in_nested_args():
    assert find_cod({"payment_method": "COD"})
    assert find_cod({"payment": {"mode": "cash_on_delivery"}})
    assert find_cod({"options": [{"type": "cod"}]})
    assert find_cod({"cod": True})
    assert find_cod({"payment_method": "zepto_cash", "cod": False}) is None
    assert find_cod({"payment_method": "upi", "items": ["Amul 1 L"]}) is None
