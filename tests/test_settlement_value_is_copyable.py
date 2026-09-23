"""
The settlement figure can be copied with one tap.

WHAT HAPPENED (live, 23 September 2026)

    the copy paste is not working again for settlement values

"Again" because this exact fault was fixed once, on /send's instruction, and
the message he actually acts on never got the same treatment.

The deposit notification is where the settlement figure lives:

    Send on to Client A: 4642.43 USDT at 107.7

That number gets typed into a wallet. render_deposit_notification wraps it
in <code> so Telegram makes it one tap to copy — but ONLY when html=True,
and Telegram only renders the markup when the message carries
parse_mode="HTML". Both call sites passed neither:

    await self.to_bridge(
        render_deposit_notification(...)      # html defaults to False
        + note,
        reply_markup=confirm_keyboard(trade_id),
    )                                          # to_bridge html defaults False

So it went out as plain text and he had to select the digits by hand on a
phone, mid-trade, out of a line that also contains a rate and a client name.
Selecting "4642.43" by dragging catches the space or the "USDT" about as
often as not.

WHY BOTH HALVES ARE ASSERTED SEPARATELY

They fail in opposite, equally silent ways. html=True without parse_mode
prints literal "<code>4642.43</code>" on screen. parse_mode without
html=True sends valid HTML containing no markup, which looks completely
normal and simply is not copyable. Neither raises.

WHAT MUST NOT COME WITH IT

Turning on HTML means every interpolated value is now markup. Account names
and client labels come from user input, and an unescaped "&" makes Telegram
reject the whole message — losing the notification for a deposit that has
already landed.
"""

import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import notifier as notifier_mod  # noqa: E402
from core.summary import render_deposit_notification  # noqa: E402

BASE = dict(
    reference="SUPA22", supplier_label="Supplier A", client_label="Client A",
    usdt_in=D("4708"), inr_out=D("499990"), tx_hash="0xabc",
    wallet_address="TWusjSZHj3HChBpM3ndBx3Rkdd1wfCujBB",
    supply_rate=D("106.2"), sell_rate=D("107.7"),
    usdt_out=D("4642.43"),
)


class TestTheFigureIsWrappedForCopying:
    def test_it_is_a_code_span_in_html(self):
        out = render_deposit_notification(**BASE, html=True)
        assert "<code>4642.43</code>" in out

    def test_it_is_bare_in_plain_text(self):
        """The renderer must not emit tags nobody will parse."""
        out = render_deposit_notification(**BASE, html=False)
        assert "<code>" not in out
        assert "4642.43" in out

    def test_it_never_carries_separators(self):
        """
        It is pasted into a wallet, where a comma is rejected at best and
        silently truncated at worst (client request, 8 September 2026).
        """
        out = render_deposit_notification(
            **{**BASE, "usdt_out": D("37209.45")}, html=True)
        assert "<code>37209.45</code>" in out
        assert "37,209" not in out

    def test_it_never_carries_trailing_zeros(self):
        """
        usdt_owed_client is NUMERIC(20,6), so the raw value is 4642.430000.
        Copying that into a wallet is the 21 September complaint again.
        """
        out = render_deposit_notification(
            **{**BASE, "usdt_out": D("4642.430000")}, html=True)
        assert "<code>4642.43</code>" in out
        assert "4642.430000" not in out


class TestBothHalvesAreWiredAtEveryCallSite:
    """
    Asserted on the source because the failure is invisible at runtime: the
    message sends, looks fine, and simply cannot be copied.
    """

    def _calls(self):
        src = inspect.getsource(notifier_mod)
        chunks = src.split("render_deposit_notification(")[1:]
        assert chunks, "no deposit notification call found"
        return chunks

    def test_every_render_passes_html(self):
        for chunk in self._calls():
            call = chunk.split("reply_markup=")[0]
            assert "html=True" in call, (
                "a deposit notification is rendered without html=True, so "
                "the settlement figure has no <code> span:\n" + call[:400]
            )

    def test_every_send_passes_html(self):
        """
        The other half. Markup with no parse_mode shows the tags on screen.
        """
        for chunk in self._calls():
            send = chunk.split("reply_markup=")[1][:200]
            assert "html=True" in send, (
                "to_bridge is called without html=True, so parse_mode is "
                "None and the <code> markup renders literally:\n" + send
            )

    def test_to_bridge_maps_html_to_parse_mode(self):
        src = inspect.getsource(notifier_mod.Notifier.to_bridge)
        assert 'parse_mode="HTML" if html else None' in src


class TestTurningOnHtmlCannotBreakTheMessage:
    def test_user_supplied_names_are_escaped(self):
        """
        Account names and group titles come from Telegram and from whatever
        the Bridge typed. An unescaped & makes Telegram reject the message
        outright — and it is the notification for a deposit that has already
        landed, so losing it is not recoverable by retrying.
        """
        out = render_deposit_notification(
            **{**BASE, "client_label": "Haze & ProperPay",
               "nominated_account": "Smith & Sons <Pvt>"},
            html=True,
        )
        assert "Haze &amp; ProperPay" in out
        assert "Smith &amp; Sons &lt;Pvt&gt;" in out
        # The only raw tags in the message are the ones we put there.
        assert out.count("<") == out.count("<code>") + out.count("</code>") \
            + out.count("<a href=") + out.count("</a>")

    def test_the_hash_link_follows_the_same_flag(self):
        plain = render_deposit_notification(**BASE, html=False)
        rich = render_deposit_notification(**BASE, html=True)
        assert "<a href=" in rich
        assert "<a href=" not in plain
