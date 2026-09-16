"""
A rate that moves before the instruction goes out can be applied.

WHAT IT COST TO NOT HAVE THIS (live, 16 September 2026)

A trade snapshots its rate when it opens. That is deliberate — reading the
rate through the join would let a later /setrate silently rewrite arithmetic
already agreed, which is how a trade instructed at ₹197,054 quietly became
₹515,054 on 11 September. The cost of the snapshot is a rate that moves
between the deposit landing and the instruction going out, leaving the trade
priced on the old one.

Bridge, 14 September 2026:

    the rate has changed but the funds have come
    Can i have the option to change the rate and it reflect
    Apologies this was one off, but as it happened, it could always happen

It was not a one-off. Two days later the rate moved again, and with no way to
change it he reached for the only lever he had — /cancel — and reopened the
trades from scratch. Cancelling took those suppliers' bank accounts out of
the client's matcher, and the client went on paying them:

    15:44  unmatched beneficiary 'Barkaati Textile'
    17:37  unmatched beneficiary 'PRIME PATH ENTERPRISES GGN'

    Peter bot is not reading the slips
    Just fyi, lots of slips keep missing Pter

The slips were the symptom. The missing command was the cause.

THE GUARDS, WHICH ARE THE WHOLE POINT

  not instructed, unpaid    allowed. Nothing has left the building; the only
                            thing moving is a figure the Bridge is holding.

  instructed                REFUSED. The client has a message naming an
                            amount. Moving the total underneath it is the
                            11 September fault exactly.

  any payment logged        REFUSED. Money already in was priced at the old
                            rate; repricing would restate settled money.

The rate is read from the rates table, never passed in, so what lands on the
trade is what /setrate recorded — with its current-rate display, its
loss-making warning, and its attribution to whoever set it.
"""

import inspect
import re
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import bridge_trade  # noqa: E402
from bot.bridge_trade import _reprice_blocker  # noqa: E402
from core.money import inr_to_usdt, margin_usdt, usdt_to_inr  # noqa: E402
from db.repo import Repo  # noqa: E402


def _code(fn) -> str:
    src = inspect.getsource(fn)
    if fn.__doc__:
        src = src.replace(fn.__doc__, "")
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


def _sql(fn) -> str:
    return re.sub(r"\s+", " ", _code(fn))


def _trade(**over):
    """A live trade that may be repriced, unless a test spoils it."""
    row = {
        "id": 1,
        "reference": "SUPA6",
        "rate_id": 10,
        "instructed_at": None,
        "paid_inr": D("0"),
        "current_rate_id": 11,
        "usdt_received": D("37736"),
        "supply_rate": D("105.50"),
        "sell_rate": D("106.50"),
        "inr_expected": D("3981148"),
        "current_supply_rate": D("106"),
        "current_sell_rate": D("107.50"),
    }
    row.update(over)
    return row


class TestWhatMayBeMoved:
    def test_an_uninstructed_unpaid_trade_on_an_old_rate_can_be(self):
        assert _reprice_blocker(_trade()) is None

    def test_an_issued_trade_may_never_be(self):
        """
        The client is holding a figure. This is the one refusal that is not a
        convenience — it is the 11 September fault written as a guard.
        """
        import datetime

        blocked = _reprice_blocker(_trade(
            instructed_at=datetime.datetime(2026, 9, 16, 15, 44)))
        assert blocked is not None
        assert "issued" in blocked

    def test_a_part_paid_trade_may_never_be(self):
        blocked = _reprice_blocker(_trade(paid_inr=D("218016")))
        assert blocked is not None
        assert "218,016" in blocked, "he should see how much, not just that"

    def test_a_pairing_with_no_rate_at_all_is_named_as_such(self):
        blocked = _reprice_blocker(_trade(current_rate_id=None))
        assert blocked is not None
        assert "no rate" in blocked

    def test_a_trade_already_on_the_newest_rate_is_not_offered(self):
        """
        Otherwise /reprice appears to do nothing, and the next move is
        /cancel — which is the whole thing this exists to prevent.
        """
        blocked = _reprice_blocker(_trade(current_rate_id=10, rate_id=10))
        assert blocked is not None
        assert "already on the rate" in blocked

    def test_being_issued_outranks_everything_else(self):
        """The strictest reason is the one he is shown."""
        import datetime

        blocked = _reprice_blocker(_trade(
            instructed_at=datetime.datetime(2026, 9, 16),
            paid_inr=D("218016"), current_rate_id=10, rate_id=10))
        assert "issued" in blocked


