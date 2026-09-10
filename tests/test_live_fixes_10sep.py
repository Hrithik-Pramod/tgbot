"""
Faults found in the client's live groups on 10 September 2026, hours after
go-live. His words against each.

Two of these are mine: the copyable USDT figure was tap-to-copy before I moved
the send instruction to the Bridge channel and dropped the parse_mode, and the
chatty parser was a threshold I set too low and never exercised in a room where
people actually talk.
"""

import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.parse import parse_payments  # noqa: E402
from core.summary import (  # noqa: E402
    render_deposit_notification, render_send_instruction,
)


class TestItStopsAnsweringConversation:
    """
    "The bot keeps messaging any message we have on group please can you fix
    that?" and "the answering everything is annoying the client".

    The rule: speak only when a UTR was present. Anything else is conversation.
    """

    CHATTER = [
        "ok",
        "will send 5 lakh by 4pm",
        "we have 3 pending",
        "2 more coming",
        "call me on 9876543210",
        "done 👍",
        "sending in 10 mins",
        "1",
        "Rs 250000 today?",
    ]

    def test_ordinary_messages_produce_no_utr_signal(self):
        for text in self.CHATTER:
            assert not parse_payments(text).saw_utr, \
                f"the bot would answer {text!r}, which is conversation"

    def test_a_real_bank_reference_is_recognised(self):
        r = parse_payments("BKIDR12026091000000001\n1500\nto Ekta Traders")
        assert r.saw_utr

    def test_a_payment_missing_its_amount_still_gets_an_answer(self):
        """
        The case the message is FOR. A UTR was sent, so someone was logging a
        payment and needs telling it did not go in.
        """
        r = parse_payments("BKIDR12026091000000001\nto Ekta Traders")
        assert r.saw_utr and r.problems and not r.payments

    def test_a_phone_number_is_not_a_bank_reference(self):
        """Long digit strings are common in chat and must not trigger a reply."""
        assert not parse_payments("call 9876543210 when done").saw_utr

    def test_a_sentence_is_never_a_reference(self):
        """
        The root cause. Spaces were squashed out before the shape test, so
        "call 9876543210 when done" became "call9876543210whendone" —
        alphanumeric, contains a digit, long enough — and was read as a UTR.
        A reference is one unbroken token; a sentence is not.
        """
        from core.parse import classify

        kind, _ = classify("call 9876543210 when done")
        assert kind != "utr"

        kind, _ = classify("BKIDR12026091000000001")
        assert kind == "utr", "a real reference must still be recognised"

    def test_a_labelled_reference_still_works_with_spaces(self):
        """
        "UTR: ABC123" is matched by the label rule before the shape rule, so
        tightening the shape rule must not break it.
        """
        r = parse_payments("UTR: BKIDR12026091000000001\n1500\nto Ekta Traders")
        assert r.payments and r.payments[0].utr == "BKIDR12026091000000001"


class TestTheBridgeCanCopyTheUsdtFigure:
    """
    "I cannot copy just the Usdt value. For sending."

    The amount the Bridge types into a wallet must be copyable on its own. A
    <code> span is one tap in Telegram — but only if the message is actually
    sent with parse_mode HTML, which is what was missing.
    """

    def test_the_send_instruction_wraps_it(self):
        out = render_send_instruction(
            client_label="Client A", usdt_out=D("19.55"),
            inr_amount=D("2092"), sell_rate=D("107"), html=True,
        )
        assert "<code>19.55</code>" in out

    def test_the_deposit_notice_wraps_the_onward_amount(self):
        out = render_deposit_notification(
            reference="SUPA1", supplier_label="Supplier A", client_label="Client A",
            usdt_in=D("19.74"), inr_out=D("2092"), tx_hash="abc",
            supply_rate=D("106"), sell_rate=D("107"), usdt_out=D("19.55"),
            html=True,
        )
        assert "<code>19.55</code>" in out

    def test_the_copyable_value_has_no_separators(self):
        """A comma in a wallet field is rejected or silently truncated."""
        out = render_send_instruction(
            client_label="Client A", usdt_out=D("14211.35"),
            inr_amount=D("1499297"), sell_rate=D("107"), html=True,
        )
        assert "<code>14211.35</code>" in out
        assert "14,211.35" not in out

    def test_plain_text_carries_no_markup(self):
        """The Bridge preview is sent without parse_mode and must stay clean."""
        out = render_send_instruction(
            client_label="Client A", usdt_out=D("19.55"),
            inr_amount=D("2092"), sell_rate=D("107"),
        )
        assert "<code>" not in out and "19.55" in out

    def test_labels_are_escaped_when_markup_is_on(self):
        """An unescaped & in a party name breaks the whole message."""
        out = render_deposit_notification(
            reference="SUPA1", supplier_label="Haze & Co", client_label="A & B",
            usdt_in=D("1"), inr_out=D("1"), tx_hash="abc",
            usdt_out=D("1"), html=True,
        )
        assert "&amp;" in out and "Haze & Co" not in out
