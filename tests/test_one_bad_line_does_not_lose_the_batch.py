"""
One slip the bot cannot place must not discard the ones it can.

WHAT HAPPENED — 7 October 2026

A client pasted a batch. One line named an account the bot could not resolve,
so it asked which account that one went to — and threw away every other
payment in the message with it.

    This stopped it then all the rest were not read
    It's missing lots and lots now
    i need to find out why this keeps happening mate
    as its needing to have final fix now
    — Bridge

He believed adding a bank account had broken something, because that is when
it became obvious. It had not. The fault was in the handler from the first
day and only shows on a MIXED message, so it grew steadily more likely as
accounts multiplied and more names became ambiguous — and a shared collection
account registered under three vendors makes almost every name ambiguous.

THE SHAPE OF IT

Three branches each ended the whole message:

    if homeless:   reply("This has NOT been recorded"); return
    if unmatched and not accounts:  reply(...); return
    if unmatched:  ask which account; return

Every payment in the message was in `staged`, and `staged` was dropped
wholesale. A batch of eight with one awkward line lost all eight — and the
client had already been told, by their own bank, that all eight had gone.

THE RULE NOW

  can be placed        recorded immediately, before anything is asked
  account has no       not recorded, Bridge told, and said in the reply —
  open trade           but only about itself
  name matched         held in the database, client asked, Bridge told —
  nothing              and only that one is carried into the question

The clean message — everything placed, nothing to ask — is untouched,
including both confirmation settings.
"""

import inspect
import re
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import client_bot  # noqa: E402


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------

def _acct(_id, name, number, trade_id):
    return {"id": _id, "account_name": name, "account_number": number,
            "ifsc": "XXXX0000000", "trade_id": trade_id,
            "supplier_label": "A VENDOR"}


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

    async def reply(self, text, **kw):
        self.replies.append({"text": text, **kw})

    async def answer(self, text, **kw):
        self.answers.append({"text": text, **kw})

    async def react(self, reaction):
        self.reactions.append(reaction)


class _State:
    def __init__(self):
        self.state = None
        self.data = {}
        self.cleared = False

    async def get_state(self): return self.state
    async def set_state(self, s): self.state = s
    async def update_data(self, **kw): self.data.update(kw)
    async def get_data(self): return dict(self.data)
    async def clear(self): self.cleared = True; self.state = None


class _Notifier:
    def __init__(self):
        self.bridge = []
        self.no_open_trade = []

    async def to_bridge(self, text, **kw): self.bridge.append(text)
    async def alert_no_open_trade(self, rows): self.no_open_trade.append(rows)
    async def check_completion(self, tid): return False
    async def check_near_completion(self, tid): return None
    async def ask_which_order(self, **kw): pass


class _Repo:
    def __init__(self, matchable, accounts):
        self._matchable = matchable
        self._accounts = accounts
        self.added = []
        self.held = []
        self.audits = []

    async def matchable_accounts_for_client(self, cid): return list(self._matchable)
    async def open_trade_accounts_for_client(self, cid): return list(self._accounts)
    async def live_trades_on_account(self, **kw): return []

    async def add_payment(self, *, trade_id, utr, amount_inr, **kw):
        self.added.append({"trade_id": trade_id, "utr": utr,
                           "amount_inr": amount_inr})
        return True, f"Recorded {utr}."

    async def hold_payment(self, *, utr, amount_inr, **kw):
        self.held.append({"utr": utr, "amount_inr": amount_inr})
        return True, len(self.held)

    async def audit_standalone(self, **kw): self.audits.append(kw)
    async def release_held_payments(self, utrs): return 0
    async def trade_paid_total(self, tid): return D("1000")


async def run(body, repo, notifier=None, state=None):
    state = state or _State()
    await client_bot.on_pasted_payment(
        _Message(body), state, {"id": 9}, repo, notifier or _Notifier(),
    )
    return state


GOOD = "BKIDR10000000000001\n200000\nto EKTA TRADERS"
BAD  = "BKIDR10000000000002\n300000\nto SOMETHING UNKNOWN"


# ----------------------------------------------------------------------
# The fault
# ----------------------------------------------------------------------

class TestAMixedMessageKeepsWhatItCanRead:
    @pytest.mark.asyncio
    async def test_the_good_payment_is_recorded(self):
        """The whole complaint, in one assertion."""
        repo = _Repo(
            matchable=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
            accounts=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
        )
        msg = _Message(GOOD + "\n\n" + BAD)
        await client_bot.on_pasted_payment(
            msg, _State(), {"id": 9}, repo, _Notifier())
        assert [a["utr"] for a in repo.added] == ["BKIDR10000000000001"], (
            "a readable payment was thrown away because another line in the "
            "same message could not be placed"
        )

    @pytest.mark.asyncio
    async def test_the_bad_one_is_still_held(self):
        repo = _Repo(
            matchable=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
            accounts=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
        )
        await run(GOOD + "\n\n" + BAD, repo)
        assert [h["utr"] for h in repo.held] == ["BKIDR10000000000002"]

    @pytest.mark.asyncio
    async def test_only_the_unanswered_one_is_carried_into_the_question(self):
        """
        The account the client taps is applied to everything still waiting.
        Leaving a recorded payment in that list would offer it up again and
        attribute it to whichever account they happened to pick.
        """
        repo = _Repo(
            matchable=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
            accounts=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
        )
        state = await run(GOOD + "\n\n" + BAD, repo)
        assert state.state == client_bot.PastedPayment.account
        assert [s["utr"] for s in state.data["staged"]] == ["BKIDR10000000000002"]

    @pytest.mark.asyncio
    async def test_the_bridge_is_told_only_about_what_is_waiting(self):
        """
        Reporting the whole message told him payments were unrecorded that
        were already safely on the ledger.
        """
        repo = _Repo(
            matchable=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
            accounts=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
        )
        notifier = _Notifier()
        await run(GOOD + "\n\n" + BAD, repo, notifier)
        said = "\n".join(notifier.bridge)
        assert "BKIDR10000000000002" in said
        assert "BKIDR10000000000001" not in said

    @pytest.mark.asyncio
    async def test_the_client_is_told_how_many_went_in(self):
        repo = _Repo(
            matchable=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
            accounts=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
        )
        msg = _Message(GOOD + "\n\n" + BAD)
        await client_bot.on_pasted_payment(
            msg, _State(), {"id": 9}, repo, _Notifier())
        shown = "\n".join(m["text"] for m in msg.answers + msg.replies)
        assert "Recorded: 1 of 2" in shown


