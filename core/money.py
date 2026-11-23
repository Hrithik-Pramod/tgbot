"""
Money and rate arithmetic.

Every monetary value in this system is a Decimal. Floats are never used and must
never be introduced: 0.1 + 0.2 != 0.3 in binary floating point, and this system
generates real payment instructions.

Rules confirmed with the client:
  - A rate is INR per 1 USDT.               (C1)
  - USDT x supply rate  = INR obligation.   (brief)
  - INR / sell rate     = USDT owed out.    (C2)
  - USDT rounds to 2 decimal places.        (C4)
  - INR rounds to whole rupees.             (C4)
  - Rounding is to nearest, half up.        (C4)
"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, NamedTuple

# Quantisation targets
USDT_PLACES = Decimal("0.01")   # 2 dp
INR_PLACES = Decimal("1")       # whole rupees


class MoneyError(ValueError):
    """Raised when a value cannot be interpreted as money."""


# --------------------------------------------------------------------------
# Parsing and rounding
# --------------------------------------------------------------------------

def to_decimal(value) -> Decimal:
    """
    Convert user input to Decimal.

    Accepts Decimal, int, and str. Strings may contain spaces, commas,
    underscores, and a leading rupee sign - clients paste from banking apps and
    the formatting is unpredictable. Floats are rejected outright: accepting one
    would silently import binary rounding error into the ledger.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        raise MoneyError(
            "float is not accepted for monetary values - pass a str or Decimal"
        )
    if isinstance(value, str):
        cleaned = value.strip()
        for junk in (",", " ", " ", "_", "₹", "Rs.", "Rs", "INR", "USDT"):
            cleaned = cleaned.replace(junk, "")
        if not cleaned:
            raise MoneyError("empty value")
        try:
            return Decimal(cleaned)
        except Exception as exc:
            raise MoneyError(f"cannot parse {value!r} as a number") from exc
    raise MoneyError(f"unsupported type {type(value).__name__}")


def round_usdt(value) -> Decimal:
    """Round a USDT amount to 2 dp, half up."""
    return to_decimal(value).quantize(USDT_PLACES, rounding=ROUND_HALF_UP)


