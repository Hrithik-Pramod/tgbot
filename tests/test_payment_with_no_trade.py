"""
A payment arriving with nothing to attach it to is never silent.

WHAT HAPPENED (live, 15 September 2026)

A trade was cancelled at 15:23. Its replacement did not exist until 15:50.
The client kept paying throughout — they had no way to know anything had
changed.

Five payments arrived in that window:

    BKIDR...3075   218,016
    BKIDR...1747   370,000
    BKIDR...1762   348,000
    BKIDR...1784   203,000
    BKIDR...1785   280,000
    ------------------------
                 1,419,016

None was parsed, recorded, acknowledged or reported. The handler returned
one line before the parser ran:

    accounts = await repo.open_trade_accounts_for_client(party["id"])
    if not accounts:
        return   # nothing open — stay quiet rather than nagging on small talk

That comment is right about conversation and wrong about money. Nobody knew
until the Bridge totted up his own list and found the bot ₹1,419,016 short.

THE RULE, WHICH ALREADY EXISTED

From the ₹902,460 loss on 11 September: the bot may decline to record
something, but it may never decline quietly. That was applied to payments it
could not attribute to an ACCOUNT. This is the same failure one step
earlier — nothing to attribute to at all — and the rule was never carried
across.

WHAT IT DOES NOW

  no open trade, and the message carries a payment
      tell the client plainly it is NOT recorded, and tell the Bridge with
      the references and amounts so he can act

  no open trade, ordinary conversation
      still silent. The chattiness fix of 10 September stands: a bot that
      answers small talk in a working group is unusable.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import client_bot  # noqa: E402


def _src():
    return inspect.getsource(client_bot.on_pasted_payment)


def _no_trade_branch():
    """
    The body of the 'nothing open' branch, up to its return.

    Keyed on `matchable` since 16 September: the handler now holds two
    lists — everything the bot can RECOGNISE, and the narrower set the client
    is SHOWN — and this branch is about having nothing at all.
    """
    src = _src()
    branch = src.split("if not matchable:")[1]
    return branch.split("\n        return")[0]


class TestItReadsBeforeItDecides:
    def test_the_message_is_parsed_even_with_no_open_trade(self):
        branch = _no_trade_branch()
        assert "parse_payments(body)" in branch, (
            "returning before parsing is the bug — the bot cannot tell a "
            "payment from small talk without reading it"
        )

    def test_the_parse_happens_before_the_return(self):
        src = _src()
        parsed_at = src.index("stray = parse_payments(body)")
        returned_at = src.index("if not matchable:")
        assert parsed_at > returned_at


class TestAPaymentIsAlwaysReported:
    def test_the_client_is_told_it_was_not_recorded(self):
        branch = _no_trade_branch()
        assert "message.reply" in branch
        assert "NOT been recorded" in branch

    def test_the_bridge_is_told(self):
        """
        The client knowing is not enough. Only the Bridge can open the trade,
        and on 15 September he found out by doing his own arithmetic.
        """
        branch = _no_trade_branch()
        assert "to_bridge" in branch

    def test_the_alert_names_the_references_and_amounts(self):
        branch = _no_trade_branch()
        assert "p.utr" in branch and "p.amount_inr" in branch

    def test_it_speaks_even_when_only_a_reference_was_readable(self):
        """
        saw_utr without a parsed amount still means somebody was logging a
        payment. Reporting it badly beats not reporting it.
        """
        branch = _no_trade_branch()
        assert "stray.saw_utr" in branch

    def test_the_alert_says_nothing_is_logged(self):
        branch = _no_trade_branch()
        assert "Nothing has been logged" in branch

    def test_it_tells_the_client_what_to_do_next(self):
        branch = _no_trade_branch()
        assert "again" in branch


class TestOrdinaryConversationIsStillIgnored:
    """
    The chattiness fix of 10 September cost a day and the client's patience.
    Nothing here may loosen it.
    """

    def test_it_only_speaks_when_a_payment_was_present(self):
        branch = _no_trade_branch()
        assert "if stray.payments or stray.saw_utr:" in branch

    def test_small_talk_produces_no_payment_and_no_utr(self):
        from core.parse import parse_payments

        for line in ["send 5 lakh by 4", "ok will do", "any update?",
                     "call me when free", "to SUPER TRADING COMPANY"]:
            result = parse_payments(line)
            assert not result.payments
            assert not result.saw_utr, f"would now speak on: {line!r}"

    def test_a_real_payment_is_recognised(self):
        """The other half — the shape that went missing must be detected."""
        from core.parse import parse_payments
        from decimal import Decimal as D

        result = parse_payments(
            "BKIDR12026091500003075\n218016\nto SUPER TRAd"
        )
        assert len(result.payments) == 1
        assert result.payments[0].amount_inr == D("218016")
        assert result.saw_utr


class TestTheNormalPathIsUntouched:
    def test_an_open_trade_still_goes_through_the_usual_flow(self):
        src = _src()
        after = src.split("result = parse_payments(body)")[-1]
        assert "match_account" in after
        assert "_record(" in after
