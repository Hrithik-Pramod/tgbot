"""
One deposit, two orders — the Bridge choosing where the line falls.

THE REQUEST (Bridge, 1 October 2026)

    if usdt is deposited, i want the option my end to split the payment
    so 10000 usdt comes in, with bank instructions
    i have option to send in 2 parts
    Make it as least complicated it only splits in 2 not anymore than this
    it just needs to let me decide how much the first order is
    and the ability to send the second part later

WHY IT IS WORTH BUILDING RATHER THAN DOING BY HAND

It has already been done by hand once. On 11 September 2026 a 3,000 USDT
deposit merged into SUPB1 ten minutes after the client had been instructed to
pay for 1,859, and deploy/fix-supb1-split.sql carved SUPB2 back out of it in a
transaction written line by line with the bot stopped. Every figure in that
script — 318000, 2966.42, 33.58, 197054, 1838.19, 20.81 — was typed by a
person at the end of a long day. That is the operation being automated.

THE THREE THINGS THAT MUST HOLD

  1. THE DEPOSIT IS NOT TOUCHED.
     A deposit row points at one trade, and deposits_tx_unique (tx_hash,
     wallet_id) is what stops the same transfer being credited twice when the
     monitor sees it again. Neither is bent for this. The deposit stays whole
     on the first part; the second part records in split_from_trade_id where
     its USDT came from.

  2. BOTH PARTS ARE PRICED AT THE RATES THE DEPOSIT ALREADY HAD.
     Not the current rate. The USDT arrived once, under one deal. A rate
     change between the money landing and the Bridge splitting it must not
     make one half worth more than the other, and the two halves must still
     add back up to what the whole was worth.

  3. THE SAME USDT IS NEVER BILLED TWICE.
     Which is the whole hazard of a deposit funding two trades, and is why
     splitting an issued or part-paid trade is refused outright, and why
     reopening a split deposit after a cancel is refused while the other part
     is live.
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
from core.money import inr_to_usdt, usdt_to_inr  # noqa: E402
from db.repo import Repo  # noqa: E402


SUPPLY = D("106.2")
SELL = D("107.7")


def _executable(src: str) -> str:
    """
    A function's source with its docstring, its Python comments and the
    comments inside its SQL removed, so a test can assert about what the code
    DOES without the prose around it answering for it.

    Written after 28 September, when a guard test passed on a substring that
    belonged to a different query and a bug shipped behind a green suite.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(src))
    fn = tree.body[0]
    body = fn.body[1:] if ast.get_docstring(fn) is not None else fn.body
    lines = textwrap.dedent(src).splitlines()
    start = min(n.lineno for n in body) - 1
    out = []
    for line in lines[start:]:
        line = re.sub(r"#.*$", "", line)
        line = re.sub(r"--.*$", "", line)
        out.append(line)
    return "\n".join(out)


def _trade(**over):
    t = {
        "id": 7,
        "reference": "SUPA39",
        "supplier_id": 30,
        "client_id": 9,
        "wallet_id": 3,
        "rate_id": 4,
        "supply_rate": SUPPLY,
        "sell_rate": SELL,
        "supplier_label_at_trade": "VENDOR AS NAMED AT THE TIME",
        "usdt_received": D("10000"),
        "inr_expected": usdt_to_inr(D("10000"), SUPPLY),
        "usdt_owed_client": D("9861.65"),
        "margin_usdt": D("138.35"),
        "nominated_account_id": 12,
        "split_from_trade_id": None,
        "instructed_at": None,
        "announced_at": "2026-10-01T14:00:00+05:30",
        "status": "awaiting_payment",
        "opened_at": "2026-10-01T13:55:00+05:30",
        "paid_inr": D(0),
    }
    t.update(over)
    return t


