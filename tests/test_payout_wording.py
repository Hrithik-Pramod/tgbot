"""
A payout landing is reported as a payout, not as a failure.

THE INCIDENT (live, 11 September 2026, 21:39)

The Bridge sent 13,992.59 USDT on to the client — the onward leg of SUPA3,
working exactly as intended. The client's wallet is monitored, so the bot saw
it arrive and told him:

    Deposit detected on a non-internal wallet
    13992.59 USDT
    Hash c0cfc37b...
    No trade was opened.

His reply: "no trade was opened?"

Nothing was wrong. The message described a correct outcome in the vocabulary
of a fault — "non-internal wallet" is a database distinction he has never had
to care about, and "No trade was opened" reads as something failing to
happen. At the end of a day of real faults, it landed as one more.

THE RULE

Every unprompted message states what happened, in the reader's terms. If the
answer to "do I need to do anything?" is no, the message says so rather than
leaving it to be inferred from an absence.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.repo import Repo  # noqa: E402
from monitor import tron  # noqa: E402


def _src():
    return inspect.getsource(tron.DepositMonitor._handle_deposit)


class TestTheWordingDescribesWhatHappened:
    def test_the_failure_wording_is_gone(self):
        src = _src()
        assert "No trade was opened." not in src, \
            "still reads as something that failed to happen"
        assert "Deposit detected on a non-internal wallet" not in src, \
            "'non-internal wallet' is a schema detail, not a fact about money"

    def test_it_says_a_payout_arrived(self):
        src = _src()
        assert "Onward payout confirmed" in src

    def test_it_answers_the_do_i_need_to_act_question(self):
        src = _src()
        assert "Nothing to action" in src

    def test_it_names_whose_wallet(self):
        """
        "a counterparty's wallet" is a fallback. When the wallet has an owner
        the message should name them, because the Bridge settles with more
        than one party and needs to know which leg this was.
        """
        src = _src()
        assert "party_label" in src
        assert "owner_label" in src

    def test_it_still_falls_back_when_the_wallet_has_no_owner(self):
        src = _src()
        assert "a counterparty's" in src

    def test_the_hash_is_still_a_link(self):
        src = _src()
        assert "tx_link(transfer['tx_hash'], html=True)" in src


class TestTheBehaviourIsUnchanged:
    """
    Only the wording moved. A payout must still open no trade, and the owner
    must still be told their funds arrived.
    """

    def test_no_trade_is_opened_for_a_non_internal_wallet(self):
        src = _src()
        guard = src.split('if not wallet["is_internal"]:')[1]
        before_return = guard.split("return")[0]
        assert "on_supplier_deposit" not in before_return

    def test_the_owner_still_gets_their_payout_notice(self):
        src = _src()
        assert "render_payout_notice" in src

    def test_the_owner_lookup_does_not_break_an_ownerless_wallet(self):
        """
        An internal wallet has no owner_party_id. Calling party_label(None)
        would raise inside the monitor loop, which is the one place an
        exception costs a missed deposit.
        """
        src = _src()
        assert "if owner else None" in src


class TestTheLookupExists:
    def test_party_label_returns_the_label(self):
        src = inspect.getsource(Repo.party_label)
        assert "SELECT label FROM parties WHERE id = $1" in src
