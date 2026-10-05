"""
A recorded payment is acknowledged the same way however it was recorded.

    Instead of thumbs up it's not sending the every time
    — Bridge, 5 October 2026

WHAT HE WAS SEEING

A payment that reads cleanly gets a 👍 on the client's own message and nothing
else. That is the whole acknowledgement, and it is deliberate: silence means
not picked up, a thumbs up means on the books.

The shared-account branch added on 1 October broke that without anyone
noticing. It returned a line of text whether the money had been RECORDED
against the established order or was WAITING on the Bridge, and the caller
could not tell the two apart — so it printed a block of text for both. Super
Trading is one account registered under three vendors, so for that client
every single payment came back as a paragraph that looked like it wanted
something.

Nothing was wrong with the money. The acknowledgement was.

WHY THIS IS NOT COSMETIC

The client reads the reply to decide whether to do anything. A thumbs up ends
it. A block of text does not, and on 15 September a correct refusal phrased
badly had the Bridge hunting a payment that was safely in the ledger —
"has happened twice, but this is not in system... its like its double
checking". Wording that misreports state costs real time, and it costs it
during trading.

THE RULE

  recorded   →  thumbs up, like any other payment
  waiting    →  words, because something genuinely needs an answer
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import client_bot  # noqa: E402


ACCOUNTS = [
    {"id": 2, "account_name": "A SHARED HOLDER",
     "account_number": "50200000000000", "ifsc": "XXXX0000000"},
    {"id": 33, "account_name": "A SHARED HOLDER",
     "account_number": "50200000000000", "ifsc": "XXXX0000000"},
]


class _P:
    def __init__(self, utr="UTR000000000001"):
        self.utr = utr
        self.amount_inr = D("289000")
        self.beneficiary = "A SHARED HOLDER"


class _Message:
    class _Chat:
        id = 1
    chat = _Chat()
    message_id = 2

    def __init__(self):
        self.reactions = []
        self.replies = []

    async def react(self, reaction):
        self.reactions.append(reaction)

    async def reply(self, text, **kw):
        self.replies.append(text)


class _Repo:
    def __init__(self, trades):
        self.trades = trades

    async def live_trades_on_account(self, **kw):
        return list(self.trades)

    async def add_payment(self, **kw):
        return True, "recorded"

    async def hold_payment(self, **kw):
        return True, 1


class _Notifier:
    def __init__(self):
        self.asked = []

    async def check_completion(self, trade_id):
        return False

    async def check_near_completion(self, trade_id):
        return None

    async def ask_which_order(self, **kw):
        self.asked.append(kw)


def _trade(tid, collected):
    return {"id": tid, "account_id": 33, "collected": collected,
            "inr_expected": D("3012885")}


class TestTheOutcomeIsReportedNotJustALine:
    @pytest.mark.asyncio
    async def test_a_recorded_payment_says_so(self):
        """
        Established path: one order is already collecting, so the money goes
        there without asking. The caller has to be able to tell that this
        happened.
        """
        repo = _Repo([_trade(14, D("1000000")), _trade(44, D(0))])
        out = await client_bot._place_on_shared_account(
            _Message(), _P(), ACCOUNTS, {"id": 9}, repo, _Notifier(),
        )
        assert out is not None
        outcome, line = out
        assert outcome == "recorded"
        assert line

    @pytest.mark.asyncio
    async def test_a_waiting_payment_says_so(self):
        """No established path, so it is held and the Bridge is asked."""
        repo = _Repo([_trade(14, D(0)), _trade(44, D(0))])
        notifier = _Notifier()
        out = await client_bot._place_on_shared_account(
            _Message(), _P(), ACCOUNTS, {"id": 9}, repo, notifier,
        )
        assert out is not None
        outcome, line = out
        assert outcome == "held"
        assert "held" in line
        assert notifier.asked, "the Bridge should have been asked"

    @pytest.mark.asyncio
    async def test_not_this_case_is_still_nothing(self):
        """One live order only — the ordinary matcher covers that."""
        repo = _Repo([_trade(14, D("1000000"))])
        out = await client_bot._place_on_shared_account(
            _Message(), _P(), ACCOUNTS, {"id": 9}, repo, _Notifier(),
        )
        assert out is None


class TestTheClientIsToldTheRightThing:
    """
    Read off the handler's source rather than driving the whole paste path,
    which needs a parser, a matcher and six repo methods to reach four lines.
    What matters is which branch each outcome takes.
    """

    def test_recorded_money_is_acknowledged_not_narrated(self):
        src = inspect.getsource(client_bot.on_pasted_payment)
        assert "_acknowledge(message)" in src
        branch = src[src.index("if (pending_any or recorded_any)"):]
        head = branch[:900]
        assert "if pending_any:" in head
        assert "_acknowledge(message)" in head, (
            "a payment recorded on the established path still gets a block "
            "of text instead of a thumbs up"
        )

    def test_waiting_money_is_still_explained_in_words(self):
        src = inspect.getsource(client_bot.on_pasted_payment)
        branch = src[src.index("if (pending_any or recorded_any)"):]
        head = branch[:900]
        assert 'message.reply("\\n".join(lines))' in head, (
            "a payment waiting on the Bridge must be said in words — the "
            "client is being told not to chase it"
        )

    def test_the_two_outcomes_are_tracked_apart(self):
        """
        One flag for both is what caused this: the caller could not tell
        recorded from waiting, so it reported the louder of the two for
        everything.
        """
        src = inspect.getsource(client_bot.on_pasted_payment)
        assert "pending_any" in src and "recorded_any" in src
        assert "held_any" not in src, (
            "the single flag is back, and with it the inability to tell "
            "money on the books from money waiting"
        )
