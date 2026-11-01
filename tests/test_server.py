from grocery_agent.server import mask, over_rate_limit


def test_mask_hides_number():
    assert mask("919812345678") == "***5678"


def test_rate_limit():
    user = "rate-test-user"
    assert not any(over_rate_limit(user, 3, now=1000 + i) for i in range(3))
    assert over_rate_limit(user, 3, now=1010)
    # An hour later the window has moved on.
    assert not over_rate_limit(user, 3, now=1000 + 3700)


async def test_upi_link_goes_to_payer_and_owner_is_told():
    from grocery_agent.config import Settings
    from grocery_agent.server import notify_family

    class WA:
        sent = []

        async def send_text(self, to, text):
            self.sent.append((to, text))

    wa = WA()
    cfg = Settings(owner_numbers=["911", "912"], payer_numbers=["911"])
    order = {"platform": "zepto", "total": 184, "items": [{"product": "Amul", "quantity": 2, "price": 68}],
             "payment_links": ["https://pay.example/abc"]}
    await notify_family(wa, cfg, "919800001234", order)
    assert wa.sent[0][0] == "911" and "https://pay.example/abc" in wa.sent[0][1]
    assert "Rs 184" in wa.sent[0][1] and "***1234" in wa.sent[0][1]
    assert wa.sent[1] == ("912", wa.sent[1][1]) and "Awaiting UPI payment" in wa.sent[1][1]
    assert "https://" not in wa.sent[1][1]


async def test_wallet_order_tells_owners():
    from grocery_agent.config import Settings
    from grocery_agent.server import notify_family

    class WA:
        sent = []

        async def send_text(self, to, text):
            self.sent.append((to, text))

    wa = WA()
    await notify_family(wa, Settings(owner_numbers=["912"], payer_numbers=["912"]), "91980000",
                        {"platform": "zepto", "total": 68, "items": [], "payment_links": []})
    assert wa.sent == [("912", wa.sent[0][1])] and wa.sent[0][1].startswith("Paid from wallet")
