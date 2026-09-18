"""
The bot does not hand over a figure it can see has already been paid.

WHAT HAPPENED (live, 18 September 2026)

Malegao sent 2,354 USDT on the 16th and GS Group sent 7,000. The Bridge paid
the client within minutes of each — 2,321.21 at 15:29 and 6,922.00 at 17:08 —
and then cancelled both trades over a rate change, before the client had ever
been invoiced. ₹992,924 of delivered settlement, billed to nobody.

Reopening them as SUPB5 and SUPD2 put that back on the client, correctly. It
also put two copy-ready USDT figures in front of the Bridge for money that had
already left: 2,321.22 and 6,922.01, differing from what he actually sent only
by the sell rate moving 107.5 to 107.7.

9,243 USDT, one tap from going out twice, with nothing in the way but a
WhatsApp message he would have had to remember at the moment he tapped.

WHAT THE BOT COULD ALREADY SEE

A payout is recorded as a deposit on the client's wallet and never carries a
trade_id. The evidence was in the table the whole time with nothing reading
it. This is the reading.

WHY THE NUMBER IS WITHHELD RATHER THAN CAPTIONED

The bare number exists because it is the thing that gets acted on — sent
alone, last, so a thumb lands on it (client, 11 September 2026: iPhone copies
the whole message). A warning printed above a copy-ready figure is read after
the copy, if at all. So the figure is withheld and he has to ask. One extra
tap against 9,243 USDT.

It is a warning, never a block. He may have good reason, and a guard people
route around is worse than none.
"""

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import bridge_trade  # noqa: E402
from db.repo import Repo  # noqa: E402


def _code(fn) -> str:
    src = inspect.getsource(fn)
    if fn.__doc__:
        src = src.replace(fn.__doc__, "")
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


def _sql(fn) -> str:
    return re.sub(r"\s+", " ", _code(fn))


class TestItLooksBeforeItHandsOverTheFigure:
    def test_the_bare_number_is_behind_the_check(self):
        src = _code(bridge_trade.confirm_final)
        assert "prior = await repo.prior_payouts_for_trade" in src
        assert "if prior:" in src

        # The unguarded send must be the else, not a line that runs regardless.
        after = src.split("if prior:")[1]
        assert "else:" in after
        unguarded = after.split("else:")[1]
        assert 'fmt_usdt_plain(trade["usdt_owed_client"])' in unguarded

    def test_the_warning_names_amount_time_and_transaction(self):
        src = _code(bridge_trade.confirm_final)
        branch = src.split("if prior:")[1].split("else:")[0]
        assert "amount_usdt" in branch
        assert "detected_at" in branch
        assert "tx_hash" in branch

    def test_it_says_what_the_trade_thinks_is_owed(self):
        """Both numbers, or he cannot tell whether they are the same money."""
        branch = _code(bridge_trade.confirm_final).split("if prior:")[1]
        assert 'trade["usdt_owed_client"]' in branch
        assert "send nothing" in branch

    def test_the_client_instruction_still_goes_out(self):
        """
        The INR side is unaffected. The client must still be invoiced — not
        invoicing them is the fault this whole episode came from.
        """
        src = _code(bridge_trade.confirm_final)
        issued_at = src.index("render_payment_slot")
        checked_at = src.index("prior_payouts_for_trade")
        assert issued_at < checked_at


class TestItWarnsAndDoesNotBlock:
    def test_the_figure_is_available_on_request(self):
        src = _code(bridge_trade.confirm_final)
        assert 'callback_data=f"pay:{trade[\'id\']}"' in src

    def test_the_reveal_gives_the_number_with_no_second_argument(self):
        src = _code(bridge_trade.show_payout_amount)
        assert 'fmt_usdt_plain(trade["usdt_owed_client"])' in src
        assert "HOLD" not in src, "arguing twice is how a guard gets routed around"

    def test_the_reveal_survives_the_trade_vanishing(self):
        src = _code(bridge_trade.show_payout_amount)
        assert "if trade is None:" in src

    def test_both_paths_are_logged(self):
        assert "log.warning" in _code(bridge_trade.confirm_final)
        assert "log.info" in _code(bridge_trade.show_payout_amount)


class TestTheQueryIsNarrowEnoughToBeBelieved:
    def test_it_only_considers_untied_transfers(self):
        """A payout never gets a trade_id; anything that has one is not this."""
        assert "d.trade_id IS NULL" in _sql(Repo.prior_payouts_for_trade)

    def test_it_follows_the_pairings_own_payout_wallet(self):
        sql = _sql(Repo.prior_payouts_for_trade)
        assert "JOIN wallets pw ON pw.id = iw.payout_wallet_id" in sql

    def test_the_window_opens_at_this_trades_first_deposit(self):
        """
        Payouts normally follow the issue, not precede it — SUPB4 was
        instructed at 13:10:31 and paid at 13:11:23. A hit before issue means
        the order was unusual, which is the whole signal.
        """
        sql = _sql(Repo.prior_payouts_for_trade)
        assert "min(d.detected_at)" in sql
        assert "d.detected_at >= t.since" in sql

    def test_it_matches_on_the_amount_owed(self):
        """
        One client, one payout wallet, every supplier. Without the amount the
        window catches every other vendor's payouts too — on 18 September that
        would have been five unrelated transfers alongside the real one.
        """
        sql = _sql(Repo.prior_payouts_for_trade)
        assert "abs(d.amount_usdt - t.owed)" in sql

    def test_the_tolerance_allows_for_a_moved_sell_rate(self):
        """
        2,321.21 paid by hand at 107.5 against 2,321.22 owed at 107.7. An
        exact match would have missed it and said nothing.
        """
        sql = _sql(Repo.prior_payouts_for_trade)
        assert "t.owed * 0.02" in sql

    def test_a_trade_owing_nothing_is_skipped(self):
        assert "t.owed > 0" in _sql(Repo.prior_payouts_for_trade)

    def test_a_trade_with_no_deposits_matches_nothing(self):
        assert "t.since IS NOT NULL" in _sql(Repo.prior_payouts_for_trade)


class TestTheRealFigures:
    """The two that prompted it, and the five that must not be swept in."""

    def test_the_tolerance_covers_the_paisa_it_has_to(self):
        from decimal import Decimal as D

        for owed, paid in ((D("2321.22"), D("2321.21")),
                           (D("6922.01"), D("6922.00"))):
            assert abs(paid - owed) <= max(owed * D("0.02"), D("0.01"))

    def test_the_other_payouts_that_day_are_not_close_enough(self):
        from decimal import Decimal as D

        owed = D("2321.22")
        for other in (D("18569.71"), D("6922.00"), D("32497"),
                      D("32497.99"), D("46425.27")):
            assert abs(other - owed) > owed * D("0.02"), (
                f"{other} would have been reported against {owed}"
            )
