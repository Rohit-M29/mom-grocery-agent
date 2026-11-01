"""Safety rails around real-money orders.

The model never decides on its own that an order is approved. An order tool may
run only when all of these hold:
  1. In an earlier turn the agent saved a proposal (platform, items, total) with
     the propose_order tool, and the total was within the spending limits.
  2. The user's message in the *current* turn is a clear "yes" in Hindi,
     Marathi or English, with no "no" in it.
  3. The order goes to the same platform the proposal named.
"""

from __future__ import annotations

import re
import unicodedata

AFFIRMATIVE = {
    # English / Hinglish (romanised)
    "yes", "yeah", "yep", "ok", "okay", "sure", "confirm", "confirmed", "done",
    "haan", "han", "haa", "ha", "haanji", "hanji", "ji", "jee", "theek", "thik",
    "chalega", "bilkul", "kardo", "karo", "order",
    # Marathi (romanised)
    "ho", "hoy", "chalel", "barobar", "kara",
    # Devanagari
    "हाँ", "हां", "हा", "जी", "ठीक", "बिल्कुल", "चलेगा", "करो", "कर", "ऑर्डर",
    "हो", "होय", "चालेल", "बरोबर", "करा",
}
AFFIRMATIVE_PHRASES = {
    "kar do", "kar dijiye", "order karo", "order kar do", "theek hai", "thik hai",
    "कर दो", "कर दीजिए", "ठीक है", "ऑर्डर करो", "ऑर्डर कर दो", "करून टाका",
}
NEGATIVE = {
    "no", "nope", "nah", "not", "dont", "don't", "cancel", "stop", "wait", "later",
    "nahi", "nahin", "nai", "na", "mat", "ruko", "ruk",
    "nako", "nahi", "naka", "thamba", "nko",
    "नहीं", "नही", "ना", "मत", "रुको", "रुकिए", "रद्द", "कैंसल",
    "नको", "नाही", "नका", "थांबा",
}
# A question is not a confirmation ("kitne ka ho gaya?" contains "ho").
QUESTION = {
    "what", "how", "which", "why", "when", "where", "who",
    "kya", "kitna", "kitne", "kitni", "kaun", "kab", "kaise", "kyun", "kyon", "kahan",
    "kiti", "kay", "kasa", "kuthe", "kon",
    "क्या", "कितना", "कितने", "कितनी", "कौन", "कब", "कैसे", "क्यों", "कहाँ",
    "किती", "काय", "कसा", "कुठे", "कोण",
}
MAX_CONFIRM_WORDS = 8


def _tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text.lower())
    # Keep Devanagari letters and combining marks together; split on everything else.
    return [t for t in re.split(r"[^\wऀ-ॿ']+", text) if t]


def is_affirmative(text: str) -> bool:
    """True only for a clear yes with no negation anywhere in the message."""
    if not text or not text.strip():
        return False
    if "?" in text:
        return False
    toks = _tokens(text)
    if any(t in NEGATIVE or t in QUESTION for t in toks):
        return False
    norm = " ".join(toks)
    if any(p in norm for p in AFFIRMATIVE_PHRASES):
        return True
    # A long message is probably saying something else; ask again rather than guess.
    return len(toks) <= MAX_CONFIRM_WORDS and any(t in AFFIRMATIVE for t in toks)


class OrderBlocked(Exception):
    """Raised when an order tool call is not allowed; the message goes back to the model."""


def check_order_allowed(*, platform: str, pending: dict | None, current_turn: int,
                        user_text: str) -> None:
    if not pending:
        raise OrderBlocked(
            "Blocked: no saved proposal. First call propose_order with the platform, items "
            "and total, read the summary to the user, and wait for them to say yes."
        )
    if pending["platform"] != platform:
        raise OrderBlocked(
            f"Blocked: the saved proposal is for {pending['platform']}, not {platform}. "
            "Propose again for this platform and ask the user."
        )
    if pending["turn"] >= current_turn:
        raise OrderBlocked(
            "Blocked: the proposal was made in this same turn. Read it to the user and "
            "wait for their reply before ordering."
        )
    if not is_affirmative(user_text):
        raise OrderBlocked(
            "Blocked: the user's latest message is not a clear yes. Ask them again to confirm."
        )


