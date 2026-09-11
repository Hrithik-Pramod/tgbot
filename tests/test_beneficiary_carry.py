"""
A beneficiary named once applies to every payment in the block.

THE INCIDENT

11 September 2026. The client pasted several payments with the account name
written once at the top. A new payment starts whenever a field repeats, so the
name attached to the first payment and to nothing after it. The bot could not
attribute the rest, so it asked which account they had gone to — and nobody
tapped. The pending conversation was held in memory; the restart discarded it.

Four payments, ₹902,460, absent from the ledger. The first anyone knew was the
client asking why a completed trade had not closed, three hours later.

The Bridge, shown the prompt afterwards:

    why did they see the other message about choosing account to use?

    And why is our bot not picking up some of the messages they are sending
    for trades?

THE RULE

Fill gaps, never overwrite. A name written above a payment always wins; a
payment with no name above it inherits the nearest one that precedes it, and
if none does, the first one in the message.
"""

import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.parse import match_account, parse_payments  # noqa: E402

ACCOUNTS = [
    {"id": 1, "account_name": "Ekta traders"},
    {"id": 5, "account_name": "Girish Kumar Ahirwar"},
]


class TestTheShapeThatLostTheMoney:
    PASTE = """to Ekta traders
BKIDR12026091100005860
213460
BKIDR12026091100007436
233000
BKIDR12026091100005832
215300
BKIDR12026091100007403
240700"""

    def test_all_four_are_read(self):
        result = parse_payments(self.PASTE)
        assert not result.problems
        assert len(result.payments) == 4

    def test_every_one_carries_the_account(self):
        result = parse_payments(self.PASTE)
        missing = [p.utr for p in result.payments if not p.beneficiary]
        assert not missing, f"still unattributed, so the bot still asks: {missing}"

    def test_every_one_resolves_to_a_real_account(self):
        """The end of the chain — this is what decides the trade."""
        result = parse_payments(self.PASTE)
        assert [match_account(p.beneficiary, ACCOUNTS) for p in result.payments] \
            == [1, 1, 1, 1]

    def test_the_amounts_are_the_ones_that_went_missing(self):
        result = parse_payments(self.PASTE)
        amounts = [p.amount_inr for p in result.payments]
        assert amounts == [D("213460"), D("233000"), D("215300"), D("240700")]
        assert sum(amounts) == D("902460")


class TestItFillsGapsWithoutOverwriting:
    def test_a_name_written_above_a_payment_always_wins(self):
        result = parse_payments(
            "to Ekta traders\n111111111111\n1000\n"
            "to Girish Kumar Ahirwar\n222222222222\n2000"
        )
        assert [p.beneficiary for p in result.payments] == [
            "Ekta traders", "Girish Kumar Ahirwar",
        ]

    def test_two_accounts_in_one_paste_keep_their_own(self):
        """
        The client's usual layout puts the name last, so a name binds to the
        payment above it. Two payments to two accounts stay separate.
        """
        result = parse_payments(
            "111111111111\n1000\nto Ekta traders\n"
            "222222222222\n2000\nto Girish Kumar Ahirwar"
        )
        assert [match_account(p.beneficiary, ACCOUNTS) for p in result.payments] \
            == [1, 5]

    def test_a_trailing_name_does_not_leak_backwards_past_a_named_payment(self):
        result = parse_payments(
            "111111111111\n1000\nto Ekta traders\n222222222222\n2000"
        )
        assert result.payments[0].beneficiary == "Ekta traders"
        # the second inherits it, which is the whole point
        assert result.payments[1].beneficiary == "Ekta traders"

    def test_a_name_written_at_the_end_reaches_back(self):
        """
        Some people type the payments and then say where they went. There is
        no other account it could mean.
        """
        result = parse_payments(
            "111111111111\n1000\n222222222222\n2000\nto Ekta traders"
        )
        assert all(p.beneficiary == "Ekta traders" for p in result.payments)

    def test_no_name_at_all_still_yields_nothing(self):
        """
        Inference only ever spreads a name the client actually wrote. With
        none in the message the bot must still stop and ask, not invent one.
        """
        result = parse_payments("111111111111\n1000\n222222222222\n2000")
        assert all(p.beneficiary is None for p in result.payments)
        assert all(match_account(p.beneficiary, ACCOUNTS) is None
                   for p in result.payments)


class TestTheOrdinaryFormatIsUnchanged:
    """
    The client usually repeats the account under every amount. That path was
    already correct and must stay bit-for-bit the same.
    """

    def test_a_name_per_payment(self):
        result = parse_payments(
            "BKIDR12026091100005720\n261000\nto Ekta traders\n"
            "BKIDR12026091100007318\n240030\nto Ekta traders"
        )
        assert len(result.payments) == 2
        assert all(p.beneficiary == "Ekta traders" for p in result.payments)

    def test_a_single_payment_with_no_name(self):
        result = parse_payments("BKIDR12026091100005720\n261000")
        assert len(result.payments) == 1
        assert result.payments[0].beneficiary is None


class TestTheLimitOfWhatCanBeInferred:
    """
    Stated so nobody later reads the carry-forward as cleverer than it is.

    A name binds to the payment it sits with — which, in this client's usual
    layout, is the one above it. A paste that opens with a header name AND
    then switches to trailing names is ambiguous to any reader, and the parser
    resolves it by position rather than guessing intent.

    This is documented rather than fixed because the fix would be a guess. If
    it ever bites, the answer is for the client to name every payment, which
    is what they already do most of the time.
    """

    def test_a_header_name_followed_by_a_trailing_name_binds_by_position(self):
        result = parse_payments(
            "to Ekta traders\n111111111111\n1000\n"
            "222222222222\n2000\nto Girish Kumar Ahirwar"
        )
        assert [match_account(p.beneficiary, ACCOUNTS) for p in result.payments] \
            == [1, 5], (
            "if this changes, the carry-forward has started overriding a name "
            "the client actually wrote"
        )


class TestItDoesNotMakeTheBotChattier:
    """
    The silence rule cost a day to get right — the bot answering every message
    containing a digit made the client's group unusable on 10 September. None
    of this may loosen it.
    """

    def test_ordinary_conversation_is_still_ignored(self):
        for line in [
            "send 5 lakh by 4",
            "ok will do",
            "call 9876543210 when done",
            "to Ekta traders",
        ]:
            result = parse_payments(line)
            assert not result.payments
            assert not result.saw_utr, f"would now speak on: {line!r}"
