"""
What each party is allowed to learn.

Written after 11 September 2026, when a client was shown the Bridge's whole
supplier list — both supplier names, all five account holders, and which
belonged to which — in their own group.

That one was caught because it was glaring. This file exists because the same
mistake in quieter form was in four other messages nobody had looked at: the
deal reference. "SUPA1" is not an opaque id, it names the supplier. A client
holding summaries for SUPA1 and SUPB1 can count the Bridge's suppliers and
tell them apart, which is the same disclosure by another route.

THE RULE

  Bridge     everything.
  Supplier   their own trade, their own accounts, what has been collected.
             Not the client, not the rates, not the margin, not the reference.
  Client     what to pay, where, and confirmation it landed.
             Not the suppliers, not how many there are, not the reference.

A reference identifies a supplier. It goes to the Bridge and no one else.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import client_bot, notifier as notifier_mod, supplier_bot  # noqa: E402


def _sending_lines(fn):
    """
    Lines that put something into a message, with comments and docstrings
    stripped out — the rule is about what is sent, not what is discussed.
    """
    out, in_doc = [], False
    for raw in inspect.getsource(fn).splitlines():
        line = raw.strip()
        if line.startswith('"""') or line.startswith("'''"):
            in_doc = not in_doc or line.count('"""') == 2 and False
            continue
        if in_doc or line.startswith("#") or not line:
            continue
        out.append(line)
    return out


class TestTheClientNeverLearnsTheSupplier:
    HANDLERS = [
        client_bot.cmd_accounts,
        client_bot._render_accounts,
        client_bot.on_pasted_payment,
        client_bot.on_edited_payment,
        client_bot.add_utr,
        client_bot.cmd_done,
    ]

    def test_no_supplier_label_reaches_a_client_message(self):
        offenders = [
            f"{fn.__name__}: {line}"
            for fn in self.HANDLERS
            for line in _sending_lines(fn)
            if "supplier_label" in line
        ]
        assert not offenders, "\n  ".join(["supplier named to a client:"] + offenders)

    def test_done_does_not_list_the_open_trades(self):
        """
        Listing them names both the references and the suppliers behind them.
        The count is all the client needs.
        """
        lines = _sending_lines(client_bot.cmd_done)
        assert not any("t['reference']" in ln or 't["reference"]' in ln
                       for ln in lines if "counterparty" not in ln and "to_bridge" not in ln), \
            "/done is still naming trades to the client"

    def test_the_clients_summary_carries_no_reference(self):
        src = inspect.getsource(client_bot.cmd_done)
        assert "counterparty_summary" in src, \
            "the client is being sent the Bridge's summary, reference and all"
        assert "include_header=False" in src


class TestTheSupplierNeverLearnsTheReference:
    def test_the_near_completion_notice_omits_it(self):
        lines = _sending_lines(notifier_mod.Notifier.check_near_completion)
        sent_to_supplier = "\n".join(lines).split("to_bridge")[0]
        assert "reference" not in sent_to_supplier, \
            "the supplier is still told the deal reference"

    def test_the_send_confirmation_omits_it(self):
        lines = _sending_lines(supplier_bot.send_account)
        assert not any("{attached}" in ln for ln in lines), \
            "/send still echoes the deal reference back to the supplier"

    def test_collection_progress_omits_it(self):
        from decimal import Decimal as D
        from core.summary import render_collection_progress

        out = render_collection_progress(expected_inr=D("2000"), paid_inr=D("1000"))
        for leaked in ("SUPA", "SUPB", "Transaction", "Client"):
            assert leaked not in out


class TestTheBridgeStillSeesEverything:
    """
    The narrowing must not blind the operator. They are the one party that
    needs the reference, both sides and the figures.
    """

    def test_the_bridge_summary_keeps_the_reference(self):
        src = inspect.getsource(notifier_mod.Notifier.check_completion)
        assert "reference=trade[\"reference\"]" in src
        assert "to_bridge(summary)" in src

    def test_the_deposit_notice_still_names_both_sides(self):
        from decimal import Decimal as D
        from core.summary import render_deposit_notification

        out = render_deposit_notification(
            reference="SUPA1", supplier_label="Supplier A", client_label="Client A",
            usdt_in=D("100"), inr_out=D("10600"), tx_hash="abc",
            supply_rate=D("106"), sell_rate=D("107"), usdt_out=D("98.9"),
        )
        assert "SUPA1" in out and "Supplier A" in out and "Client A" in out
