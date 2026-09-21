"""
The bot reads the half of the slip the money went TO.

WHAT HAPPENED (live, 21 September 2026, mid-trade)

A payment slip was pasted carrying both ends of the transfer. The bot took
the wrong one:

    typed:  "from 881410110011285"
    utr:    BKIDR12026092100006053
    amount: ₹228,000

881410110011285 is not a registered account and never was — it is the
client's own, the account the money LEFT. The bot compared
"from881410110011285" against every supplier account name, matched nothing,
and asked which account it was.

    and what stopped the bot pick up slips?

Nothing stopped it. It never had a chance: it was looking at the sender.

HOW IT GOT THERE

classify() ends with "anything that reads as words is a name", which is
right for "Barkaati Textile" and wrong for any line that happens to contain
a word. "from 881410110011285" contains "from", so it fell through every
other rule and landed on the beneficiary catch-all.

The parser already knew "to" means the beneficiary. It had never been told
that "from" means the opposite.

WHY THE RULE IS NARROW

Only a line that BEGINS with a sender word. "Transfer from HDFC to Ekta
Traders" names the beneficiary further along and still has to be read — the
rule discards the sender's line, not every mention of the word.
"""

import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.parse import classify, match_account, parse_payments  # noqa: E402


class TestTheSendersLineIsDiscarded:
    def test_the_exact_line_from_the_incident(self):
        assert classify("from 881410110011285")[0] == "noise"

    def test_case_does_not_matter(self):
        for line in ["From 881410110011285", "FROM 881410110011285"]:
            assert classify(line)[0] == "noise"

    def test_the_other_words_banks_use(self):
        for line in ["Sender: 881410110011285",
                     "Debited from 881410110011285",
                     "Debit 881410110011285",
                     "Payer 881410110011285",
                     "Remitter: HDFC 0012"]:
            assert classify(line)[0] == "noise", line

    def test_a_separator_after_the_word_is_handled(self):
        for line in ["from: 881410110011285", "from - 881410110011285",
                     "From. 881410110011285"]:
            assert classify(line)[0] == "noise", line


class TestTheBeneficiaryIsStillRead:
    def test_the_agreed_format_is_untouched(self):
        assert classify("to Ekta traders") == ("beneficiary", "Ekta traders")
        assert classify("Acc name - Barkaati Textile")[0] == "beneficiary"

    def test_a_bare_name_is_still_a_name(self):
        assert classify("Barkaati Textile") == ("beneficiary", "Barkaati Textile")
        assert classify("SUPER TRADING COMPANY (STC)")[0] == "beneficiary"

    def test_a_word_beginning_with_from_is_not_a_sender(self):
        """
        The lookahead earns its place. "Froment Industries" is a name, and
        discarding it would lose a payment.
        """
        assert classify("froment industries")[0] == "beneficiary"
        assert classify("Fromage Traders")[0] == "beneficiary"

    def test_from_in_the_middle_keeps_the_line(self):
        """
        Some banks write both ends on one line. The beneficiary is in there
        and must survive.
        """
        kind, value = classify("Transfer from HDFC to Ekta Traders")
        assert kind == "beneficiary"
        assert "Ekta Traders" in value

    def test_the_line_still_matches_its_account(self):
        accounts = [{"id": 1, "account_name": "Ekta traders"},
                    {"id": 2, "account_name": "Barkaati Textile"}]
        _, value = classify("Transfer from HDFC to Ekta Traders")
        assert match_account(value, accounts) == 1


class TestTheWholeSlipNow:
    def test_the_slip_that_failed_now_reads_its_beneficiary(self):
        """
        The same paste, with the beneficiary line the bank puts beside the
        sender. Before the fix the sender's line won and the payment went
        unmatched.
        """
        result = parse_payments(
            "BKIDR12026092100006053\n"
            "228000\n"
            "from 881410110011285\n"
            "to Barkaati Textile"
        )
        assert len(result.payments) == 1
        p = result.payments[0]
        assert p.utr == "BKIDR12026092100006053"
        assert p.amount_inr == D("228000")
        assert p.beneficiary == "Barkaati Textile"

    def test_a_slip_with_only_a_sender_names_nobody(self):
        """
        Honest silence. With no beneficiary the payment is still read and
        the client is asked which account — but the bot must not offer the
        sender's number as an answer.
        """
        result = parse_payments(
            "BKIDR12026092100006053\n228000\nfrom 881410110011285"
        )
        assert len(result.payments) == 1
        assert result.payments[0].beneficiary is None

    def test_the_amount_and_utr_survive_a_sender_line(self):
        result = parse_payments(
            "from 881410110011285\nBKIDR12026092100006053\n228000"
        )
        assert result.saw_utr
        assert result.payments[0].amount_inr == D("228000")

    def test_ordinary_conversation_is_still_ignored(self):
        """The 10 September chattiness fix stands."""
        for line in ["send 5 lakh by 4", "ok will do", "any update?"]:
            r = parse_payments(line)
            assert not r.payments and not r.saw_utr, line
