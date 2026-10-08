"""
A payment already on the ledger is answered, not interrogated.

WHAT HAPPENED — 8 October 2026

PUNBR…8869, ₹251,000 to SUPER TRADING COMPANY (STC), was recorded correctly on
BRAV19 at 14:55. At 19:25 and again at 19:32 the client pasted it again, and
the bot replied:

    PUNBR10000000000000
    251000
    to ?  (account not recognised)

    Which account did these go to?
    [ KISAN TRADERS ••2794 — ₹1,144,304 left ]
    [ KISAN TRADERS ••2794 — ₹4,999,896 left ]
    … six more buttons …

    Again peter / From nowhere — Bridge

Nothing was wrong with the money. The bot was asking the client to resolve its
own bookkeeping, about a payment it had banked four hours earlier.

WHY

Super Trading is one account under three vendors, so the name is ambiguous and
the payment goes down the shared-account branch. That branch found the order
already collecting, tried to record it, and the ledger refused the duplicate —
correctly. The branch then returned None, which dropped the payment back into
the unmatched pile, and the unmatched pile asks the client which account.

A duplicate is a complete answer. It was being treated as a failure to find
one.

WHAT THE DIAGNOSTIC DID

This took one query to find, because the branch now records why it declined.
The row said it in its own words:

    "reason": "the established order refused it, almost certainly a
               duplicate; the ordinary path will report it"

The comment was right about the cause and wrong that the ordinary path would
report it properly — it asked a question instead. The same fault on 7 October
cost an evening of reconstructing chat screenshots.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import client_bot  # noqa: E402


SHARED = [
    {"id": 2, "account_name": "SUPER TRADING COMPANY (STC)",
     "account_number": "50200000000000", "ifsc": "XXXX0000000",
     "trade_id": 101},
    {"id": 33, "account_name": "SUPER TRADING COMPANY (STC)",
     "account_number": "50200000000000", "ifsc": "XXXX0000000",
     "trade_id": 107},
]


class _Msg:
    class _Chat:
        id = 1
    chat = _Chat()
    message_id = 2

    def __init__(self, text=""):
        self.text = text
        self.caption = None
        self.replies = []
        self.answers = []
        self.reactions = []

    async def reply(self, t, **kw): self.replies.append(t)
    async def answer(self, t, **kw): self.answers.append(t)
    async def react(self, r): self.reactions.append(r)


class _State:
    def __init__(self):
        self.state = None
        self.data = {}
    async def get_state(self): return self.state
    async def set_state(self, s): self.state = s
    async def update_data(self, **kw): self.data.update(kw)
    async def get_data(self): return dict(self.data)
    async def clear(self): self.state = None


class _Notifier:
    def __init__(self):
        self.bridge = []
        self.asked = []
    async def to_bridge(self, t, **kw): self.bridge.append(t)
    async def ask_which_order(self, **kw): self.asked.append(kw)
    async def check_completion(self, tid): return False
    async def check_near_completion(self, tid): return None
    async def alert_no_open_trade(self, rows): pass


class _Repo:
    """A ledger that already holds the payment being pasted."""

    def __init__(self, *, already_recorded=True, collected=D("1000000")):
        self.already = already_recorded
        self.collected = collected
        self.audits = []
        self.held = []

    async def matchable_accounts_for_client(self, cid): return list(SHARED)
    async def open_trade_accounts_for_client(self, cid): return list(SHARED)

    async def live_trades_on_account(self, **kw):
        return [{"id": 107, "account_id": 33, "collected": self.collected,
                 "inr_expected": D("3012885"), "reference": "BRAV19",
                 "opened_at": None, "outstanding": D("2012885"),
                 "vendor": "A VENDOR"}]

    async def add_payment(self, **kw):
        if self.already:
            return False, "UTR has already been recorded. Not added again."
        return True, "Recorded."

    async def hold_payment(self, **kw):
        self.held.append(kw)
        return True, 1

    async def audit_standalone(self, **kw): self.audits.append(kw)
    async def release_held_payments(self, utrs): return 0
    async def trade_paid_total(self, tid): return D("3473100")


BODY = "PUNBR10000000000000\n251000\nto SUPER TRADING COMPANY (STC)"


class TestTheClientIsNotAskedAboutMoneyAlreadyBanked:
    @pytest.mark.asyncio
    async def test_no_question_is_asked(self):
        """The whole complaint."""
        repo, msg = _Repo(), _Msg(BODY)
        state = _State()
        await client_bot.on_pasted_payment(
            msg, state, {"id": 9}, repo, _Notifier())
        shown = "\n".join(msg.answers + msg.replies)
        assert "Which account" not in shown, (
            "the client is being asked to place a payment the bot recorded "
            "hours ago"
        )
        assert state.state is None

    @pytest.mark.asyncio
    async def test_it_says_the_payment_is_already_counted(self):
        repo, msg = _Repo(), _Msg(BODY)
        await client_bot.on_pasted_payment(
            msg, _State(), {"id": 9}, repo, _Notifier())
        shown = "\n".join(msg.answers + msg.replies)
        assert "already recorded" in shown
        assert "Already counted in the total below" in shown

    @pytest.mark.asyncio
    async def test_it_is_not_silently_thumbed_up(self):
        """
        A duplicate is never acknowledged silently — the client has to know
        that one did not go in a second time (E3). On 15 September a correct
        refusal phrased badly sent the Bridge hunting a payment that was
        safely in the ledger.
        """
        repo, msg = _Repo(), _Msg(BODY)
        await client_bot.on_pasted_payment(
            msg, _State(), {"id": 9}, repo, _Notifier())
        assert msg.reactions == []
        assert msg.replies, "the client was told nothing at all"

    @pytest.mark.asyncio
    async def test_nothing_is_held_and_the_bridge_is_not_disturbed(self):
        """
        There is no question outstanding, so nothing should be written to
        the waiting list and nobody should be chased about it.
        """
        repo, notifier = _Repo(), _Notifier()
        await client_bot.on_pasted_payment(
            _Msg(BODY), _State(), {"id": 9}, repo, notifier)
        assert repo.held == []
        assert notifier.asked == []
        assert notifier.bridge == []

    @pytest.mark.asyncio
    async def test_the_reason_is_still_written_down(self):
        repo = _Repo()
        await client_bot.on_pasted_payment(
            _Msg(BODY), _State(), {"id": 9}, repo, _Notifier())
        reasons = [a["detail"]["reason"] for a in repo.audits
                   if a["action"] == "payment.shared_account_declined"]
        assert reasons and "already recorded" in reasons[0]


class TestAGenuineFirstPaymentIsUnaffected:
    @pytest.mark.asyncio
    async def test_it_is_recorded_and_acknowledged(self):
        repo, msg = _Repo(already_recorded=False), _Msg(BODY)
        await client_bot.on_pasted_payment(
            msg, _State(), {"id": 9}, repo, _Notifier())
        assert msg.reactions, "a clean payment should get the thumbs up"
        shown = "\n".join(msg.answers + msg.replies)
        assert "already recorded" not in shown

    @pytest.mark.asyncio
    async def test_an_order_not_yet_collecting_still_asks_the_bridge(self):
        repo = _Repo(already_recorded=False, collected=D(0))
        notifier = _Notifier()
        await client_bot.on_pasted_payment(
            _Msg(BODY), _State(), {"id": 9}, repo, notifier)
        assert notifier.asked, "the Bridge should have been asked"
        assert repo.held, "and the money written down while he decides"


class TestTheConfirmedMessageDoesNotContradictItself:
    """
    After the client taps an account the reply read:

        to ?  (account not recognised)
        Recorded. Running total: ₹3,473,100

    Two statements about the same payment, the alarming one first. Noticed
    4 October, still there on the 8th. It makes a correctly recorded payment
    look like a silent mis-record, every single time.
    """

    def test_the_unrecognised_line_is_replaced(self):
        src = inspect.getsource(client_bot.pasted_pick_account)
        assert "account not recognised" in src, (
            "the stale line is no longer being dealt with"
        )
        assert "chosen_name" in src

    def test_it_names_the_account_the_client_picked(self):
        src = inspect.getsource(client_bot.pasted_pick_account)
        assert 'f"to {chosen_name}"' in src

    def test_the_question_is_still_stripped(self):
        src = inspect.getsource(client_bot.pasted_pick_account)
        assert 'split("Which account")[0]' in src

    def test_it_survives_not_finding_the_name(self):
        """
        The account list is re-read, and a row can disappear between the
        question and the answer. A missing name must leave the message
        readable rather than throw.
        """
        src = inspect.getsource(client_bot.pasted_pick_account)
        assert "if chosen_name:" in src
