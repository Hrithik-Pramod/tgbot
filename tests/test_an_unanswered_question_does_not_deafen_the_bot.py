"""
An unanswered question must never silence the next payment.

    a trade gets this message, probably edited after by the client, and the
    rest has the geting a jump choice ... this then stops the bot from
    working ... because it is waiting for an answer that is not coming from
    the client
    — Bridge, 24 October 2026, 19:46

    Hi Peter, for your information also I'm having to copy and paste all
    trades at the moment as they're not getting picked up by the bot
    — Bridge, 24 October 2026, 23:47

He diagnosed this from the outside, with no access to the code, and he was
exactly right.

WHAT THE CODE DID

on_pasted_payment opened with:

    if await state.get_state() is not None:
        return  # mid-conversation; the FSM handlers own this message

"Which account did these go to?" sets PastedPayment.account. That question
is answered by TAPPING A BUTTON. If the client does not tap — and on
24 October they did not, they edited the original message instead — the
state is never cleared, and from that moment every payment that person pasted
into that group hit this line and was dropped. No reply, no reaction, no log
line, nothing in the audit trail.

The client's whole protocol is "no acknowledgement means it was not picked
up". So the slips kept coming and the desk went back to copying by hand.

WHICH STATES MAY SWALLOW A MESSAGE

Only the two that asked for typed text: /add's "how much?" and "what is the
UTR?". A message arriving then IS the answer.

Everything else is answered with a button, and a pasted payment arriving
while one is open means the client has moved on. The question is abandoned,
not obeyed — and abandoning it costs nothing, because the money behind it is
already on held_payments and the chaser keeps asking about it.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import client_bot  # noqa: E402
from bot.client_bot import AddPayment, PastedPayment  # noqa: E402


def _acct(_id, name, number, trade_id):
    return {"id": _id, "account_name": name, "account_number": number,
            "ifsc": "XXXX0000000", "trade_id": trade_id,
            "supplier_label": "A VENDOR"}


ACCOUNTS = [_acct(1, "EKTA TRADERS", "50200000000000", 77)]
SLIP = "BKIDR10000000000001\n200000\nto EKTA TRADERS"
NEXT_SLIP = "BKIDR10000000000002\n250000\nto EKTA TRADERS"


class _Message:
    class _Chat:
        id = 1
    chat = _Chat()
    message_id = 2

    def __init__(self, text):
        self.text = text
        self.caption = None
        self.replies = []
        self.answers = []
        self.reactions = []

    async def reply(self, text, **kw): self.replies.append({"text": text, **kw})
    async def answer(self, text, **kw): self.answers.append({"text": text, **kw})
    async def react(self, reaction): self.reactions.append(reaction)


class _State:
    def __init__(self, state=None):
        self.state = state
        self.data = {}
        self.cleared = False

    async def get_state(self): return self.state
    async def set_state(self, s): self.state = s
    async def update_data(self, **kw): self.data.update(kw)
    async def get_data(self): return dict(self.data)

    async def clear(self):
        self.cleared = True
        self.state = None


class _Notifier:
    def __init__(self):
        self.bridge = []

    async def to_bridge(self, text, **kw): self.bridge.append(text)
    async def alert_no_open_trade(self, rows): pass
    async def check_completion(self, tid): return False
    async def check_near_completion(self, tid): return None
    async def ask_which_order(self, **kw): pass


class _Repo:
    def __init__(self):
        self.added = []
        self.released = []

    async def matchable_accounts_for_client(self, cid): return list(ACCOUNTS)
    async def open_trade_accounts_for_client(self, cid): return list(ACCOUNTS)
    async def live_trades_on_account(self, **kw): return []
    async def collection_path(self, **kw): return None
    async def recorded_amounts(self, utrs): return {}

    async def add_payment(self, *, trade_id, utr, amount_inr, **kw):
        self.added.append({"trade_id": trade_id, "utr": utr,
                           "amount_inr": amount_inr})
        return True, f"Recorded {utr}."

    async def hold_payment(self, **kw): return True, 1
    async def audit_standalone(self, **kw): pass
    async def release_held_payments(self, utrs):
        self.released.extend(utrs)
        return 0

    async def trade_paid_total(self, tid): return D("1000")


async def _paste(body, state, repo=None, notifier=None):
    repo = repo or _Repo()
    await client_bot.on_pasted_payment(
        _Message(body), state, {"id": 9}, repo, notifier or _Notifier()
    )
    return repo


class TestTheDeafness:
    @pytest.mark.asyncio
    async def test_a_payment_lands_while_a_button_question_is_open(self):
        """
        The whole complaint in one assertion. Before the fix this recorded
        nothing at all.
        """
        state = _State(PastedPayment.account.state)
        repo = await _paste(NEXT_SLIP, state)
        assert repo.added, (
            "a pasted payment was silently dropped because an unanswered "
            "question was still on screen — the bot is deaf again"
        )
        assert repo.added[0]["utr"] == "BKIDR10000000000002"

    @pytest.mark.asyncio
    async def test_the_stale_question_is_abandoned(self):
        state = _State(PastedPayment.account.state)
        await _paste(NEXT_SLIP, state)
        assert state.cleared

    @pytest.mark.asyncio
    async def test_the_confirm_step_does_not_deafen_it_either(self):
        state = _State(PastedPayment.confirm.state)
        repo = await _paste(NEXT_SLIP, state)
        assert repo.added

    @pytest.mark.asyncio
    async def test_the_account_button_step_of_add_does_not_either(self):
        """
        AddPayment.account is also answered by a button, so it is in the
        same class — only amount and utr asked for words.
        """
        state = _State(AddPayment.account.state)
        repo = await _paste(NEXT_SLIP, state)
        assert repo.added


class TestTypedQuestionsStillOwnTheirAnswer:
    """
    The other half. /add asks two questions whose answer is text, and
    reading "250000" as a fresh payment instead of as the answer would be a
    different bug with the same shape.
    """

    @pytest.mark.asyncio
    async def test_the_amount_question_keeps_its_answer(self):
        state = _State(AddPayment.amount.state)
        repo = await _paste(NEXT_SLIP, state)
        assert repo.added == []
        assert not state.cleared, "it abandoned a question it had just asked"

    @pytest.mark.asyncio
    async def test_the_utr_question_keeps_its_answer(self):
        state = _State(AddPayment.utr.state)
        repo = await _paste(NEXT_SLIP, state)
        assert repo.added == []
        assert not state.cleared

    @pytest.mark.asyncio
    async def test_an_idle_chat_is_unaffected(self, caplog):
        """
        No assertion on state.cleared here: the ordinary clean path clears
        state at the END of the handler anyway, so `cleared` is true either
        way and asserting on it proves nothing. What distinguishes the two
        is whether a question was ABANDONED, which is logged.
        """
        state = _State(None)
        with caplog.at_level("INFO"):
            repo = await _paste(SLIP, state)
        assert repo.added
        assert "button question was open" not in caplog.text

    @pytest.mark.asyncio
    async def test_abandoning_says_so_in_the_log(self, caplog):
        state = _State(PastedPayment.account.state)
        with caplog.at_level("INFO"):
            await _paste(NEXT_SLIP, state)
        assert "button question was open" in caplog.text


class TestAnEditUnsticksIt:
    """
    24 October: the client answered by EDITING the original message. The
    edit handler recorded the payment and left the question set, so the bot
    went deaf at the exact moment it looked like it was working.
    """

    @pytest.mark.asyncio
    async def test_the_edit_clears_the_question(self):
        state = _State(PastedPayment.account.state)
        repo = _Repo()
        await client_bot.on_edited_payment(
            _Message(SLIP), state, {"id": 9}, repo, _Notifier()
        )
        assert repo.added, "the edit did not record the payment"
        assert state.cleared, (
            "the question is still open, so the next slip will be dropped"
        )

    @pytest.mark.asyncio
    async def test_an_edit_with_nothing_open_is_unaffected(self):
        state = _State(None)
        repo = _Repo()
        await client_bot.on_edited_payment(
            _Message(SLIP), state, {"id": 9}, repo, _Notifier()
        )
        assert repo.added
        assert not state.cleared

    def test_the_handler_takes_the_state(self):
        """
        It did not, which is why it could not clear anything.
        """
        assert "state" in inspect.signature(
            client_bot.on_edited_payment).parameters


class TestTheGuardItself:
    def test_it_no_longer_bails_on_any_state_at_all(self):
        src = inspect.getsource(client_bot.on_pasted_payment)
        head = src[:src.index("body = message.text")]
        assert "if await state.get_state() is not None:\n        return" not in head, (
            "back to bailing on every state — this is the deafness"
        )

    def test_only_the_two_typed_questions_are_listed(self):
        """
        The list is the whole safety argument. Anything added to it can
        silence the bot again, so it is pinned.
        """
        assert client_bot._STATES_THAT_OWN_TYPED_TEXT == (
            AddPayment.amount, AddPayment.utr
        )

    @pytest.mark.asyncio
    async def test_the_check_reads_the_list(self):
        """
        Guards against the check being hard-coded to something else that
        happens to agree today.
        """
        for s in (AddPayment.amount, AddPayment.utr):
            assert await client_bot._is_answering_a_typed_question(
                _State(s.state))
        for s in (PastedPayment.account, PastedPayment.confirm,
                  AddPayment.account):
            assert not await client_bot._is_answering_a_typed_question(
                _State(s.state))
        assert not await client_bot._is_answering_a_typed_question(_State(None))

    def test_abandoning_a_question_is_logged(self):
        """
        Dropping a question silently is how this took two days to find.
        """
        src = inspect.getsource(client_bot.on_pasted_payment)
        head = src[:src.index("body = message.text")]
        assert "log.info" in head
