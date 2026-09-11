"""
Free-form payment paste parser.

Client A's team sends payments as a pasted block rather than answering prompts,
in whatever order and formatting they happen to use (client note, 8 Sep 2026):

    BKIDR12026090800000380      BKIDR12026090800000380      Amount 249000
    249000                      249 000                     to Ekta traders
    to Ekta traders             to Ekta traders             UTR BKIDR1202609...

All three are the same payment. Rather than forcing the client to change how
they work, the bot reads what they already send.

THE APPROACH
Lines are classified by SHAPE, not by position, so field order does not matter:

  - a run of digits and letters, 8+ characters   -> a UTR
  - digits only, short                           -> an amount
  - words                                        -> a beneficiary name

An explicit label ("UTR ...", "Amount ...", "to ...") always wins over the
shape rule, which is what makes the one genuinely ambiguous case tractable: a
bank whose UTRs are all digits. See `AMBIGUOUS_DIGIT_LEN`.

Nothing here decides anything on its own. The parser returns what it believes
it read and the caller confirms it with the client before a single figure
reaches the ledger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .money import MoneyError, normalise_utr, round_inr, to_decimal

# An all-digit token at least this long is read as a UTR rather than an amount.
# Indian UTRs run 12-22 characters; a single INR payment of 10^12 is not a
# realistic trade. Below this, digits are read as money.
AMBIGUOUS_DIGIT_LEN = 12

# Explicit labels. These beat every shape rule.
#
# (?![a-z]) rather than \b: \b needs a word/non-word transition, so "Rs." would
# fail — the "." and the following space are both non-word, and the match
# collapses to "Rs" leaving ". 249 000", which reads as ₹0.249. The lookahead
# just says "not the middle of a longer word", which is what was meant, and it
# still keeps "to" from matching "total".
#
# "." is a separator so "Rs." and "Amt." are consumed with the label.
_LBL_UTR = re.compile(r"^\s*(?:utr|rrn|ref(?:erence)?|txn|transaction)(?![a-z])[:\-–.\s]*", re.I)
_LBL_AMT = re.compile(r"^\s*(?:amount|amt|rs|inr|₹)(?![a-z])[:\-–.\s]*", re.I)
# "Acc name - Ekta traders" is the client's agreed format (8 Sep 2026), so the
# optional " name" has to be consumed with the label — otherwise the
# beneficiary comes out as "name - Ekta traders".
_LBL_BEN = re.compile(
    r"^\s*(?:to|acc(?:ount)?(?:\s+name)?|beneficiary|benef|name)(?![a-z])[:\-–.\s]*",
    re.I,
)

# Lines that carry no payment data. Pasting alongside a screenshot brings the
# surrounding chrome with it.
_NOISE_TIME = re.compile(
    r"^\s*(?:"
    r"\d{1,2}[:.]\d{2}\s*(?:am|pm)?"           # 11:04 am
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"          # 08/09/2026
    r")\s*$",
    re.I,
)

# Matched as a vocabulary rather than a pattern, because these arrive in every
# combination: "Payment Successful", "Transfer complete", "IMPS", "Sent".
_NOISE_WORDS = {
    "payment", "payments", "transfer", "transferred", "transaction", "txn",
    "successful", "success", "completed", "complete", "done", "sent", "paid",
    "status", "money", "imps", "neft", "rtgs", "upi", "bank", "details",
}


def _is_noise(line: str) -> bool:
    """
    True when a line is screenshot furniture rather than payment data.

    A line counts as noise only if it contains no digits and every word in it
    is a known status word — so "Payment Successful" is dropped while "Ekta
    traders" is kept. Being conservative here matters: wrongly discarding a
    line loses a payment, while wrongly keeping one is caught at confirmation.
    """
    if _NOISE_TIME.match(line):
        return True
    words = re.findall(r"[A-Za-z]+", line)
    if not words or any(c.isdigit() for c in line):
        return False
    return all(w.lower() in _NOISE_WORDS for w in words)

_STRIP = str.maketrans("", "", "   ,'")


@dataclass
class ParsedPayment:
    utr: str
    amount_inr: Decimal
    beneficiary: Optional[str] = None

    @property
    def complete(self) -> bool:
        return bool(self.utr) and self.amount_inr > 0


@dataclass
class ParseResult:
    payments: list[ParsedPayment] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    # Whether anything UTR-shaped appeared at all.
    #
    # This is what separates "a payment that did not parse" from "someone
    # talking". The bot lives in a group where people hold ordinary
    # conversations, and almost any sentence with a number in it produces a
    # problem — so problems alone is far too eager a reason to speak. On
    # 10 September 2026 the client's group was being answered on every message
    # containing a digit, which their team found intolerable and rightly so.
    #
    # A bank reference is long and distinctive. If one is present the sender
    # was trying to log a payment and deserves to be told it failed. If not,
    # silence.
    saw_utr: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.payments) and not self.problems


def _digits_only(token: str) -> bool:
    return token.translate(_STRIP).isdigit()


def classify(line: str) -> tuple[str, str]:
    """
    Work out what one line is.

    Returns (kind, value) where kind is 'utr', 'amount', 'beneficiary' or
    'noise'. Labels are consumed; the value is what remains.
    """
    raw = line.strip()
    if not raw:
        return "noise", ""

    # Labels first — they are the client telling us directly.
    if (m := _LBL_BEN.match(raw)) and raw[m.end():].strip():
        return "beneficiary", raw[m.end():].strip()
    if (m := _LBL_AMT.match(raw)) and raw[m.end():].strip():
        return "amount", raw[m.end():].strip()
    if (m := _LBL_UTR.match(raw)) and raw[m.end():].strip():
        return "utr", raw[m.end():].strip()

    if _is_noise(raw):
        return "noise", raw

    squashed = raw.translate(_STRIP)

    if _digits_only(raw):
        # The one genuinely ambiguous case. Length decides: an Indian UTR is
        # 12+ characters, a trade tranche is not 10^12 rupees.
        if len(squashed) >= AMBIGUOUS_DIGIT_LEN:
            return "utr", raw
        return "amount", raw

    # Decimal amounts: "249000.00", "2,49,000.50"
    if re.fullmatch(r"\d[\d.]*", squashed) and squashed.count(".") <= 1:
        return "amount", raw

    # Mixed letters and digits — a reference.
    #
    # `squashed` has already had its spaces removed, so testing it alone reads
    # any sentence containing a number as one long alphanumeric run:
    # "call 9876543210 when done" becomes "call9876543210whendone", which is
    # alphanumeric, contains a digit and is over eight characters. That is why
    # the bot answered nearly every message in the client's live group on
    # 10 September 2026.
    #
    # Requiring a single unbroken token fixes that but breaks a real format —
    # some banking apps paste a UTR with spaces in it, "BKIDR1 2026 0908
    # 00000380", and that must still be read.
    #
    # What separates the two is not the spaces but what sits between them.
    # Every chunk of a spaced reference carries digits; a sentence is mostly
    # words. So: one token, or every token contains a digit.
    tokens = raw.split()
    every_token_has_a_digit = all(
        any(c.isdigit() for c in t) for t in tokens
    )
    if (
        squashed.isalnum()
        and any(c.isdigit() for c in squashed)
        and len(squashed) >= 8
        and (len(tokens) == 1 or every_token_has_a_digit)
    ):
        return "utr", raw

    # Anything else that reads as words is a name.
    if re.search(r"[A-Za-z]{2,}", raw):
        return "beneficiary", raw

    return "noise", raw


def _carry_beneficiary(records: list[dict[str, str]]) -> None:
    """
    A beneficiary named once in a block applies to the whole block.

    THE BUG THIS FIXES (live, 11 September 2026)

    A new payment starts whenever a field repeats, so the client's habitual
    shorthand —

        to Ekta traders
        BKIDR...5860
        213460
        BKIDR...7436
        233000

    — gives the account to the FIRST payment and nothing to any of the others.
    Each of those then reaches match_account as None, the bot cannot attribute
    them, and it stops to ask which account they went to.

    Nobody tapped. The pending conversation lived in memory and a restart threw
    it away: four payments totalling ₹902,460 were lost exactly this way, and
    the first anyone knew was the client asking why a completed trade had not
    closed. The Bridge, on being shown the prompt: "why did they see the other
    message about choosing account to use?"

    WHY INFERRING IS SAFER THAN ASKING

    Guessing an account is normally forbidden here, because a wrong one puts
    money against the wrong trade. This is not a guess. The name is one the
    client wrote, in this message, above these payments — it is the only
    reading of what they sent. Set against that, asking has a known and
    expensive failure mode: the question goes unanswered and the money
    disappears from the ledger entirely.

    HOW FAR IT CARRIES

    Forward, then backwards into any leading gap. A block that names two
    different accounts still splits at the second name, so each payment takes
    the account written above it — filling gaps never overrides a name that is
    actually there.
    """
    last: Optional[str] = None
    for rec in records:
        if rec.get("beneficiary"):
            last = rec["beneficiary"]
        elif last is not None:
            rec["beneficiary"] = last

    # Payments listed before the name was written. Only reachable when no
    # earlier name exists, so this cannot overwrite anything.
    first = next((r["beneficiary"] for r in records if r.get("beneficiary")), None)
    if first is not None:
        for rec in records:
            rec.setdefault("beneficiary", first)


def parse_payments(text: str) -> ParseResult:
    """
    Read one or more payments out of a pasted block.

    Field order does not matter. A new payment starts whenever a field arrives
    that the current one already has, which is what lets several payments be
    pasted in a single message without separators.
    """
    result = ParseResult()
    if not text or not text.strip():
        result.problems.append("Nothing to read.")
        return result

    current: dict[str, str] = {}
    records: list[dict[str, str]] = []

    def flush() -> None:
        if current.get("utr") or current.get("amount"):
            records.append(dict(current))
        current.clear()

    for line in text.splitlines():
        kind, value = classify(line)
        if kind == "noise":
            continue
        if kind in current:
            # Repeated field means the previous payment is finished.
            flush()
        current[kind] = value
    flush()

    _carry_beneficiary(records)

    if not records:
        result.problems.append(
            "I could not find a UTR and an amount in that message."
        )
        return result

    for i, rec in enumerate(records, 1):
        where = f"Entry {i}: " if len(records) > 1 else ""

        if "utr" not in rec:
            result.problems.append(f"{where}no UTR found.")
            continue

        result.saw_utr = True
        if "amount" not in rec:
            result.problems.append(f"{where}no amount found.")
            continue

        try:
            utr = normalise_utr(rec["utr"])
        except MoneyError as exc:
            result.problems.append(f"{where}{exc}")
            continue

        try:
            amount = round_inr(to_decimal(rec["amount"]))
        except MoneyError:
            result.problems.append(f"{where}could not read {rec['amount']!r} as an amount.")
            continue

        if amount <= 0:
            result.problems.append(f"{where}amount must be greater than zero.")
            continue

        result.payments.append(
            ParsedPayment(utr=utr, amount_inr=amount,
                          beneficiary=rec.get("beneficiary"))
        )

    return result


def match_account(name: Optional[str], accounts) -> Optional[int]:
    """
    Match a pasted beneficiary name to a registered account.

    Deliberately forgiving about spacing and case — "Ekta traders", "EKTA
    TRADERS" and "Ekta  Traders" are the same account to a human and should be
    to the bot. Deliberately unforgiving about anything else: a wrong match
    would attribute money to the wrong account, so an uncertain name returns
    None and the caller asks.

    `accounts` is any iterable of rows with 'id' and 'account_name'.
    """
    if not name:
        return None

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    target = norm(name)
    if not target:
        return None

    exact = [a for a in accounts if norm(a["account_name"]) == target]
    if len(exact) == 1:
        return exact[0]["id"]
    if len(exact) > 1:
        return None  # genuinely ambiguous; make the client choose

    # One-sided containment, e.g. "Ekta" against "Ekta Traders Pvt Ltd".
    #
    # A fragment must be at least three characters to count. "STC" is a name
    # somebody meant; "Su" is two letters that happen to appear inside one, and
    # matching on it would be luck rather than intent. The other direction —
    # a full account name appearing inside a longer pasted string — needs no
    # floor, because the account name itself supplies the length.
    partial = [
        a for a in accounts
        if (len(target) >= 3 and target in norm(a["account_name"]))
        or norm(a["account_name"]) in target
    ]
    if len(partial) == 1:
        return partial[0]["id"]
    if len(partial) > 1:
        return None

    # Leading words, e.g. "Super Trading Company Pvt" against
    # "SUPER TRADING COMPANY (STC)". Neither contains the other, so
    # containment misses it, but they plainly mean the same account.
    #
    # Client request, 11 September 2026: "does not need to be full word match,
    # only first of the word Super etc". People type the start of a name and
    # stop.
    #
    # Still strict in the way that matters: a candidate only counts if it is
    # the ONLY one that fits. Two accounts sharing an opening word send the
    # client back to the buttons rather than guessing between them, because a
    # wrong match puts money against the wrong account and now the wrong trade.
    def first_word(s: str) -> str:
        for token in re.split(r"[^A-Za-z0-9]+", s.lower()):
            if token:
                return token
        return ""

    head = first_word(name)
    if len(head) >= 3:
        by_head = [a for a in accounts if first_word(a["account_name"]) == head]
        if len(by_head) == 1:
            return by_head[0]["id"]

    # Last resort: a shared opening run of characters, long enough not to be a
    # coincidence. "supertrading…" against "supertradingcompanystc".
    if len(target) >= 5:
        by_prefix = [
            a for a in accounts
            if norm(a["account_name"])[:5] == target[:5]
        ]
        if len(by_prefix) == 1:
            return by_prefix[0]["id"]

    return None
