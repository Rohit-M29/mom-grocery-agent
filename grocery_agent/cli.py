"""Talk to the agent from a terminal, without WhatsApp.

    python -m grocery_agent.cli                  # type messages
    python -m grocery_agent.cli --voice          # also save spoken replies to replies/*.ogg
    python -m grocery_agent.cli --audio note.ogg # send a recorded voice note first
    python -m grocery_agent.cli --list-tools     # show what each connected app can do
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .agent import GroceryAgent
from .commerce import Commerce
from .config import settings
from .speech import Speech, guess_language
from .store import Store


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="local-test")
    ap.add_argument("--voice", action="store_true", help="save spoken replies as .ogg files")
    ap.add_argument("--audio", help="path to a voice note to transcribe and send first")
    ap.add_argument("--list-tools", action="store_true",
                    help="print each connected app's tools and which ones count as placing an order")
    args = ap.parse_args()

    if args.list_tools:
        async with Commerce(settings.load_servers()) as commerce:
            for server in commerce.servers.values():
                print(f"\n{server.label} ({'can order' if server.can_order else 'compare only'})")
                for exposed in sorted(server.tools):
                    _, tool = commerce.route(exposed)
                    if server.can_order and commerce.is_order_tool(server, tool):
                        tag = "  <- PLACES ORDER: needs a spoken yes"
                    elif not server.can_order and commerce.is_cart_or_order_tool(server, tool):
                        tag = "  <- blocked (compare only)"
                    else:
                        tag = ""
                    print(f"  {tool}{tag}")
        return

    store = Store(settings.db_path)
    speech = Speech(settings) if (args.voice or args.audio) else None

    async def print_order(user: str, order: dict, result: str) -> None:
        state = "AWAITING UPI PAYMENT" if order.get("payment_links") else "PAID FROM WALLET"
        print(f"\n[ORDER {state}] {order['platform']} Rs {order['total']:g}")
        for link in order.get("payment_links", []):
            print(f"  pay: {link}")
        print(result, "\n")

    async with Commerce(settings.load_servers()) as commerce:
        agent = GroceryAgent(settings, store, commerce, on_order=print_order)
        print("Connected:", ", ".join(p["name"] for p in commerce.platforms()) or "no shopping apps")
        print("Type a message (Ctrl+C to quit).\n")

        pending_audio = args.audio
        n = 0
        while True:
            language = None
            if pending_audio:
                text, language = await speech.transcribe(Path(pending_audio).read_bytes())
                print(f"you (voice, {language}): {text}")
                pending_audio = None
            else:
                try:
                    text = await asyncio.to_thread(input, "you: ")
                except (EOFError, KeyboardInterrupt):
                    break
                if not text.strip():
                    continue
            answer = await agent.reply(args.user, text)
            print(f"agent: {answer}\n")
            if args.voice and speech:
                n += 1
                out = Path("replies") / f"reply_{n}.ogg"
                out.parent.mkdir(exist_ok=True)
                out.write_bytes(await speech.speak(answer, language or guess_language(answer)))
                print(f"(saved {out})")

    if speech:
        await speech.aclose()


if __name__ == "__main__":
    asyncio.run(main())
