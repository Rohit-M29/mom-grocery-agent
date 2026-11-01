import pytest

from grocery_agent.units import G, ML, PC, compare_offers, parse_quantity


@pytest.mark.parametrize("text,amount,unit,packs", [
    ("1 L", 1000, ML, 1),
    ("1ltr", 1000, ML, 1),
    ("1 Litre", 1000, ML, 1),
    ("500 ml", 500, ML, 1),
    ("200ml", 200, ML, 1),
    ("2 x 500 ml", 1000, ML, 2),
    ("2x500ml", 1000, ML, 2),
    ("500 ml x 2", 1000, ML, 2),
    ("Amul Taaza Toned Milk 1 L", 1000, ML, 1),
    ("1.5kg", 1500, G, 1),
    ("5 kg", 5000, G, 1),
    ("250 gm", 250, G, 1),
    ("Pack of 6", 6, PC, 1),
    ("1 dozen", 12, PC, 1),
    ("6 pcs", 6, PC, 1),
    ("Coke 300 ml (Pack of 6)", 1800, ML, 6),
    ("1 लीटर", 1000, ML, 1),
    ("5 किलो", 5000, G, 1),
])
def test_parse_quantity(text, amount, unit, packs):
    q = parse_quantity(text)
    assert q is not None
    assert (q.amount, q.unit, q.packs) == (amount, unit, packs)


def test_unparseable():
    assert parse_quantity("Fresh Coriander") is None
    assert parse_quantity("") is None


OFFERS = [
    {"platform": "zepto", "product": "Amul Taaza", "pack": "200 ml", "price": 15},
    {"platform": "zepto", "product": "Amul Taaza", "pack": "1 L", "price": 68},
    {"platform": "instamart", "product": "Amul Taaza", "pack": "1 L", "price": 70},
    {"platform": "blinkit", "product": "Amul Taaza", "pack": "2 x 500 ml", "price": 66},
    {"platform": "blinkit", "product": "Amul Taaza", "pack": "1 L", "price": 65, "in_stock": False},
]


def test_one_litre_never_returns_200ml_as_match():
    r = compare_offers("1 litre", OFFERS)
    assert [o["platform"] for o in r["exact_matches"]] == ["zepto", "instamart"]
    assert all(o["size"] == "1 L" for o in r["exact_matches"])
    assert r["equivalent_multipacks"][0]["size"] == "2 x 500 ml"
    assert any(o["size"] == "200 ml" for o in r["other_sizes"])
    assert "zepto" in r["note"] and "68" in r["note"]


def test_out_of_stock_is_skipped():
    r = compare_offers("1 L", OFFERS)
    assert all(o.get("in_stock") is not False for o in r["exact_matches"])


def test_no_exact_match_tells_agent_to_ask():
    r = compare_offers("1 L", [OFFERS[0]])
    assert r["exact_matches"] == [] and r["equivalent_multipacks"] == []
    assert "200 ml" in r["note"] and "ask" in r["note"].lower()


def test_no_size_requested_ranks_by_unit_price():
    r = compare_offers("", OFFERS[:3])
    # 1 L at 68 (68/L) beats 200 ml at 15 (75/L)
    assert r["other_sizes"][0]["price"] == 68
    assert r["other_sizes"][0]["price_per_unit"] == "Rs 68.00/L"


def test_bad_offer_rows_are_reported_not_crashing():
    r = compare_offers("1 kg", [{"platform": "zepto", "product": "Sugar", "pack": "loose", "price": 50},
                                 {"platform": "zepto", "product": "Sugar 1 kg"}])
    assert len(r["not_understood"]) == 2