class TestHeIsAlwaysToldWhy:
    """
    A refusal that does not say why sends him back to /cancel, and cancelling
    is what broke the matcher. Every blocked trade names its own reason.
    """

    def test_the_picker_lists_the_reason_for_each_held_trade(self):
        src = _code(bridge_trade.cmd_reprice)
        assert "_reprice_blocker(t)" in src
        assert "Not offered" in src

    def test_with_nothing_movable_it_still_explains_each_one(self):
        src = _code(bridge_trade.cmd_reprice)
        branch = src.split("if not movable:")[1].split("return")[0]
        assert "_reprice_blocker(t)" in branch
        assert "/setrate" in branch, "the usual cause is that no new rate exists yet"

    def test_it_says_what_to_do_with_an_issued_trade_instead(self):
        src = _code(bridge_trade.cmd_reprice)
        assert "Cancel and re-issue" in src


class TestTheFiguresAreShownBeforeAnythingMoves:
    def test_both_totals_appear_in_the_confirmation(self):
        src = _code(bridge_trade.reprice_pick)
        assert src.count("fmt_inr(") >= 3
        assert "difference" in src

    def test_the_preview_matches_core_money(self):
        """
        The number in the message and the number written to the trade must be
        produced the same way. 37,736 USDT at 106 is ₹4,000,016 — the figure
        the Bridge paid out by hand on 16 September.
        """
        t = _trade()
        from core.money import round_inr

        previewed = round_inr(
            D(t["usdt_received"]) * D(t["current_supply_rate"]))
        assert previewed == usdt_to_inr(t["usdt_received"], t["current_supply_rate"])
        assert previewed == D("4000016")

    def test_the_onward_obligation_moves_with_it(self):
        owed = inr_to_usdt(D("4000016"), D("107.50"))
        assert owed == D("37209.45")
        assert margin_usdt(D("37736"), D("106"), D("107.50")) == D("526.55")

    def test_a_stale_pick_is_refused_rather_than_applied(self):
        """
        An instruction can go out between the list being drawn and a button
        being tapped. The blocker is evaluated again on the tap.
        """
        src = _code(bridge_trade.reprice_pick)
        assert "_reprice_blocker(t) is not None" in src
        assert "Nothing was changed" in src


class TestTheGuardsAreEnforcedWhereItCounts:
    """
    The picker is a convenience. The refusals have to hold in the write, under
    a lock, because that is the only place that sees the truth.
    """

    def test_the_row_is_locked_before_it_is_judged(self):
        assert "FOR UPDATE" in _sql(Repo.reprice_trade)

    def test_an_instructed_trade_is_refused_in_the_write(self):
        sql = _sql(Repo.reprice_trade)
        assert 't["instructed_at"] is not None' in sql

    def test_a_paid_trade_is_refused_in_the_write(self):
        sql = _sql(Repo.reprice_trade)
        assert "SELECT COALESCE(sum(amount_inr), 0) FROM payments WHERE trade_id = $1" in sql
        assert "if paid:" in sql

    def test_a_closed_trade_is_refused(self):
        sql = _sql(Repo.reprice_trade)
        assert 'if t["status"] not in ("open", "awaiting_payment"):' in sql

    def test_the_rate_is_read_not_passed_in(self):
        """
        Accepting a rate as an argument would bypass /setrate's current-rate
        display, its loss warning and its record of who set it.
        """
        params = inspect.signature(Repo.reprice_trade).parameters
        assert set(params) == {"self", "trade_id", "actor_party_id"}
        assert "FROM rates" in _sql(Repo.reprice_trade)

    def test_the_arithmetic_is_core_moneys_and_not_its_own(self):
        """
        Rounding rules exist in exactly one place. A second copy here would
        drift, and drift in money is not a rounding error, it is a discrepancy
        nobody can explain later.
        """
        sql = _sql(Repo.reprice_trade)
        assert "usdt_to_inr(usdt, r[\"supply_rate\"])" in sql
        assert "inr_to_usdt(new_inr, r[\"sell_rate\"])" in sql

    def test_the_change_is_written_to_the_audit_log(self):
        sql = _sql(Repo.reprice_trade)
        assert 'action="trade.reprice"' in sql
        for field in ("from_rates", "to_rates", "from_inr", "to_inr"):
            assert field in sql

    def test_nothing_else_about_the_trade_is_touched(self):
        """
        Specifically not status, not instructed_at, not usdt_received. A
        reprice changes the price and nothing else.
        """
        sql = _sql(Repo.reprice_trade)
        update = sql.split("UPDATE trades")[1].split("WHERE id = $1")[0]
        for column in ("status", "instructed_at", "usdt_received",
                       "announced_at", "nominated_account_id"):
            assert column not in update


class TestItDoesNotLeakOutOfTheBridge:
    def test_only_the_bridge_router_has_it(self):
        from bot import client_bot, supplier_bot

        for module in (client_bot, supplier_bot):
            src = Path(module.__file__).read_text()
            assert 'Command("reprice")' not in src

    def test_the_client_is_not_told_anything_on_a_reprice(self):
        """
        Correct by construction — an uninstructed trade has never been
        mentioned to them — and worth pinning, because the obvious "helpful"
        addition here would be to send them the new figure.
        """
        src = _code(bridge_trade.reprice_confirm)
        assert "notifier" not in src
        assert "/issue" in src, "he still has to send it deliberately"
