"""
Money that lands between two orders is placed when the next one opens.

WHAT HAPPENED — 26 October 2026

    18:04:34   BRAV42 completed   ₹5,423,953 expected, fully collected
    18:20:57   ₹200,000 arrives for Royal Trading, written "ROYALTRAD"

Sixteen minutes. The client pays in a continuous stream; orders are issued
in batches. Everything landing in the gap has no order to go on.

    It's not correct — Bridge, 26 October 2026

He was looking at this:

    Written as "ROYALTRAD", which matches no registered account.
    There is no open order to put it against. Check the account is
    registered and a trade is open.

Both sentences were false. The account IS registered, and the bot had
already recognised it — the audit for that same payment, one row earlier:

    payment.shared_account_declined   account_number_last4: "7492"
    payment.held                      account_number: null

It resolved the account and then dropped it on the way to the hold, because
the hold was written for the other kind of failure — a name matching nothing
at all.

WHY THE LOST FIELD MATTERED MORE THAN THE WORDING

A payment tagged to an account can be placed the moment an order opens on
that account. A payment tagged to nothing can only ever be placed by hand,
by someone who remembers it exists.

So the hold now keeps the account, and the chase re-reads that account every
time it goes out rather than repeating what was true when the money arrived.
Nothing open yet, it says so and promises to come back. An order open, it
carries a button. Since the chase repeats every six hours, the money places
itself without anyone hunting for it.

No new callback handler: hp: already re-resolves the live orders on a hold's
account when it is tapped, so it has always been able to place money onto an
order that opened after the question was asked. Nothing ever gave it one.
"""

import inspect
import sys
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import client_bot, notifier as notifier_mod  # noqa: E402
from bot.client_bot import _resolved_account_for  # noqa: E402

ACCOUNT = "50200000000000"
IFSC = "HDFC0000001"

MATCHABLE = [
    {"id": 9, "account_name": "royal trading company",
     "account_number": ACCOUNT, "ifsc": IFSC, "trade_id": None,
     "supplier_label": "VENDOR ONE"},
    {"id": 28, "account_name": "ROYAL TRADING COMPANY",
     "account_number": ACCOUNT, "ifsc": IFSC, "trade_id": None,
     "supplier_label": "VENDOR TWO"},
]


class TestTheHoldKeepsWhatItWorkedOut:
    def test_a_truncated_name_still_resolves_to_its_account(self):
        """
        ROYALTRAD is royaltradingcompany truncated. The matcher always got
        this right; the hold is what threw it away.
        """
        assert _resolved_account_for(
            {"beneficiary": "ROYALTRAD"}, MATCHABLE) == (ACCOUNT, IFSC)

    def test_the_way_the_client_retyped_it_resolves_too(self):
        """At 18:36 they tried again as "ROYAL trading". Same account."""
        assert _resolved_account_for(
            {"beneficiary": "ROYAL trading"}, MATCHABLE) == (ACCOUNT, IFSC)

    def test_a_name_matching_nothing_gives_nothing(self):
        """
        The other kind of hold is unchanged — inventing an account here is
        the guess held_payments exists to avoid.
        """
        assert _resolved_account_for(
            {"beneficiary": "INDURKAR TEXTILES"}, MATCHABLE) == (None, None)

    def test_a_slip_with_no_name_gives_nothing(self):
        assert _resolved_account_for({}, MATCHABLE) == (None, None)
        assert _resolved_account_for({"beneficiary": None}, MATCHABLE) == \
            (None, None)

    def test_both_hold_sites_pass_it_on(self):
        """
        Two call sites had the same bug last week for a different field.
        """
        src = (ROOT / "bot" / "client_bot.py").read_text(encoding="utf-8")
        assert src.count("_resolved_account_for(s, matchable)") == 2


class _Repo:
    def __init__(self, waiting, on_account):
        self.waiting = waiting
        self.on_account = on_account
        self.chased = []

    async def waiting_held_payments(self, **kw): return list(self.waiting)
    async def live_trades_on_account(self, **kw): return list(self.on_account)
    async def placeable_trades_for_client(self, cid): return []
    async def mark_held_chased(self, hid): self.chased.append(hid)


class _Notifier(notifier_mod.Notifier):
    def __init__(self, repo):
        self.repo = repo
        self.sent = []

    async def to_bridge(self, text, **kw):
        self.sent.append({"text": text, **kw})


