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
from .whatsapp import WhatsApp, parse_messages, valid_signature

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
        yield
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
    for msg in parse_messages(await request.json()):
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

            try:
                voice = await speech.speak(answer, language or guess_language(answer))
                await wa.send_voice(sender, voice)
            except Exception:
                log.exception("Voice reply failed; sending text only")
            # Text too, so the family can read back prices later.
            await wa.send_text(sender, answer)
        except Exception:
            log.exception("Failed to handle message from %s", mask(sender))
            try:
                await wa.send_text(sender, "माफ़ कीजिए, कुछ गड़बड़ हो गई। थोड़ी देर में फिर कोशिश कीजिए।")
            except Exception:
                log.exception("Could not send the error message either")
