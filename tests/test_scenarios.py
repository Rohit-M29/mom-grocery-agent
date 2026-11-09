"""More whole WhatsApp conversations, run through the real message handler.

Same setup as test_end_to_end.py: real handler, storage, agent loop, fake shop over MCP and
safety rails; a script plays Claude's part, so no keys are needed. Each script also makes
the model misbehave (ordering after a "no", ignoring a limit) to show the code stops it.

Run with `pytest tests/test_scenarios.py -s` to print the conversations.
"""

import sys
import time
from pathlib import Path

from test_end_to_end import ScriptedClaude, _msg, _text, _tool

from grocery_agent import server
from grocery_agent.agent import GroceryAgent
from grocery_agent.commerce import Commerce
from grocery_agent.config import Settings
from grocery_agent.store import Store

MOM, ME = "919800000001", "919800000002"
SHOP = str(Path(__file__).with_name("fake_shop_server.py"))
MILK = [{"platform": "fakeshop", "product": "Amul Taaza Toned Milk", "pack": "1 L", "price": 68,
         "product_id": "m1"},
        {"platform": "othershop", "product": "Amul Taaza Toned Milk", "pack": "1 L", "price": 70}]
SUGAR = [{"platform": "fakeshop", "product": "Madhur Sugar", "pack": "1 kg", "price": 48, "product_id": "s1"}]
ONE_MILK = [{"product": "Amul Taaza", "pack": "1 L", "quantity": 1, "price": 68}]


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    async def mark_read(self, message_id): ...

    async def download_media(self, media_id):
        return media_id.encode()

    async def send_text(self, to, text):
        self.sent.append(("text", to, text))
        return f"wamid.{len(self.sent)}"

    async def send_voice(self, to, ogg):
        self.sent.append(("voice", to, ogg.decode()))
        return f"wamid.{len(self.sent)}", f"media.{len(self.sent)}"

    async def send_audio(self, to, media_id):
        self.sent.append(("voice (resent)", to, media_id))
        return f"wamid.{len(self.sent)}"


class FakeSpeech:
    def __init__(self, language):
        self.language, self.heard = language, {}

    async def transcribe(self, audio):
        return self.heard[audio.decode()], self.language

    async def speak(self, text, language):
        return f"[{language} voice] {text}".encode()


class Conversation:
    """Mom sends voice notes; everything the bot sends back is recorded."""

    def __init__(self, title, tmp_path, monkeypatch, script, language="hi-IN"):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used")
        self.title, self.script = title, script
        self.s = Settings(db_path=str(tmp_path / "chat.db"), allowed_numbers=[MOM], owner_numbers=[ME],
                          payer_numbers=[ME], max_order_rupees=1500, weekly_cap_rupees=5000,
                          read_reminder_minutes=10, read_reminder_max=1)
        monkeypatch.setattr(server, "settings", self.s)
        self.wa, self.speech, self.store = FakeWhatsApp(), FakeSpeech(language), Store(self.s.db_path)
        self.placed, self.transcript, self._n = [], [], 0

    async def __aenter__(self):
        self.commerce = Commerce([
            {"name": "fakeshop", "label": "FakeShop", "command": sys.executable, "args": [SHOP, "fakeshop"],
             "can_order": True},
            {"name": "othershop", "label": "OtherShop", "command": sys.executable,
             "args": [SHOP, "othershop"], "can_order": False},
        ])
        await self.commerce.__aenter__()

        async def on_order(user, order, result):
            self.placed.append(order)

        agent = GroceryAgent(self.s, self.store, self.commerce, on_order=on_order)
        self.claude = agent.client = ScriptedClaude(self.script)
        server.state.update(store=self.store, wa=self.wa, speech=self.speech, agent=agent)
        return self

    async def __aexit__(self, *exc):
        await self.commerce.__aexit__(*exc)
        print(f"\n\n=== {self.title} ===")
        for who, what in self.transcript:
            print(f"{who}: {what}")
        for blocked in (r for r in self.tool_results() if r.startswith("Blocked")):
            print(f"   [code stopped the model] {blocked}")

    async def say(self, text):
        self._n += 1
        note = f"note{self._n}"
        self.speech.heard[note] = text
        self.transcript.append(("Mom (voice)", text))
        await server._handle({"id": f"w{self._n}", "from": MOM, "type": "audio", "media_id": note})
        self._record()

    async def wait(self, minutes):
        self.transcript.append(("...", f"{minutes} minutes later, Mom still hasn't opened it"))
        await server.check_unread(now=time.time() + minutes * 60)
        self._record()

    def _record(self):
        shown = sum(1 for who, _ in self.transcript if who.startswith("->"))
        for kind, to, body in self.wa.sent[shown:]:
            self.transcript.append((f"-> {kind} to {'Mom' if to == MOM else 'you'}", body))

    def tool_results(self):
        """Every tool result the model saw, in order."""
        msgs = self.claude.requests[-1] if self.claude.requests else []
        return [r["content"] for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
                for r in m["content"] if r.get("type") == "tool_result"]


