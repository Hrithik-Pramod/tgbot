"""
Cancelling asks which of his two reasons, and talks him out of one of them.

WHAT HE TOLD US (Bridge, 19 September 2026)

    I'm cancelling what I want to happen is The Usdt goes out to the client
    which is fine, but under these circumstances there are only two reasons
    to cancel a trade one is that they are sending more Usdt or they have
    picked the incorrect account to use

    If you can please consider this

That reframes the command. He has never once meant "write this trade off" —
he means "start this part again", and the USDT reaching the client is the
expected outcome rather than a problem. The product had been treating every
cancel as a write-off, which is why the money kept ending up unbilled: the
tool was doing something other than what he was reaching for it to do.

AND ONE OF HIS TWO REASONS DOES NOT NEED A CANCEL

  wrong account   /issue re-issues the same trade to different accounts,
                  allocates only the unpaid balance, keeps the deposit, the
                  reference and every payment logged, and since
                  18 September tells the client to stop paying the old one.
                  Cancelling to change an account throws all of that away to
                  reach the same place by a worse road — and it is what
                  stranded 9,354 USDT twice this week.

  more USDT       an UNINSTRUCTED trade absorbs a further deposit by itself
                  and the total rises. Only once the client holds a figure
                  does the extra have to be its own trade, which is their
                  own rule from 11 September after 1,859 USDT quietly became
                  4,859.

So in three of the four combinations, the right answer is "you do not need
to do this". Neither is blocked — he knows things the bot does not.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import bridge_trade  # noqa: E402


def _code(fn) -> str:
    src = inspect.getsource(fn)
    if fn.__doc__:
        src = src.replace(fn.__doc__, "")
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


class TestHeIsAskedWhichOfTheTwo:
    def test_both_of_his_reasons_are_offered(self):
        src = _code(bridge_trade.cancel_pick)
        assert "sending more USDT" in src
        assert "Wrong account was picked" in src

    def test_there_is_still_a_way_out_for_anything_else(self):
        """
        He said there are only two. He is describing the common case, not
        legislating — a flow with no escape gets abandoned halfway.
        """
        src = _code(bridge_trade.cancel_pick)
        assert "Something else" in src

    def test_the_free_text_route_still_records_a_reason(self):
        src = _code(bridge_trade.cancel_other_reason)
        assert "audit log" in src


class TestTheWrongAccountIsNotACancel:
    def test_it_points_at_reissuing_instead(self):
        src = _code(bridge_trade.cancel_wrong_account)
        assert "do not need to cancel" in src
        assert "Re-issue" in src

    def test_the_button_goes_straight_into_the_issue_flow(self):
        """
        Telling him the right command and making him find it is half an
        answer. This is the same callback the deposit notice uses.
        """
        src = _code(bridge_trade.cancel_wrong_account)
        assert 'callback_data=f"cf:{trade[\'id\']}"' in src

    def test_it_says_what_re_issuing_preserves(self):
        src = _code(bridge_trade.cancel_wrong_account)
        for kept in ("deposit", "reference", "already paid"):
            assert kept in src

    def test_nothing_is_cancelled_on_that_path(self):
        src = _code(bridge_trade.cancel_wrong_account)
        assert "cancel_trade" not in src
        assert "_finish_cancel" not in src

    def test_he_can_still_insist(self):
        src = _code(bridge_trade.cancel_wrong_account)
        assert "cwforce" in src


class TestMoreUsdtDependsOnWhetherItWasIssued:
    def test_an_uninstructed_trade_is_left_alone(self):
        """
        It absorbs the next deposit by itself. Cancelling would throw away a
        trade that was about to do exactly what he wants.
        """
        src = _code(bridge_trade.cancel_more_usdt)
        assert 'if trade["instructed_at"] is None:' in src
        branch = src.split('if trade["instructed_at"] is None:')[1].split("return")[0]
        assert "do not need to cancel" in branch
        assert "_finish_cancel" not in branch

    def test_an_issued_trade_is_cancelled_with_that_reason(self):
        """
        The client is holding a figure, so the extra genuinely has to be its
        own trade. Here cancelling is right and it proceeds without asking
        him to type out a reason he has already given.
        """
        src = _code(bridge_trade.cancel_more_usdt)
        assert "_finish_cancel(" in src
        assert '"supplier is sending more USDT"' in src

    def test_he_can_still_insist_on_the_uninstructed_one(self):
        src = _code(bridge_trade.cancel_more_usdt)
        assert "cwforce" in src


class TestInsistingStillRecordsWhy:
    def test_it_asks_for_a_reason_rather_than_cancelling_silently(self):
        src = _code(bridge_trade.cancel_anyway)
        assert "Cancel.reason" in src
        assert "audit log" in src

    def test_it_carries_the_trade_through(self):
        """
        The state was cleared when the bot offered the better route, so the
        trade has to come back from the callback or the wrong one is
        cancelled.
        """
        src = _code(bridge_trade.cancel_anyway)
        assert 'int(call.data.split(":", 1)[1])' in src
        assert "trade_id=" in src


class TestTheCleanupIsUnavoidable:
    def test_only_one_function_cancels(self):
        callers = [
            name for name, fn in vars(bridge_trade).items()
            if callable(fn)
            and getattr(fn, "__module__", None) == bridge_trade.__name__
            and "cancel_trade" in inspect.getsource(fn)
        ]
        assert callers == ["_finish_cancel"]

    def test_that_function_always_looks_for_stranded_money(self):
        src = _code(bridge_trade._finish_cancel)
        assert "stranded_deposits" in src
