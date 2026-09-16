"""
One settlement house, two vendors, and still no guessing.

THE REQUEST (Bridge, 16 September 2026)

    We may need to use these accounts on seperated vendors, is there a way to
    tag to the Supplier who sent, as opposed to specific supplier group?

SUPER TRADING COMPANY (STC) is registered twice — once under IndoLondon,
once under New SUPER Group — because two vendors genuinely settle through
it. My first instinct was to ask him to retire one registration. That was
wrong: it would have cost him a real vendor relationship to work around a
limitation of ours.

WHY IT BECAME URGENT

match_account refuses to choose between two identically-named accounts, and
always has. Until 16 September the client-side matcher only saw suppliers
the client had been INSTRUCTED to pay, so usually only one STC was in the
list and it matched cleanly. Widening the matcher that evening — so the
cancelled trades of Malegao - Sam and GS Group stopped hiding their accounts
— put both STC rows in front of it every time. Every STC payment would now
have gone to the buttons.

That was my change and I did not see this coming out of it.

THE ANSWER, WHICH IS HIS

The tie is not a tie. A client pays an instruction, and an instruction only
exists where a trade is open. A vendor with nothing open cannot be the one
who sent. So an ambiguous name is re-run against only the candidates
carrying a live trade — tagging the payment to the supplier who sent, rather
than to whichever group happens to hold the registration.

WHAT IS UNCHANGED

  both vendors open     a genuine tie. Still refuses, still asks, and the
                        buttons carry the last four digits so the question
                        can actually be answered.
  an exact match        still wins over a partial one. The narrowing is a
                        fallback, never the first attempt.
  a matched account
  with no live trade    still reported to the Bridge and never recorded.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.parse import match_account, match_beneficiary  # noqa: E402

STC = "SUPER TRADING COMPANY (STC)"


def acct(id, name, trade=None):
    return {"id": id, "account_name": name, "trade_id": trade}


class TestTheSupplierWhoSentIsTheOneWithATrade:
    def test_the_registration_with_the_open_trade_wins(self):
        """
        Both rows are STC. IndoLondon has nothing open; New SUPER Group does.
        The client can only be paying the one they were instructed on.
        """
        accounts = [acct(2, STC, None), acct(13, STC, 77)]
        assert match_beneficiary(STC, accounts) == 13

    def test_it_works_whichever_side_holds_the_trade(self):
        accounts = [acct(2, STC, 91), acct(13, STC, None)]
        assert match_beneficiary(STC, accounts) == 2

    def test_the_old_matcher_would_have_refused_both(self):
        """The thing being fixed, pinned so the fix cannot quietly vanish."""
        accounts = [acct(2, STC, None), acct(13, STC, 77)]
        assert match_account(STC, accounts) is None

    def test_a_third_registration_with_no_trade_changes_nothing(self):
        accounts = [acct(2, STC, None), acct(13, STC, 77), acct(21, STC, None)]
        assert match_beneficiary(STC, accounts) == 13


class TestATrueTieStillRefuses:
    """
    Two vendors both mid-trade through the same house is the case where
    guessing would put real money against the wrong vendor. It has to ask.
    """

    def test_two_open_trades_on_one_name_is_refused(self):
        accounts = [acct(2, STC, 91), acct(13, STC, 77)]
        assert match_beneficiary(STC, accounts) is None

    def test_neither_having_a_trade_is_refused(self):
        accounts = [acct(2, STC, None), acct(13, STC, None)]
        assert match_beneficiary(STC, accounts) is None

    def test_a_name_nobody_holds_is_still_unmatched(self):
        accounts = [acct(2, STC, 91), acct(13, "Ekta traders", None)]
        assert match_beneficiary("Barkaati Textile", accounts) is None


class TestTheNarrowingCannotInventAMatch:
    def test_an_exact_match_beats_a_partial_one_on_a_live_account(self):
        """
        The reason the full list is tried first. Preferring live accounts up
        front would send "Ekta traders" to "Ekta traders Pvt Ltd" purely
        because the second had a trade open — a wrong vendor, arriving
        through the fix.
        """
        accounts = [
            acct(2, "Ekta traders", None),
            acct(13, "Ekta traders Pvt Ltd", 77),
        ]
        assert match_beneficiary("Ekta traders", accounts) == 2

    def test_an_unambiguous_idle_account_still_matches(self):
        """
        It must still resolve, so the no-open-trade alert can fire on it.
        Silence is what cost ₹1,419,016 on 15 September.
        """
        accounts = [acct(2, "Barkaati Textile", None), acct(13, STC, 77)]
        assert match_beneficiary("Barkaati Textile", accounts) == 2

    def test_a_single_candidate_is_untouched(self):
        assert match_beneficiary(STC, [acct(2, STC, None)]) == 2
        assert match_beneficiary(STC, [acct(2, STC, 77)]) == 2

    def test_every_candidate_being_live_leaves_the_result_alone(self):
        accounts = [acct(2, "Ekta traders", 91), acct(13, STC, 77)]
        assert match_beneficiary("Ekta traders", accounts) == 2

    def test_an_empty_name_matches_nothing(self):
        assert match_beneficiary(None, [acct(2, STC, 77)]) is None
        assert match_beneficiary("", [acct(2, STC, 77)]) is None

    def test_no_candidates_at_all(self):
        assert match_beneficiary(STC, []) is None


class TestTheHandlersUseIt:
    def test_both_client_paths_go_through_the_tiebreak(self):
        import inspect

        from bot import client_bot

        for fn in (client_bot.on_pasted_payment, client_bot.on_edited_payment):
            src = inspect.getsource(fn)
            assert "match_beneficiary(" in src
            assert "match_account(" not in src, (
                "the plain matcher cannot break the STC tie"
            )

    def test_a_rows_missing_trade_key_is_treated_as_idle(self):
        """
        Not every candidate list carries a trade. Reading one that does not
        must not raise inside a money path.
        """
        accounts = [{"id": 2, "account_name": STC}, acct(13, STC, 77)]
        assert match_beneficiary(STC, accounts) == 13
