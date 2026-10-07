"""
The Bridge is told when a vendor registers an account another already uses.

WHY

One bank account under several vendors is the single thing that makes a
beneficiary name unanswerable. Every slip reads identically, so nothing but a
person can say whose order a payment belongs to.

It built up without anyone being told. By 7 October 2026 SUPER TRADING COMPANY
(STC) was registered under four vendors and KISAN TRADERS under two, and each
addition quietly made every payment to that name ambiguous. The first anyone
noticed was a client being shown eight buttons with two identical pairs on it:

    why and also im seeing duplicate on accounts, Kisan traers is not in use
    So we had 2 trades on, 1 live, then accounts were added, something else
    happened.. need to go root and branch on this, permanent fix.
    — Bridge, 7 October 2026

He was right that adding an account was involved, and wrong about how: it did
not break anything, it made an existing weakness visible.

WHAT THIS IS NOT

It is not a refusal. Vendors genuinely do share a collection account — that is
why the ask-the-Bridge path exists — and blocking it would stop real trading.
It is a notice, so sharing is something he decides rather than something he
discovers two weeks later from a screenshot.
"""

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import supplier_bot  # noqa: E402
from db.repo import Repo  # noqa: E402


class _Message:
    def __init__(self, text):
        self.text = text
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append(text)


class _State:
    def __init__(self, data):
        self.data = dict(data)
        self.cleared = False

    async def get_data(self): return dict(self.data)
    async def clear(self): self.cleared = True
    async def set_state(self, s): pass
    async def update_data(self, **kw): self.data.update(kw)


class _Notifier:
    def __init__(self):
        self.bridge = []

    async def to_bridge(self, text, **kw):
        self.bridge.append(text)


class _Repo:
    def __init__(self, sharing):
        self.sharing = sharing
        self.added = []

    async def add_bank_account(self, **kw):
        self.added.append(kw)
        return 99

    async def others_sharing_account(self, **kw):
        return list(self.sharing)


DATA = {"account_name": "SUPER TRADING COMPANY (STC)",
        "account_number": "50200000000000"}


async def _register(repo, notifier):
    msg = _Message("HDFC0005183")
    await supplier_bot.account_ifsc(
        msg, _State(DATA), {"id": 7, "label": "Uncle"}, repo, notifier)
    return msg


class TestTheBridgeIsToldAboutSharing:
    @pytest.mark.asyncio
    async def test_he_is_warned_and_told_who_else_uses_it(self):
        repo = _Repo([
            {"id": 2, "account_name": "SUPER TRADING COMPANY (STC)",
             "vendor": "IndoLondon group NEW", "has_live_order": True},
            {"id": 26, "account_name": "SUPER TRADING COMPANY (STC)",
             "vendor": "BIG BOSS", "has_live_order": False},
        ])
        notifier = _Notifier()
        await _register(repo, notifier)
        said = notifier.bridge[0]
        assert "ALREADY registered" in said
        assert "IndoLondon group NEW" in said
        assert "BIG BOSS" in said

    @pytest.mark.asyncio
    async def test_it_says_which_of_them_is_trading_now(self):
        """
        A vendor with a live order on that account is the one that will make
        the next payment ambiguous. A dormant registration is tidy-up.
        """
        repo = _Repo([
            {"id": 2, "account_name": "X", "vendor": "IndoLondon group NEW",
             "has_live_order": True},
            {"id": 26, "account_name": "X", "vendor": "BIG BOSS",
             "has_live_order": False},
        ])
        notifier = _Notifier()
        await _register(repo, notifier)
        said = notifier.bridge[0]
        assert "IndoLondon group NEW (trading now)" in said
        assert "BIG BOSS (trading now)" not in said

    @pytest.mark.asyncio
    async def test_it_says_what_the_consequence_is(self):
        """
        "Already registered elsewhere" is a fact. "Every payment here will
        come to you to place" is why he should care.
        """
        repo = _Repo([{"id": 2, "account_name": "X", "vendor": "IndoLondon",
                       "has_live_order": True}])
        notifier = _Notifier()
        await _register(repo, notifier)
        said = notifier.bridge[0]
        assert "come to you to place" in said
        assert "/account_remove" in said

    @pytest.mark.asyncio
    async def test_the_account_is_still_registered(self):
        """
        A notice, not a refusal. Vendors do share accounts and blocking it
        would stop real trading.
        """
        repo = _Repo([{"id": 2, "account_name": "X", "vendor": "IndoLondon",
                       "has_live_order": True}])
        await _register(repo, _Notifier())
        assert repo.added, "the account was not registered"

    @pytest.mark.asyncio
    async def test_the_vendor_is_told_nothing_about_other_vendors(self):
        """
        The 11 September disclosure rule. A vendor learning which other
        vendors share their account learns the shape of the book.
        """
        repo = _Repo([{"id": 2, "account_name": "X",
                       "vendor": "IndoLondon group NEW",
                       "has_live_order": True}])
        msg = await _register(repo, _Notifier())
        shown = "\n".join(msg.sent)
        assert "IndoLondon" not in shown
        assert "ALREADY registered" not in shown
        assert "Stored." in shown


class TestAnOrdinaryAccountIsUnchanged:
    @pytest.mark.asyncio
    async def test_nothing_extra_is_said(self):
        repo = _Repo([])
        notifier = _Notifier()
        await _register(repo, notifier)
        said = notifier.bridge[0]
        assert "New account registered by Uncle" in said
        assert "ALREADY" not in said

    @pytest.mark.asyncio
    async def test_the_vendor_still_gets_their_confirmation(self):
        repo = _Repo([])
        msg = await _register(repo, _Notifier())
        assert any("Stored." in s for s in msg.sent)


class TestTheQuery:
    def test_it_excludes_the_vendor_registering_it(self):
        sql = inspect.getsource(Repo.others_sharing_account)
        assert "b.party_id <> $1" in sql

    def test_it_only_counts_live_registrations(self):
        """
        A removed account is not sharing anything.
        """
        sql = inspect.getsource(Repo.others_sharing_account)
        assert "b.is_active" in sql

    def test_it_matches_the_physical_account_not_the_name(self):
        """
        The name is the thing that is unreliable — that is the whole problem.
        Two vendors with the same holder name at different banks are not
        sharing an account and the client can tell them apart.
        """
        sql = inspect.getsource(Repo.others_sharing_account)
        assert "b.account_number = $2" in sql
        assert "b.ifsc = $3" in sql
        assert "account_name =" not in sql