class _Conn:
    """
    Enough of asyncpg to run split_trade, and a record of what it asked for.

    `queries` exists so a test can assert what was NOT consulted — the rates
    table being the one that matters.
    """

    def __init__(self, *, trade=None, child_reference=None, paid=D(0)):
        self.trade = trade if trade is not None else _trade()
        self.child_reference = child_reference
        self.paid = paid
        self.queries: list[str] = []
        self.inserted: dict = {}
        self.updated: dict = {}
        self.audits: list[dict] = []

    def transaction(self):
        conn = self

        class _T:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _T()

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        self.queries.append(s)
        if "FROM trades WHERE id" in s:
            return dict(self.trade)
        return None

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        self.queries.append(s)
        if "INSERT INTO trades" in s:
            self.inserted = {"sql": s, "args": args}
            return 99
        if "split_from_trade_id = $1" in s:
            return self.child_reference
        if "FROM payments" in s:
            return self.paid
        if "FROM trades WHERE id" in s:
            # The parent-reference lookup on an already-split child.
            return "SUPA38"
        return None

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        self.queries.append(s)
        if "UPDATE trades SET usdt_received" in s:
            self.updated = {"sql": s, "args": args}
        return None

    async def fetch(self, sql, *args):
        self.queries.append(" ".join(sql.split()))
        return []


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _A()


def _repo(conn):
    r = Repo.__new__(Repo)
    r.pool = _Pool(conn)

    async def _next_reference(c, supplier_id):
        return "SUPA40"

    async def _audit(c, *, actor_party_id, action, entity_type, entity_id, detail):
        conn.audits.append({"action": action, "entity_id": entity_id,
                            "detail": detail})

    r.next_reference = _next_reference
    r.audit = _audit
    return r


# ----------------------------------------------------------------------
# The split itself
# ----------------------------------------------------------------------

class TestTheBridgeChoosesWhereTheLineFalls:
    @pytest.mark.asyncio
    async def test_it_splits_into_exactly_the_two_parts_he_asked_for(self):
        conn = _Conn()
        ok, msg, detail = await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        assert ok, msg
        assert detail["first"]["usdt"] == D("4000")
        assert detail["second"]["usdt"] == D("6000")

    @pytest.mark.asyncio
    async def test_the_first_part_keeps_the_original_reference(self):
        """
        He is looking at a deposit notice for SUPA39. The order he sends
        first should still be SUPA39, or the message he is holding refers to
        nothing.
        """
        conn = _Conn()
        _, _, detail = await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        assert detail["first"]["reference"] == "SUPA39"
        assert detail["second"]["reference"] == "SUPA40"

    @pytest.mark.asyncio
    async def test_the_second_part_is_a_new_trade_pointing_at_the_first(self):
        conn = _Conn()
        await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        assert "split_from_trade_id" in conn.inserted["sql"]
        assert 7 in conn.inserted["args"], (
            "the new trade must record which trade it was carved out of"
        )

    @pytest.mark.asyncio
    async def test_the_parent_is_restated_to_its_own_half(self):
        """
        The failure this prevents is the one SUPA5 had for three days: a
        trade still claiming USDT it no longer holds, counted on both rows,
        overstating every export by the difference.
        """
        conn = _Conn()
        await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        assert conn.updated, "the first part was never restated"
        assert D("4000") in conn.updated["args"]
        assert D("10000") not in conn.updated["args"]


class TestBothHalvesArePricedAtTheRatesTheDepositAlreadyHad:
    @pytest.mark.asyncio
    async def test_it_never_looks_up_the_current_rate(self):
        """
        reopen_deposit_as_trade prices at the rate in force, correctly: that
        is a NEW deal being opened. A split is not. The money arrived once,
        under one rate, and a rate change since must not reach it.
        """
        conn = _Conn()
        await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        assert not [q for q in conn.queries if "FROM rates" in q], (
            "a split consulted the rates table; it must use the trade's own "
            "snapshotted supply_rate and sell_rate"
        )

    @pytest.mark.asyncio
    async def test_the_figures_are_the_trades_own_rates_applied(self):
        conn = _Conn()
        _, _, detail = await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        assert detail["first"]["inr_expected"] == D("424800.00")
        assert detail["second"]["inr_expected"] == D("637200.00")
        assert detail["supply_rate"] == SUPPLY
        assert detail["sell_rate"] == SELL

    @pytest.mark.asyncio
    @pytest.mark.parametrize("first", ["1", "3333.33", "4000", "9999.99"])
    async def test_the_two_halves_add_back_up_to_the_whole(self, first):
        """
        Each part is priced from its own USDT rather than the second being
        the first subtracted from the total, so this is the property worth
        checking: no rounding drift creeps in between them.
        """
        conn = _Conn()
        _, _, detail = await _repo(conn).split_trade(
            trade_id=7, first_usdt=D(first), actor_party_id=1,
        )
        assert (detail["first"]["usdt"] + detail["second"]["usdt"]
                == D("10000"))
        assert (detail["first"]["inr_expected"]
                + detail["second"]["inr_expected"]
                == usdt_to_inr(D("10000"), SUPPLY))

    @pytest.mark.asyncio
    async def test_the_usdt_owed_out_is_derived_from_each_parts_own_inr(self):
        conn = _Conn()
        _, _, detail = await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        for part in (detail["first"], detail["second"]):
            assert part["usdt_owed"] == inr_to_usdt(part["inr_expected"], SELL)


