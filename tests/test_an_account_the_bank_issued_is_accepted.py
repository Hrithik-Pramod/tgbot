"""
If a bank issued it, the bot takes it.

WHAT IT COST — 26 NOVEMBER 2026, 22:03

    ok, the raj 2.0 is not working
    its That does not look like an account number. Indian account numbers
    are 9 to 18 digits.
    on UNcle
    — Bridge

He was registering a collection account on Uncle and the bot refused it. The
rule was ^\\d{9,18}$ — digits only — and it refused two real things:

  - virtual collection accounts, which payment aggregators issue with a
    letter prefix identifying the merchant. That is what a desk collecting at
    volume is given, so the "unusual" case was the normal one.
  - the shorter numbers some banks still carry, under nine digits.

WHY THE OLD RULE WAS THE WRONG SHAPE, NOT MERELY THE WRONG NUMBERS

A validator here exists to catch a fat finger: a phone number, half an
IFSC, a pasted sentence. It is not the authority on what a bank may issue,
and it cannot be — the desk has the paperwork and the bot does not. When the
two disagreed about a real account the bot won, and there was no way to
overrule it. A vendor could not collect, at 22:00, because of a regex.

So the rule is now wide enough for anything a bank issues and still narrow
enough to catch rubbish, and nothing is accepted silently: the number is
echoed back on saving and /account_remove takes it off.

THE HOLE WIDENING IT OPENED, AND CLOSED

An IFSC is eleven alphanumerics containing digits, so it satisfies the new
pattern — and the very next question asks for the IFSC, which is precisely
when somebody pastes it one field early. An IFSC stored as an account number
is a beneficiary that matches no payment, discovered only when a real
payment to it cannot be placed. Checked explicitly.

CASE

Upper-cased on the way in. others_sharing_account compares the stored text,
and that comparison is the only thing standing between the desk and two
vendors collecting into one account without anybody being told — see
test_two_vendors_one_account. "yesb12345" and "YESB12345" must not read as
two different beneficiaries.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import supplier_bot  # noqa: E402
from bot.supplier_bot import ACCOUNT_RE, IFSC_RE  # noqa: E402


class _State:
    def __init__(self): self.data, self.state = {}, None
    async def update_data(self, **kw): self.data.update(kw); return self.data
    async def set_state(self, s): self.state = s
    async def get_data(self): return self.data


class _Msg:
    def __init__(self, text): self.text, self.sent = text, []
    async def answer(self, t, **kw): self.sent.append(t)


async def _submit(text):
    """Returns (stored_number_or_None, reply)."""
    msg, st = _Msg(text), _State()
    await supplier_bot.account_number(msg, st)
    return st.data.get("account_number"), msg.sent[0] if msg.sent else ""


@pytest.mark.asyncio
class TestWhatBanksActuallyIssue:
    async def test_a_virtual_collection_account_is_accepted(self):
        """
        The case that was refused. A letter prefix and digits.
        """
        stored, reply = await _submit("YESB1234567890")
        assert stored == "YESB1234567890", reply

    async def test_a_short_account_number_is_accepted(self):
        """
        Eight digits. The old floor was nine.
        """
        stored, _ = await _submit("12345678")
        assert stored == "12345678"

    async def test_a_long_one_is_accepted(self):
        """
        Eighteen digits, the old ceiling. Written as a repeat rather than a
        plausible number: test_no_live_data_committed scans this repository
        for digit runs that could be a real account, and it caught the first
        version of this line. The guard is right — a fixture that looks real
        is indistinguishable from a leak to anyone reading the public repo.
        """
        stored, _ = await _submit("5" + "0" * 17)
        assert stored == "5" + "0" * 17

    async def test_spaces_and_dashes_are_still_stripped(self):
        stored, _ = await _submit("5020 0000-0000 00")
        assert stored == "50200000000000"

    async def test_lowercase_is_stored_upper(self):
        """
        Not cosmetic. others_sharing_account compares stored text, and that
        comparison is what tells the desk two vendors share a beneficiary.
        """
        stored, _ = await _submit("yesb1234567890")
        assert stored == "YESB1234567890"


@pytest.mark.asyncio
class TestRubbishIsStillRefused:
    async def test_an_ifsc_is_not_an_account_number(self):
        """
        The hole widening the rule opened. Eleven alphanumerics with digits
        passes the pattern, and the next question asks for exactly this.
        """
        stored, reply = await _submit("EXBK0001234")
        assert stored is None
        assert "IFSC" in reply

    async def test_a_sentence_is_refused(self):
        stored, _ = await _submit("I will send it tomorrow")
        assert stored is None

    async def test_letters_alone_are_refused(self):
        stored, _ = await _submit("ACCOUNTNUMBER")
        assert stored is None

    async def test_too_short_is_refused(self):
        stored, _ = await _submit("1234567")
        assert stored is None

    async def test_too_long_is_refused(self):
        stored, _ = await _submit("1" * 21)
        assert stored is None

    async def test_empty_is_refused(self):
        stored, _ = await _submit("")
        assert stored is None

    async def test_punctuation_is_refused(self):
        """
        Only spaces and dashes are stripped. Anything else is a sign the
        wrong thing was pasted.
        """
        stored, _ = await _submit("5010/0123/4567")
        assert stored is None


class TestTheRuleItself:
    def test_a_digit_is_required(self):
        assert not ACCOUNT_RE.match("ABCDEFGHIJ")
        assert ACCOUNT_RE.match("ABCDEFGHI1")

    def test_the_bounds(self):
        assert not ACCOUNT_RE.match("1234567")
        assert ACCOUNT_RE.match("12345678")
        assert ACCOUNT_RE.match("1" * 20)
        assert not ACCOUNT_RE.match("1" * 21)

    def test_the_old_rule_is_gone(self):
        """
        Guards the fix itself. If this comes back, so does the 22:00 call.

        Asserted on the compiled pattern, NOT on the file text. The first
        version searched supplier_bot.py for the old regex and failed —
        because the comment explaining why it was removed quotes it. A
        correct change marked as a regression.

        Fifth time prose has satisfied a source scan in this project, and
        the first time it did so in reverse. See tests/sqltext.py.
        """
        assert ACCOUNT_RE.pattern != r"^\d{9,18}$"
        assert ACCOUNT_RE.match("YESB1234567890")

    @pytest.mark.asyncio
    async def test_the_message_no_longer_claims_digits_only(self):
        """
        Asserted on what is SENT, for the same reason — the explanation
        above the rule quotes the old wording verbatim.
        """
        _, reply = await _submit("I will send it tomorrow")
        assert "9 to 18 digits" not in reply
        assert "letters or digits" in reply

    def test_ifsc_is_unchanged(self):
        """
        The IFSC rule was never the problem and is not relaxed.
        """
        assert IFSC_RE.match("EXBK0001234")
        assert not IFSC_RE.match("EXBK1001234")
        assert not IFSC_RE.match("EXB0001234")
