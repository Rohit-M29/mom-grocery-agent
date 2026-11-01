"""Minimal WhatsApp Cloud API client: download voice notes, send text and voice replies."""

from __future__ import annotations

import hashlib
import hmac

import httpx

from .config import Settings


def valid_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Check Meta's X-Hub-Signature-256 header so only WhatsApp can call the webhook."""
    if not app_secret:
        return False
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_messages(payload: dict) -> list[dict]:
    """Flatten a webhook payload into [{'id', 'from', 'type', 'text' | 'media_id'}]."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []) or []:
                item = {"id": msg.get("id"), "from": msg.get("from"), "type": msg.get("type")}
                if msg.get("type") == "text":
                    item["text"] = msg.get("text", {}).get("body", "")
                elif msg.get("type") == "audio":
                    item["media_id"] = msg.get("audio", {}).get("id")
                out.append(item)
    return out


class WhatsApp:
    def __init__(self, settings: Settings):
        self.s = settings
        self.http = httpx.AsyncClient(
            base_url=f"https://graph.facebook.com/{settings.wa_graph_version}",
            headers={"Authorization": f"Bearer {settings.wa_token}"}, timeout=60,
        )

    async def download_media(self, media_id: str) -> bytes:
        meta = await self.http.get(f"/{media_id}")
        meta.raise_for_status()
        r = await self.http.get(meta.json()["url"])
        r.raise_for_status()
        return r.content

    async def send_text(self, to: str, text: str) -> None:
        r = await self.http.post(f"/{self.s.wa_phone_number_id}/messages", json={
            "messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text},
        })
        r.raise_for_status()

    async def send_voice(self, to: str, ogg: bytes) -> None:
        up = await self.http.post(
            f"/{self.s.wa_phone_number_id}/media",
            data={"messaging_product": "whatsapp", "type": "audio/ogg"},
            files={"file": ("reply.ogg", ogg, "audio/ogg")},
        )
        up.raise_for_status()
        r = await self.http.post(f"/{self.s.wa_phone_number_id}/messages", json={
            "messaging_product": "whatsapp", "to": to, "type": "audio",
            "audio": {"id": up.json()["id"]},
        })
        r.raise_for_status()

    async def mark_read(self, message_id: str) -> None:
        await self.http.post(f"/{self.s.wa_phone_number_id}/messages", json={
            "messaging_product": "whatsapp", "status": "read", "message_id": message_id,
        })

    async def aclose(self) -> None:
        await self.http.aclose()