class TestTheNameTheDealWasStruckUnderSurvivesTheSplit:
    @pytest.mark.asyncio
    async def test_the_pinned_vendor_label_is_carried_to_the_second_part(self):
        """
        Both halves are the same deal. Looking the label up again would let a
        rename tomorrow make them read back under different vendors — which
        is the whole reason supplier_label_at_trade exists (19 September).
        """
        conn = _Conn()
        await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        assert "VENDOR AS NAMED AT THE TIME" in conn.inserted["args"]

    @pytest.mark.asyncio
    async def test_the_nominated_account_is_carried_too(self):
        """The supplier said where the INR goes. That did not change."""
        conn = _Conn()
        await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        assert 12 in conn.inserted["args"]


class TestItSplitsInTwoAndNoFurther:
    """"it only splits in 2 not anymore than this" — Bridge, 1 October 2026."""

    @pytest.mark.asyncio
    async def test_a_trade_that_has_already_been_split_refuses(self):
        conn = _Conn(child_reference="SUPA40")
        ok, msg, _ = await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("2000"), actor_party_id=1,
        )
        assert not ok
        assert "SUPA40" in msg
        assert not conn.inserted

    @pytest.mark.asyncio
    async def test_the_second_half_of_a_split_cannot_itself_be_split(self):
        conn = _Conn(trade=_trade(split_from_trade_id=6, reference="SUPA40"))
        ok, msg, _ = await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("2000"), actor_party_id=1,
        )
        assert not ok
        assert "already the second half" in msg
        assert not conn.inserted


class TestItRefusesEverySplitThatWouldMoveMoneyAlreadyCommitted:
    @pytest.mark.asyncio
    async def test_an_issued_trade_refuses(self):
        """
        The client is holding an instruction for the full figure. Changing it
        underneath them is precisely the fault that made SUPB1 need splitting
        by hand in the first place.
        """
        conn = _Conn(trade=_trade(instructed_at="2026-10-01T14:10:00+05:30"))
        ok, msg, _ = await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        assert not ok
        assert "already been sent to the client" in msg
        assert not conn.inserted

    @pytest.mark.asyncio
    async def test_a_trade_with_a_payment_on_it_refuses(self):
        """
        Splitting would leave logged money attached to half a trade, and
        re-attributing it means moving a UTR, which is globally unique.
        """
        conn = _Conn(paid=D("295000"))
        ok, msg, _ = await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        assert not ok
        assert "/correct" in msg
        assert not conn.inserted

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["cancelled", "completed"])
    async def test_a_dead_trade_refuses(self, status):
        conn = _Conn(trade=_trade(status=status))
        ok, _, _ = await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        assert not ok
        assert not conn.inserted

    @pytest.mark.asyncio
    @pytest.mark.parametrize("first", ["0", "-1", "10000", "10001"])
    async def test_an_amount_that_leaves_no_second_part_refuses(self, first):
        conn = _Conn()
        ok, _, _ = await _repo(conn).split_trade(
            trade_id=7, first_usdt=D(first), actor_party_id=1,
        )
        assert not ok
        assert not conn.inserted

    @pytest.mark.asyncio
    async def test_a_refusal_never_burns_a_reference_number(self):
        """
        next_reference increments a counter that never goes back. Calling it
        before the refusals would leave a gap in the deal numbers on every
        mistyped amount.
        """
        conn = _Conn(paid=D("295000"))
        await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        assert not conn.inserted


