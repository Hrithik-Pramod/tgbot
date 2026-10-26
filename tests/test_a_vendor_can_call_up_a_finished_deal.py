"""
A vendor can call up a deal they have finished and read its UTRs.

    I need a feature for the vendors. I need them to be able to view old
    trades over the last 3 to 4 days and show the summary with UTRs. A
    completed progress
    — Bridge, 23 October 2026

    Also the statement thing i mentioned, so the vendor can call up an old
    deal and see the related UTRs?
    — Bridge, 26 October 2026

/progress reports the order being collected. The moment a deal completed it
disappeared from the vendor's view, so checking what had been paid against it
meant asking the Bridge — which is the question /progress was built to
remove, arriving a day later than it used to.

WHAT IS DELIBERATELY NOT HERE

The deal reference. Decision D4 and the 11 September disclosure hold: a
vendor is owed the collection of their own INR and no more. Showing SUPA61
would give the Bridge and the vendor a shared name for a deal, which is
genuinely useful, and it was considered and declined on 26 October. A
finished deal is identified by the day it closed and the amount collected.

That is a decision, not an oversight, which is why there is a test for it.

AND CANCELLED DEALS ARE NOT LISTED

A cancelled order has no collection to report. Listing it invites "what
happened to this one", which is a conversation for the Bridge rather than a
statement.

WHY THERE IS NO NEW BUTTON HANDLER

sst: already answers with a statement, already confirms the trade belongs to
the vendor before sending a figure, and was deliberately never state-gated.
Neither trade_belongs_to_supplier nor trade_detail filters on status, so it
worked on a closed deal the whole time — nothing listed one. Reusing it means
the ownership check cannot drift apart between the two lists.
"""

import inspect
import re
import sys
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import menu, supplier_bot  # noqa: E402
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


def _closed(tid, days_ago, paid):
    return {"id": tid,
            "opened_at": datetime(2026, 10, 20) - timedelta(days=days_ago),
            "completed_at": datetime(2026, 10, 24) - timedelta(days=days_ago),
            "inr_expected": D(paid), "paid_inr": D(paid)}


class _Repo:
    def __init__(self, closed=(), *, owns=True, detail=None, payments=()):
        self.closed = list(closed)
        self.owns = owns
        self.detail = detail
        self.payments = list(payments)
        self.asked = []

    async def recent_closed_trades_for_supplier(self, sid, **kw):
        self.asked.append((sid, kw))
        return list(self.closed)

    async def trade_belongs_to_supplier(self, *, trade_id, supplier_id):
        return self.owns

    async def trade_detail(self, tid): return self.detail
    async def trade_payments(self, tid): return list(self.payments)


PAYMENTS = [
    {"utr": "BKIDR10000000000001", "amount_inr": D("200000"),
     "account_name": "EKTA TRADERS"},
    {"utr": "BKIDR10000000000002", "amount_inr": D("300000"),
     "account_name": "EKTA TRADERS"},
]


class TestTheList:
    @pytest.mark.asyncio
    async def test_finished_deals_are_listed(self):
        repo = _Repo([_closed(1, 0, "5445193"), _closed(2, 2, "2230200")])
        msg = _Msg()
        await supplier_bot.cmd_history(msg, {"id": 30}, repo)
        text = msg.sent[0]["text"]
        assert "1." in text and "2." in text
        assert "5,445,193" in text

    @pytest.mark.asyncio
    async def test_each_one_opens_its_statement(self):
        repo = _Repo([_closed(7, 0, "100000"), _closed(8, 1, "200000")])
        msg = _Msg()
        await supplier_bot.cmd_history(msg, {"id": 30}, repo)
        datas = [b.callback_data for row in
                 msg.sent[0]["reply_markup"].inline_keyboard for b in row]
        assert datas == ["sst:7", "sst:8"]

    @pytest.mark.asyncio
    async def test_a_quiet_vendor_is_pointed_at_progress(self):
        """
        An empty list must not read as a failure, and must say where the
        live deal is — otherwise they ask the Bridge, which is the point.
        """
        repo = _Repo([])
        msg = _Msg()
        await supplier_bot.cmd_history(msg, {"id": 30}, repo)
        said = msg.sent[0]["text"]
        assert "no completed deals" in said
        assert "/progress" in said
        assert "reply_markup" not in msg.sent[0]

    @pytest.mark.asyncio
    async def test_it_asks_only_for_this_vendors_deals(self):
        repo = _Repo([_closed(1, 0, "1")])
        await supplier_bot.cmd_history(_Msg(), {"id": 30}, repo)
        assert repo.asked[0][0] == 30


