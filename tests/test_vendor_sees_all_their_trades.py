"""
A vendor with several orders can see each one.

    the vendors, can they have same function as me, where they can see all
    their trades, and click a button to see statement on a trade, as they
    sometimes have multiple
    — Bridge, 17 October 2026

/progress answered "how is the current one going", which was right when a
vendor had one order at a time. With several open it answers about one and
says nothing about the rest — so the vendor asks the Bridge, which is the
question /progress was added to remove.

WHAT A VENDOR MAY SEE

Their own collection, and nothing else. No deal reference, no rates, no
margin, no client name (decision D4, and the 11 September disclosure, when a
list went out reading "Supplier A — …" and told a counterparty the shape of
the book). The orders are numbered for the buttons rather than named.

AND NOTHING OF ANYONE ELSE'S

The button carries a trade id. A vendor who edits one must not be able to
read another vendor's collection, so ownership is confirmed from the
database before a single figure is sent — never trusted from the callback.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import supplier_bot  # noqa: E402
from db.repo import Repo  # noqa: E402


class _Msg:
    def __init__(self):
        self.sent = []

    async def answer(self, t, **kw): self.sent.append({"text": t, **kw})


class _Call:
    def __init__(self, data):
        self.data = data
        self.message = _Msg()
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append({"text": text, **kw})


def _t(tid, paid, expected=D("3000000"), instructed=True):
    return {"id": tid, "opened_at": None,
            "instructed_at": "x" if instructed else None,
            "inr_expected": expected, "paid_inr": paid}


class _Repo:
    def __init__(self, trades, *, owns=True, detail=None, payments=()):
        self.trades = list(trades)
        self.owns = owns
        self.detail = detail
        self.payments = list(payments)
        self.asked_for = []

    async def open_trades_for_supplier(self, sid): return list(self.trades)

    async def open_trade_for_supplier(self, sid):
        return self.trades[0] if self.trades else None

    async def trade_belongs_to_supplier(self, *, trade_id, supplier_id):
        self.asked_for.append((trade_id, supplier_id))
        return self.owns

    async def trade_detail(self, tid): return self.detail

    async def trade_payments(self, tid): return list(self.payments)


PAYMENTS = [{"utr": "BKIDR10000000000001", "amount_inr": D("200000"),
             "account_name": "EKTA TRADERS"}]


class TestSeveralOrders:
    @pytest.mark.asyncio
    async def test_all_of_them_are_listed(self):
        repo = _Repo([_t(1, D("1000000")), _t(2, D("500000"))])
        msg = _Msg()
        await supplier_bot.cmd_progress(msg, {"id": 30}, repo)
        text = msg.sent[0]["text"]
        assert "more than one trade" in text
        assert "1." in text and "2." in text

    @pytest.mark.asyncio
    async def test_each_gets_a_statement_button(self):
        repo = _Repo([_t(1, D("1000000")), _t(2, D("500000"))])
        msg = _Msg()
        await supplier_bot.cmd_progress(msg, {"id": 30}, repo)
        datas = [b.callback_data for row in
                 msg.sent[0]["reply_markup"].inline_keyboard for b in row]
        assert datas == ["sst:1", "sst:2"]

    @pytest.mark.asyncio
    async def test_one_not_yet_instructed_says_so(self):
        repo = _Repo([_t(1, D("1000000")),
                      _t(2, D(0), expected=None, instructed=False)])
        msg = _Msg()
        await supplier_bot.cmd_progress(msg, {"id": 30}, repo)
        assert "awaiting payment instructions" in msg.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_it_names_no_client_and_no_reference(self):
        """
        A vendor is owed the collection of their own INR and no more.
        """
        repo = _Repo([_t(1, D("1000000")), _t(2, D("500000"))])
        msg = _Msg()
        await supplier_bot.cmd_progress(msg, {"id": 30}, repo)
        text = msg.sent[0]["text"]
        for leak in ("SUPA", "BRAV", "Haze", "rate", "margin"):
            assert leak not in text, leak


class TestOneOrderIsUnchanged:
    @pytest.mark.asyncio
    async def test_it_answers_straight_away(self):
        """A list of one is a worse version of the statement they came for."""
        repo = _Repo([_t(1, D("1000000"))], payments=PAYMENTS)
        msg = _Msg()
        await supplier_bot.cmd_progress(msg, {"id": 30}, repo)
        assert "reply_markup" not in msg.sent[0]
        assert "more than one" not in msg.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_no_orders_still_says_so(self):
        repo = _Repo([])
        msg = _Msg()
        await supplier_bot.cmd_progress(msg, {"id": 30}, repo)
        assert "no trade in progress" in msg.sent[0]["text"]


class TestAVendorSeesOnlyTheirOwn:
    @pytest.mark.asyncio
    async def test_ownership_is_checked_before_anything_is_sent(self):
        repo = _Repo([], owns=False)
        call = _Call("sst:999")
        await supplier_bot.supplier_statement(call, {"id": 30}, repo)
        assert repo.asked_for == [(999, 30)]
        assert call.message.sent == [], "a figure was sent anyway"
        assert call.answers[0].get("show_alert")

    @pytest.mark.asyncio
    async def test_their_own_statement_is_sent(self):
        repo = _Repo([], owns=True, payments=PAYMENTS,
                     detail={"id": 1, "inr_expected": D("3000000"),
                             "paid_inr": D("1000000")})
        call = _Call("sst:1")
        await supplier_bot.supplier_statement(call, {"id": 30}, repo)
        assert call.message.sent
        assert "Payments received" in call.message.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_an_uninstructed_order_says_so_rather_than_showing_nil(self):
        repo = _Repo([], owns=True,
                     detail={"id": 1, "inr_expected": None,
                             "paid_inr": D(0)})
        call = _Call("sst:1")
        await supplier_bot.supplier_statement(call, {"id": 30}, repo)
        assert "not been issued yet" in call.message.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_a_trade_that_has_gone_is_handled(self):
        repo = _Repo([], owns=True, detail=None)
        call = _Call("sst:1")
        await supplier_bot.supplier_statement(call, {"id": 30}, repo)
        assert call.message.sent == []
        assert call.answers[0].get("show_alert")

    def test_the_check_reads_the_database_not_the_callback(self):
        """
        The id comes back from the client side and cannot be trusted.
        """
        src = inspect.getsource(supplier_bot.supplier_statement)
        assert "trade_belongs_to_supplier" in src
        check = src.index("trade_belongs_to_supplier")
        send = src.index("render_mini_statement")
        assert check < send, "figures are sent before ownership is confirmed"

    def test_the_query_matches_on_the_supplier(self):
        sql = inspect.getsource(Repo.trade_belongs_to_supplier)
        assert "supplier_id = $2" in sql

    def test_the_button_is_not_state_gated(self):
        """
        The list can be scrolled back to tomorrow, and a dead button sends
        them to the Bridge with the question /progress exists to answer.
        """
        src = (ROOT / "bot" / "supplier_bot.py").read_text(encoding="utf-8")
        line = [l for l in src.splitlines() if 'startswith("sst:")' in l]
        assert line and "StatesGroup" not in line[0]


class TestTheListIsOrderedUsefully:
    def test_the_one_being_collected_comes_first(self):
        """
        Newest-first would put the untouched order at the top and bury the
        one they are waiting on — the same reasoning as /progress itself.
        """
        sql = inspect.getsource(Repo.open_trades_for_supplier)
        assert "ORDER BY (t.instructed_at IS NULL), t.instructed_at, t.opened_at" in sql

    def test_only_live_orders(self):
        sql = inspect.getsource(Repo.open_trades_for_supplier)
        assert "t.status IN ('open', 'awaiting_payment')" in sql

    def test_it_selects_nothing_a_vendor_may_not_see(self):
        sql = inspect.getsource(Repo.open_trades_for_supplier)
        select = sql[sql.index("SELECT"):sql.index("FROM trades")]
        for leak in ("reference", "supply_rate", "sell_rate", "margin",
                     "client"):
            assert leak not in select, leak
