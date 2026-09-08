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

from html import escape as html_escape

from .money import (
    build_sum_line,
    check_total,
    fmt_inr,
    fmt_inr_plain,
    fmt_usdt,
    fmt_usdt_plain,
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


def render_payment_slot(slot: PaymentSlot, *, html: bool = False) -> str:
    """
    The instruction sent to the client, in the exact layout the client specified.

    With html=True the account number is wrapped in <code> so it is tap-to-copy
    — it is transcribed into a banking app, which is exactly where a mistyped
    digit costs the most.
    """
    esc = html_escape if html else (lambda s: s)
    num = f"<code>{slot.account_number}</code>" if html else slot.account_number
    return (
        "New slot\n"
        f"Acc num - {num}\n"
        f"Ifsc - {esc(slot.ifsc)}\n"
        f"Acc name - {esc(slot.account_name)}\n"
        f"{fmt_inr_plain(slot.amount_inr)}"
    )


def render_trade_summary(
    payments: Sequence[Payment],
    *,
    reference: str | None = None,
    expected_inr: Decimal | None = None,
    include_header: bool = True,
) -> str:
    """
    Render the summary distributed on /done.

    Goes to the client, the Bridge, and the relevant supplier (brief, Step 6).

    Each payment renders as three lines - UTR, plain amount, beneficiary - then
    the arithmetic line closes it. If the expected total is supplied, a plain
    statement of the difference is appended when the figures do not match.
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

    if expected_inr is not None:
        result = check_total(total, expected_inr)
        if not result.exact:
            # Reported, never judged. There is no tolerance band: the client
            # asked for exact figures, so any difference at all is shown and
            # the decision is the Bridge's.
            lines.append("")
            direction = "Short by" if result.difference < 0 else "Over by"
            lines.append(
                f"{direction} ₹{fmt_inr(abs(result.difference))} "
                f"against expected ₹{fmt_inr(expected_inr)}"
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
    wallet_address: str | None = None,
) -> str:
    """
    Sent to the Bridge channel when a supplier deposit is detected on-chain.

    The header names the supplier and the internal wallet that received the
    funds (client request, 8 September 2026: "at header must state Supplier A
    and Supplier B when notifying me with Internal wallet address, to ensure no
    manual errors my side").

    The address is printed in full, never truncated. The whole point is that the
    Bridge can compare it against the wallet they are about to act on, and an
    abbreviated address defeats that — the first and last characters of two
    different addresses can easily match.
    """
    header = [f"{supplier_label.upper()}  ·  Transaction {reference}"]
    if wallet_address:
        header.append(f"Internal wallet {wallet_address}")

    return "\n".join(header) + (
        f"\n\nIncoming deposit from {supplier_label} to {client_label}\n"
        f"USDT = {fmt_usdt(usdt_in)} to Send INR {fmt_inr(inr_out)}\n"
        f"Hash {tx_hash}"
    )


def render_client_confirmation(
    *,
    client_label: str,
    usdt_out: Decimal,
    inr_amount: Decimal,
    slots: Sequence[PaymentSlot],
    html: bool = False,
) -> str:
    """
    The confirmation the Bridge approves before it is forwarded to the client.

    Two client requests from 8 September 2026 shape this:

      * The USDT figure carries no thousands separators. It is pasted straight
        into a wallet, and a comma there is at best rejected and at worst
        silently truncated. INR keeps its separators — that one is read, not
        pasted.

      * With html=True the USDT figure is wrapped in <code>, which Telegram
        renders as tap-to-copy. That is the "copy button" — Telegram has no
        real one, but a code span is one tap and works on mobile and desktop.

    Everything interpolated is escaped, because account names come from user
    input and an unescaped "&" would break the whole message.
    """
    esc = html_escape if html else (lambda s: s)
    usdt = fmt_usdt_plain(usdt_out)

    lines = [
        f"Confirm amount and details to send to {esc(client_label)}",
        f"USDT = {f'<code>{usdt}</code>' if html else usdt}"
        f" to Send INR {fmt_inr(inr_amount)}",
        "",
    ]
    for slot in slots:
        lines.append(render_payment_slot(slot, html=html))
        lines.append("")
    return "\n".join(lines).rstrip()
