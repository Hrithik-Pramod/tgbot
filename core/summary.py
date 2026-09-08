"""
Trade summary rendering.

The format here is copied from a real summary the client supplied, not invented.
It is reproduced character for character because suppliers and clients read these
messages to reconcile against their own bank statements, and any deviation
creates doubt about whether the figures are the same ones they are looking at.

Client's example:

    EXBKR12026090700002248
    229000
    to Alpha Traders
    ...
    229000+230000+246000+263000+291000+240297 = 1,499,297
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .money import (
    build_sum_line,
    check_total,
    fmt_inr,
    fmt_inr_plain,
    fmt_usdt,
    round_inr,
)


@dataclass(frozen=True)
class Payment:
    """One INR tranche logged by the client via /add."""
    utr: str
    amount_inr: Decimal
    beneficiary_name: str


@dataclass(frozen=True)
class PaymentSlot:
    """
    A payment instruction issued to the client.

    Corresponds to the client's "New slot" note - the account details and amount
    the client is told to pay to.
    """
    account_name: str
    account_number: str
    ifsc: str
    amount_inr: Decimal


def render_payment_slot(slot: PaymentSlot) -> str:
    """
    The instruction sent to the client, in the exact layout the client specified.
    """
    return (
        "New slot\n"
        f"Acc num - {slot.account_number}\n"
        f"Ifsc - {slot.ifsc}\n"
        f"Acc name - {slot.account_name}\n"
        f"{fmt_inr_plain(slot.amount_inr)}"
    )


def render_trade_summary(
    payments: Sequence[Payment],
    *,
    reference: str | None = None,
    expected_inr: Decimal | None = None,
    sell_rate: Decimal | None = None,
    include_header: bool = True,
) -> str:
    """
    Render the summary distributed on /done.

    Goes to the client, the Bridge, and the relevant supplier (brief, Step 6).

    Each payment renders as three lines - UTR, plain amount, beneficiary - then
    the arithmetic line closes it. If the expected total and sell rate are
    supplied, a reconciliation note is appended when the figures do not match.
    """
    if not payments:
        raise ValueError("cannot render a summary with no payments")

    lines: list[str] = []

    if include_header and reference:
        lines.append(f"Transaction {reference}")
        lines.append("")

    for p in payments:
        lines.append(p.utr)
        lines.append(fmt_inr_plain(p.amount_inr))
        lines.append(f"to {p.beneficiary_name}")

    lines.append("")
    lines.append(build_sum_line(p.amount_inr for p in payments))

    total = round_inr(sum(p.amount_inr for p in payments))

    if expected_inr is not None and sell_rate is not None:
        result = check_total(total, expected_inr, sell_rate)
        if not result.exact:
            lines.append("")
            if result.difference < 0:
                lines.append(
                    f"Short by ₹{fmt_inr(abs(result.difference))} "
                    f"against expected ₹{fmt_inr(expected_inr)}"
                )
            else:
                lines.append(
                    f"Over by ₹{fmt_inr(result.difference)} "
                    f"against expected ₹{fmt_inr(expected_inr)}"
                )
            if result.within_tolerance:
                lines.append(
                    f"Within tolerance (₹{fmt_inr(result.tolerance)}) — "
                    "carry to the next round."
                )
            else:
                lines.append(
                    f"OUTSIDE tolerance (₹{fmt_inr(result.tolerance)}) — "
                    "please check before closing."
                )

    return "\n".join(lines)


def render_completion_notice(total_inr: Decimal) -> str:
    """
    The short confirmation the client asked for, quoting their own wording:
    "This order is completed - Rs 1,499,297 sent in full."
    """
    return f"This order is completed — ₹{fmt_inr(total_inr)} sent in full."


def render_deposit_notification(
    *,
    reference: str,
    supplier_label: str,
    client_label: str,
    usdt_in: Decimal,
    inr_out: Decimal,
    tx_hash: str,
) -> str:
    """
    Sent to the Bridge channel when a supplier deposit is detected on-chain.
    Follows the layout in the original brief.
    """
    return (
        f"Transaction {reference}\n"
        f"Incoming deposit from {supplier_label} to {client_label}\n"
        f"USDT = {fmt_usdt(usdt_in)} to Send INR {fmt_inr(inr_out)}\n"
        f"Hash {tx_hash}"
    )


def render_client_confirmation(
    *,
    client_label: str,
    usdt_out: Decimal,
    inr_amount: Decimal,
    slots: Sequence[PaymentSlot],
) -> str:
    """
    The confirmation the Bridge approves before it is forwarded to the client.
    """
    lines = [
        f"Confirm amount and details to send to {client_label}",
        f"USDT = {fmt_usdt(usdt_out)} to Send INR {fmt_inr(inr_amount)}",
        "",
    ]
    for slot in slots:
        lines.append(render_payment_slot(slot))
        lines.append("")
    return "\n".join(lines).rstrip()
