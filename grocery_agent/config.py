"""Settings, read from environment variables (or a .env file)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _list(name: str) -> list[str]:
    return [p.strip().lstrip("+") for p in os.getenv(name, "").split(",") if p.strip()]


@dataclass
class Settings:
    # Claude
    model: str = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
    effort: str = os.getenv("CLAUDE_EFFORT", "low")

    # Who may use the bot. Numbers in international format without "+", e.g. 919812345678.
    allowed_numbers: list[str] = field(default_factory=lambda: _list("ALLOWED_NUMBERS"))
    # Who gets a WhatsApp copy of every order (you).
    owner_numbers: list[str] = field(default_factory=lambda: _list("OWNER_NUMBERS"))

    # Spending limits in rupees.
    max_order_rupees: float = float(os.getenv("MAX_ORDER_RUPEES", "1500"))
    weekly_cap_rupees: float = float(os.getenv("WEEKLY_CAP_RUPEES", "5000"))

    # Delivery context the agent should assume.
    home_pincode: str = os.getenv("HOME_PINCODE", "")

    # Prepaid only. Cash on delivery is always refused, whatever is set here.
    #   wallet: the app's prepaid wallet (e.g. Zepto Cash), topped up by the family; no PIN needed
    #   upi:    a UPI payment link, sent to PAYER_NUMBERS; a person pays with their PIN
    allowed_payment_methods: list[str] = field(
        default_factory=lambda: [m.lower() for m in _list("ALLOWED_PAYMENT_METHODS")] or ["wallet", "upi"])
    default_payment: str = os.getenv("DEFAULT_PAYMENT", "wallet")
    # Who receives UPI payment links. Defaults to OWNER_NUMBERS.
    payer_numbers: list[str] = field(default_factory=lambda: _list("PAYER_NUMBERS") or _list("OWNER_NUMBERS"))

    # Abuse and cost limits.
    max_messages_per_hour: int = int(os.getenv("MAX_MESSAGES_PER_HOUR", "40"))
    # Conversations and order records older than this are deleted.
    data_retention_days: int = int(os.getenv("DATA_RETENTION_DAYS", "90"))

    # Minutes before an unopened price quote is resent (then owners told); 0 turns it off.
    read_reminder_minutes: float = float(os.getenv("READ_REMINDER_MINUTES", "10"))
    read_reminder_max: int = int(os.getenv("READ_REMINDER_MAX", "1"))

    # Sarvam AI (speech to text, text to speech)
    sarvam_api_key: str = os.getenv("SARVAM_API_KEY", "")
    sarvam_stt_model: str = os.getenv("SARVAM_STT_MODEL", "saarika:v2.5")
    sarvam_tts_model: str = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
    sarvam_speaker: str = os.getenv("SARVAM_SPEAKER", "priya")

    # WhatsApp Cloud API
    wa_token: str = os.getenv("WHATSAPP_TOKEN", "")
    wa_phone_number_id: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    wa_verify_token: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    wa_app_secret: str = os.getenv("WHATSAPP_APP_SECRET", "")
    wa_graph_version: str = os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0")

    # Storage and shopping-app connections
    db_path: str = os.getenv("DB_PATH", "data/agent.db")
    servers_file: str = os.getenv("SERVERS_FILE", "servers.json")

    # A conversation starts fresh after this much silence (hours) or this many turns.
    session_idle_hours: float = float(os.getenv("SESSION_IDLE_HOURS", "3"))
    session_max_turns: int = int(os.getenv("SESSION_MAX_TURNS", "30"))

    def load_servers(self) -> list[dict]:
        path = Path(self.servers_file)
        if not path.exists():
            return []
        servers = json.loads(path.read_text())
        return [s for s in servers if s.get("enabled", True)]


settings = Settings()
