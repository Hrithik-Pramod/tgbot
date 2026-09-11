"""
A trade nobody has spoken for shows no nominated account.

THE INCIDENT (live, 11 September 2026, 21:36)

14,151 USDT arrived from Supplier A and opened SUPA3. The Bridge's
notification read:

    Supplier nominated: Ekta traders

Nobody had nominated anything. The supplier had not run /send for this
deposit at all — when he did, moments later, he chose a different account
("he just chose super trading").

The Bridge:

    money sent, but no one chose supplier
    why did it default?
    its should not do that

WHERE IT CAME FROM

latest_nomination read the newest 'trade.account_nominated' row in the audit
log. The audit log is permanent and append-only by design, so the query
happily returned a nomination made at 15:42 for SUPA1 — a trade that had
been closed for hours. Every subsequent deposit from that supplier inherited
it. SUPA2 did too; it simply went unquestioned.

WHY IT MATTERS MORE THAN IT LOOKS

A default that reads as a decision is worse than no default. The confirm flow
shows the nominated account first and pre-ticked, so the normal path is one
tap — which means a stale nomination is one tap away from sending a client to
pay an account the supplier never asked for.

THE FIX

pending_sends holds a claim that has been made and not yet attached to a
trade. on_supplier_deposit consumes it as soon as the trade opens, so it is
offered exactly once. No outstanding claim means no nomination, and the
Bridge is told so in as many words.
"""

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import bridge_trade  # noqa: E402
from db.repo import Repo  # noqa: E402


def _sql(fn) -> str:
    return re.sub(r"\s+", " ", inspect.getsource(fn))


class TestANominationIsOfferedOnce:
    def test_it_no_longer_reads_the_audit_log(self):
        src = _sql(Repo.latest_nomination)
        assert "audit_log" not in src, (
            "the audit log is permanent, so it always has an answer — "
            "including for trades that closed hours ago"
        )
        assert "trade.account_nominated" not in src

    def test_it_reads_an_unconsumed_claim(self):
        src = _sql(Repo.latest_nomination)
        assert "FROM pending_sends" in src
        assert "matched_trade_id IS NULL" in src, (
            "without this a claim already attached to a trade is offered again"
        )

    def test_it_ignores_a_claim_with_no_account_on_it(self):
        """A /send abandoned before the account step has a null account, and
        selecting it would set nominated_account_id to nothing useful."""
        src = _sql(Repo.latest_nomination)
        assert "bank_account_id IS NOT NULL" in src

    def test_it_takes_the_most_recent_claim(self):
        src = _sql(Repo.latest_nomination)
        assert "ORDER BY created_at DESC" in src and "LIMIT 1" in src


class TestTheClaimIsConsumedWhenTheTradeOpens:
    def test_the_deposit_handler_matches_the_pending_send(self):
        """
        This is what makes the nomination one-shot. Without it the same claim
        would be offered to the next deposit as well, and we would be back to
        a stale default by another route.
        """
        from bot.notifier import Notifier

        src = inspect.getsource(Notifier.on_supplier_deposit)
        assert "latest_nomination" in src
        assert "match_pending_send" in src
        # and the match must happen after the trade exists to attach it to
        assert src.index("latest_nomination") < src.index("match_pending_send")


class TestTheBridgeIsToldWhenThereIsNoNomination:
    def test_the_confirm_flow_says_so_plainly(self):
        """
        Silence here would be read as 'no preference'. The Bridge needs to
        know the difference between a supplier who chose and one who has not
        spoken yet, because only the second is a reason to wait.
        """
        src = inspect.getsource(bridge_trade.confirm_start)
        assert "has not nominated an account yet" in src

    def test_a_nominated_account_is_still_marked_and_shown_first(self):
        src = inspect.getsource(bridge_trade.confirm_start)
        assert "Supplier nominated:" in src
        assert 'a["id"] != nominated' in src