_COD = re.compile(
    # Not bare "cash": "Zepto Cash" is the prepaid wallet.
    r"(^|[^a-z])(cod|cash[\s_-]*on[\s_-]*delivery|pay[\s_-]*on[\s_-]*delivery)([^a-z]|$)"
    r"|कैश ऑन डिलीवरी|नकद",
    re.IGNORECASE,
)
_PAYMENT_ALIASES = {
    "wallet": ("wallet", "zepto cash", "zepto_cash", "zeptocash", "money", "balance", "prepaid"),
    "upi": ("upi", "payment link", "payment_link", "reserve", "upi_reserve"),
}


def is_cod(value: str) -> bool:
    return bool(_COD.search(value or ""))


def payment_kind(method: str) -> str | None:
    """Map a free-text payment method to 'wallet' or 'upi'; None if unknown."""
    m = (method or "").lower()
    if is_cod(m):
        return None
    for kind, words in _PAYMENT_ALIASES.items():
        if any(w in m for w in words):
            return kind
    return None


def check_payment(method: str, allowed: list[str]) -> str:
    if is_cod(method):
        raise OrderBlocked(
            "Blocked: cash on delivery is not allowed. Orders must be prepaid: "
            f"use one of {', '.join(allowed)}."
        )
    kind = payment_kind(method)
    if kind is None or kind not in allowed:
        raise OrderBlocked(
            f"Blocked: payment method '{method}' is not allowed. Use one of: {', '.join(allowed)}."
        )
    return kind


def find_cod(value) -> str | None:
    """Look through tool arguments (any nesting) for a cash-on-delivery choice."""
    if isinstance(value, str):
        return value if is_cod(value) else None
    if isinstance(value, dict):
        for k, v in value.items():
            if is_cod(str(k)) and v not in (False, None, 0, ""):
                return f"{k}={v}"
            if hit := find_cod(v):
                return hit
    if isinstance(value, (list, tuple)):
        for v in value:
            if hit := find_cod(v):
                return hit
    return None


def payment_kinds_in(value) -> set[str]:
    """Every prepaid payment kind named anywhere in tool arguments."""
    found: set[str] = set()
    if isinstance(value, str):
        if kind := payment_kind(value):
            found.add(kind)
    elif isinstance(value, dict):
        for v in value.values():
            found |= payment_kinds_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            found |= payment_kinds_in(v)
    return found


def check_checkout_payment(args: dict, confirmed_kind: str, require_explicit: bool = True) -> None:
    """The checkout request must be prepaid with the method the user confirmed."""
    if hit := find_cod(args):
        raise OrderBlocked(f"Blocked: cash on delivery found in the checkout request ({hit}). "
                           "Orders must be prepaid; use the wallet or a UPI link.")
    kinds = payment_kinds_in(args)
    if kinds and kinds != {confirmed_kind}:
        raise OrderBlocked(f"Blocked: checkout uses {', '.join(sorted(kinds))} but the user "
                           f"confirmed {confirmed_kind}.")
    if not kinds and require_explicit:
        raise OrderBlocked(
            f"Blocked: the checkout request does not name a payment method, and the shop's "
            f"default might be cash on delivery. Pass the {confirmed_kind} payment method explicitly."
        )


def check_spend(total: float, *, max_order: float, weekly_cap: float, spent_this_week: float) -> None:
    if total <= 0:
        raise OrderBlocked("Blocked: the total must be a positive rupee amount.")
    if total > max_order:
        raise OrderBlocked(
            f"Blocked: Rs {total:g} is above the per-order limit of Rs {max_order:g}. "
            "Tell the user to split the order or ask the family to raise the limit."
        )
    if spent_this_week + total > weekly_cap:
        raise OrderBlocked(
            f"Blocked: this order would take this week's spending to Rs {spent_this_week + total:g}, "
            f"above the weekly limit of Rs {weekly_cap:g}."
        )