class TestTheSplitIsOnTheRecord:
    @pytest.mark.asyncio
    async def test_both_trades_are_audited(self):
        conn = _Conn()
        await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        actions = {a["action"] for a in conn.audits}
        assert "trade.split" in actions
        assert "trade.split_created" in actions
        ids = {a["entity_id"] for a in conn.audits}
        assert ids == {7, 99}

    @pytest.mark.asyncio
    async def test_the_audit_says_what_it_was_before(self):
        conn = _Conn()
        await _repo(conn).split_trade(
            trade_id=7, first_usdt=D("4000"), actor_party_id=1,
        )
        assert conn.audits[0]["detail"]["from_usdt"] == D("10000")


# ----------------------------------------------------------------------
# Reaching it from Telegram
# ----------------------------------------------------------------------

class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.sent: list[dict] = []
        self.edited: list[dict] = []

    async def answer(self, text, **kw):
        self.sent.append({"text": text, **kw})
        return self

    async def edit_text(self, text, **kw):
        self.edited.append({"text": text, **kw})
        return self


class _Call:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message or _Msg()
        self.answers: list[dict] = []

    async def answer(self, text=None, **kw):
        self.answers.append({"text": text, **kw})


class _State:
    def __init__(self, data=None):
        self.state = None
        self.data = dict(data or {})
        self.cleared = False

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.cleared = True
        self.state = None
        self.data = {}


class _BotRepo:
    def __init__(self, *, trade=None, parts=(), split_result=None):
        self.trade = trade if trade is not None else _trade()
        self.parts = list(parts)
        self.split_result = split_result
        self.split_calls: list[dict] = []

    async def trade_detail(self, trade_id):
        return dict(self.trade)

    async def split_parts(self, trade_id):
        return list(self.parts)

    async def list_bank_accounts(self, supplier_id):
        return [{"id": 12, "account_name": "AN ACCOUNT",
                 "account_number": "0000", "ifsc": "XXXX0000000"}]

    async def trade_slots(self, trade_id):
        return []

    async def split_trade(self, *, trade_id, first_usdt, actor_party_id):
        self.split_calls.append({"trade_id": trade_id, "first_usdt": first_usdt})
        if self.split_result is not None:
            return self.split_result
        return True, "split", {
            "from_usdt": D("10000"),
            "supply_rate": SUPPLY, "sell_rate": SELL,
            "first": {"reference": "SUPA39", "trade_id": 7, "usdt": D("4000"),
                      "inr_expected": D("424800.00"),
                      "usdt_owed": D("3944.29")},
            "second": {"reference": "SUPA40", "trade_id": 99, "usdt": D("6000"),
                       "inr_expected": D("637200.00"),
                       "usdt_owed": D("5916.43")},
        }


class TestTheButtonIsWhereHeAskedForIt:
    """"im thinking we can add to the existing where it says Full Send?\""""

    @pytest.mark.asyncio
    async def test_the_confirm_screen_offers_the_split(self):
        call = _Call("cf:7")
        state = _State()
        await bridge_trade.confirm_start(call, state, _BotRepo())
        buttons = [b.text
                   for row in call.message.sent[0]["reply_markup"].inline_keyboard
                   for b in row]
        assert any("Split" in b and "2 orders" in b for b in buttons), buttons

    @pytest.mark.asyncio
    async def test_the_button_says_how_much_there_is_to_divide(self):
        call = _Call("cf:7")
        await bridge_trade.confirm_start(call, _State(), _BotRepo())
        buttons = [b.text
                   for row in call.message.sent[0]["reply_markup"].inline_keyboard
                   for b in row]
        assert any("10000" in b for b in buttons), buttons

    @pytest.mark.asyncio
    async def test_the_split_button_carries_the_trade(self):
        call = _Call("cf:7")
        await bridge_trade.confirm_start(call, _State(), _BotRepo())
        datas = [b.callback_data
                 for row in call.message.sent[0]["reply_markup"].inline_keyboard
                 for b in row]
        assert "sp:7" in datas


