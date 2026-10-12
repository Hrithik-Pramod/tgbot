"""
The bot's own sentences are never a beneficiary.

WHAT HAPPENED — 12 October 2026, 18:15

The audit log recorded an unmatched beneficiary with this as the name:

    "typed": "held — being confirmed, nothing further needed from you"

That is the bot's own reply. A message carrying a UTR, an amount, and a
quoted line of the bot's output was read back as a payment, and a record was
created attributing money to a phrase.

Clients quote replies back when they are chasing something, and a screenshot
of the chat contains both sides of the conversation. The parser is
deliberately forgiving about what counts as a name — "Ekta traders" must
survive — so a sentence in a reply looks exactly like a beneficiary to it.

Nothing was lost this time: the payment was already recorded, and the phantom
was voided within five minutes. But it put a question to the Bridge about
money that was already on the books, during the same three hours he was
being asked nineteen other questions.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import parse  # noqa: E402
from core.parse import parse_payments  # noqa: E402


class TestOurOwnWordsAreNotNames:
    def test_the_line_that_caused_it(self):
        body = ("BKIDR10000000000001\n"
                "380090\n"
                "held — being confirmed, nothing further needed from you")
        result = parse_payments(body)
        names = [p.beneficiary for p in result.payments]
        assert all(n is None or "being confirmed" not in n for n in names), (
            f"the bot read its own reply back as a beneficiary: {names}"
        )

    def test_the_whole_reply_block_pasted_back(self):
        """
        What a client actually sends when they quote the bot: the heading,
        their own slip, and the bot's reading of it.
        """
        body = ("Read this as:\n\n"
                "BKIDR10000000000001\n"
                "380090\n"
                "to ?  (account not recognised)\n\n"
                "Which account did these go to?")
        result = parse_payments(body)
        for p in result.payments:
            assert p.beneficiary is None or "recognised" not in p.beneficiary

    def test_a_duplicate_notice_quoted_back(self):
        body = ("BKIDR10000000000001\n"
                "380090\n"
                "UTR BKIDR10000000000001 has already been recorded. "
                "Not added again.")
        result = parse_payments(body)
        for p in result.payments:
            assert (p.beneficiary is None
                    or "already been recorded" not in p.beneficiary)

    def test_a_running_total_line_is_not_a_name(self):
        body = ("BKIDR10000000000001\n"
                "380090\n"
                "Recorded. Running total: 3473100")
        result = parse_payments(body)
        for p in result.payments:
            assert (p.beneficiary is None
                    or "Running total" not in p.beneficiary)


class TestRealNamesStillRead:
    """
    The parser is forgiving on purpose — wrongly discarding a line loses a
    payment. This must stay a narrow exception for sentences the bot itself
    writes, not a general tightening.
    """

    def test_an_ordinary_beneficiary_is_untouched(self):
        result = parse_payments(
            "BKIDR10000000000001\n200000\nto ROYAL TRADING COMPANY")
        assert len(result.payments) == 1
        assert result.payments[0].beneficiary == "ROYAL TRADING COMPANY"

    def test_a_name_containing_an_ordinary_word_still_reads(self):
        for name in ("Total Exports Ltd", "Record Traders",
                     "Confirmed Logistics", "Account Services Pvt"):
            result = parse_payments(f"BKIDR10000000000001\n200000\nto {name}")
            assert len(result.payments) == 1, name
            assert result.payments[0].beneficiary == name

    def test_several_real_payments_still_read(self):
        body = "\n\n".join(
            f"BKIDR1000000000000{i}\n20000{i}\nto EKTA TRADERS"
            for i in (1, 2, 3))
        assert len(parse_payments(body).payments) == 3


class TestTheListIsDeliberate:
    def test_every_phrase_is_something_the_bot_says(self):
        """
        Each entry must be traceable to a real message, or this becomes a
        list of words nobody may be called.
        """
        from bot import client_bot
        from db import repo as repo_mod

        haystack = "\n".join([
            Path(client_bot.__file__).read_text(encoding="utf-8"),
            Path(repo_mod.__file__).read_text(encoding="utf-8"),
        ]).lower()

        for phrase in parse._OUR_OWN_WORDS:
            assert phrase.lower() in haystack, (
                f"{phrase!r} is filtered out but the bot never says it — "
                "either it was reworded, or the entry is wrong"
            )

    def test_the_phrases_are_long_enough_to_be_distinctive(self):
        """
        A short phrase would catch real company names. Every entry has to be
        a clause rather than a word — two words only where they are long
        enough together to be nobody's trading name.
        """
        for phrase in parse._OUR_OWN_WORDS:
            words = len(phrase.split())
            assert words >= 2, phrase
            assert words >= 3 or len(phrase) >= 12, (
                f"{phrase!r} is short enough to catch a real company name"
            )
