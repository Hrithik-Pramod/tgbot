"""
A mini statement: the UTRs against one trade, and what they come to.

THE REQUEST (Bridge, 23 September 2026)

    i need to have option of mini statement on trading
    im thinking for the progress, we have a next step, choose trade
    then after choosing trade it gives a summary of utrs so far and a total
    with percentage
    As may be needing this for specific vendor reasons.
    Also think that this would be useful vendor side option, same progress
    with UTRs so far listed

Two screens, one renderer. The Bridge's copy carries the deal reference and
the vendor; the supplier's carries neither, and the call site is what
guarantees it — see test_disclosure_boundaries.py.

WHAT THIS FIXED ON THE WAY PAST

render_collection_progress, which the supplier had been reading since
10 September, printed the percentage as f"{pct:.0f}%". That ROUNDS. A
supplier owed ₹17 on ₹4,000,016 was told "100% collected" and had no reason
to chase anybody. The Bridge's side was fixed on 21 September; the
supplier's had been wrong eleven days longer and nobody had looked.

WHY THE BUTTONS ARE NOT STATE-GATED

cancel_pick was gated on Cancel.pick, nothing set it, and the buttons were
silently dead for days — "says loading does not let me", 22 September. A
mini statement is a read. A button pressed on yesterday's message should
render today's figures, not match no handler and spin for ever.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import bridge_bot, supplier_bot  # noqa: E402
from core.summary import (render_collection_progress,  # noqa: E402
                          render_mini_statement)

PAYMENTS = [
    {"utr": "BKIDR12026092100004142", "amount_inr": D("217000"),
     "account_name": "SUPER TRADING COMPANY (STC)"},
    {"utr": "BKIDR12026092100004206", "amount_inr": D("307000"),
     "account_name": "SUPER TRADING COMPANY (STC)"},
    {"utr": "PUNBR52026092100511473", "amount_inr": D("209000"),
     "account_name": "royal trading company"},
]


class TestEveryUtrIsListed:
    def test_each_one_appears(self):
        out = render_mini_statement(
            payments=PAYMENTS, expected_inr=D("1000000"), paid_inr=D("733000"))
        for p in PAYMENTS:
            assert p["utr"] in out

    def test_each_carries_its_amount(self):
        out = render_mini_statement(
            payments=PAYMENTS, expected_inr=D("1000000"), paid_inr=D("733000"))
        assert "217,000" in out and "307,000" in out and "209,000" in out

    def test_the_account_is_named_on_each_line(self):
        """
        A trade re-issued mid-collection has payments against two accounts.
        "Why is that one in the old account" should be answerable from the
        statement, not the audit log.
        """
        out = render_mini_statement(
            payments=PAYMENTS, expected_inr=D("1000000"), paid_inr=D("733000"))
        assert "SUPER TRADING COMPANY (STC)" in out
        assert "royal trading company" in out

    def test_they_are_numbered_in_order(self):
        out = render_mini_statement(
            payments=PAYMENTS, expected_inr=D("1000000"), paid_inr=D("733000"))
        assert out.index("1. BKIDR12026092100004142") < \
               out.index("2. BKIDR12026092100004206") < \
               out.index("3. PUNBR52026092100511473")

    def test_nothing_paid_says_so_plainly(self):
        """
        An empty list under a "Payments received" heading reads as a bug.
        SUPB7 sat at zero for six hours on 21 September.
        """
        out = render_mini_statement(
            payments=[], expected_inr=D("499990"), paid_inr=D("0"))
        assert "Nothing has been paid" in out
        assert "0% collected" in out


class TestTheTotalAndThePercentage:
    def test_the_count_is_stated(self):
        out = render_mini_statement(
            payments=PAYMENTS, expected_inr=D("1000000"), paid_inr=D("733000"))
        assert "3 payments" in out

    def test_one_payment_is_not_pluralised(self):
        out = render_mini_statement(
            payments=PAYMENTS[:1], expected_inr=D("1000000"), paid_inr=D("217000"))
        assert "1 payment)" in out

    def test_the_percentage_rounds_down(self):
        """73.3% collected, not 73.3 rounded to anything flattering."""
        out = render_mini_statement(
            payments=PAYMENTS, expected_inr=D("1000000"), paid_inr=D("733000"))
        assert "73% collected" in out

    def test_a_whisker_short_is_never_a_hundred(self):
        out = render_mini_statement(
            payments=PAYMENTS, expected_inr=D("4000016"), paid_inr=D("3999999"))
        assert "99% collected" in out
        assert "Outstanding   ₹17" in out

    def test_paid_in_full_says_nil_not_a_negative_zero(self):
        out = render_mini_statement(
            payments=PAYMENTS, expected_inr=D("733000"), paid_inr=D("733000"))
        assert "Outstanding   nil" in out
        assert "100% collected" in out

    def test_an_overpayment_is_not_disguised(self):
        out = render_mini_statement(
            payments=PAYMENTS, expected_inr=D("700000"), paid_inr=D("733000"))
        assert "OVERPAID" in out
        assert "33,000" in out


class TestTheSupplierPercentageNoLongerFlatters:
    def test_the_old_renderer_stopped_rounding_up(self):
        """
        The eleven-day bug. f"{pct:.0f}%" on 3,999,999 of 4,000,016 printed
        "100% collected" with ₹17 outstanding.
        """
        out = render_collection_progress(
            expected_inr=D("4000016"), paid_inr=D("3999999"))
        assert "99% collected" in out
        assert "100%" not in out

    def test_it_still_reads_a_hundred_when_it_is_finished(self):
        out = render_collection_progress(
            expected_inr=D("4000016"), paid_inr=D("4000016"))
        assert "100% collected" in out


class TestTheBridgeSideIsWiredUp:
    def test_progress_offers_the_trades(self):
        src = inspect.getsource(bridge_bot.cmd_progress)
        assert "list_open_trades()" in src
        assert 'callback_data=f"mst:{t[\'id\']}"' in src
        assert "reply_markup=kb" in src

    def test_no_keyboard_when_nothing_is_open(self):
        """A "pick a trade" prompt with no trades to pick is noise."""
        src = inspect.getsource(bridge_bot.cmd_progress)
        assert "kb = None" in src

    def test_the_handler_is_not_state_gated(self):
        """
        The 22 September lesson, held in place. A gated read is a button
        that dies silently the moment the state is not what somebody
        assumed.
        """
        src = inspect.getsource(bridge_bot)
        line = next(l for l in src.splitlines() if 'startswith("mst:")' in l)
        assert line.strip() == '@router.callback_query(F.data.startswith("mst:"))', (
            f"the mst: handler has picked up an extra filter: {line.strip()}"
        )

    def test_the_bridge_gets_the_reference_and_vendor(self):
        src = inspect.getsource(bridge_bot.mini_statement)
        assert "reference=trade['reference']" in src.replace('"', "'")
        assert "vendor=trade['supplier_label']" in src.replace('"', "'")

    def test_a_deleted_trade_does_not_crash_the_button(self):
        """Buttons outlive the things they point at."""
        src = inspect.getsource(bridge_bot.mini_statement)
        assert "if trade is None" in src


class TestTheSupplierSideIsWiredUp:
    def test_it_renders_the_statement(self):
        src = inspect.getsource(supplier_bot.cmd_progress)
        assert "render_mini_statement(" in src
        assert "trade_payments(trade['id'])" in src.replace('"', "'")

    def test_it_is_scoped_to_their_own_trade(self):
        """
        open_trade_for_supplier takes the supplier's own party id, so the
        trade — and therefore every UTR on it — is theirs. This is the whole
        safety argument and it should be visible in the source.
        """
        src = inspect.getsource(supplier_bot.cmd_progress)
        assert "open_trade_for_supplier(party['id'])" in src.replace('"', "'")

    def test_an_uninstructed_trade_still_says_so(self):
        """
        No expected figure means no percentage to show. That branch predates
        this change and must survive it.
        """
        src = inspect.getsource(supplier_bot.cmd_progress)
        assert 'if not trade["inr_expected"]' in src
