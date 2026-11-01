"""Pack-size parsing and offer comparison.

This is deliberately plain code, not LLM judgement: "1 litre" must never quietly
become "200 ml", so size matching and price ranking are deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Every quantity is normalised to one of three base units.
ML, G, PC = "ml", "g", "pc"

_UNIT_ALIASES: dict[str, tuple[str, float]] = {
    # volume
    "ml": (ML, 1), "mls": (ML, 1), "millilitre": (ML, 1), "milliliter": (ML, 1),
    "l": (ML, 1000), "lt": (ML, 1000), "ltr": (ML, 1000), "ltrs": (ML, 1000),
    "litre": (ML, 1000), "litres": (ML, 1000), "liter": (ML, 1000), "liters": (ML, 1000),
    "लीटर": (ML, 1000), "लिटर": (ML, 1000), "मिली": (ML, 1),
    # weight
    "g": (G, 1), "gm": (G, 1), "gms": (G, 1), "gram": (G, 1), "grams": (G, 1), "gr": (G, 1),
    "kg": (G, 1000), "kgs": (G, 1000), "kilo": (G, 1000), "kilos": (G, 1000),
    "kilogram": (G, 1000), "kilograms": (G, 1000),
    "किलो": (G, 1000), "ग्राम": (G, 1),
    # count
    "pc": (PC, 1), "pcs": (PC, 1), "piece": (PC, 1), "pieces": (PC, 1), "unit": (PC, 1),
    "units": (PC, 1), "n": (PC, 1), "nos": (PC, 1), "no": (PC, 1), "pack": (PC, 1),
    "dozen": (PC, 12), "दर्जन": (PC, 12), "डझन": (PC, 12),
}

_NUM = r"(\d+(?:\.\d+)?)"
_UNIT = r"([a-zA-Zऀ-ॿ]+)"
# "2 x 500 ml", "2x500ml", "500 ml x 2", "pack of 6"
_MULTI_PREFIX = re.compile(rf"{_NUM}\s*[x×*]\s*{_NUM}\s*{_UNIT}", re.IGNORECASE)
_MULTI_SUFFIX = re.compile(rf"{_NUM}\s*{_UNIT}\s*[x×*]\s*{_NUM}", re.IGNORECASE)
_PACK_OF = re.compile(rf"pack\s+of\s+{_NUM}", re.IGNORECASE)
_SINGLE = re.compile(rf"{_NUM}\s*{_UNIT}", re.IGNORECASE)


@dataclass(frozen=True)
class Quantity:
    amount: float  # total amount in the base unit (ml, g or pc)
    unit: str      # ML, G or PC
    packs: int = 1  # how many retail packs make up the amount (2 x 500 ml -> 2)

    @property
    def per_pack(self) -> float:
        return self.amount / self.packs

    def describe(self) -> str:
        def fmt(v: float, u: str) -> str:
            if u == ML and v >= 1000:
                return f"{v / 1000:g} L"
            if u == G and v >= 1000:
                return f"{v / 1000:g} kg"
            return f"{v:g} {u}"

        if self.packs > 1:
            return f"{self.packs} x {fmt(self.per_pack, self.unit)}"
        return fmt(self.amount, self.unit)


def _unit(token: str) -> tuple[str, float] | None:
    return _UNIT_ALIASES.get(token.lower().rstrip("."))


def parse_quantity(text: str) -> Quantity | None:
    """Parse a pack-size string such as '1 L', '2 x 500 ml', '1.5kg' or 'Pack of 6'."""
    if not text:
        return None
    t = text.replace(",", " ")

    m = _MULTI_PREFIX.search(t)
    if m and (u := _unit(m.group(3))):
        packs, each = int(float(m.group(1))), float(m.group(2))
        return Quantity(packs * each * u[1], u[0], packs)

    m = _MULTI_SUFFIX.search(t)
    if m and (u := _unit(m.group(2))):
        each, packs = float(m.group(1)), int(float(m.group(3)))
        return Quantity(packs * each * u[1], u[0], packs)

    for m in _SINGLE.finditer(t):
        if u := _unit(m.group(2)):
            single = Quantity(float(m.group(1)) * u[1], u[0])
            pack_of = _PACK_OF.search(t)
            if pack_of and single.unit != PC:
                packs = int(float(pack_of.group(1)))
                return Quantity(single.amount * packs, single.unit, packs)
            return single

    m = _PACK_OF.search(t)
    if m:
        return Quantity(float(m.group(1)), PC)
    return None


def compare_offers(requested: str, offers: list[dict], tolerance_pct: float = 0.0) -> dict:
    """Rank offers for one requested item.

    requested: the size the user asked for, e.g. "1 litre". May be empty when the
        user named no size; then everything is ranked by price per unit.
    offers: [{"platform", "product", "pack", "price", "in_stock"?, "eta_minutes"?}]
        `price` is the rupee price for one listed pack.

    Returns exact single-pack matches, equivalent multi-pack matches (2 x 500 ml
    for 1 L), and the rest, each sorted by price, plus a plain-language note
    the agent can use when nothing matches exactly.
    """
    want = parse_quantity(requested) if requested else None
    exact, equivalent, other, unparsed = [], [], [], []

    for raw in offers:
        if raw.get("in_stock") is False:
            continue
        try:
            price = float(raw["price"])
        except (KeyError, TypeError, ValueError):
            unparsed.append({**raw, "reason": "no price"})
            continue
        qty = parse_quantity(str(raw.get("pack") or raw.get("product") or ""))
        if qty is None:
            unparsed.append({**raw, "reason": "pack size not understood"})
            continue

        row = {
            **raw,
            "price": price,
            "size": qty.describe(),
            "price_per_unit": _per_unit_label(price, qty),
            "_per_base": price / qty.amount,
        }
        if want is None or qty.unit != want.unit:
            other.append(row)
            continue

        tol = want.amount * tolerance_pct / 100
        same_total = abs(qty.amount - want.amount) <= tol
        if same_total and qty.packs == want.packs:
            exact.append(row)
        elif same_total:
            equivalent.append(row)
        else:
            other.append(row)

    for bucket in (exact, equivalent):
        bucket.sort(key=lambda r: r["price"])
    other.sort(key=lambda r: r["_per_base"])
    for bucket in (exact, equivalent, other):
        for r in bucket:
            r.pop("_per_base", None)

    if want is None:
        note = "No size was requested; offers are ranked by price per unit."
    elif exact:
        best = exact[0]
        note = f"Best exact match for {want.describe()}: {best['product']} on {best['platform']} at Rs {best['price']:g}."
    elif equivalent:
        best = equivalent[0]
        note = (f"No single {want.describe()} pack in stock. Closest: {best['size']} of "
                f"{best['product']} on {best['platform']} at Rs {best['price']:g}. Ask the user before choosing it.")
    elif other:
        sizes = sorted({r["size"] for r in other if r.get("size")})
        note = (f"No {want.describe()} pack found. Available sizes: {', '.join(sizes)}. "
                "Tell the user and ask which one they want; do not pick a different size on your own.")
    else:
        note = "Nothing in stock matched."

    return {
        "requested": want.describe() if want else None,
        "exact_matches": exact,
        "equivalent_multipacks": equivalent,
        "other_sizes": other,
        "not_understood": unparsed,
        "note": note,
    }


def _per_unit_label(price: float, q: Quantity) -> str:
    if q.unit == ML:
        return f"Rs {price / q.amount * 1000:.2f}/L"
    if q.unit == G:
        return f"Rs {price / q.amount * 1000:.2f}/kg"
    return f"Rs {price / q.amount:.2f}/pc"
