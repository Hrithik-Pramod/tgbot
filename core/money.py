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
    """
    inr = usdt_to_inr(usdt_in, supply_rate)
    owed = inr_to_usdt(inr, sell_rate)
    return round_usdt(to_decimal(usdt_in) - owed)


# --------------------------------------------------------------------------
# Tolerance (answer E1)
# --------------------------------------------------------------------------

TOLERANCE_USDT = Decimal("1")


def tolerance_inr(sell_rate) -> Decimal:
    """
    The client set the tolerance at "less than 1 USDT", but payments arrive in
    INR. Convert at the trade's sell rate so the threshold means the same thing
    in the currency actually being compared.

    NOTE: open question 1 - confirm the tolerance should track the sell rate
    rather than being a fixed rupee figure.
    """
    return round_inr(TOLERANCE_USDT * to_decimal(sell_rate))


class ToleranceCheck(NamedTuple):
    within_tolerance: bool
    exact: bool
    difference: Decimal      # positive = overpaid, negative = short
    tolerance: Decimal


def check_total(paid_inr, expected_inr, sell_rate) -> ToleranceCheck:
    """
    Compare what the client actually paid against what was expected.

    Per E1 the trade is allowed to close inside tolerance, but the Bridge is
    always notified of any shortfall so it can be carried to the next round.
    """
    paid = round_inr(paid_inr)
    expected = round_inr(expected_inr)
    diff = paid - expected
    tol = tolerance_inr(sell_rate)
    return ToleranceCheck(
        within_tolerance=abs(diff) <= tol,
        exact=diff == 0,
        difference=diff,
        tolerance=tol,
    )


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
