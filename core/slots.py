"""
Payment slot allocation.

Splitting a trade's INR obligation across several of the supplier's accounts is
arithmetic that must not drift: the slots have to sum to exactly what the
deposit justifies. Over-allocating would instruct the client to pay more than
they owe; under-allocating leaves money uncollected with nothing flagging it.

The rules live here rather than in the bot handler so they can be tested
without a Telegram harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence

from .money import MoneyError, round_inr, to_decimal


@dataclass(frozen=True)
class Allocation:
    allocated: Decimal
    remaining: Decimal
    complete: bool
    over: bool


def allocate(amounts: Iterable, expected) -> Allocation:
    """
    Summarise how much of `expected` a set of slot amounts covers.

    Everything is rounded to whole rupees first, so the total of the slots is
    the sum of the figures the client will actually be shown — not a more
    precise number that happens to disagree with them.
    """
    expected_d = round_inr(expected)
    total = sum((round_inr(a) for a in amounts), Decimal(0))
    remaining = expected_d - total
    return Allocation(
        allocated=total,
        remaining=remaining,
        complete=remaining == 0,
        over=remaining < 0,
    )


def check_new_slot(existing: Sequence, amount, expected) -> Decimal:
    """
    Validate one more slot against what is already allocated.

    Returns the rounded amount if it is acceptable, and raises MoneyError with a
    message fit to show the Bridge if it is not.
    """
    amount_d = round_inr(to_decimal(amount))
    if amount_d <= 0:
        raise MoneyError("Amount must be greater than zero.")

    state = allocate(existing, expected)
    if amount_d > state.remaining:
        raise MoneyError(
            f"That is more than the {state.remaining:,.0f} still to allocate."
        )
    return amount_d