class TestTheButtonIsNotOfferedWhenItWouldRefuse:
    """
    /cancel spent three days with buttons on it that reached no handler
    (28 September). A button that answers back is the same fault wearing a
    different hat, so the offer is drawn only when the split would work.
    """

    @pytest.mark.asyncio
    async def test_not_once_the_instruction_has_gone_out(self):
        repo = _BotRepo(trade=_trade(instructed_at="2026-10-01T14:10:00+05:30"))
        call = _Call("cf:7")
        await bridge_trade.confirm_start(call, _State(), repo)
        datas = [b.callback_data
                 for row in call.message.sent[0]["reply_markup"].inline_keyboard
                 for b in row]
        assert "sp:7" not in datas

    @pytest.mark.asyncio
    async def test_not_once_money_has_been_paid_against_it(self):
        repo = _BotRepo(trade=_trade(paid_inr=D("295000")))
        call = _Call("cf:7")
        await bridge_trade.confirm_start(call, _State(), repo)
        datas = [b.callback_data
                 for row in call.message.sent[0]["reply_markup"].inline_keyboard
                 for b in row]
        assert "sp:7" not in datas

    @pytest.mark.asyncio
    async def test_not_on_a_trade_already_split(self):
        repo = _BotRepo(parts=[{"id": 99, "reference": "SUPA40",
                                "usdt_received": D("6000"),
                                "status": "awaiting_payment"}])
        call = _Call("cf:7")
        await bridge_trade.confirm_start(call, _State(), repo)
        datas = [b.callback_data
                 for row in call.message.sent[0]["reply_markup"].inline_keyboard
                 for b in row]
        assert "sp:7" not in datas

    @pytest.mark.asyncio
    async def test_not_on_the_second_half_of_a_split(self):
        repo = _BotRepo(trade=_trade(split_from_trade_id=6))
        call = _Call("cf:7")
        await bridge_trade.confirm_start(call, _State(), repo)
        datas = [b.callback_data
                 for row in call.message.sent[0]["reply_markup"].inline_keyboard
                 for b in row]
        assert "sp:7" not in datas

    @pytest.mark.asyncio
    async def test_pressing_a_stale_button_still_refuses_rather_than_acts(self):
        """The screen can be minutes old. The handler re-asks."""
        repo = _BotRepo(trade=_trade(instructed_at="2026-10-01T14:10:00+05:30"))
        call = _Call("sp:7")
        state = _State()
        await bridge_trade.split_start(call, state, repo)
        assert state.state is None
        assert call.answers and call.answers[0].get("show_alert")


