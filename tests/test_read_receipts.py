import pytest

from grocery_agent import server
from grocery_agent.config import Settings
from grocery_agent.store import Store
from grocery_agent.whatsapp import parse_statuses

MOM, ME = "919800000001", "919800000002"
PENDING = {"platform": "zepto", "items": [], "total": 184, "payment_method": "wallet",
           "payment": "wallet", "turn": 2}
T0 = 1_000_000.0


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    async def send_text(self, to, text):
        self.sent.append(("text", to, text))
        return f"wamid.t{len(self.sent)}"

    async def send_audio(self, to, media_id):
        self.sent.append(("audio", to, media_id))
        return f"wamid.a{len(self.sent)}"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    s = Settings(db_path=str(tmp_path / "rr.db"), allowed_numbers=[MOM], owner_numbers=[ME],
                 read_reminder_minutes=10, read_reminder_max=1)
    monkeypatch.setattr(server, "settings", s)
    store, wa = Store(s.db_path), FakeWhatsApp()
    monkeypatch.setitem(server.state, "store", store)
    monkeypatch.setitem(server.state, "wa", wa)
    # The proposal is saved in her session, as the agent does with propose_order.
    store.save_session(MOM, {"messages": [], "turn": 2, "pending": PENDING})
    return store, wa


def test_parse_statuses():
    payload = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid.1", "status": "read", "recipient_id": MOM, "timestamp": "1"},
        {"id": "wamid.2"},
    ]}}]}]}
    assert parse_statuses(payload) == [{"id": "wamid.1", "status": "read", "recipient": MOM}]
    assert parse_statuses({"entry": [{"changes": [{"value": {"messages": []}}]}]}) == []


def test_status_never_moves_backwards(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.watch_reply(MOM, PENDING, ["wamid.1"], "media.1", now=T0)
    store.record_status("wamid.1", "read", MOM)
    store.record_status("wamid.1", "delivered", MOM)  # arrives late
    store.record_status("wamid.1", "failed", MOM)
    assert store.open_watches(MOM)[0]["seen"] == 1


def test_receipt_before_tracking_is_kept(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.record_status("wamid.1", "read", MOM)  # she had the chat open
    store.watch_reply(MOM, PENDING, ["wamid.1", "wamid.2"], "media.1", now=T0)
    assert store.open_watches(MOM)[0]["seen"] == 1


def test_receipts_for_other_messages_do_not_count(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.watch_reply(MOM, PENDING, ["wamid.new"], "media.1", now=T0)
    store.record_status("wamid.old", "read", MOM)
    assert store.open_watches(MOM)[0]["seen"] == 0


async def test_unopened_quote_is_resent_then_family_told(setup):
    store, wa = setup
    store.watch_reply(MOM, PENDING, ["wamid.v", "wamid.t"], "media.1", now=T0)

    await server.check_unread(now=T0 + 5 * 60)
    assert wa.sent == []

    await server.check_unread(now=T0 + 11 * 60)
    assert wa.sent == [("audio", MOM, "media.1")]

    await server.check_unread(now=T0 + 15 * 60)
    assert len(wa.sent) == 1

    await server.check_unread(now=T0 + 22 * 60)
    assert wa.sent[1][:2] == ("text", ME)
    assert "zepto, Rs 184" in wa.sent[1][2] and "22 min" in wa.sent[1][2] and MOM not in wa.sent[1][2]

    await server.check_unread(now=T0 + 60 * 60)
    assert len(wa.sent) == 2


async def test_read_receipt_stops_reminders(setup):
    store, wa = setup
    store.watch_reply(MOM, PENDING, ["wamid.v", "wamid.t"], "media.1", now=T0)
    store.record_status("wamid.v", "delivered", MOM)
    store.record_status("wamid.v", "read", MOM)
    await server.check_unread(now=T0 + 30 * 60)
    assert wa.sent == [] and store.open_watches(MOM) == []


async def test_reading_the_reminder_counts(setup):
    store, wa = setup
    store.watch_reply(MOM, PENDING, ["wamid.v"], "media.1", now=T0)
    await server.check_unread(now=T0 + 11 * 60)
    store.record_status("wamid.a1", "read", MOM)
    await server.check_unread(now=T0 + 30 * 60)
    assert len(wa.sent) == 1


async def test_failed_delivery_tells_family_at_once(setup):
    store, wa = setup
    store.watch_reply(MOM, PENDING, ["wamid.v", "wamid.t"], "media.1", now=T0)
    store.record_status("wamid.v", "failed", MOM)
    store.record_status("wamid.t", "failed", MOM)
    await server.check_unread(now=T0 + 60)
    assert wa.sent[0][:2] == ("text", ME) and "Could not deliver" in wa.sent[0][2]


async def test_no_reminder_once_proposal_is_gone(setup):
    store, wa = setup
    store.watch_reply(MOM, PENDING, ["wamid.v"], "media.1", now=T0)
    store.save_session(MOM, {"messages": [], "turn": 3, "pending": None})
    await server.check_unread(now=T0 + 30 * 60)
    assert wa.sent == [] and store.open_watches(MOM) == []


async def test_text_only_reply_goes_straight_to_family(setup):
    store, wa = setup
    store.watch_reply(MOM, PENDING, ["wamid.t"], None, now=T0)  # the voice note failed
    await server.check_unread(now=T0 + 11 * 60)
    assert wa.sent[0][:2] == ("text", ME)


def test_signed_status_webhook_is_recorded(setup, monkeypatch):
    import hashlib
    import hmac
    import json

    from fastapi.testclient import TestClient

    store, _ = setup
    monkeypatch.setattr(server.settings, "wa_app_secret", "secret")
    store.watch_reply(MOM, PENDING, ["wamid.v"], "media.1", now=T0)
    body = json.dumps({"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid.v", "status": "read", "recipient_id": MOM}]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    r = TestClient(server.app).post("/webhook", content=body,
                                    headers={"x-hub-signature-256": sig, "content-type": "application/json"})
    assert r.status_code == 200 and store.open_watches(MOM)[0]["seen"] == 1
