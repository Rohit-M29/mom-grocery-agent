"""A whole WhatsApp conversation, run through the real message handler.

Real: the webhook handler, conversation storage, turn counting, the agent loop, the
MCP connection to a pretend shop, size matching, the order and payment rails.
Faked: WhatsApp's servers, Sarvam's speech service, and Claude (a script plays the
model's part, choosing the tool calls a model would make), so no keys are needed.

Run with `pytest tests/test_end_to_end.py -s` to print the conversation.
"""

import itertools
import json
import sys
from pathlib import Path

import anthropic
from fastapi.testclient import TestClient

from grocery_agent import server
from grocery_agent.agent import GroceryAgent
from grocery_agent.commerce import Commerce
from grocery_agent.config import Settings
from grocery_agent.store import Store

MOM, ME = "919800000001", "919800000002"
SHOP = str(Path(__file__).with_name("fake_shop_server.py"))
_ids = itertools.count(1)


def _msg(*content, stop="end_turn"):
    return anthropic.types.Message.model_validate({
        "id": f"msg_{next(_ids)}", "type": "message", "role": "assistant", "model": "scripted",
        "content": list(content), "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    })


def _tool(name, **args):
    return {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args}


def _text(t):
    return {"type": "text", "text": t}


OFFERS = [
    {"platform": "fakeshop", "product": "Amul Taaza Toned Milk", "pack": "200 ml", "price": 15, "product_id": "m2"},
    {"platform": "fakeshop", "product": "Amul Taaza Toned Milk", "pack": "1 L", "price": 68, "product_id": "m1"},
    {"platform": "othershop", "product": "Amul Taaza Toned Milk", "pack": "1 L", "price": 70},
    {"platform": "othershop", "product": "Amul Taaza Toned Milk", "pack": "2 x 500 ml", "price": 66},
]

# What the model "decides" at each step. Each turn ends with a text reply.
SCRIPT = [
    # Turn 1: "ek litre Amul doodh chahiye"
    _msg(_tool("fakeshop__search_products", query="amul milk"),
         _tool("othershop__search_products", query="amul milk"), stop="tool_use"),
    _msg(_tool("compare_offers", requested="1 litre", offers=OFFERS), stop="tool_use"),
    _msg(_text("अमूल ताज़ा एक लीटर FakeShop पर 68 रुपये में है, OtherShop पर 70 रुपये। "
               "FakeShop से एक पैकेट ले लूं?")),
    # Turn 2: "haan" -> add to cart, propose, and (wrongly) try to check out at once
    _msg(_tool("fakeshop__add_to_cart", product_id="m1", quantity=1), stop="tool_use"),
    _msg(_tool("propose_order", platform="fakeshop", total_rupees=68, payment_method="zepto cash",
               items=[{"product": "Amul Taaza", "pack": "1 L", "quantity": 1, "price": 68}]),
         stop="tool_use"),
    _msg(_tool("fakeshop__checkout", payment_method="zepto_cash"), stop="tool_use"),
    _msg(_text("कुल 68 रुपये, Zepto Cash से पेमेंट होगा। ऑर्डर कर दूं?")),
    # Turn 3: "haan kar do" -> first tries cash on delivery (blocked), then the wallet
    _msg(_tool("fakeshop__checkout", payment_method="cod"), stop="tool_use"),
    _msg(_tool("fakeshop__checkout", payment_method="zepto_cash"), stop="tool_use"),
    _msg(_text("ऑर्डर हो गया, Zepto Cash से पेमेंट हो गया है।")),
]


class ScriptedClaude:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.messages = self

    async def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"], default=str)))
        return self.script.pop(0)


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    async def mark_read(self, message_id): ...

    async def download_media(self, media_id):
        return b"voice-note:" + media_id.encode()

    async def send_text(self, to, text):
        self.sent.append(("text", to, text))

    async def send_voice(self, to, ogg):
        self.sent.append(("voice", to, ogg.decode()))


class FakeSpeech:
    heard = {"note1": ("एक लीटर अमूल दूध चाहिए", "hi-IN"),
             "note2": ("हाँ", "hi-IN"),
             "note3": ("हाँ कर दो", "hi-IN")}

    async def transcribe(self, audio):
        return self.heard[audio.decode().split(":")[1]]

    async def speak(self, text, language):
        return f"[{language} voice] {text}".encode()


async def test_whatsapp_conversation_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used")
    s = Settings(db_path=str(tmp_path / "e2e.db"), allowed_numbers=[MOM], owner_numbers=[ME],
                 payer_numbers=[ME], max_order_rupees=1500, weekly_cap_rupees=5000)
    monkeypatch.setattr(server, "settings", s)
    configs = [
        {"name": "fakeshop", "label": "FakeShop", "command": sys.executable, "args": [SHOP, "fakeshop"],
         "can_order": True},
        {"name": "othershop", "label": "OtherShop", "command": sys.executable,
         "args": [SHOP, "othershop"], "can_order": False},
    ]
    wa, store, placed = FakeWhatsApp(), Store(s.db_path), []

    async with Commerce(configs) as commerce:
        async def on_order(user, order, result):
            placed.append(order)

        agent = GroceryAgent(s, store, commerce, on_order=on_order)
        claude = ScriptedClaude(SCRIPT)
        agent.client = claude
        server.state.update(store=store, wa=wa, speech=FakeSpeech(), agent=agent)

        # A stranger is ignored entirely.
        await server._handle({"id": "x", "from": "910000000000", "type": "text", "text": "hi"})
        assert wa.sent == []

        transcript = []
        for i in (1, 2, 3):
            before = len(wa.sent)
            await server._handle({"id": f"w{i}", "from": MOM, "type": "audio", "media_id": f"note{i}"})
            transcript.append(("Mom (voice)", FakeSpeech.heard[f"note{i}"][0]))
            transcript += [(f"-> {kind} to {'Mom' if to == MOM else 'Rohit'}", body)
                           for kind, to, body in wa.sent[before:]]

    # Every reply went out as a voice note and as text, in Hindi.
    voices = [b for k, to, b in wa.sent if k == "voice" and to == MOM]
    assert len(voices) == 3 and all(v.startswith("[hi-IN voice]") for v in voices)

    # Turn 1: the exact 1 L match won, not the 200 ml pack.
    compare = next(r for r in claude.requests[2][-1]["content"] if r["type"] == "tool_result")
    assert json.loads(compare["content"])["exact_matches"][0]["price"] == 68

    # Turn 2: checking out in the same turn as the proposal was blocked.
    turn2_results = [r for m in claude.requests[6] if m["role"] == "user" and isinstance(m["content"], list)
                     for r in m["content"] if r.get("type") == "tool_result"]
    assert any("same turn" in r["content"] for r in turn2_results)

    # Turn 3: cash on delivery was refused, then the wallet order went through once.
    turn3_results = [r for m in claude.requests[8] if m["role"] == "user" and isinstance(m["content"], list)
                     for r in m["content"] if r.get("type") == "tool_result"]
    assert any("cash on delivery found" in r["content"] for r in turn3_results)
    assert len(placed) == 1 and placed[0]["status"] == "paid"
    assert store.spent_since(3600) == 68

    # The model saw the shop data and the block messages; the history was stored.
    assert claude.script == []

    print("\n\n=== Simulated WhatsApp conversation ===")
    for who, what in transcript:
        print(f"{who}: {what}")
    print("\n=== Tool results the model saw (turn 2 and 3) ===")
    for r in turn2_results + turn3_results:
        print("-", r["content"][:160].replace("\n", " "))


def test_webhook_rejects_unsigned_requests(monkeypatch):
    monkeypatch.setattr(server.settings, "wa_app_secret", "secret")
    monkeypatch.setattr(server.settings, "wa_verify_token", "verify-me")
    client = TestClient(server.app)  # no lifespan: no real connections are opened
    assert client.post("/webhook", json={"entry": []}).status_code == 401
    assert client.post("/webhook", json={"entry": []},
                       headers={"x-hub-signature-256": "sha256=deadbeef"}).status_code == 401
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                        "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                          "hub.challenge": "42"}).status_code == 403