class TestAnAccountWithNothingOpen:
    @pytest.mark.asyncio
    async def test_the_rest_of_the_batch_still_goes_in(self):
        """
        The account is real and registered, but its trade has closed. That
        used to discard the whole message too.
        """
        repo = _Repo(
            matchable=[_acct(1, "EKTA TRADERS", "50200000000000", 77),
                       _acct(2, "CLOSED TRADERS", "50200000000000", None)],
            accounts=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
        )
        closed = "BKIDR10000000000003\n400000\nto CLOSED TRADERS"
        await run(GOOD + "\n\n" + closed, repo)
        assert [a["utr"] for a in repo.added] == ["BKIDR10000000000001"]

    @pytest.mark.asyncio
    async def test_the_bridge_is_still_told_about_it(self):
        repo = _Repo(
            matchable=[_acct(1, "EKTA TRADERS", "50200000000000", 77),
                       _acct(2, "CLOSED TRADERS", "50200000000000", None)],
            accounts=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
        )
        notifier = _Notifier()
        await run(GOOD + "\n\nBKIDR10000000000003\n400000\nto CLOSED TRADERS",
                  repo, notifier)
        assert notifier.no_open_trade, "the Bridge was not told"

    @pytest.mark.asyncio
    async def test_the_client_is_told_it_was_not_recorded(self):
        repo = _Repo(
            matchable=[_acct(1, "EKTA TRADERS", "50200000000000", 77),
                       _acct(2, "CLOSED TRADERS", "50200000000000", None)],
            accounts=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
        )
        msg = _Message(GOOD + "\n\nBKIDR10000000000003\n400000\nto CLOSED TRADERS")
        await client_bot.on_pasted_payment(
            msg, _State(), {"id": 9}, repo, _Notifier())
        shown = "\n".join(m["text"] for m in msg.answers + msg.replies)
        assert "NOT recorded" in shown
        assert "no open trade" in shown


class TestNothingToOfferAtAll:
    @pytest.mark.asyncio
    async def test_it_is_held_rather_than_merely_announced(self):
        """
        Before 7 October this branch wrote nothing down: the client was told
        it had not been recorded, the Bridge was told once, and the only
        trace afterwards was two chat messages.
        """
        repo = _Repo(
            matchable=[_acct(1, "EKTA TRADERS", "50200000000000", None)],
            accounts=[],
        )
        await run(BAD, repo)
        assert [h["utr"] for h in repo.held] == ["BKIDR10000000000002"]

    @pytest.mark.asyncio
    async def test_nothing_is_recorded(self):
        repo = _Repo(
            matchable=[_acct(1, "EKTA TRADERS", "50200000000000", None)],
            accounts=[],
        )
        await run(BAD, repo)
        assert repo.added == []


class TestTheOrdinaryMessageIsUntouched:
    @pytest.mark.asyncio
    async def test_a_clean_paste_still_records_and_acknowledges(self):
        repo = _Repo(
            matchable=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
            accounts=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
        )
        msg = _Message(GOOD)
        await client_bot.on_pasted_payment(
            msg, _State(), {"id": 9}, repo, _Notifier())
        assert [a["utr"] for a in repo.added] == ["BKIDR10000000000001"]
        assert msg.reactions, "the thumbs up is the whole acknowledgement"

    @pytest.mark.asyncio
    async def test_several_clean_payments_all_go_in(self):
        repo = _Repo(
            matchable=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
            accounts=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
        )
        body = "\n\n".join(
            f"BKIDR1000000000000{i}\n100000\nto EKTA TRADERS" for i in (1, 2, 3))
        await run(body, repo)
        assert len(repo.added) == 3

    @pytest.mark.asyncio
    async def test_a_clean_paste_asks_nothing(self):
        repo = _Repo(
            matchable=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
            accounts=[_acct(1, "EKTA TRADERS", "50200000000000", 77)],
        )
        state = await run(GOOD, repo)
        assert state.state is None


class TestNoBranchEndsTheWholeMessageAnyMore:
    def test_the_three_branches_report_only_themselves(self):
        """
        The regression guard. The fault was not one branch — it was three
        that each spoke for the whole message.
        """
        src = inspect.getsource(client_bot.on_pasted_payment)
        marker = "EVERY PAYMENT STANDS ON ITS OWN"
        assert marker in src, "the per-payment split has been removed"
        after = src[src.index(marker):]
        for name in ("ready", "homeless", "unmatched"):
            assert re.search(rf"\b{name}\s*=\s*\[s for s in staged", after), name

    def test_the_readable_ones_are_recorded_before_anything_is_asked(self):
        src = inspect.getsource(client_bot.on_pasted_payment)
        after = src[src.index("if ready:"):]
        record = after.index("_record(")
        ask = after.index("PastedPayment.account")
        assert record < ask, (
            "the question is asked before the readable payments are banked"
        )
