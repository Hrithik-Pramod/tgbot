"""
A counterparty is told the desk paid them only when the desk paid them.

WHAT HAPPENED — 5 October 2026, 13:58 IST

100 USDT reached the client's own wallet. The bot posted to them:

    Funds received
    USDT = 100.00
    Hash 1e2b0cf4…

and the client answered "This was not me". It was their own counterparty
moving funds internally; it had nothing to do with this desk.

    this is an odd one, can we set so that it ignores any settlements that do
    not match the sending.
    Nothing at all, if unrelated to us, it will most likely be intrnal fund
    movement
    — Bridge, 5 October 2026

WHY THIS IS WORTH A TABLE

Nothing was mis-recorded. No trade existed, no figure moved, the ledger was
entirely right — and that is the point. The LEDGER was right and the MESSAGE
was wrong, sent by this desk's bot to the party least able to check it, saying
money had arrived from a desk that had not sent it. A counterparty may
reconcile against that.

WHY THE SENDER AND NOT THE AMOUNT

Matching the arrival against an outstanding payout would be a guess. A guess
that is right most of the time is the kind that gets believed on the occasion
it is wrong, which is the whole failure mode here. The sender is a fact on the
chain.

Eight addresses had ever paid this client: seven with months of history and
over 1.2m USDT between them, and the one that caused this, which sent once and
had never been seen before.

THE RULE

  from a known address        the counterparty is told, as before
  from the desk's own wallet  likewise — a vendor wallet forwarding a
                              settlement is the desk's money moving
  from anything else          the counterparty is told NOTHING, and the
                              Bridge is asked whether it was his, with a
                              button that both tells them and remembers the
                              address
"""

import inspect
import re
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import bridge_trade  # noqa: E402
from db.repo import Repo  # noqa: E402
from monitor import tron  # noqa: E402


# ----------------------------------------------------------------------
# The monitor's decision
# ----------------------------------------------------------------------

class _Notifier:
    def __init__(self):
        self.to_bridge_calls = []
        self.to_party_calls = []

    async def to_bridge(self, text, **kw):
        self.to_bridge_calls.append({"text": text, **kw})

    async def to_party(self, party_id, text, **kw):
        self.to_party_calls.append({"party_id": party_id, "text": text})

    async def on_supplier_deposit(self, **kw):
        raise AssertionError("a counterparty wallet must not open a trade")


class _Repo:
    def __init__(self, *, known: bool):
        self.known = known
        self.asked_about = []

    async def record_deposit(self, **kw):
        return 1154873

    async def is_known_payout_source(self, address):
        self.asked_about.append(address)
        return self.known

    async def party_label(self, party_id):
        return "A COUNTERPARTY"


def _monitor(repo, notifier):
    m = tron.DepositMonitor.__new__(tron.DepositMonitor)
    m.repo = repo
    m.notifier = notifier
    return m


CLIENT_WALLET = {"id": 2, "is_internal": False, "owner_party_id": 9,
                 "address": "TExampleClientWallet0000000000000"}

TRANSFER = {"tx_hash": "0" * 64, "amount": D("100"), "block": 1,
            "from_address": "TExampleSender000000000000000000",
            "confirmed": True, "timestamp_ms": 0}


class TestMoneyFromOutsideTheDesk:
    @pytest.mark.asyncio
    async def test_the_counterparty_is_told_nothing(self):
        """
        The whole request. "Nothing at all, if unrelated to us."
        """
        repo, notifier = _Repo(known=False), _Notifier()
        await _monitor(repo, notifier)._handle_deposit(CLIENT_WALLET, TRANSFER)
        assert notifier.to_party_calls == [], (
            "a counterparty was told the desk paid them money the desk did "
            "not send"
        )

    @pytest.mark.asyncio
    async def test_the_bridge_is_told_instead(self):
        repo, notifier = _Repo(known=False), _Notifier()
        await _monitor(repo, notifier)._handle_deposit(CLIENT_WALLET, TRANSFER)
        assert len(notifier.to_bridge_calls) == 1
        said = notifier.to_bridge_calls[0]["text"]
        assert "do not recognise" in said
        assert "NOT been told" in said

    @pytest.mark.asyncio
    async def test_he_can_claim_it_as_his(self):
        repo, notifier = _Repo(known=False), _Notifier()
        await _monitor(repo, notifier)._handle_deposit(CLIENT_WALLET, TRANSFER)
        kb = notifier.to_bridge_calls[0]["reply_markup"]
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert datas == ["ps:1154873"], datas

    @pytest.mark.asyncio
    async def test_the_decision_is_made_on_the_sender(self):
        repo, notifier = _Repo(known=False), _Notifier()
        await _monitor(repo, notifier)._handle_deposit(CLIENT_WALLET, TRANSFER)
        assert repo.asked_about == ["TExampleSender000000000000000000"]

    @pytest.mark.asyncio
    async def test_it_is_still_recorded_as_a_deposit(self):
        """
        The arrival is true and stays on record. Hiding it would make the
        ledger disagree with the chain, which this system has paid for twice.
        """
        repo, notifier = _Repo(known=False), _Notifier()
        m = _monitor(repo, notifier)
        recorded = []
        orig = repo.record_deposit

        async def _spy(**kw):
            recorded.append(kw)
            return await orig(**kw)

        repo.record_deposit = _spy
        await m._handle_deposit(CLIENT_WALLET, TRANSFER)
        assert recorded, "the deposit was not written down"


