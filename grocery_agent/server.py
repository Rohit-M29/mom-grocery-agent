"""WhatsApp webhook: voice note in -> transcribe -> agent -> voice note (and text) out.

Run with:  uvicorn grocery_agent.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .agent import GroceryAgent
from .commerce import Commerce
from .config import settings
from .speech import Speech, guess_language
from .store import Store
from .whatsapp import WhatsApp, parse_messages, parse_statuses, valid_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("grocery_agent")

state: dict = {}
_seen: OrderedDict[str, None] = OrderedDict()   # WhatsApp retries webhooks; skip repeats
_user_locks: dict[str, asyncio.Lock] = {}        # one message at a time per person
_recent: dict[str, deque] = {}                   # message times per person, for rate limiting


def mask(number: str) -> str:
    """Logs show only the last 4 digits of a phone number."""
    return f"***{number[-4:]}" if number else "?"


def over_rate_limit(sender: str, limit: int, now: float | None = None) -> bool:
    now = now or time.time()
    times = _recent.setdefault(sender, deque())
    while times and now - times[0] > 3600:
        times.popleft()
    if len(times) >= limit:
        return True
    times.append(now)
    return False


async def notify_family(wa, cfg, user: str, order: dict) -> None:
    """Tell the family about an order; send UPI payment links to the payers."""
    lines = [f"- {i.get('quantity', 1)} x {i.get('product')} {i.get('pack', '')} @ Rs {i.get('price')}"
             for i in order["items"]]
    summary = f"{order['platform']} order for {mask(user)}: Rs {order['total']:g}\n" + "\n".join(lines)
    if order.get("payment_links"):
        # UPI needs a person's PIN: the order only goes ahead once someone pays.
        pay = (f"PAYMENT NEEDED - {summary}\n\nPay with UPI here (check the amount is "
               f"Rs {order['total']:g} before entering your PIN):\n" + "\n".join(order["payment_links"]))
        for payer in cfg.payer_numbers:
            await wa.send_text(payer, pay)
        for owner in sorted(set(cfg.owner_numbers) - set(cfg.payer_numbers)):
            await wa.send_text(owner, f"Awaiting UPI payment - {summary}")
    else:
        for owner in cfg.owner_numbers:
            await wa.send_text(owner, f"Paid from wallet - {summary}")


def watch_if_confirming(user: str, message_ids: list[str], media_id: str | None) -> None:
    """If this reply asks her to confirm an order, wait for WhatsApp to say she opened it."""
    if settings.read_reminder_minutes <= 0 or not message_ids:
        return
    store: Store = state["store"]
    pending = store.load_session(user, settings.session_idle_hours, settings.session_max_turns)["pending"]
    if pending:
        store.watch_reply(user, pending, message_ids, media_id)


async def check_unread(now: float | None = None) -> None:
    """Resend unopened price quotes, then tell the family if she still hasn't seen one."""
    now = now or time.time()
    store: Store = state["store"]
    wa: WhatsApp = state["wa"]
    wait = settings.read_reminder_minutes * 60
    for user in [w["user"] for w in store.open_watches()]:
        async with _user_locks.setdefault(user, asyncio.Lock()):
            # She may have replied while we waited for the lock, which ends the watch.
            watch = next(iter(store.open_watches(user)), None)
            if watch is None:
                continue
            pending = store.load_session(user, settings.session_idle_hours,
                                         settings.session_max_turns)["pending"]
            if watch["seen"] or not pending or pending["turn"] != watch["proposal_turn"]:
                store.clear_watch(user)
                continue
            quote = f"{watch['platform']}, Rs {watch['total']:g}"
            if watch["all_failed"]:
                note = (f"Could not deliver the price message ({quote}) to {mask(user)} on WhatsApp. "
                        "The order is waiting for her yes - maybe give her a call.")
            elif now - watch["last_sent"] < wait:
                continue
            elif watch["media_id"] and watch["reminders"] < settings.read_reminder_max:
                try:
                    message_id = await wa.send_audio(user, watch["media_id"])
                except Exception:
                    log.exception("Reminder to %s failed", mask(user))
                    message_id = None
                store.record_reminder(user, message_id, now)
                log.info("Resent unopened price quote to %s", mask(user))
                continue
            else:
                minutes = round((now - watch["started"]) / 60)
                note = (f"{mask(user)} hasn't opened the price message ({quote}) for about {minutes} min. "
                        "The order is waiting for her yes - maybe give her a call.")
            for owner in sorted(set(settings.owner_numbers) - {user}):
                try:
                    await wa.send_text(owner, note)
                except Exception:
                    log.exception("Could not tell %s about an unopened quote", mask(owner))
            store.mark_alerted(user)