def _quote_milk_and_propose(reply):
    """Turn script: search, compare, add the 1 L pack, save the proposal, ask."""
    return [
        _msg(_tool("fakeshop__search_products", query="amul milk"),
             _tool("othershop__search_products", query="amul milk"), stop="tool_use"),
        _msg(_tool("compare_offers", requested="1 litre", offers=MILK), stop="tool_use"),
        _msg(_tool("fakeshop__add_to_cart", product_id="m1", quantity=1), stop="tool_use"),
        _msg(_tool("propose_order", platform="fakeshop", total_rupees=68, payment_method="zepto cash",
                   items=ONE_MILK), stop="tool_use"),
        _msg(_text(reply)),
    ]


async def test_mom_says_no(tmp_path, monkeypatch):
    script = _quote_milk_and_propose("अमूल ताज़ा एक लीटर 68 रुपये, Zepto Cash से। ऑर्डर कर दूं?") + [
        # The model wrongly tries to check out anyway.
        _msg(_tool("fakeshop__checkout", payment_method="zepto_cash"), stop="tool_use"),
        _msg(_text("ठीक है, ऑर्डर नहीं किया। कुछ और चाहिए तो बताइए।")),
    ]
    async with Conversation("1. Mom says no", tmp_path, monkeypatch, script) as chat:
        await chat.say("एक लीटर अमूल दूध चाहिए")
        await chat.say("नहीं, अभी मत करो")

    assert chat.placed == []
    assert any("not a clear yes" in r for r in chat.tool_results())
    assert chat.claude.script == []


async def test_two_items_in_one_message(tmp_path, monkeypatch):
    items = ONE_MILK + [{"product": "Madhur Sugar", "pack": "1 kg", "quantity": 1, "price": 48}]
    script = [
        _msg(_tool("fakeshop__search_products", query="amul milk"),
             _tool("othershop__search_products", query="amul milk"),
             _tool("fakeshop__search_products", query="sugar"), stop="tool_use"),
        _msg(_tool("compare_offers", requested="1 litre", offers=MILK),
             _tool("compare_offers", requested="1 kg", offers=SUGAR), stop="tool_use"),
        _msg(_tool("fakeshop__add_to_cart", product_id="m1", quantity=1),
             _tool("fakeshop__add_to_cart", product_id="s1", quantity=1), stop="tool_use"),
        _msg(_tool("propose_order", platform="fakeshop", total_rupees=116, payment_method="zepto cash",
                   items=items), stop="tool_use"),
        _msg(_text("दूध एक लीटर 68 रुपये और शक्कर एक किलो 48 रुपये, दोनों FakeShop पर। "
                   "कुल 116 रुपये, Zepto Cash से। ऑर्डर कर दूं?")),
        _msg(_tool("fakeshop__checkout", payment_method="zepto_cash"), stop="tool_use"),
        _msg(_text("ऑर्डर हो गया। 116 रुपये Zepto Cash से कट गए।")),
    ]
    async with Conversation("2. Two items in one message", tmp_path, monkeypatch, script) as chat:
        await chat.say("एक लीटर अमूल दूध और एक किलो शक्कर")
        await chat.say("हाँ")

    assert len(chat.placed) == 1 and chat.placed[0]["total"] == 116
    assert [i["product"] for i in chat.placed[0]["items"]] == ["Amul Taaza", "Madhur Sugar"]
    assert chat.claude.script == []


