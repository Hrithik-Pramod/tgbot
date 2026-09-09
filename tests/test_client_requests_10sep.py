"""
The five changes asked for after the first live test, 10 September 2026.

Grouped by the request rather than by module, because that is how they will be
checked when the client asks whether they were done. His words are quoted
against each one.
"""

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest
from aiogram.filters import Command
from aiogram.utils.magic_filter import MagicFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import client_bot  # noqa: E402
from core.summary import (  # noqa: E402
    TRONSCAN_TX, Payment, render_payout_notice, render_trade_summary, tx_link,
    PaymentSlot, render_deposit_notification, render_payment_slot,
    render_send_instruction,
)


class TestDepositNotificationCarriesTheRates:
    """ "need to include the rate" and "need the step where it says how much
    usdt i am to send to client". """

    def _render(self, **over):
        args = dict(
            reference="SUPA1", supplier_label="Supplier A", client_label="Client A",
            usdt_in=D("19.74"), inr_out=D("2092"),
            tx_hash="2666780ac3865a654f168dae435a8d4a6c01c92987dbfa",
            wallet_address="TLETtEyvR7qm9ifTZCsAKBtZKHjrmWyNQZ",
            supply_rate=D("106"), sell_rate=D("107"), usdt_out=D("19.55"),
        )
        args.update(over)
        return render_deposit_notification(**args)

    def test_the_buy_rate_is_shown(self):
        assert "106" in self._render()

    def test_the_onward_amount_and_rate_are_shown(self):
        out = self._render()
        assert "19.55" in out and "107" in out
        assert "Send on to Client A" in out

    def test_the_header_the_client_asked_for_is_unchanged(self):
        """
        "at header must state Supplier A and Supplier B when notifying me with
        Internal wallet address" — 8 September. Adding rates must not disturb it.
        """
        lines = self._render().splitlines()
        assert lines[0] == "SUPPLIER A  ·  Transaction SUPA1"
        assert lines[1] == "Internal wallet TLETtEyvR7qm9ifTZCsAKBtZKHjrmWyNQZ"

    def test_the_wallet_address_is_never_truncated(self):
        assert "TLETtEyvR7qm9ifTZCsAKBtZKHjrmWyNQZ" in self._render()

    def test_the_amount_the_bridge_pastes_onward_has_no_separators(self):
        """
        "i dont want any commas" — 8 September.

        The rule that request became: a figure that gets pasted carries no
        separators, a figure that only gets read keeps them. A comma in a wallet
        field is rejected at best and silently truncated at worst.

        `usdt_out` is what the Bridge types into a wallet to send the client, so
        it is plain. `usdt_in` and the INR are read off the message, so they stay
        formatted — which is also how the client's own example was written.
        """
        out = self._render(usdt_in=D("14211.35"), usdt_out=D("14000.5"),
                           inr_out=D("1499297"))
        assert "14000.50" in out and "14,000.50" not in out, \
            "the onward amount is pasted into a wallet and must be plain"
        assert "14,211.35" in out       # read, not pasted
        assert "1,499,297" in out       # as in the client's own example

    def test_it_still_renders_without_rates(self):
        """Older callers, and any path where the rate is not to hand."""
        out = self._render(supply_rate=None, sell_rate=None, usdt_out=None)
        assert "SUPA1" in out and "Hash" in out


class TestSendInstructionGoesToTheBridge:
    """ "Confirm amount and details to send to Client A ... NEEDs rate i am
    giving client". """

    def test_it_carries_the_sell_rate(self):
        out = render_send_instruction(
            client_label="Client A", usdt_out=D("19.55"),
            inr_amount=D("2092"), sell_rate=D("107"),
        )
        assert "107" in out
        assert "USDT = 19.55" in out
        assert "Send INR 2,092" in out

    def test_the_usdt_figure_is_tap_to_copy_in_html(self):
        out = render_send_instruction(
            client_label="Client A", usdt_out=D("19.55"),
            inr_amount=D("2092"), sell_rate=D("107"), html=True,
        )
        assert "<code>19.55</code>" in out

    def test_the_client_label_is_escaped(self):
        out = render_send_instruction(
            client_label="Smith & Co", usdt_out=D("1"), inr_amount=D("1"),
            html=True,
        )
        assert "Smith &amp; Co" in out


class TestSlotsAreSelfContained:
    """ "need to be seperate messages to client, or their bot wont pick it up".

    Each slot is rendered on its own and sent on its own, so the client's parser
    receives one complete instruction per message with nothing else in it.
    """

    SLOTS = [
        PaymentSlot(account_name="Ekta Traders", account_number="123456789012345",
                    ifsc="EXBK0001234", amount_inr=D("1500")),
        PaymentSlot(account_name="Ekta Traders Pvt Ltd",
                    account_number="123456789012346",
                    ifsc="EXBK0001234", amount_inr=D("592")),
    ]

    def test_each_slot_stands_alone(self):
        for slot in self.SLOTS:
            out = render_payment_slot(slot)
            assert out.startswith("New slot")
            assert slot.account_number in out
            assert slot.ifsc in out
            assert slot.account_name in out

    def test_one_slot_never_mentions_another(self):
        first = render_payment_slot(self.SLOTS[0])
        assert self.SLOTS[1].account_name not in first
        assert "592" not in first

    def test_the_account_number_is_tap_to_copy(self):
        out = render_payment_slot(self.SLOTS[0], html=True)
        assert "<code>123456789012345</code>" in out

    def test_the_amount_has_no_separators(self):
        big = PaymentSlot(account_name="X", account_number="1", ifsc="Y",
                          amount_inr=D("249000"))
        assert "249000" in render_payment_slot(big)
        assert "249,000" not in render_payment_slot(big)