async def _reminder_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await check_unread()
        except Exception:
            log.exception("Read-receipt check failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    store = Store(settings.db_path)
    store.purge_older_than(settings.data_retention_days)
    wa = WhatsApp(settings)
    speech = Speech(settings)
    async with Commerce(settings.load_servers()) as commerce:
        async def notify_owner(user: str, order: dict, result: str) -> None:
            await notify_family(wa, settings, user, order)

        state.update(store=store, wa=wa, speech=speech,
                     agent=GroceryAgent(settings, store, commerce, on_order=notify_owner))
        reminders = asyncio.create_task(_reminder_loop()) if settings.read_reminder_minutes > 0 else None
        yield
        if reminders:
            reminders.cancel()
    await wa.aclose()
    await speech.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/webhook")
async def verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == settings.wa_verify_token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(status_code=403)


@app.post("/webhook")
async def incoming(request: Request):
    body = await request.body()
    if not valid_signature(settings.wa_app_secret, body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=401)
    payload = await request.json()
    store: Store | None = state.get("store")
    for receipt in parse_statuses(payload):
        if receipt["status"] == "failed":
            log.warning("WhatsApp could not deliver a message to %s", mask(receipt["recipient"] or ""))
        if store and receipt["recipient"] in settings.allowed_numbers:
            store.record_status(receipt["id"], receipt["status"], receipt["recipient"])
    for msg in parse_messages(payload):
        if msg["id"] in _seen:
            continue
        _seen[msg["id"]] = None
        if len(_seen) > 1000:
            _seen.popitem(last=False)
        # Answer WhatsApp straight away; the work (a few seconds) runs in the background.
        asyncio.create_task(_handle(msg))
    return {"ok": True}


async def _handle(msg: dict) -> None:
    sender = msg["from"]
    if sender not in settings.allowed_numbers:
        log.warning("Ignoring message from number not in ALLOWED_NUMBERS: %s", mask(sender))
        return
    if over_rate_limit(sender, settings.max_messages_per_hour):
        log.warning("Rate limit reached for %s", mask(sender))
        return
    wa: WhatsApp = state["wa"]
    speech: Speech = state["speech"]
    agent: GroceryAgent = state["agent"]

    lock = _user_locks.setdefault(sender, asyncio.Lock())
    async with lock:
        try:
            # She wrote back, so whatever we were waiting on her to open has been dealt with.
            state["store"].clear_watch(sender)
            await wa.mark_read(msg["id"])
            if msg["type"] == "audio":
                audio = await wa.download_media(msg["media_id"])
                text, language = await speech.transcribe(audio)
                if not text:
                    await wa.send_text(sender, "आवाज़ साफ़ नहीं आई, फिर से बोलिए।")
                    return
            elif msg["type"] == "text":
                text, language = msg["text"], None
            else:
                await wa.send_text(sender, "कृपया बोलकर या लिखकर बताइए कौन सा सामान चाहिए।")
                return

            log.info("%s said (%s): %s", mask(sender), language, text)
            answer = await agent.reply(sender, text)
            log.info("Reply to %s: %s", mask(sender), answer)

            sent, media_id = [], None
            try:
                voice = await speech.speak(answer, language or guess_language(answer))
                voice_id, media_id = await wa.send_voice(sender, voice)
                sent.append(voice_id)
            except Exception:
                log.exception("Voice reply failed; sending text only")
            # Text too, so the family can read back prices later.
            sent.append(await wa.send_text(sender, answer))
            try:
                watch_if_confirming(sender, sent, media_id)
            except Exception:
                log.exception("Could not start waiting for a read receipt")
        except Exception:
            log.exception("Failed to handle message from %s", mask(sender))
            try:
                await wa.send_text(sender, "माफ़ कीजिए, कुछ गड़बड़ हो गई। थोड़ी देर में फिर कोशिश कीजिए।")
            except Exception:
                log.exception("Could not send the error message either")
