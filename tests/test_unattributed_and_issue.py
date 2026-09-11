"""
Two holes found on 11 September 2026, both about being unable to act.

MONEY IN LIMBO

Four payments totalling ₹902,460 reached the client's group and were never
recorded. None of them carried a thumbs up. The sequence: the client pasted a
payment the bot could not attribute, the bot asked "which account did these go
to?", and nobody ever tapped. The payment sits in a pending conversation —
held in memory — and a restart discards it without a trace.

Nobody was told. The first anyone knew was the client asking why a completed
trade had not closed, three hours later.

NO DOOR TO THE FLOW

The Bridge needed to issue an instruction and could not reach the flow: the
only route was a button on a deposit notification he had scrolled past. He
tried /send, which only writes a note into his own channel, and was stuck
mid-trade with the client waiting.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import bridge_trade, client_bot  # noqa: E402
from db.repo import Repo  # noqa: E402


class TestUnattributedPaymentsAreNotSilent:
    def test_the_bridge_is_told_when_a_payment_cannot_be_matched(self):
        src = inspect.getsource(client_bot.on_pasted_payment)
        asked = src.split("Which account did these go to?")[1]
        assert "to_bridge" in asked, (
            "the client is asked which account, but nothing tells the Bridge "
            "that money is sitting unrecorded"
        )

    def test_the_alert_says_it_is_not_recorded(self):
        src = inspect.getsource(client_bot.on_pasted_payment)
        assert "NOT\n" in src or "NOT " in src, \
            "the alert must be explicit that nothing is logged yet"

    def test_the_alert_names_the_payments(self):
        """
        A count is not enough — the Bridge has to be able to chase a specific
        reference and amount.
        """
        src = inspect.getsource(client_bot.on_pasted_payment)
        asked = src.split("Which account did these go to?")[1]
        assert "s['utr']" in asked and "s['amount']" in asked

    def test_it_fires_before_the_handler_returns(self):
        """
        The alert must not sit after an early return, which is how the
        duplicate-UTR branch skipped the completion check earlier today.
        """
        src = inspect.getsource(client_bot.on_pasted_payment)
        asked = src.split("Which account did these go to?")[1]
        alert_at = asked.index("to_bridge")
        return_at = asked.index("return")
        assert alert_at < return_at, "the alert is unreachable"


class TestTheBridgeCanAlwaysReachTheConfirmFlow:
    def test_the_issue_command_exists(self):
        assert hasattr(bridge_trade, "cmd_issue")
        src = inspect.getsource(bridge_trade)
        assert 'Command("issue")' in src

    def test_it_reuses_the_existing_confirm_flow(self):
        """
        A second way in, not a second implementation. The callback must be the
        same one the deposit notification's button uses.
        """
        src = inspect.getsource(bridge_trade.cmd_issue)
        assert 'f"cf:{t[\'id\']}"' in src or "cf:" in src

    def test_it_shows_what_is_outstanding_not_the_gross(self):
        """
        Same lesson as the confirm flow itself: showing the gross total after
        a client has part-paid invites instructing them to pay twice.
        """
        src = inspect.getsource(bridge_trade.cmd_issue)
        assert "outstanding" in src
        assert "paid" in src

    def test_the_query_supplies_what_the_command_needs(self):
        """
        cmd_issue reads paid_inr off each row. It was not in the query, and
        this would have been a KeyError the first time the Bridge ran it —
        mid-trade, with the client waiting.
        """
        src = inspect.getsource(Repo.list_open_trades)
        assert "paid_inr" in src

    def test_it_says_so_when_there_is_nothing_to_issue(self):
        src = inspect.getsource(bridge_trade.cmd_issue)
        assert "no open trades" in src.lower()
