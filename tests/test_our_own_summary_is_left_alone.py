"""
The bot's own summary, pasted back, is not a batch of payments.

WHAT HAPPENED — 16 October 2026, 10:17:23

Twenty-two payments reported in the same second. All "ROYAL TRADING
COMPANY". One of them typed "TRADE COMPLETED".

Somebody had pasted the bot's own closing summary back into the group, and
the bot read every line of it as a payment. It happened on the morning the
Bridge was already asking why slips were not being picked up:

    The body is starting to act up again. It waited and missed a load of
    trays to paste them. The bot is not picking up slips
    — Bridge, 17 October 2026

WHY IT CANNOT BE FILTERED LINE BY LINE

The summary renders each payment as

    BKIDR1...
    232000
    to ROYAL TRADING COMPANY

which is character for character what a real slip looks like. There is
nothing in those three lines to tell them apart, and there should not be —
they are a faithful record of a real payment.

Only the HEADING says what the block is, and a heading is the whole
message's business, not one line's. So the test is on the message.

NOTHING WAS EVER AT RISK

Every UTR in such a block is already on the ledger and the duplicate guard
refuses them all. The cost was not money, it was twenty-two alarms at once
that he had to read through to discover none of them mattered — while he
was looking for a real failure.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import parse  # noqa: E402
from core.parse import looks_like_our_own_summary, parse_payments  # noqa: E402


TRADE_COMPLETED = """TRADE COMPLETED

BKIDR10000000000001
232000
to ROYAL TRADING COMPANY
BKIDR10000000000002
261634
to ROYAL TRADING COMPANY

232000+261634 = 493,634

Send Next Trade"""

MINI_STATEMENT = """Payments received

  1. BKIDR10000000000001
     ₹232,000   →  ROYAL TRADING COMPANY

Collected     ₹232,000
Expected      ₹3,000,000
Outstanding   ₹2,768,000"""


class TestOurOwnBlocksAreRecognised:
    def test_the_completion_summary(self):
        assert looks_like_our_own_summary(TRADE_COMPLETED)

    def test_the_mini_statement(self):
        assert looks_like_our_own_summary(MINI_STATEMENT)

    def test_a_collection_progress_block(self):
        assert looks_like_our_own_summary(
            "Collection progress\n\nExpected ₹1\nCollected ₹1")

    def test_a_chase_notice(self):
        assert looks_like_our_own_summary(
            "STILL WAITING — a payment is not recorded.\n\n₹243,000")

    def test_it_is_not_case_sensitive(self):
        assert looks_like_our_own_summary("trade completed\n\nanything")


class TestRealSlipsAreNot:
    def test_a_single_payment(self):
        assert not looks_like_our_own_summary(
            "BKIDR10000000000001\n200000\nto ROYAL TRADING COMPANY")

    def test_a_batch_of_payments(self):
        body = "\n\n".join(
            f"BKIDR1000000000000{i}\n20000{i}\nto EKTA TRADERS"
            for i in (1, 2, 3))
        assert not looks_like_our_own_summary(body)

    def test_a_screenshot_caption(self):
        assert not looks_like_our_own_summary(
            "Payment Successful\nBKIDR10000000000001\n200000\nto EKTA TRADERS")

    def test_ordinary_conversation(self):
        for line in ("trade is completed on our side",
                     "sending next trade shortly",
                     "what is outstanding on this"):
            assert not looks_like_our_own_summary(line), line


class TestTheHandlerLeavesItAlone:
    def test_the_block_is_not_read_as_payments(self):
        """
        Without the guard this parses as two payments and reports both.
        """
        assert len(parse_payments(TRADE_COMPLETED).payments) == 2, (
            "the premise has changed — the block no longer parses as "
            "payments, so this test is no longer about anything"
        )
        assert looks_like_our_own_summary(TRADE_COMPLETED), (
            "and yet the handler would read it"
        )

    def test_the_handler_checks_before_parsing(self):
        import inspect
        from bot import client_bot
        src = inspect.getsource(client_bot.on_pasted_payment)
        guard = src.index("looks_like_our_own_summary")
        parse_call = src.index("result = parse_payments(body)")
        assert guard < parse_call, (
            "the message is parsed before it is recognised as our own"
        )


class TestTheHeadingsAreDeliberate:
    def test_each_one_is_something_the_bot_prints(self):
        from core import summary
        # Every place the bot composes a block, not just the renderers:
        # "Outstanding across the book" is built in the /progress handler.
        haystack = "\n".join(
            Path(p).read_text(encoding="utf-8").lower()
            for p in (summary.__file__,
                      ROOT / "bot" / "notifier.py",
                      ROOT / "bot" / "bridge_bot.py",
                      ROOT / "bot" / "supplier_bot.py")
        )
        for heading in parse._OUR_OWN_BLOCKS:
            assert heading in haystack, (
                f"{heading!r} is filtered but the bot never prints it"
            )

    def test_each_one_is_a_phrase_not_a_word(self):
        """
        A single word would silence real messages. "Outstanding" alone would
        catch a client asking what is outstanding.
        """
        for heading in parse._OUR_OWN_BLOCKS:
            assert len(heading.split()) >= 2, heading
