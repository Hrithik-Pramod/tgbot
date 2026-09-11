"""
A client correcting a payment by editing the message.

Live, 11 September 2026. A payment was pasted, the bot replied "account not
recognised", and the client edited their message to add "to Ekta traders". On
screen the message then read perfectly. The bot never saw the correction —
Telegram delivers an edit as a different kind of update, and nothing was
listening for it.

That is the worst shape a bug can take: the evidence in front of the human
says one thing and the system's record says another, with nothing to show the
two ever diverged. It cost an hour of looking in the wrong place.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from bot import client_bot  # noqa: E402


class TestEditsAreReceivedAtAll:
    def test_a_handler_is_registered_for_edited_messages(self):
        handlers = client_bot.router.observers["edited_message"].handlers
        assert handlers, "edits are still going nowhere"
        assert any(h.callback.__name__ == "on_edited_payment" for h in handlers)

    def test_the_auth_middleware_covers_edits(self):
        """
        Edits arrive on their own observer. Without the middleware the handler
        runs with no `party` and raises — and an unregistered chat would not be
        filtered, which is the one thing that middleware exists to do.
        """
        src = inspect.getsource(main.build_dispatcher)
        assert "dp.edited_message.middleware" in src

    def test_edits_are_filtered_the_same_way_as_new_messages(self):
        """Commands and non-payment chatter must not be picked up either."""
        src = inspect.getsource(client_bot.on_edited_payment)
        assert "parse_payments" in src
        handler = next(
            h for h in client_bot.router.observers["edited_message"].handlers
            if h.callback.__name__ == "on_edited_payment"
        )
        assert handler.filters, "the edit handler accepts anything at all"


class TestWhatAnEditDoes:
    """
    Three outcomes, each of which has to be right on its own.
    """

    def test_an_already_recorded_payment_is_never_recorded_twice(self):
        """
        Amended 11 September 2026. This used to require existing_utrs and an
        unconditional "already recorded" reply. The reply turned out to be
        wrong for the common case — see test_edited_no_op.py — and the check
        now compares AMOUNTS, not just whether the reference is known.

        What has not changed, and is what this test is really for: an edit
        can never cause a second ledger entry for a reference already in it.
        """
        src = inspect.getsource(client_bot.on_edited_payment)
        assert "recorded_amounts" in src, \
            "an edit must tell an already-logged payment from a new one"
        assert "p.utr not in recorded" in src, \
            "only references absent from the ledger may be recorded"

    def test_a_payment_not_yet_recorded_is_recorded(self):
        src = inspect.getsource(client_bot.on_edited_payment)
        assert "_record" in src

    def test_an_unmatched_account_asks_for_a_new_message(self):
        """
        An edit must not open a button conversation: the buttons would attach
        to a message whose text can change again underneath them.
        """
        src = inspect.getsource(client_bot.on_edited_payment)
        assert "NEW message" in src
        assert "set_state" not in src, \
            "an edit must not start an FSM conversation"

    def test_it_stays_silent_on_ordinary_edited_chatter(self):
        src = inspect.getsource(client_bot.on_edited_payment)
        assert "if not result.payments:" in src and "return" in src
