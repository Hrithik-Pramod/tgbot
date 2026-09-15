"""
A refused duplicate is reassurance, not an error.

WHAT HAPPENED (live, 15 September 2026, 21:06)

A payment already recorded against the open trade was pasted a second time.
The bot refused it, which is exactly right — the global unique index on the
reference is what makes counting the same money twice impossible. It then
said:

    UTR MAHBR52026091525060733 has already been recorded. Not added again.
    Recorded 0 of 1.
    Running total: ₹961,008

"Recorded 0 of 1" reads as a failure. The Bridge read it that way, went
looking, and could not find what he thought had been lost:

    has happened twice, but this is not in system.
    saying it was already sent?
    its like its double checking

The payment was there the whole time — ₹240,000 against SUPA8, recorded at
21:05:33, forty seconds before the message he was worried about. Nothing was
broken. The sentence was.

THE POINT

The duplicate rule stays exactly as it is. What changes is that a message
about money says what is true about the money: it is counted, and there is
nothing to do.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import client_bot  # noqa: E402


def _src():
    return inspect.getsource(client_bot._record)


class TestNothingNewReadsAsNothingWrong:
    def test_the_zero_case_has_its_own_wording(self):
        src = _src()
        assert "if added:" in src, (
            "a single sentence cannot serve both 'two of three went in' and "
            "'this was already counted'"
        )

    def test_it_says_the_payment_is_counted(self):
        src = _src()
        zero_case = src.split("else:")[1]
        assert "already" in zero_case and "counted" in zero_case

    def test_it_says_no_action_is_needed(self):
        """
        The Bridge went looking for a payment that was never missing. The
        message has to close that loop, not leave it open.
        """
        src = _src()
        assert "No action needed" in src

    def test_the_misleading_count_is_gone_from_the_zero_case(self):
        src = _src()
        zero_case = src.split("else:")[1].split("await message.reply")[0]
        assert "Recorded 0" not in zero_case
        assert "len(added)" not in zero_case


class TestThePartialCaseIsUnchanged:
    """
    One duplicate alongside one good payment is a different thing, and the
    client does need the count then — it tells them one of the two did not
    go in.
    """

    def test_a_partial_batch_still_reports_the_count(self):
        src = _src()
        assert 'f"Recorded {len(added)} of {len(staged)}."' in src

    def test_the_rejected_references_are_still_named(self):
        """E3: a duplicate is never hidden, whichever wording is used."""
        src = _src()
        assert "*rejected" in src

    def test_the_running_total_is_still_shown(self):
        src = _src()
        assert "*totals" in src


class TestTheRuleItselfIsUntouched:
    def test_duplicates_are_still_refused(self):
        """
        The wording changed; the protection did not. Recording the same
        reference twice is what the unique index exists to prevent.
        """
        schema = (Path(__file__).resolve().parents[1] / "db" / "schema.sql")
        text = schema.read_text(encoding="utf-8")
        assert "CREATE UNIQUE INDEX payments_utr_unique ON payments (utr)" in text

    def test_completion_is_still_checked_after_a_rejection(self):
        """
        A paste can carry one duplicate and one payment that finishes the
        trade. Returning early on the duplicate would leave it open.
        """
        src = _src()
        assert "check_completion" in src
