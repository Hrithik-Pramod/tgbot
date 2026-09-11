"""
The client is not sent a closing document.

Bridge request, 11 September 2026, after seeing it land in the client's group:

    stop for client
    any completion message

Until 10 September a trade closed only when someone typed /done, so the
summary was deliberate and rare. Auto-completion made it arrive unprompted —
thirty-three lines into an eighteen-member group the instant the last payment
was recorded — and the Bridge does not want a counterparty holding the same
document he reconciles from.

WHAT THE CLIENT STILL GETS

  A thumbs up on each payment message. That is the acknowledgement their team
  was told to rely on ("no acknowledgement means it was not picked up") and it
  is untouched here.

  An answer to /done, because they typed it. One line, no figures.

WHAT IS UNCHANGED

  The Bridge and the supplier both still receive the full itemised summary.
  Silencing the client must not silence the two parties who have to agree the
  money actually balanced.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import client_bot  # noqa: E402
from bot.notifier import Notifier  # noqa: E402
from core import summary as summary_mod  # noqa: E402


class TestTheSwitchDefaultsToSilence:
    def test_it_is_off_unless_the_environment_turns_it_on(self):
        assert summary_mod.CLIENT_CLOSING_SUMMARY is False, (
            "the default must be silence — a deploy that forgets the env var "
            "must not start messaging the client again"
        )

    def test_it_is_a_switch_and_not_a_deletion(self):
        """
        The itemised format came from the client's own specification. If they
        ask for it back it should be an env change and a restart, not a code
        change and a deploy.
        """
        src = inspect.getsource(summary_mod)
        assert 'os.environ.get("CLIENT_CLOSING_SUMMARY"' in src


class TestAutomaticCompletionTellsTheClientNothing:
    def test_both_client_messages_are_behind_the_switch(self):
        src = inspect.getsource(Notifier.check_completion)
        guarded = src.split("if CLIENT_CLOSING_SUMMARY:")[1]
        head = guarded.split("await self.to_bridge(summary)")[0]
        assert "counterparty_summary" in head
        assert "render_completion_notice" in head

    def test_nothing_reaches_the_client_outside_the_switch(self):
        """
        The specific failure to guard against: one of the two calls left
        outside the branch, so the client still gets half a closing message.
        """
        src = inspect.getsource(Notifier.check_completion)
        before, _, after = src.partition("if CLIENT_CLOSING_SUMMARY:")
        # the supplier's copy lives after the branch and is allowed
        after_client = after.split("render_supplier_summary")[0]
        outside = before + after_client.split("await self.to_bridge(summary)")[-1]
        assert 'to_party(trade["client_id"]' not in outside


class TestTheOtherTwoPartiesAreUnaffected:
    def test_the_bridge_still_gets_the_full_summary(self):
        src = inspect.getsource(Notifier.check_completion)
        assert "await self.to_bridge(summary)" in src
        # and it must not be inside the client branch
        assert not src.split("if CLIENT_CLOSING_SUMMARY:")[1].startswith(
            "\n            await self.to_bridge"
        )

    def test_the_supplier_still_gets_theirs(self):
        src = inspect.getsource(Notifier.check_completion)
        assert 'to_party(\n            trade["supplier_id"]' in src \
            or 'trade["supplier_id"],' in src
        assert "render_supplier_summary" in src

    def test_the_overpaid_alert_still_fires(self):
        """
        An overpayment is money that has to go back to someone. Quietening the
        client must not quieten that.
        """
        src = inspect.getsource(Notifier.check_completion)
        assert "OVERPAID" in src


class TestDoneStillAnswersTheClient:
    def test_it_does_not_go_silent(self):
        """
        The client typed the command. A command that does its work without
        replying is indistinguishable from one that failed — which is how
        /done was reported broken in the first place.
        """
        src = inspect.getsource(client_bot.cmd_done)
        assert "Closed." in src

    def test_the_acknowledgement_carries_no_figures(self):
        src = inspect.getsource(client_bot.cmd_done)
        answer = src.split("else:")[-1]
        assert "fmt_inr" not in answer
        assert "counterparty_summary" not in answer


class TestThePaymentAcknowledgementSurvives:
    def test_the_thumbs_up_is_untouched(self):
        """
        This is the one signal the client's team was told to rely on. It is not
        a completion message and must not be caught by this change.
        """
        src = inspect.getsource(client_bot._acknowledge)
        assert "👍" in src
        assert "CLIENT_CLOSING_SUMMARY" not in src
