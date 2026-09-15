"""Typed money. Decimal arithmetic only (spec §3.2); never floats or model math."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO_DECIMAL_CURRENCIES = {"JPY", "KRW"}


def quantize(amount: Decimal, currency: str) -> Decimal:
    places = Decimal("1") if currency.upper() in ZERO_DECIMAL_CURRENCIES else Decimal("0.01")
    return amount.quantize(places, rounding=ROUND_HALF_UP)


def parse_amount(value) -> Decimal:
    if value is None:
        raise ValueError("amount required")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # floats are only accepted at the API boundary; convert via str to avoid binary noise
        return Decimal(str(value))
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as e:
        raise ValueError(f"invalid amount {value!r}") from e


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self):
        object.__setattr__(self, "currency", self.currency.upper())
        object.__setattr__(self, "amount", quantize(parse_amount(self.amount), self.currency))

    def __add__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.amount - other.amount, self.currency)

    def _same(self, other: "Money") -> None:
        if other.currency != self.currency:
            raise ValueError(f"currency mismatch {self.currency} vs {other.currency}")

    def is_zero(self) -> bool:
        return self.amount == 0

    def to_dict(self) -> dict:
        return {"amount": str(self.amount), "currency": self.currency}

    def __str__(self) -> str:
        return f"{self.amount:,} {self.currency}"


def convert(amount: Decimal, rate: Decimal, to_currency: str) -> Decimal:
    """Convert using an explicit rate (source/date recorded by the caller)."""
    return quantize(parse_amount(amount) * parse_amount(rate), to_currency)


def split_balanced(total: Decimal, weights: list[Decimal], currency: str) -> list[Decimal]:
    """Allocate `total` across weights so the parts sum exactly to total (invariant 5)."""
    total = quantize(parse_amount(total), currency)
    if not weights:
        return []
    wsum = sum(weights)
    if wsum == 0:
        raise ValueError("weights sum to zero")
    parts = [quantize(total * w / wsum, currency) for w in weights]
    drift = total - sum(parts)
    if drift != 0:
        parts[-1] = quantize(parts[-1] + drift, currency)
    return parts