class TestWhatAVendorMayNotSee:
    @pytest.mark.asyncio
    async def test_the_list_names_no_reference_client_or_rate(self):
        repo = _Repo([_closed(1, 0, "5445193"), _closed(2, 1, "2230200")])
        msg = _Msg()
        await supplier_bot.cmd_history(msg, {"id": 30}, repo)
        text = msg.sent[0]["text"]
        for leak in ("SUPA", "BRAV", "Haze", "ProperPay", "rate", "margin",
                     "client"):
            assert leak not in text, leak

    def test_the_query_selects_nothing_it_should_not(self):
        sql = inspect.getsource(Repo.recent_closed_trades_for_supplier)
        select = sql[sql.index("SELECT"):sql.index("FROM trades")]
        for leak in ("reference", "supply_rate", "sell_rate", "margin",
                     "client"):
            assert leak not in select, leak

    def test_showing_the_reference_was_declined_on_purpose(self):
        """
        Pinning the decision, not the code. If someone later wants vendors
        to see SUPA61 that is a real choice with an argument for it — but it
        reverses decision D4 and must be made deliberately, not by adding a
        column to a SELECT.
        """
        doc = Repo.recent_closed_trades_for_supplier.__doc__ or ""
        assert "D4" in doc
        assert "declined" in doc


class TestWhichDealsQualify:
    def test_only_completed_ones(self):
        sql = inspect.getsource(Repo.recent_closed_trades_for_supplier)
        assert "t.status = 'completed'" in sql
        assert "cancelled" not in sql[sql.index("WHERE"):sql.index("ORDER BY")]

    def test_it_is_a_few_days_not_everything(self):
        sql = inspect.getsource(Repo.recent_closed_trades_for_supplier)
        assert "completed_at > now()" in sql
        assert "days" in sql

    def test_four_days_as_asked_for(self):
        p = inspect.signature(
            Repo.recent_closed_trades_for_supplier).parameters
        assert p["days"].default == 4
        assert p["limit"].default == 10

    def test_the_newest_is_first(self):
        """
        "The one I just did" is what they are nearly always asking about.
        """
        sql = inspect.getsource(Repo.recent_closed_trades_for_supplier)
        assert "ORDER BY t.completed_at DESC" in sql

    def test_a_deal_with_no_completed_at_cannot_slip_in(self):
        """
        completed_at is nullable, and NULL > now() - interval is NULL, not
        false — safe here, but only by accident. Said explicitly.
        """
        sql = inspect.getsource(Repo.recent_closed_trades_for_supplier)
        assert "t.completed_at IS NOT NULL" in sql

    def test_the_interval_is_cast_not_interpolated(self):
        sql = inspect.getsource(Repo.recent_closed_trades_for_supplier)
        assert "($2::text || ' days')::interval" in sql

    def test_the_query_parses(self):
        import pglast
        sql = inspect.getsource(Repo.recent_closed_trades_for_supplier)
        i = sql.index('"""', sql.index("conn.fetch"))
        j = sql.index('"""', i + 3)
        body = "\n".join(re.sub(r"--.*$", "", l)
                         for l in sql[i + 3:j].splitlines())
        pglast.parse_sql(body.replace("$1", "1").replace("$2", "'4'")
                             .replace("$3", "10"))


class TestTheStatementItselfIsUnchanged:
    @pytest.mark.asyncio
    async def test_a_closed_deal_statement_lists_its_utrs(self):
        """
        The thing he actually asked for.
        """
        repo = _Repo(owns=True, payments=PAYMENTS,
                     detail={"id": 1, "inr_expected": D("500000"),
                             "paid_inr": D("500000")})
        call = _Call("sst:1")
        await supplier_bot.supplier_statement(call, {"id": 30}, repo)
        said = call.message.sent[0]["text"]
        assert "BKIDR10000000000001" in said
        assert "BKIDR10000000000002" in said

    @pytest.mark.asyncio
    async def test_another_vendors_closed_deal_is_refused(self):
        """
        The list is new; the ownership check is the old one, and it has to
        still bite on a trade id typed into a callback by hand.
        """
        repo = _Repo(owns=False)
        call = _Call("sst:999")
        await supplier_bot.supplier_statement(call, {"id": 30}, repo)
        assert call.message.sent == []
        assert call.answers[0].get("show_alert")

    def test_no_second_statement_handler_was_added(self):
        """
        Two handlers would be two ownership checks, and one of them would
        eventually be wrong.
        """
        src = (ROOT / "bot" / "supplier_bot.py").read_text(encoding="utf-8")
        assert src.count('startswith("sst:")') == 1


class TestItIsReachable:
    def test_history_is_in_the_vendor_menu(self):
        """
        This is exactly how /issue stayed invisible for eight days.
        """
        assert any(c == "history" for c, _ in menu.MENUS["supplier"])

    def test_the_command_is_registered(self):
        src = (ROOT / "bot" / "supplier_bot.py").read_text(encoding="utf-8")
        assert 'Command("history")' in src
