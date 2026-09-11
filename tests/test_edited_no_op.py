"""
Editing a message the bot has already read is not an error.

WHAT HAPPENED (live, 11 September 2026, 19:15)

The bot was down. The client sent three payments as photos with captions. At
19:12 the Bridge asked "which account?", so they edited each caption to add
the account name. The bot came back at 19:15 and Telegram delivered the whole
backlog at once — the original messages AND the edits.

The originals were parsed and recorded. Then the edit handler ran on each one,
found the reference already in the ledger, and replied:

    That payment is already recorded — editing the message does not change
    what was logged. If something was wrong, tell the Bridge rather than
    editing.

Three times, about payments recorded seconds earlier from those very messages.
The client: "meaning?". The Bridge: "bot is clearing i am doing manually".

The reply was not just noise, it was false. They had edited before anything
was logged, and they had done exactly what they were asked to do.

THE RULE

  amounts agree      say nothing. The edit changed nothing.
  amounts differ     tell the client to speak to a person, and tell the
                     Bridge — a recorded figure being restated is the one
                     case here that can cost money.

A reference already existing is not on its own a reason to speak. Only a
disagreement about the figure is.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import client_bot  # noqa: E402
from db.repo import Repo  # noqa: E402


def _src():
    return inspect.getsource(client_bot.on_edited_payment)


class TestANoOpEditIsSilent:
    def test_it_compares_amounts_rather_than_just_existence(self):
        src = _src()
        assert "recorded_amounts" in src, (
            "existing_utrs only says whether the reference is known, which "
            "cannot tell a harmless edit from a restated figure"
        )
        assert "recorded[p.utr] != p.amount_inr" in src

    def test_matching_amounts_return_without_replying(self):
        """
        The branch for 'nothing changed' must reach a bare return with no
        message of any kind in it.
        """
        src = _src()
        branch = src.split("if not restated:")[1].split("return")[0]
        assert "message.reply" not in branch
        assert "to_bridge" not in branch
        assert "message.answer" not in branch

    def test_the_old_unconditional_reply_is_gone(self):
        src = _src()
        assert "editing the message does not change what was logged" not in src, (
            "the wording that fired on every edit is still there"
        )


class TestARestatedAmountIsNotSilent:
    def test_the_client_is_told_to_talk_to_a_person(self):
        src = _src()
        after = src.split("if not restated:")[1]
        assert "message.reply" in after
        assert "Bridge" in after

    def test_the_bridge_is_told_too(self):
        """
        This is the half that matters. A client restating a logged figure is
        something the Bridge must hear about without having to be watching
        the client's group at the time.
        """
        src = _src()
        after = src.split("if not restated:")[1]
        assert "to_bridge" in after

    def test_the_alert_names_both_figures(self):
        src = _src()
        assert "recorded[p.utr]" in src and "p.amount_inr" in src
        assert "logged" in src and "edited to" in src

    def test_the_alert_says_the_ledger_was_not_changed(self):
        src = _src()
        assert "ledger is unchanged" in src.lower() or \
               "nothing has been adjusted" in src.lower()


class TestANewPaymentInAnEditStillLands:
    """
    The original reason this handler exists: on 11 September a client pasted a
    payment the bot could not attribute and fixed it by editing the message.
    Quietening the no-op case must not quieten that.
    """

    def test_fresh_payments_are_still_recorded(self):
        src = _src()
        assert "fresh = [p for p in result.payments if p.utr not in recorded]" in src
        assert "_record(" in src

    def test_an_unmatched_account_still_asks_for_a_new_message(self):
        src = _src()
        assert "NEW message" in src


class TestTheQueryBacksIt:
    def test_recorded_amounts_returns_the_figures(self):
        src = inspect.getsource(Repo.recorded_amounts)
        assert "SELECT utr, amount_inr FROM payments" in src
        assert "r[\"amount_inr\"]" in src

    def test_it_handles_an_empty_list(self):
        """asyncpg would otherwise be handed an empty array and the handler
        would raise on a message with no payments in it."""
        src = inspect.getsource(Repo.recorded_amounts)
        assert "if not utrs:" in src