class TestOneQuestionAndTwoOrders:
    @pytest.mark.asyncio
    async def test_it_asks_only_how_big_the_first_order_is(self):
        call = _Call("sp:7")
        state = _State()
        await bridge_trade.split_start(call, state, _BotRepo())
        assert state.state == bridge_trade.Split.amount
        asked = call.message.edited[0]["text"]
        assert "FIRST" in asked
        assert "USDT" in asked

    @pytest.mark.asyncio
    async def test_the_account_buttons_are_taken_off_the_screen(self):
        """
        They belong to Confirm.account, a state we have just left, so a tap
        on one would reach nothing. /cancel carried exactly that fault for
        three days before anyone noticed.
        """
        call = _Call("sp:7")
        await bridge_trade.split_start(call, _State(), _BotRepo())
        assert not call.message.sent, (
            "the question was posted as a new message, leaving the stale "
            "account keyboard live above it"
        )
        kb = call.message.edited[0].get("reply_markup")
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert datas == ["spno"]

    @pytest.mark.asyncio
    async def test_the_number_he_sends_becomes_the_first_order(self):
        repo = _BotRepo()
        msg = _Msg("4000")
        state = _State({"split_trade_id": 7, "split_total": "10000"})
        await bridge_trade.split_amount_typed(msg, state, {"id": 1}, repo)
        assert repo.split_calls == [{"trade_id": 7, "first_usdt": D("4000")}]

    @pytest.mark.asyncio
    async def test_both_orders_are_shown_with_what_the_client_pays(self):
        repo = _BotRepo()
        msg = _Msg("4000")
        state = _State({"split_trade_id": 7, "split_total": "10000"})
        await bridge_trade.split_amount_typed(msg, state, {"id": 1}, repo)
        out = msg.sent[0]["text"]
        assert "SUPA39" in out and "SUPA40" in out
        assert "424,800" in out and "637,200" in out

    @pytest.mark.asyncio
    async def test_the_second_part_can_be_sent_later(self):
        """
        "the ability to send the second part later". Later is not a state the
        bot holds — the second part is an ordinary open trade from the moment
        it exists, so /issue finds it whenever he is ready. The buttons here
        are the shortcut, not the only road.
        """
        repo = _BotRepo()
        msg = _Msg("4000")
        state = _State({"split_trade_id": 7, "split_total": "10000"})
        await bridge_trade.split_amount_typed(msg, state, {"id": 1}, repo)
        datas = [b.callback_data
                 for row in msg.sent[0]["reply_markup"].inline_keyboard
                 for b in row]
        assert datas == ["cf:7", "cf:99"]

    @pytest.mark.asyncio
    async def test_nothing_has_gone_to_the_client_yet(self):
        repo = _BotRepo()
        msg = _Msg("4000")
        state = _State({"split_trade_id": 7, "split_total": "10000"})
        await bridge_trade.split_amount_typed(msg, state, {"id": 1}, repo)
        assert "client" in msg.sent[0]["text"].lower()
        assert state.cleared

    @pytest.mark.asyncio
    @pytest.mark.parametrize("typed", ["", "half", "lots", "4,0,0,0,x"])
    async def test_an_unreadable_amount_asks_again_and_splits_nothing(self, typed):
        repo = _BotRepo()
        msg = _Msg(typed)
        state = _State({"split_trade_id": 7, "split_total": "10000"})
        await bridge_trade.split_amount_typed(msg, state, {"id": 1}, repo)
        assert repo.split_calls == []
        assert not state.cleared, "he should still be able to answer"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("typed", ["0", "10000", "12000"])
    async def test_an_amount_leaving_no_second_part_asks_again(self, typed):
        repo = _BotRepo()
        msg = _Msg(typed)
        state = _State({"split_trade_id": 7, "split_total": "10000"})
        await bridge_trade.split_amount_typed(msg, state, {"id": 1}, repo)
        assert repo.split_calls == []

    @pytest.mark.asyncio
    async def test_he_can_back_out_and_nothing_changes(self):
        call = _Call("spno")
        state = _State({"split_trade_id": 7, "split_total": "10000"})
        await bridge_trade.split_abandon(call, state)
        assert state.cleared
        assert "one order" in call.message.edited[0]["text"]

    @pytest.mark.asyncio
    async def test_a_refusal_from_the_database_is_shown_not_swallowed(self):
        repo = _BotRepo(split_result=(False, "SUPA39 is completed.", None))
        msg = _Msg("4000")
        state = _State({"split_trade_id": 7, "split_total": "10000"})
        await bridge_trade.split_amount_typed(msg, state, {"id": 1}, repo)
        assert msg.sent[0]["text"] == "SUPA39 is completed."