def _held(account_number=ACCOUNT):
    return {"id": 1, "client_id": 3, "utr": "BKIDR10000000000001",
            "amount_inr": D("200000"), "account_number": account_number,
            "ifsc": IFSC, "typed_beneficiary": "ROYALTRAD",
            "asked_at": datetime(2026, 10, 26, 18, 20)}


def _order(tid, ref, outstanding):
    return {"id": tid, "reference": ref, "vendor": "A VENDOR",
            "outstanding": D(outstanding), "account_id": 28}


def _buttons(sent):
    return [b.callback_data
            for row in sent["reply_markup"].inline_keyboard for b in row]


class TestTheGap:
    @pytest.mark.asyncio
    async def test_with_nothing_open_it_says_so_and_promises_to_return(self):
        """
        The 18:20 message. It must not claim the account is unregistered,
        and it must not read as a dead end.
        """
        repo = _Repo([_held()], on_account=[])
        n = _Notifier(repo)
        await n.chase_held_payments()
        said = n.sent[0]["text"]
        assert "matches no registered account" not in said
        assert "Nothing is collecting into this account" in said
        assert "as soon as one opens" in said

    @pytest.mark.asyncio
    async def test_when_an_order_opens_it_offers_to_place_it(self):
        """
        The whole point. Jason issues the next Royal Trading order and the
        next reminder is a button, not a complaint.
        """
        repo = _Repo([_held()], on_account=[_order(140, "BRAV43", "3000000")])
        n = _Notifier(repo)
        await n.chase_held_payments()
        assert "hp:1:140" in _buttons(n.sent[0])
        assert "An order is now collecting" in n.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_it_still_will_not_choose_between_two(self):
        repo = _Repo([_held()], on_account=[_order(140, "BRAV43", "1"),
                                            _order(141, "SUPA70", "2")])
        n = _Notifier(repo)
        await n.chase_held_payments()
        said = n.sent[0]["text"]
        assert "will not guess" in said
        assert _buttons(n.sent[0])[:2] == ["hp:1:140", "hp:1:141"]

    @pytest.mark.asyncio
    async def test_the_account_is_read_at_chase_time_not_at_hold_time(self):
        """
        The hold row never changes. If the chase printed a fixed sentence
        from it, the money would still be waiting after the order opened.
        """
        src = inspect.getsource(notifier_mod.Notifier.chase_held_payments)
        lookup = src.index("live_trades_on_account")
        decision = src.index("len(on_account) > 1")
        assert lookup < decision

    @pytest.mark.asyncio
    async def test_not_ours_is_offered_in_every_case(self):
        """
        The only way to stop being asked. It was there before and must
        survive all three branches.
        """
        for on_account in ([], [_order(140, "BRAV43", "1")],
                           [_order(140, "B", "1"), _order(141, "S", "2")]):
            repo = _Repo([_held()], on_account=on_account)
            n = _Notifier(repo)
            await n.chase_held_payments()
            assert "hpx:1" in _buttons(n.sent[0]), on_account


class TestTheOtherHoldIsUnchanged:
    @pytest.mark.asyncio
    async def test_a_nameless_hold_still_takes_the_other_branch(self):
        """
        A payment whose name matched nothing has no account, and must still
        be offered every open order rather than this account-based path.
        """
        repo = _Repo([_held(account_number=None)], on_account=[])
        n = _Notifier(repo)
        await n.chase_held_payments()
        assert "matches no registered account" in n.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_every_chase_is_marked(self):
        repo = _Repo([_held()], on_account=[])
        n = _Notifier(repo)
        await n.chase_held_payments()
        assert repo.chased == [1]


class TestNoSecondPlacementPath:
    def test_hp_resolves_the_orders_when_it_is_tapped(self):
        """
        Why no new handler was needed: hp: looks the orders up again at tap
        time, so a button offered now still places correctly later — and an
        order that has since closed is refused rather than guessed at.
        """
        from bot import bridge_trade
        src = inspect.getsource(bridge_trade.place_held_payment)
        assert "live_trades_on_account" in src
        assert "no longer open" in src

    def test_there_is_still_one_hp_handler(self):
        src = (ROOT / "bot" / "bridge_trade.py").read_text(encoding="utf-8")
        assert src.count('startswith("hp:")') == 1
