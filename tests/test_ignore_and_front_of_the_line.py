"""
Two things the Bridge asked for on 16 October 2026.

    can you move uncle 30 to the front of the line?
    Sorry just for this, and i think an ignore button and the option to
    switch to different deal may be ideal

IGNORE — a question with no acceptable answer

15 October: ₹243,000 arrived named "INDURKAR TEXTILES PRIVATE LIMITED", an
account nobody has registered. There was nothing to place it on, and a held
payment had exactly one ending: placed on an order.

    they sent an incorrect trade to our channel, but now, how would i remove
    this

So it sat on the waiting list being chased, for ever, over money that was
never his. A chaser nobody can satisfy is one he learns to ignore, which
defeats the only job it has — and that chaser is what stands between a
forgotten payment and a lost one.

FRONT OF THE LINE — deciding before the money arrives

The path could only be set by answering a question when a payment happened to
turn up. That is the wrong way round. He knows which order he wants filled
next before the money lands, and waiting for a payment to ask him means the
first of a batch goes wherever the last decision pointed.

/collect lets him say so in advance. Same mechanism underneath — it writes
the same path a prompt would — so there is one way a path gets set, not two.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import bridge_trade, menu, notifier as notifier_mod  # noqa: E402
from db.repo import Repo  # noqa: E402


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------

class _Msg:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def answer(self, t, **kw): self.sent.append({"text": t, **kw})
    async def edit_text(self, t, **kw): self.edited.append({"text": t, **kw})


class _Call:
    def __init__(self, data):
        self.data = data
        self.message = _Msg()
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append({"text": text, **kw})


class _State:
    def __init__(self, data=None):
        self.state = None
        self.data = dict(data or {})
        self.cleared = False

    async def set_state(self, s): self.state = s
    async def update_data(self, **kw): self.data.update(kw)
    async def get_data(self): return dict(self.data)
    async def clear(self): self.cleared = True; self.state = None


class _Pool:
    def acquire(self):
        class _A:
            async def __aenter__(self):
                class _C:
                    async def fetchval(self, *a, **k): return 3
                return _C()
            async def __aexit__(self, *a): return False
        return _A()


class _Repo:
    def __init__(self, *, accounts=(), trades=(), path=None, dismiss=(True, "ok")):
        self.pool = _Pool()
        self.accounts = list(accounts)
        self.trades = list(trades)
        self.path = path
        self.dismiss_result = dismiss
        self.dismissed = []
        self.paths_set = []

    async def shared_accounts_with_live_orders(self, client_id):
        return list(self.accounts)

    async def live_trades_on_account(self, **kw): return list(self.trades)

    async def collection_path(self, **kw):
        return {"trade_id": self.path} if self.path else None

    async def set_collection_path(self, **kw): self.paths_set.append(kw)

    async def trade_detail(self, tid):
        return {"id": tid, "reference": f"BRAV{tid}",
                "status": "awaiting_payment"}

    async def dismiss_held_payment(self, *, held_id, actor_party_id, reason):
        self.dismissed.append({"held_id": held_id, "reason": reason})
        return self.dismiss_result


def _acct(n="ROYAL TRADING COMPANY", number="50200000000000", orders=2,
          current=None):
    return {"account_number": number, "ifsc": "XXXX0000000",
            "account_name": n, "live_orders": orders,
            "current_trade_id": current}


def _order(tid, ref, outstanding=D("1000000")):
    return {"id": tid, "reference": ref, "vendor": "A VENDOR",
            "outstanding": outstanding, "account_id": 28,
            "supplier_id": 30, "collected": D(0),
            "inr_expected": D("2000000"), "opened_at": None}


# ----------------------------------------------------------------------
# Ignore
# ----------------------------------------------------------------------

class TestIgnoringAPaymentThatIsNotOurs:
    @pytest.mark.asyncio
    async def test_it_is_set_aside(self):
        repo = _Repo(dismiss=(True, "UTR set aside."))
        call = _Call("hpx:7")
        await bridge_trade.dismiss_held_payment(call, {"id": 1}, repo)
        assert repo.dismissed == [
            {"held_id": 7, "reason": "dismissed from the prompt as not ours"}
        ]
        assert "set aside" in call.message.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_tapping_twice_does_not_undo_anything(self):
        """
        The prompt outlives its context — it can be tapped hours later, after
        a restart, and possibly after it was already placed on an order.
        """
        repo = _Repo(dismiss=(False, "already been dealt with"))
        call = _Call("hpx:7")
        await bridge_trade.dismiss_held_payment(call, {"id": 1}, repo)
        assert "already been dealt with" in call.message.sent[0]["text"]

    def test_the_button_is_offered_when_he_is_asked(self):
        src = inspect.getsource(notifier_mod.Notifier.ask_which_order)
        assert "hpx:" in src
        assert "Not ours" in src

    def test_the_button_is_offered_when_he_is_chased(self):
        src = inspect.getsource(notifier_mod.Notifier.chase_held_payments)
        assert "hpx:" in src

    def test_it_is_offered_even_with_nothing_to_place_it_on(self):
        """
        The case that caused it. An unregistered account has no order to
        offer, and that is exactly when he needs to be able to say no.
        """
        src = inspect.getsource(notifier_mod.Notifier.chase_held_payments)
        after = src[src.index("There is no open order"):]
        assert "hpx:" in after

    def test_the_handler_is_not_state_gated(self):
        src = (ROOT / "bot" / "bridge_trade.py").read_text(encoding="utf-8")
        line = [l for l in src.splitlines() if 'startswith("hpx:")' in l]
        assert line and "StatesGroup" not in line[0]

    def test_nothing_is_deleted_and_no_payment_is_created(self):
        """
        It is a decision about money, recorded as one. A figure that never
        reached the ledger still has to be accountable later.
        """
        # Executable code only. The prose explains that nothing is deleted,
        # which means the word "deleted" appears in it — a scan of the raw
        # source finds the explanation and calls it the offence.
        import ast
        import textwrap
        fn = ast.parse(textwrap.dedent(
            inspect.getsource(Repo.dismiss_held_payment))).body[0]
        body = fn.body[1:] if ast.get_docstring(fn) else fn.body
        src = "\n".join(
            textwrap.dedent(inspect.getsource(Repo.dismiss_held_payment))
            .splitlines()[min(n.lineno for n in body) - 1:])

        assert "DELETE" not in src.upper()
        assert "INSERT INTO payments" not in src
        assert "payment.dismissed" in src
        assert "resolved_trade_id = NULL" in src

    def test_it_refuses_one_already_dealt_with(self):
        src = inspect.getsource(Repo.dismiss_held_payment)
        assert 'h["resolved_at"] is not None' in src


# ----------------------------------------------------------------------
# Front of the line
# ----------------------------------------------------------------------

class TestMovingAnOrderToTheFrontOfTheLine:
    @pytest.mark.asyncio
    async def test_it_offers_only_accounts_with_a_real_choice(self):
        repo = _Repo(accounts=[_acct()])
        msg = _Msg()
        await bridge_trade.cmd_collect(msg, _State(), repo, {"id": 1})
        buttons = [b.text for row in
                   msg.sent[0]["reply_markup"].inline_keyboard for b in row]
        assert any("ROYAL TRADING COMPANY" in b for b in buttons)

    @pytest.mark.asyncio
    async def test_with_nothing_shared_it_says_so_plainly(self):
        repo = _Repo(accounts=[])
        msg = _Msg()
        await bridge_trade.cmd_collect(msg, _State(), repo, {"id": 1})
        assert "nothing to choose between" in msg.sent[0]["text"]
        assert "reply_markup" not in msg.sent[0]

    @pytest.mark.asyncio
    async def test_the_current_choice_is_marked(self):
        repo = _Repo(trades=[_order(30, "BRAV30"), _order(31, "SUPA31")],
                     path=30)
        call = _Call("cl:50200000000000:XXXX0000000")
        await bridge_trade.collect_pick_account(call, _State(), repo)
        buttons = [b.text for row in
                   call.message.edited[0]["reply_markup"].inline_keyboard
                   for b in row]
        assert any(b.startswith("✓ BRAV30") for b in buttons), buttons

    @pytest.mark.asyncio
    async def test_with_no_choice_yet_it_says_the_next_payment_will_ask(self):
        repo = _Repo(trades=[_order(30, "BRAV30"), _order(31, "SUPA31")])
        call = _Call("cl:50200000000000:XXXX0000000")
        await bridge_trade.collect_pick_account(call, _State(), repo)
        assert "will ask" in call.message.edited[0]["text"]

    @pytest.mark.asyncio
    async def test_choosing_sets_the_path(self):
        """The whole point: decided before the money arrives."""
        repo = _Repo(trades=[_order(30, "BRAV30")])
        state = _State({"account_number": "50200000000000",
                        "ifsc": "XXXX0000000", "client_id": 3})
        call = _Call("clt:30")
        await bridge_trade.collect_pick_trade(call, state, {"id": 1}, repo)
        assert len(repo.paths_set) == 1
        assert repo.paths_set[0]["trade_id"] == 30
        assert "first in line" in call.message.edited[0]["text"]

    @pytest.mark.asyncio
    async def test_it_refuses_an_order_that_has_closed(self):
        repo = _Repo(trades=[_order(30, "BRAV30")])

        async def _closed(tid):
            return {"id": tid, "reference": "BRAV30", "status": "completed"}

        repo.trade_detail = _closed
        call = _Call("clt:30")
        await bridge_trade.collect_pick_trade(
            call, _State({"account_number": "x", "ifsc": "y", "client_id": 3}),
            {"id": 1}, repo)
        assert repo.paths_set == []
        assert call.answers[0].get("show_alert")

    @pytest.mark.asyncio
    async def test_an_account_no_longer_collecting_is_handled(self):
        repo = _Repo(trades=[])
        call = _Call("cl:50200000000000:XXXX0000000")
        state = _State()
        await bridge_trade.collect_pick_account(call, state, repo)
        assert "any more" in call.message.edited[0]["text"]
        assert state.cleared


class TestThePathIsSetOneWay:
    def test_the_command_writes_the_same_path_a_prompt_does(self):
        """
        Two ways to set it would be two things to keep in step. The command
        and the prompt both call set_collection_path.
        """
        for fn in (bridge_trade.collect_pick_trade,
                   bridge_trade.place_held_payment):
            assert "set_collection_path" in inspect.getsource(fn), fn.__name__

    def test_only_genuinely_shared_accounts_are_offered(self):
        sql = inspect.getsource(Repo.shared_accounts_with_live_orders)
        assert "HAVING count(DISTINCT t.id) > 1" in sql

    def test_it_only_counts_accounts_an_order_was_issued_against(self):
        """
        Same rule as everywhere else: a deal is a supplier AND an account.
        """
        sql = inspect.getsource(Repo.shared_accounts_with_live_orders)
        assert "JOIN payment_slots ps" in sql

    def test_the_command_is_in_the_menu(self):
        """
        /issue was invisible for eight days because it was not advertised.
        """
        bridge = dict(menu.MENUS["bridge"])
        assert "collect" in bridge
        assert bridge["collect"]