class TestExplorerLinks:
    """ "can this have the tronscan clickable hash, not just hash alone?" """

    HASH = "2666780ac3865a654f168dae435a8d4a6c01c92987dbfaf0e53961c0080c9c26"

    def test_html_produces_a_tronscan_link(self):
        out = tx_link(self.HASH, html=True)
        assert f'href="https://tronscan.org/#/transaction/{self.HASH}"' in out

    def test_the_whole_hash_stays_visible(self):
        """
        Not shortened to "view on TronScan". Two transactions can share a
        prefix, so anyone reconciling needs to read the full string.
        """
        assert f">{self.HASH}</a>" in tx_link(self.HASH, html=True)

    def test_plain_text_is_still_the_bare_hash(self):
        assert tx_link(self.HASH) == self.HASH

    def test_an_empty_hash_does_not_produce_a_broken_link(self):
        assert tx_link("", html=True) == ""

    def test_the_deposit_notice_links_the_hash(self):
        out = render_deposit_notification(
            reference="SUPA1", supplier_label="Supplier A", client_label="Client A",
            usdt_in=D("19.74"), inr_out=D("2092"), tx_hash=self.HASH, html=True,
        )
        assert "tronscan.org" in out


class TestPayoutNotice:
    """ "i just sent money onto client account but bot did not notify client of
    hash?" — the client is now told, with the hash, when funds reach their own
    wallet. """

    HASH = "2f948c999cd91f1834504cbeb8e4e31d074f5f4d6e4eff44736a203c7b375f15"

    def test_it_states_the_amount_and_links_the_hash(self):
        out = render_payout_notice(amount=D("19.55"), tx_hash=self.HASH, html=True)
        assert "19.55" in out
        assert f"{TRONSCAN_TX}{self.HASH}" in out

    def test_the_amount_has_no_separators(self):
        out = render_payout_notice(amount=D("14211.35"), tx_hash=self.HASH)
        assert "14211.35" in out and "14,211.35" not in out

    def test_it_says_nothing_about_the_trade(self):
        """
        The client's wallet receiving funds is not the place to restate rates,
        references or margins. D4 also applies: nothing identifying the supplier.
        """
        out = render_payout_notice(amount=D("19.55"), tx_hash=self.HASH)
        for leak in ("SUPA", "Supplier", "rate", "margin", "INR"):
            assert leak not in out


class TestSupplierSeesNoReference:
    """ "notification to supplier needs header TRADE COMPLETED" and "remove the
    SUPA1 as dont want them to see that". """

    PAYMENTS = [
        Payment(utr="BKIDR12026091000000001", amount_inr=D("1500"),
                beneficiary_name="Ekta Traders"),
        Payment(utr="BKIDR12026091000000002", amount_inr=D("592"),
                beneficiary_name="Ekta Traders Pvt Ltd"),
    ]

    def _supplier_copy(self):
        return "TRADE COMPLETED\n\n" + render_trade_summary(
            self.PAYMENTS, expected_inr=D("2092"), include_header=False
        )

    def test_the_header_is_trade_completed(self):
        assert self._supplier_copy().startswith("TRADE COMPLETED")

    def test_the_deal_reference_is_absent(self):
        out = self._supplier_copy()
        assert "SUPA1" not in out and "Transaction" not in out

    def test_the_figures_are_unchanged(self):
        """
        All three parties must still reconcile against identical numbers — only
        the heading differs.
        """
        supplier = self._supplier_copy()
        everyone_else = render_trade_summary(
            self.PAYMENTS, reference="SUPA1", expected_inr=D("2092")
        )
        for line in ("BKIDR12026091000000001", "1500", "to Ekta Traders",
                     "1500+592 = 2,092"):
            assert line in supplier and line in everyone_else


class TestPhotosWithCaptions:
    """
    "clien usually send image with text, will the bot allow for that?"

    It did not. A photo carries its text in `caption` and leaves `text` as None,
    so a filter on text alone dropped the message with no reaction and no
    record — indistinguishable, from the sender's side, from a bot that is down.
    """

    class _Msg:
        def __init__(self, text=None, caption=None):
            self.text = text
            self.caption = caption

        def __getattr__(self, name):
            return None

    @staticmethod
    def _paste_filter():
        for handler in client_bot.router.observers["message"].handlers:
            if handler.callback.__name__ != "on_pasted_payment":
                continue
            return [
                f.callback for f in handler.filters
                if not isinstance(f.callback, Command)
            ]
        raise AssertionError("the paste handler is gone")

    def _matches(self, msg):
        return all(bool(f(msg)) for f in self._paste_filter())

    PASTE = "BKIDR12026091000000001\n1500\nto Ekta Traders"

    def test_a_photo_caption_is_accepted(self):
        assert self._matches(self._Msg(caption=self.PASTE)), \
            "a screenshot with the details in the caption is still being ignored"

    def test_a_plain_text_paste_still_works(self):
        assert self._matches(self._Msg(text=self.PASTE))

    def test_a_photo_with_no_caption_is_ignored(self):
        """Nothing to read. Not an error, just not a payment."""
        assert not self._matches(self._Msg())

    def test_a_command_in_a_caption_is_not_swallowed(self):
        """The same trap that killed /done, one field over."""
        assert not self._matches(self._Msg(caption="/done@pt_client_desk_bot"))

    @pytest.mark.parametrize("command", ["/done", "/accounts", "/add"])
    def test_commands_still_reach_their_handlers(self, command):
        assert not self._matches(self._Msg(text=command))