async def test_marathi_with_a_question_first(tmp_path, monkeypatch):
    script = _quote_milk_and_propose("अमूल ताजा एक लिटर FakeShop वर 68 रुपये. Zepto Cash ने ऑर्डर करू का?") + [
        # "किती झाले?" is a question, not a yes; the model wrongly tries to order.
        _msg(_tool("fakeshop__checkout", payment_method="zepto_cash"), stop="tool_use"),
        _msg(_text("एकूण 68 रुपये. ऑर्डर करू का?")),
        _msg(_tool("fakeshop__checkout", payment_method="zepto_cash"), stop="tool_use"),
        _msg(_text("ऑर्डर झाली. 68 रुपये Zepto Cash मधून गेले.")),
    ]
    async with Conversation("3. Marathi, with a question before the yes", tmp_path, monkeypatch, script,
                            language="mr-IN") as chat:
        await chat.say("एक लिटर अमूल दूध पाहिजे")
        await chat.say("किती झाले?")
        assert chat.placed == []
        await chat.say("होय, करा")

    assert len(chat.placed) == 1 and chat.placed[0]["total"] == 68
    voices = [b for k, to, b in chat.wa.sent if k == "voice"]
    assert len(voices) == 3 and all(v.startswith("[mr-IN voice]") for v in voices)
    assert chat.claude.script == []


async def test_order_over_the_limit_is_blocked(tmp_path, monkeypatch):
    big = [{"product": "Madhur Sugar", "pack": "1 kg", "quantity": 35, "price": 48}]
    script = [
        _msg(_tool("fakeshop__search_products", query="sugar"), stop="tool_use"),
        _msg(_tool("fakeshop__add_to_cart", product_id="s1", quantity=35), stop="tool_use"),
        _msg(_tool("propose_order", platform="fakeshop", total_rupees=1680, payment_method="zepto cash",
                   items=big), stop="tool_use"),
        _msg(_text("35 किलो शक्कर 1680 रुपये की होगी, पर एक ऑर्डर में 1500 रुपये तक ही ले सकती हूँ। "
                   "कम कर दूं, या घर वालों से लिमिट बढ़वाएँ?")),
        # She says yes anyway, and the model wrongly tries to check out.
        _msg(_tool("fakeshop__checkout", payment_method="zepto_cash"), stop="tool_use"),
        _msg(_text("माफ़ कीजिए, 1500 रुपये से ज़्यादा का ऑर्डर नहीं हो सकता। कितनी शक्कर चाहिए, कम करके बताइए।")),
    ]
    async with Conversation("4. Order over Rs 1,500", tmp_path, monkeypatch, script) as chat:
        await chat.say("पैंतीस किलो शक्कर चाहिए")
        await chat.say("हाँ कर दो")

    assert chat.placed == []
    results = chat.tool_results()
    assert any("above the per-order limit of Rs 1500" in r for r in results)
    assert any("no saved proposal" in r for r in results)
    assert chat.claude.script == []


async def test_mom_does_not_open_the_price(tmp_path, monkeypatch):
    script = _quote_milk_and_propose("अमूल ताज़ा एक लीटर 68 रुपये, Zepto Cash से। ऑर्डर कर दूं?") + [
        _msg(_tool("fakeshop__checkout", payment_method="zepto_cash"), stop="tool_use"),
        _msg(_text("ऑर्डर हो गया, Zepto Cash से पेमेंट हो गया है।")),
    ]
    async with Conversation("5. Mom doesn't open the price message", tmp_path, monkeypatch, script) as chat:
        await chat.say("एक लीटर अमूल दूध चाहिए")
        await chat.wait(5)
        await chat.wait(11)
        await chat.wait(22)
        await chat.say("हाँ")

    kinds = [(k, to) for k, to, _ in chat.wa.sent]
    assert kinds[2] == ("voice (resent)", MOM)
    assert kinds[3] == ("text", ME) and "hasn't opened the price message" in chat.wa.sent[3][2]
    assert len(chat.placed) == 1 and chat.store.open_watches(MOM) == []
    assert chat.claude.script == []