class TestMoneyFromTheDesk:
    @pytest.mark.asyncio
    async def test_the_counterparty_is_told_exactly_as_before(self):
        repo, notifier = _Repo(known=True), _Notifier()
        await _monitor(repo, notifier)._handle_deposit(CLIENT_WALLET, TRANSFER)
        assert len(notifier.to_party_calls) == 1
        assert notifier.to_party_calls[0]["party_id"] == 9
        assert "Funds received" in notifier.to_party_calls[0]["text"]

    @pytest.mark.asyncio
    async def test_the_bridge_still_gets_his_confirmation(self):
        repo, notifier = _Repo(known=True), _Notifier()
        await _monitor(repo, notifier)._handle_deposit(CLIENT_WALLET, TRANSFER)
        assert "Onward payout confirmed" in notifier.to_bridge_calls[0]["text"]

    @pytest.mark.asyncio
    async def test_it_is_not_offered_as_something_to_claim(self):
        repo, notifier = _Repo(known=True), _Notifier()
        await _monitor(repo, notifier)._handle_deposit(CLIENT_WALLET, TRANSFER)
        assert notifier.to_bridge_calls[0].get("reply_markup") is None


# ----------------------------------------------------------------------
# What counts as the desk's
# ----------------------------------------------------------------------

class TestWhatCountsAsOurs:
    def test_the_desks_own_wallets_count_without_being_listed(self):
        """
        A vendor's receiving wallet forwarding a settlement is the desk's
        money moving. Listing those separately would mean keeping the same
        fact in two places.
        """
        src = inspect.getsource(Repo.is_known_payout_source)
        assert "FROM payout_sources" in src
        assert "FROM wallets" in src

    def test_a_retired_wallet_still_counts(self):
        """
        Retirement means "stop watching it for deposits", not "it was never
        ours". A settlement forwarded from a wallet retired afterwards is
        still a settlement from this desk.
        """
        src = " ".join(inspect.getsource(Repo.is_known_payout_source).split())
        wallets = src[src.index("FROM wallets"):]
        assert "retired_at" not in wallets[:120]

    def test_an_unknown_sender_is_never_assumed_to_be_ours(self):
        src = inspect.getsource(Repo.is_known_payout_source)
        assert "if not address:" in src
        assert "return False" in src

    @pytest.mark.asyncio
    async def test_no_sender_at_all_is_not_ours(self):
        r = Repo.__new__(Repo)
        r.pool = None          # any database access would raise
        assert await r.is_known_payout_source(None) is False
        assert await r.is_known_payout_source("") is False


