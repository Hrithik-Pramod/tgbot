"""
What a client is allowed to see.

Live complaint, 11 September 2026. Asked which account a payment went to, the
bot offered the client this list, in their own group:

    Supplier A — Bhagavati  trading co
    Supplier A — Ekta traders
    Supplier A — SUPER TRADING COMPANY (STC)
    Supplier B — Girish Kumar Ahirwar
    Supplier B — Wasim Salim Shaikh

The client had been instructed to pay one of them. From that list they could
read how many suppliers the Bridge uses, how many accounts each holds, whose
names are on them, and which belong together. None of that is theirs.

I introduced it hours earlier, widening the picker to every open trade and
adding supplier labels so the client could "see which trade their money landed
on". Nobody asked for that. The trade is resolved from the account internally
and never needs saying out loud — which is the rule these tests hold.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import client_bot  # noqa: E402
from db.repo import Repo  # noqa: E402

ACCOUNTS = [
    {"id": 1, "account_name": "Ekta traders", "account_number": "20100073057883",
     "ifsc": "UTIB0000001", "trade_id": 10, "reference": "SUPA1"},
    {"id": 2, "account_name": "Girish Kumar Ahirwar",
     "account_number": "50100898878700", "ifsc": "HDFC0000003",
     "trade_id": 20, "reference": "SUPB1"},
]


class TestNothingClientFacingNamesASupplier:
    """
    Stated against the source, because the leak was in message construction
    rather than in any value the client could compute.
    """

    CLIENT_FACING = [
        "cmd_accounts", "_render_accounts", "on_pasted_payment",
        "add_utr", "on_edited_payment",
    ]

    def test_no_client_facing_handler_mentions_a_supplier_label(self):
        offenders = []
        for name in self.CLIENT_FACING:
            src = inspect.getsource(getattr(client_bot, name))
            # Only flag it being put INTO a message, not discussed in comments.
            for line in src.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"'):
                    continue
                if "supplier_label" in stripped:
                    offenders.append(f"{name}: {stripped}")
        assert not offenders, (
            "a supplier label is being shown to the client:\n  "
            + "\n  ".join(offenders)
        )

    def test_the_account_rows_no_longer_carry_a_supplier_label(self):
        """
        Removed at source. If the column is not there, it cannot be printed by
        something written later.
        """
        src = inspect.getsource(Repo.open_trade_accounts_for_client)
        assert "supplier_label" not in src

    def test_render_accounts_prints_only_bank_details(self):
        out = "\n".join(_ for _ in client_bot._render_accounts(ACCOUNTS))
        assert "Ekta traders" in out
        assert "20100073057883" in out
        for leaked in ("Supplier A", "Supplier B", "SUPA", "SUPB"):
            assert leaked not in out

    def test_render_accounts_does_not_group_by_anything(self):
        """
        Grouping is itself the disclosure: it says which accounts belong
        together even without naming the supplier.
        """
        out = "\n".join(client_bot._render_accounts(ACCOUNTS))
        assert "Accounts under" not in out


class TestThePickerIsNarrowedToWhatWasInstructed:
    """
    A client should be offered the accounts they were told to pay, not every
    account the Bridge holds.
    """

    def test_it_prefers_accounts_with_an_issued_instruction(self):
        src = inspect.getsource(Repo.open_trade_accounts_for_client)
        assert "payment_slots" in src, \
            "the picker is no longer narrowed to instructed accounts"

    def test_it_still_returns_something_before_any_instruction(self):
        """
        Narrowing must not leave the client with an empty list before the
        Bridge has confirmed — there would be nothing to choose and a payment
        would be lost.
        """
        src = inspect.getsource(Repo.open_trade_accounts_for_client)
        assert src.count("SELECT") >= 2, "the fallback query is gone"

    def test_the_trade_is_still_carried(self):
        """
        The whole point of the earlier fix. Attribution must survive the
        narrowing.
        """
        src = inspect.getsource(Repo.open_trade_accounts_for_client)
        assert "t.id AS trade_id" in src