def round_inr(value) -> Decimal:
    """Round an INR amount to whole rupees, half up."""
    return to_decimal(value).quantize(INR_PLACES, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Rate conversions
# --------------------------------------------------------------------------

def usdt_to_inr(usdt, supply_rate) -> Decimal:
    """
    Supplier side. USDT received x supply rate = INR obligation.

    Example from the brief: 1000 USDT at rate 100 -> 100,000 INR.
    """
    usdt_d = to_decimal(usdt)
    rate_d = to_decimal(supply_rate)
    if usdt_d <= 0:
        raise MoneyError("USDT amount must be positive")
    if rate_d <= 0:
        raise MoneyError("supply rate must be positive")
    return round_inr(usdt_d * rate_d)


def inr_to_usdt(inr, sell_rate) -> Decimal:
    """
    Client side. INR obligation / sell rate = USDT owed out.

    Example confirmed by the client: 100,000 INR at sell rate 111
    -> 900.9009009... -> 900.90 after rounding.
    """
    inr_d = to_decimal(inr)
    rate_d = to_decimal(sell_rate)
    if inr_d <= 0:
        raise MoneyError("INR amount must be positive")
    if rate_d <= 0:
        raise MoneyError("sell rate must be positive")
    return round_usdt(inr_d / rate_d)


def margin_usdt(usdt_in, supply_rate, sell_rate) -> Decimal:
    """
    Bridge margin in USDT, which is where it sits per answer A3.

    Received from supplier, minus what is owed onward to the client.

    This is the WHOLE spread and stays that way. Where an introducer takes
    part of it, split_three_ways is what divides it — see there.
    """
    inr = usdt_to_inr(usdt_in, supply_rate)
    owed = inr_to_usdt(inr, sell_rate)
    return round_usdt(to_decimal(usdt_in) - owed)


class Division(NamedTuple):
    """How one deposit divides. The three always sum to usdt_in."""
    inr_expected: Decimal        # what the client sends, and the vendor gets
    client_usdt: Decimal         # what goes out to the client
    introducer_usdt: Decimal     # the introducer's commission
    bridge_usdt: Decimal         # what is left, which is the desk's


def split_three_ways(usdt_in, supply_rate, buy_rate, sell_rate) -> Division:
    """
    Divide a deposit between the client, the introducer and the desk.

    THE THREE RATES (Bridge, 23 November 2026)

        Supply is the rate the supplier is providing
        Buy is the rate I enter including the introducers fee
        Sell rate is what I sell to the client for

        the difference between the supply and buy is a margin we do not
        receive, this is available on a separate telegram channel for the
        introducer

    His own worked example, on 10,000 USDT at 106.20 / 107.20 / 108.20:

        106.2*10000 = 1062000
        1062000/107.2 = 9906.7164
        1062000/108.2 = 9815.1571
        Total spread is 10000-9815.1571 = 184.8429 USDT
        USDT For Introducer is 10000-9906.7164 = 93.2836
        Remaining commission for partners = 184.8429-93.2836 = 91.5593

    So the commission is USDT, not INR — a slice of the spread, measured by
    applying the buy rate where the sell rate would otherwise go. Nothing
    the client or the vendor sees changes: inr_expected is still the supply
    rate, and the client is still owed at the sell rate.

    NO INTRODUCER IS buy == supply

    Then 1062000/106.2 is 10000 exactly, the introducer's share is zero, and
    the desk keeps the whole 184.84 — which is what margin_usdt returns
    today. Every vendor without an introducer therefore runs down this same
    path with buy set equal to supply, and there is no second code path to
    forget to update. That is the reason buy_rate defaults to supply_rate
    rather than to NULL.

    THE DESK TAKES THE REMAINDER

    Three figures rounded to 2dp from one deposit will not add up on their
    own, and a commission report that does not reconcile to the deposit is
    worse than no report. So the client's share and the introducer's share
    are each computed properly, and the desk's is whatever is left.

    He chose the same thing himself without being asked: "Remaining
    commission for partners" is a subtraction, not a third calculation. It
    is also the lesson from the split of 16 November, applied at the
    design stage this time rather than after a rupee went missing.
    """
    usdt = to_decimal(usdt_in)

    # Checked on the RATE, not on the rounded share.
    #
    # The first version raised when the introducer's share came out
    # negative, which looked equivalent and is not. With buy == supply —
    # every vendor that has no introducer at all — inr is rounded UP to
    # whole rupees, so inr / supply can exceed the deposit by a fraction of
    # a cent and the share lands at -0.01. That is rounding, not a rate
    # error, and raising on it would have refused ordinary deposits: the
    # real ones carry three decimals (66,843.569 on 16 November) and a
    # search found it in seconds.
    if to_decimal(buy_rate) < to_decimal(supply_rate):
        # Paying the introducer out of the vendor's money rather than out of
        # the spread is not the deal, and is almost certainly a typo at
        # /specialrate.
        raise MoneyError("the buy rate cannot be below the supply rate")

    inr = usdt_to_inr(usdt, supply_rate)

    client = inr_to_usdt(inr, sell_rate)
    at_buy = inr_to_usdt(inr, buy_rate)

    # Floored, for the rounding case above. A genuine negative is already
    # impossible: buy is not below supply.
    introducer = max(round_usdt(usdt - at_buy), Decimal("0.00"))

    return Division(
        inr_expected=inr,
        client_usdt=client,
        introducer_usdt=introducer,
        bridge_usdt=round_usdt(usdt - client - introducer),
    )


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------
#
# There is deliberately no tolerance band. The client asked for exact figures
# rounded to 2 decimal places and nothing else (8 September 2026): a trade is
# never "close enough", and the bot never decides that a shortfall is
# acceptable. It reports the difference and leaves the judgement to the Bridge.


class TotalCheck(NamedTuple):
    exact: bool
    difference: Decimal      # positive = overpaid, negative = short


def check_total(paid_inr, expected_inr) -> TotalCheck:
    """
    Compare what the client actually paid against what was expected.

    Reports only. Any difference at all is surfaced — there is no threshold
    below which a discrepancy is hidden.
    """
    diff = round_inr(paid_inr) - round_inr(expected_inr)
    return TotalCheck(exact=diff == 0, difference=diff)


# --------------------------------------------------------------------------
# UTR handling
# --------------------------------------------------------------------------

_UTR_STRIP = re.compile(r"[\s\-_]+")


def normalise_utr(raw: str) -> str:
    """
    Clean a UTR for storage and comparison.

    The brief requires spaces to be stripped. We also drop hyphens and
    underscores and uppercase the result, because clients paste from a dozen
    different banking apps and the same reference arrives formatted differently
    each time. Duplicate detection depends on this being consistent.

    Validation is deliberately loose. The client's own UTRs look like
    EXBKR1 + YYYYMMDD + 8 digits (22 chars, Bank of India), but every bank
    formats differently - hard-coding that pattern would reject valid UTRs from
    any other bank.
    """
    if not isinstance(raw, str):
        raise MoneyError("UTR must be a string")
    cleaned = _UTR_STRIP.sub("", raw).upper()
    if not cleaned:
        raise MoneyError("UTR is empty")
    if not cleaned.isalnum():
        raise MoneyError("UTR must contain only letters and digits")
    if not 8 <= len(cleaned) <= 32:
        raise MoneyError(f"UTR length {len(cleaned)} is outside the expected 8-32 range")
    return cleaned


# --------------------------------------------------------------------------
# Formatting (answer F1: Western grouping)
# --------------------------------------------------------------------------

def fmt_inr(value) -> str:
    """Format INR with Western thousands separators, no decimals."""
    return f"{round_inr(value):,}"


def fmt_usdt(value) -> str:
    """Format USDT to 2 dp with Western thousands separators."""
    return f"{round_usdt(value):,.2f}"


def pct_collected(paid, expected) -> str:
    """
    How far along a collection is, as a percentage.

    Client request, 21 September 2026: "on MYFX, the progress can you add %
    to my look up as well."

    NEVER ROUNDS UP TO 100

    A trade on ₹3,999,999 of ₹4,000,016 is 99.9996% collected, and every
    obvious way of writing that prints "100%". A hundred per cent means
    finished, and this system has spent a fortnight learning what a figure
    that looks finished and is not costs: ₹902,460 on 11 September,
    ₹995,495 twice the week after. So 100% appears only when nothing at
    all is outstanding; anything short reads 99%.

    Rounds DOWN for the same reason. 61.8% is 61%, because overstating
    progress is the direction that hurts.
    """
    paid_d, expected_d = to_decimal(paid), to_decimal(expected)
    if expected_d <= 0:
        return "—"
    if paid_d >= expected_d:
        return "100%" if paid_d == expected_d else f"{int(paid_d / expected_d * 100)}%"
    return f"{min(99, int(paid_d / expected_d * 100))}%"


def fmt_rate(value) -> str:
    """
    A rate as somebody would say it: 106.2, not 106.200000.

    Rates are NUMERIC(20, 6) so the database hands back every trailing zero,
    and every screen that printed one interpolated it raw. /viewrate read
    "Buy 106.200000", and so did /setrate, /reprice and the reopen notice.

    Bridge, 21 September 2026, mid-trade:

        copying from the telegram, something is not working right, copying
        from Telegram gets extra long numbers set not value of trade

    Trailing zeros only, never rounding: a rate of 106.125 keeps all three
    decimals, because truncating a rate would change the arithmetic rather
    than merely tidy the display.
    """
    d = to_decimal(value).normalize()
    # normalize() turns 100 into 1E+2. Undo that.
    return f"{d:f}"


def fmt_usdt_plain(value) -> str:
    """
    Format USDT to 2 dp with no separators at all.

    Used for the figure the Bridge sends to the client (client request,
    8 Sep 2026: "on amount to send to client in USDT, i dont want any commas").
    That number gets pasted straight into a wallet, and a comma in a wallet's
    amount field is at best rejected and at worst silently truncated.
    """
    return f"{round_usdt(value):.2f}"


def fmt_inr_plain(value) -> str:
    """
    Format INR with no separators.

    The client's real summary lists each tranche unformatted (229000) and only
    the final total with commas (1,499,297). This reproduces that exactly.
    """
    return str(round_inr(value))


def build_sum_line(amounts: Iterable) -> str:
    """
    Build the arithmetic line that closes every summary.

    Reproduces the client's format precisely:
        229000+230000+246000+263000+291000+240297 = 1,499,297

    Note the asymmetry - operands are plain, the total is grouped. That is how
    the client writes it, so that is how the bot writes it.
    """
    values = [round_inr(a) for a in amounts]
    if not values:
        raise MoneyError("cannot build a sum line from an empty list")
    left = "+".join(str(v) for v in values)
    return f"{left} = {fmt_inr(sum(values))}"