class TestTheSeedDoesNotLeakAddresses:
    def test_the_migration_seeds_by_query_not_by_literal(self):
        """
        This repository is public. A payout address committed to it hands
        anyone the desk's settlement history.
        """
        mig = (ROOT / "deploy" / "migrate-014-payout-sources.sql").read_text(
            encoding="utf-8")
        assert "INSERT INTO payout_sources" in mig
        assert "SELECT d.from_address" in mig
        assert not re.search(r"\bT[A-Za-z0-9]{33}\b", mig), (
            "a real TRON address is committed in the migration"
        )

    def test_one_send_is_not_enough_to_be_trusted(self):
        """
        One send is not a pattern — it is exactly what the stranger looked
        like on 5 October.
        """
        mig = (ROOT / "deploy" / "migrate-014-payout-sources.sql").read_text(
            encoding="utf-8")
        assert "HAVING count(*) >= 2" in mig

    def test_it_only_seeds_from_counterparty_wallets(self):
        """
        Deposits onto the desk's OWN internal wallets are vendors paying in.
        Seeding from those would make every vendor's sending address count as
        a desk payout source.
        """
        mig = (ROOT / "deploy" / "migrate-014-payout-sources.sql").read_text(
            encoding="utf-8")
        assert "WHERE NOT w.is_internal" in mig

    def test_it_can_be_run_twice(self):
        mig = (ROOT / "deploy" / "migrate-014-payout-sources.sql").read_text(
            encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS" in mig
        assert "ON CONFLICT (address) DO NOTHING" in mig

    def test_the_schema_agrees(self):
        schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        assert "CREATE TABLE payout_sources" in schema


# ----------------------------------------------------------------------
# Claiming it afterwards
# ----------------------------------------------------------------------

class _Call:
    def __init__(self, data):
        self.data = data
        self.message = _Msg()
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


class _Msg:
    def __init__(self):
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append(text)


class _ClaimRepo:
    def __init__(self, deposit, *, added=True):
        self.deposit = deposit
        self.added = added
        self.calls = []

    async def deposit_detail(self, deposit_id):
        return dict(self.deposit) if self.deposit else None

    async def add_payout_source(self, *, address, actor_party_id, note=None):
        self.calls.append(address)
        return self.added


DEPOSIT = {"id": 1154873, "amount_usdt": D("100"), "tx_hash": "0" * 64,
           "from_address": "TExampleSender000000000000000000",
           "is_internal": False, "owner_party_id": 9}


class TestClaimingAnUnknownSettlement:
    @pytest.mark.asyncio
    async def test_the_counterparty_is_told_once_he_confirms(self):
        repo, notifier = _ClaimRepo(DEPOSIT), _Notifier()
        call = _Call("ps:1154873")
        await bridge_trade.confirm_payout_source(call, {"id": 1}, repo, notifier)
        assert len(notifier.to_party_calls) == 1
        assert "Funds received" in notifier.to_party_calls[0]["text"]

    @pytest.mark.asyncio
    async def test_the_address_is_remembered(self):
        repo, notifier = _ClaimRepo(DEPOSIT), _Notifier()
        await bridge_trade.confirm_payout_source(
            _Call("ps:1154873"), {"id": 1}, repo, notifier)
        assert repo.calls == ["TExampleSender000000000000000000"]

    @pytest.mark.asyncio
    async def test_confirming_twice_tells_them_once(self):
        """
        The question can sit for hours and survive a restart, so the button
        outlives its context and can be pressed again.
        """
        repo, notifier = _ClaimRepo(DEPOSIT, added=False), _Notifier()
        await bridge_trade.confirm_payout_source(
            _Call("ps:1154873"), {"id": 1}, repo, notifier)
        assert notifier.to_party_calls == []

    @pytest.mark.asyncio
    async def test_a_transfer_with_no_sender_changes_nothing(self):
        repo, notifier = _ClaimRepo({**DEPOSIT, "from_address": None}), _Notifier()
        call = _Call("ps:1154873")
        await bridge_trade.confirm_payout_source(call, {"id": 1}, repo, notifier)
        assert repo.calls == []
        assert notifier.to_party_calls == []
        assert "no sender" in call.message.sent[0]

    @pytest.mark.asyncio
    async def test_a_deposit_that_has_gone_is_handled(self):
        repo, notifier = _ClaimRepo(None), _Notifier()
        call = _Call("ps:1154873")
        await bridge_trade.confirm_payout_source(call, {"id": 1}, repo, notifier)
        assert notifier.to_party_calls == []
        assert call.message.sent

    def test_the_button_is_not_state_gated(self):
        """
        It can be pressed hours later, after a restart. A state filter would
        leave the counterparty never told about a settlement that was real.
        """
        src = (ROOT / "bot" / "bridge_trade.py").read_text(encoding="utf-8")
        line = [l for l in src.splitlines()
                if "ps:" in l and "callback_query" in l]
        assert line, "the handler's decorator was not found"
        assert "StatesGroup" not in line[0]
        assert re.search(r'@router\.callback_query\(F\.data\.startswith\("ps:"\)\)',
                         line[0])
