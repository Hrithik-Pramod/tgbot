"""
Fixing a mistake must be as reachable as making one.

THE NIGHT THAT PRODUCED THIS — 4-5 October 2026

One evening of ordinary trading found four holes in the correction side, and
every one of them was worked around by hand:

  a payment on a LIVE order could not be touched
        /correct listed completed trades only. ₹255,000 sat on the wrong
        vendor's open order and there was no way in.

            Supa44 not on list
            also on /correct, it only shows list of completed, not live

  there was no way to move a payment
        Promised on 1 October as the backstop for exactly this, never built.
        It was done with SQL written against the live ledger at 00:08.

  removing a payment left the order saying it was paid
        BRAV14 was ₹255,000 short and still marked completed. Nothing on
        screen contradicted it, and no confirmation could ever fire again
        because a completed order cannot complete twice. He found it:

            actually no it doesnt, can we ensure that it does in future?

  a returned payment had no name
        A bounce is not a typo. The money went back, and an export showing a
        trade short means nothing without knowing which of the two happened.

            Also - One returned ( bounced back) ... so how do we deal with
            something like this.

THE THING BEING FIXED IS NOT THE FOUR SYMPTOMS

The recording path has been hardened for a month. The correcting path had
never been used in anger, so none of this had been found. The rule these
tests hold is that money can always be put right from inside the bot, with a
reason attached, by the person who noticed — never with SQL at midnight.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import bridge_trade  # noqa: E402
from db.repo import Repo  # noqa: E402


# ----------------------------------------------------------------------
# A fake connection good enough for the correction paths
# ----------------------------------------------------------------------

def _trade(tid, ref, status="awaiting_payment", expected=D("3012885"),
           paid=D("3012885"), supplier_id=30):
    return {"id": tid, "reference": ref, "status": status,
            "inr_expected": expected, "paid": paid,
            "supplier_id": supplier_id}


class _Conn:
    def __init__(self, trades, payment=None, *, same_account=None):
        self.trades = {t["id"]: dict(t) for t in trades}
        self.payment = payment
        self.same_account = same_account
        self.audits: list[dict] = []
        self.execs: list[tuple] = []

    def transaction(self):
        conn = self

        class _T:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _T()

    def _by_ref(self, ref):
        return next((t for t in self.trades.values()
                     if t["reference"] == ref), None)

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if "FROM payments p" in s and "WHERE p.id" in s:
            return dict(self.payment) if self.payment else None
        if "FROM trades t WHERE t.id" in s or "FROM trades WHERE id" in s:
            t = self.trades.get(args[0])
            return dict(t) if t else None
        return None

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if "FROM bank_accounts" in s:
            return self.same_account
        return None

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        self.execs.append((s, args))
        if "UPDATE trades SET status = 'awaiting_payment'" in s:
            self.trades[args[0]]["status"] = "awaiting_payment"
        if "UPDATE payments SET trade_id" in s:
            self.payment["trade_id"] = args[1]
        if "DELETE FROM payments" in s:
            tid = self.payment["trade_id"]
            self.trades[tid]["paid"] -= self.payment["amount_inr"]
            self.payment = None
        return None

    async def fetch(self, sql, *args):
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

    async def _audit(c, *, actor_party_id, action, entity_type, entity_id, detail):
        conn.audits.append({"action": action, "entity_id": entity_id,
                            "detail": detail})

    r.audit = _audit
    return r


# ----------------------------------------------------------------------
# Taking money off an order
# ----------------------------------------------------------------------

class TestRemovingAPaymentReopensTheOrder:
    @pytest.mark.asyncio
    async def test_a_completed_order_that_is_now_short_reopens(self):
        """BRAV14, exactly: ₹255,000 bounced off a completed order."""
        conn = _Conn(
            [_trade(14, "BRAV14", status="completed")],
            payment={"id": 5, "trade_id": 14, "utr": "U1",
                     "amount_inr": D("255000"), "reference": "BRAV14"},
        )
        ok, msg = await _repo(conn).void_payment(
            payment_id=5, actor_party_id=1, reason="bounced", returned=True,
        )
        assert ok, msg
        assert conn.trades[14]["status"] == "awaiting_payment"
        assert "open again" in msg

    @pytest.mark.asyncio
    async def test_an_order_still_covered_is_left_closed(self):
        """
        Removing a duplicate from an overpaid order does not reopen it.
        Closing a trade is claim_completion's job and two things deciding a
        trade is finished is two things that can disagree.
        """
        conn = _Conn(
            [_trade(14, "BRAV14", status="completed", paid=D("3300000"))],
            payment={"id": 5, "trade_id": 14, "utr": "U1",
                     "amount_inr": D("255000"), "reference": "BRAV14"},
        )
        ok, msg = await _repo(conn).void_payment(
            payment_id=5, actor_party_id=1, reason="entered twice",
        )
        assert ok
        assert conn.trades[14]["status"] == "completed"
        assert "open again" not in msg

    @pytest.mark.asyncio
    async def test_an_order_that_was_never_closed_is_untouched(self):
        conn = _Conn(
            [_trade(14, "BRAV14", paid=D("1000000"))],
            payment={"id": 5, "trade_id": 14, "utr": "U1",
                     "amount_inr": D("255000"), "reference": "BRAV14"},
        )
        await _repo(conn).void_payment(
            payment_id=5, actor_party_id=1, reason="typo",
        )
        assert not [e for e in conn.execs
                    if "UPDATE trades SET status" in e[0]]


class TestAReturnedPaymentIsNotATypo:
    @pytest.mark.asyncio
    async def test_it_is_recorded_as_returned(self):
        """
        An export showing an order ₹255,000 short means nothing without
        knowing whether someone typed it wrong or the bank sent it back.
        """
        conn = _Conn(
            [_trade(14, "BRAV14", status="completed")],
            payment={"id": 5, "trade_id": 14, "utr": "U1",
                     "amount_inr": D("255000"), "reference": "BRAV14"},
        )
        await _repo(conn).void_payment(
            payment_id=5, actor_party_id=1, reason="bank returned it",
            returned=True,
        )
        actions = [a["action"] for a in conn.audits]
        assert "payment.returned" in actions
        assert "payment.void" not in actions

    @pytest.mark.asyncio
    async def test_a_mistake_is_still_recorded_as_a_mistake(self):
        conn = _Conn(
            [_trade(14, "BRAV14", status="completed")],
            payment={"id": 5, "trade_id": 14, "utr": "U1",
                     "amount_inr": D("255000"), "reference": "BRAV14"},
        )
        await _repo(conn).void_payment(
            payment_id=5, actor_party_id=1, reason="typo",
        )
        actions = [a["action"] for a in conn.audits]
        assert "payment.void" in actions
        assert "payment.returned" not in actions

    @pytest.mark.asyncio
    async def test_the_reason_survives_either_way(self):
        conn = _Conn(
            [_trade(14, "BRAV14", status="completed")],
            payment={"id": 5, "trade_id": 14, "utr": "U1",
                     "amount_inr": D("255000"), "reference": "BRAV14"},
        )
        await _repo(conn).void_payment(
            payment_id=5, actor_party_id=1, reason="bounced at the bank",
            returned=True,
        )
        assert conn.audits[0]["detail"]["reason"] == "bounced at the bank"
        assert conn.audits[0]["detail"]["returned_by_bank"] is True


# ----------------------------------------------------------------------
# Moving money to the order it belongs to
# ----------------------------------------------------------------------

class TestMovingAPayment:
    def _conn(self, **over):
        payment = {"id": 5, "trade_id": 44, "utr": "UTR000000000001",
                   "amount_inr": D("255000"), "beneficiary_account_id": 2,
                   "account_number": "50200000000000", "ifsc": "HDFC0005183",
                   "from_reference": "SUPA44"}
        payment.update(over)
        return _Conn(
            [_trade(44, "SUPA44", expected=D("5325000"), paid=D("255000")),
             _trade(14, "BRAV14", expected=D("3012885"), paid=D("2757885"),
                    supplier_id=31)],
            payment=payment, same_account=33,
        )

    @pytest.mark.asyncio
    async def test_it_moves_to_the_order_chosen(self):
        """The live case: ₹255,000 off IndoLondon's order onto Uncle's."""
        conn = self._conn()
        ok, msg = await _repo(conn).move_payment(
            payment_id=5, to_trade_id=14, actor_party_id=1,
            reason="belongs to Uncle",
        )
        assert ok, msg
        assert conn.payment["trade_id"] == 14
        assert "SUPA44" in msg and "BRAV14" in msg

    @pytest.mark.asyncio
    async def test_the_account_follows_to_the_same_physical_account(self):
        """
        One account, several vendors — the shape of the whole problem. The
        account number stays true and only the vendor's registration of it
        changes.
        """
        conn = self._conn()
        await _repo(conn).move_payment(
            payment_id=5, to_trade_id=14, actor_party_id=1, reason="x",
        )
        upd = next(e for e in conn.execs if "UPDATE payments SET trade_id" in e[0])
        assert upd[1][2] == 33, "should point at the destination vendor's row"

    @pytest.mark.asyncio
    async def test_without_a_matching_registration_the_original_is_kept(self):
        """
        A payment pointing at the account it was genuinely paid into beats one
        pointing at the right vendor's wrong account.
        """
        conn = self._conn()
        conn.same_account = None
        await _repo(conn).move_payment(
            payment_id=5, to_trade_id=14, actor_party_id=1, reason="x",
        )
        upd = next(e for e in conn.execs if "UPDATE payments SET trade_id" in e[0])
        assert upd[1][2] == 2

    @pytest.mark.asyncio
    async def test_the_order_it_left_reopens_if_it_is_now_short(self):
        conn = _Conn(
            [_trade(44, "SUPA44", status="completed", expected=D("5325000"),
                    paid=D("5325000")),
             _trade(14, "BRAV14")],
            payment={"id": 5, "trade_id": 44, "utr": "U1",
                     "amount_inr": D("255000"), "beneficiary_account_id": 2,
                     "account_number": None, "ifsc": None,
                     "from_reference": "SUPA44"},
        )
        # The source drops below its total once the payment leaves.
        conn.trades[44]["paid"] = D("5070000")
        ok, msg = await _repo(conn).move_payment(
            payment_id=5, to_trade_id=14, actor_party_id=1, reason="x",
        )
        assert ok
        assert conn.trades[44]["status"] == "awaiting_payment"
        assert "no longer covered" in msg

    @pytest.mark.asyncio
    async def test_moving_onto_the_same_order_is_refused(self):
        conn = self._conn()
        ok, msg = await _repo(conn).move_payment(
            payment_id=5, to_trade_id=44, actor_party_id=1, reason="x",
        )
        assert not ok
        assert "already on" in msg

    @pytest.mark.asyncio
    async def test_moving_onto_a_cancelled_order_is_refused(self):
        conn = self._conn()
        conn.trades[14]["status"] = "cancelled"
        ok, msg = await _repo(conn).move_payment(
            payment_id=5, to_trade_id=14, actor_party_id=1, reason="x",
        )
        assert not ok
        assert "cancelled" in msg

    @pytest.mark.asyncio
    async def test_it_is_on_the_record_with_both_ends_and_the_reason(self):
        """
        A payment that changes hands without a reason is indistinguishable
        from one that was wrong in the first place.
        """
        conn = self._conn()
        await _repo(conn).move_payment(
            payment_id=5, to_trade_id=14, actor_party_id=1,
            reason="landed on the only live order while BRAV14 was closed",
        )
        moved = next(a for a in conn.audits if a["action"] == "payment.moved")
        assert moved["detail"]["from_trade"] == "SUPA44"
        assert moved["detail"]["to_trade"] == "BRAV14"
        assert "BRAV14 was closed" in moved["detail"]["reason"]


# ----------------------------------------------------------------------
# Reaching it from Telegram
# ----------------------------------------------------------------------

class TestCorrectReachesLiveOrders:
    def test_the_command_no_longer_asks_only_for_completed_trades(self):
        src = inspect.getsource(bridge_trade.cmd_correct)
        assert "correctable_trades" in src
        assert "recent_completed_trades" not in src

    def test_the_query_includes_live_orders(self):
        sql = inspect.getsource(Repo.correctable_trades)
        assert "'open', 'awaiting_payment', 'completed'" in sql

    def test_live_orders_are_offered_first(self):
        """
        A mistake on a live order is the urgent one — it is still collecting
        money against a wrong figure.
        """
        sql = inspect.getsource(Repo.correctable_trades)
        assert "ORDER BY (t.status = 'completed')" in sql

    def test_every_correction_is_offered(self):
        src = inspect.getsource(bridge_trade.correct_pick)
        for data in ("cob", "com", "cop"):
            assert f'callback_data="{data}"' in src

    def test_reopen_is_offered_only_on_a_closed_order(self):
        src = inspect.getsource(bridge_trade.correct_pick)
        assert 'trade["status"] == "completed"' in src

    def test_a_payment_with_no_account_is_still_correctable(self):
        """
        An inner join here hid exactly the payments most likely to need
        correcting: the ones the bot could not attribute in the first place.
        """
        sql = inspect.getsource(Repo.trade_payment_rows)
        assert "LEFT JOIN bank_accounts" in sql

    def test_every_state_the_flow_filters_on_is_one_it_sets(self):
        """
        The /cancel fault of 28 September, which left three days of buttons
        reaching no handler. Checked here too because this flow gained a
        state.
        """
        src = Path(bridge_trade.__file__).read_text(encoding="utf-8")
        import re
        gated = set(re.findall(r"@router\.\w+\(\s*Correct\.(\w+)", src))
        setters = set(re.findall(r"set_state\(\s*Correct\.(\w+)\s*\)", src))
        assert gated <= setters, f"never set: {gated - setters}"