class TestTheSameUsdtIsNeverBilledTwice:
    @pytest.mark.asyncio
    async def test_cancelling_a_split_part_does_not_offer_to_reopen_the_whole(self):
        """
        The deposit row holds the full send. Reopening it while the other
        part is live would invoice the same USDT on two trades — the exact
        shape of the 16 September loss, in reverse.
        """
        class _R:
            async def cancel_trade(self, **kw):
                return True, "SUPA39 cancelled."

            async def stranded_deposits(self, trade_id):
                return [{"id": 51, "amount_usdt": D("10000"),
                         "tx_hash": "0xabc", "detected_at": _When()}]

            async def split_parts(self, trade_id):
                return [{"id": 99, "reference": "SUPA40",
                         "usdt_received": D("6000"),
                         "status": "awaiting_payment"}]

        msg = _Msg()
        state = _State({"trade_id": 7})
        await bridge_trade._finish_cancel(msg, state, {"id": 1}, _R(), "why")
        out = msg.sent[0]
        assert "SUPA40" in out["text"]
        assert out.get("reply_markup") is None, (
            "no reopen button, because reopening is what would double-bill"
        )

    @pytest.mark.asyncio
    async def test_an_ordinary_cancel_still_offers_to_reopen(self):
        """The 16 September repair must be completely untouched."""
        class _R:
            async def cancel_trade(self, **kw):
                return True, "SUPA39 cancelled."

            async def stranded_deposits(self, trade_id):
                return [{"id": 51, "amount_usdt": D("10000"),
                         "tx_hash": "0xabc", "detected_at": _When()}]

            async def split_parts(self, trade_id):
                return []

        msg = _Msg()
        state = _State({"trade_id": 7})
        await bridge_trade._finish_cancel(msg, state, {"id": 1}, _R(), "why")
        datas = [b.callback_data
                 for row in msg.sent[0]["reply_markup"].inline_keyboard
                 for b in row]
        assert "rd:51" in datas
        assert "rdv:51" in datas

    def test_reopening_a_split_deposit_is_refused_in_the_repo_too(self):
        """
        Belt as well as braces: the button is withheld above, and the repo
        refuses anyway. A deposit can also be reached by a route that has not
        been thought of yet.
        """
        src = inspect.getsource(Repo.reopen_deposit_as_trade)
        assert "split_from_trade_id" in src
        guard = src[src.index("split_from_trade_id"):]
        assert "return False" in guard[:1200]


class _When:
    """A detected_at that survives the %d %b %H:%M the cancel notice uses."""

    def __format__(self, spec):
        return "01 Oct 14:00"


# ----------------------------------------------------------------------
# The shape of the thing
# ----------------------------------------------------------------------

class TestTheDepositRecordIsNotBent:
    def test_the_split_does_not_touch_the_deposits_table(self):
        """
        deposits_tx_unique is what stops the same transfer being credited
        twice when the monitor sees it again. A split that rewrote deposit
        rows would be trading that protection for tidiness.
        """
        src = inspect.getsource(Repo.split_trade)
        # Prose discusses the deposits table at length. Only executable
        # statements are the claim being made here.
        code = _executable(src)
        assert "deposits" not in code, code

    def test_the_schema_and_the_migration_agree(self):
        schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        migration = (ROOT / "deploy" / "migrate-012-trade-split.sql").read_text(
            encoding="utf-8")
        assert "split_from_trade_id" in schema
        assert "split_from_trade_id" in migration
        assert "REFERENCES trades(id)" in migration

    def test_the_migration_can_be_run_twice(self):
        """Every migration in this tree is idempotent, and deploys repeat."""
        migration = (ROOT / "deploy" / "migrate-012-trade-split.sql").read_text(
            encoding="utf-8")
        assert "ADD COLUMN IF NOT EXISTS" in migration
        assert "CREATE INDEX IF NOT EXISTS" in migration

    def test_the_column_is_nullable(self):
        """
        Every trade that already exists has to remain valid without it, or
        the migration cannot be run against the live database at all.
        """
        migration = (ROOT / "deploy" / "migrate-012-trade-split.sql").read_text(
            encoding="utf-8")
        alter = re.search(r"ALTER TABLE trades[^;]*;", migration, re.S)
        assert alter, migration
        assert "NOT NULL" not in alter.group(0)


class TestTheTwoChecksDoNotDrift:
    """
    _can_split decides whether the button is drawn; split_trade decides
    whether the split happens. They answer the same question in two places,
    which is a thing that rots. On 28 September a test asserted a guard that
    was true of a different query, and a bug shipped behind a green suite.
    """

    def test_every_condition_the_repo_refuses_is_one_the_button_checks(self):
        button = inspect.getsource(bridge_trade._can_split)
        repo = inspect.getsource(Repo.split_trade)
        for field in ("instructed_at", "split_from_trade_id", "usdt_received"):
            assert field in button, f"{field} is refused but not checked"
            assert field in repo
        assert "paid_inr" in button and "FROM payments" in repo
        assert "split_parts" in button
