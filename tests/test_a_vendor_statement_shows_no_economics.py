"""
A statement a vendor may be handed: their collection, and nothing else.

    i need from you something also, the statement ability i have on my end,
    i need an adjusted one for the vendor

    no rates shown only the attached, for exporting the last 90 days

    set up a test area first, so we can be sure no errors, as RATES AND
    AMOUNTS MUST NOT BE SHOWN
    — Bridge, 13 November 2026

He sent his own /export filtered to one vendor, with six columns already
deleted, and said "note the fields". Those six are the deal economics:
Supply rate, Sell rate, USDT received, INR expected, USDT owed client,
Margin USDT. Per-payment "Payment INR" he kept, and it is the point of a
statement — those figures are already on the vendor's own bank statement.

ONE COLUMN IS DROPPED THAT HIS EXAMPLE STILL HAD

Client. His file was his own export narrowed to Uncle, not a document built
to hand over, so the column survived. A vendor must never learn who the
Bridge's counterparty is: on 11 September 2026 a list went out reading
"Supplier A — …" and told a counterparty the shape of the book.

ONE COLUMN IS NEW TO VENDORS

Reference. /progress and /history deliberately number a vendor's orders
rather than naming them (decision D4, and this was reaffirmed on
26 October). He asked for it, and it is their own deal number — it gives him
and a vendor a shared name for a deal on a phone call. That is a deliberate
narrowing of D4, which is why it is asserted here rather than assumed.

WHY A SEPARATE QUERY RATHER THAN A FILTER

export_rows is the Bridge's own export and will have columns added to it —
that is a normal thing to do. If this were that query with columns dropped
at the writer, the next column added would appear in a vendor's statement
silently. The forbidden fields are never selected, and this test reads the
SELECT.
"""

import csv
import inspect
import io
import re
import sys
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import bridge_trade, menu  # noqa: E402
from bot.bridge_trade import (  # noqa: E402
    VENDOR_STATEMENT_COLUMNS, render_vendor_statement_csv,
)
from db.repo import Repo  # noqa: E402


FORBIDDEN = ("supply_rate", "sell_rate", "usdt_received", "inr_expected",
             "usdt_owed_client", "margin_usdt")


def _sql(method) -> str:
    """
    The SQL literal only — not the docstring above it, and not the --
    comments inside it.

    The docstring matters. These queries are mostly explanation, and the
    explanation here lists exactly the columns that must never appear:
    "supply_rate, sell_rate, usdt_received…", "client", "export_rows". A
    scan over the whole source finds every one of them in the paragraph
    saying why they are absent, and fails.

    Third time in this project. test_collect_actually_moves_the_money found
    "ORDER BY" in prose; test_a_trade_in_motion_is_not_interrupted found the
    sentence it had just deleted, quoted in the comment recording its
    deletion. A test that reads English and calls it SQL will eventually
    mark a correct change as a regression, or worse, miss a real one.
    """
    src = inspect.getsource(method)
    i = src.index('"""', src.index("conn.fetch"))
    j = src.index('"""', i + 3)
    body = src[i + 3:j]
    return "\n".join(re.sub(r"--.*$", "", l) for l in body.splitlines())


STATEMENT = _sql(Repo.vendor_statement_rows)
SELECT = STATEMENT[STATEMENT.index("SELECT"):STATEMENT.index("FROM trades")]


ROWS = [
    {"reference": "REF1", "status": "completed",
     "opened_at": datetime(2026, 11, 1, 9, 30),
     "completed_at": datetime(2026, 11, 1, 18, 4),
     "utr": "BKIDR10000000000001", "amount_inr": D("200000"),
     "beneficiary": "EKTA TRADERS"},
    {"reference": "REF2", "status": "open",
     "opened_at": datetime(2026, 11, 2, 10, 0), "completed_at": None,
     "utr": None, "amount_inr": None, "beneficiary": None},
]


class TestTheEconomicsAreNotThere:
    def test_the_query_selects_none_of_them(self):
        for col in FORBIDDEN:
            assert col not in SELECT, col

    def test_the_file_has_none_of_them(self):
        text = render_vendor_statement_csv(ROWS).decode("utf-8-sig")
        for word in ("rate", "USDT", "Margin", "expected"):
            assert word.lower() not in text.lower(), word

    def test_the_columns_are_exactly_what_he_specified(self):
        """
        His attached file, minus Supplier and Client. Pinned as a list so
        adding one is a deliberate act with a test to change.
        """
        assert VENDOR_STATEMENT_COLUMNS == [
            "Reference", "Status", "Opened", "Completed",
            "UTR", "Payment INR", "Beneficiary",
        ]

    def test_the_header_matches_the_row_width(self):
        """
        A row longer than the header shifts every value one column left in
        Excel, which is how a figure ends up under an innocent heading.
        """
        rows = list(csv.reader(io.StringIO(
            render_vendor_statement_csv(ROWS).decode("utf-8-sig"))))
        assert all(len(r) == len(VENDOR_STATEMENT_COLUMNS) for r in rows)


class TestTheClientIsNeverNamed:
    def test_the_query_does_not_join_the_client(self):
        """
        export_rows joins parties twice, for supplier and client. This one
        joins neither — there is nothing to select from them.
        """
        assert "client" not in SELECT
        assert "JOIN parties" not in STATEMENT

    def test_the_file_cannot_carry_a_client_name(self):
        text = render_vendor_statement_csv(ROWS).decode("utf-8-sig")
        assert "Client" not in text

    def test_the_wide_export_still_has_it(self):
        """
        Guards the premise. The Bridge's own export is unchanged, and if it
        ever stops carrying the client this test is about nothing.
        """
        assert "c.label AS client" in _sql(Repo.export_rows)


class TestItIsOneVendorsOwnDeals:
    def test_it_is_scoped_to_the_supplier(self):
        assert "t.supplier_id = $1" in STATEMENT

    def test_ninety_days(self):
        assert inspect.signature(
            Repo.vendor_statement_rows).parameters["days"].default == 90
        assert "($2::text || ' days')::interval" in STATEMENT

    def test_a_deal_with_no_payments_still_appears(self):
        """
        A statement showing only the deals that got paid is not a
        statement.
        """
        assert "LEFT JOIN payments" in STATEMENT
        rows = list(csv.reader(io.StringIO(
            render_vendor_statement_csv(ROWS).decode("utf-8-sig"))))
        assert rows[2][0] == "REF2" and rows[2][4] == ""

    def test_the_query_parses(self):
        import pglast
        pglast.parse_sql(STATEMENT.replace("$1", "1").replace("$2", "'90'"))


class TestItIsSeparateFromTheWideExport:
    def test_it_is_its_own_query(self):
        """
        Not export_rows with a filter. A column added to the Bridge's
        export must not be able to reach a vendor.
        """
        assert "export_rows" not in _sql(Repo.vendor_statement_rows)

    def test_the_writer_is_its_own_too(self):
        src = inspect.getsource(render_vendor_statement_csv)
        for col in ("Supply rate", "Margin", "USDT"):
            assert col not in src


class TestTheBridgeSeesItFirst:
    """
    "set up a test area first". The test area is the real output: he picks
    a vendor and gets the exact file that vendor would get.
    """

    def test_the_command_is_on_the_bridge(self):
        src = (ROOT / "bot" / "bridge_trade.py").read_text(encoding="utf-8")
        assert 'Command("vendorstatement")' in src

    def test_no_vendor_can_run_it_yet(self):
        """
        The vendor-facing command is a second deploy, after he has read
        one. Shipping both at once is not a test area.
        """
        src = (ROOT / "bot" / "supplier_bot.py").read_text(encoding="utf-8")
        assert "vendor_statement_rows" not in src
        assert "vendorstatement" not in src

    def test_it_is_in_the_bridge_menu_only(self):
        assert any(c == "vendorstatement" for c, _ in menu.MENUS["bridge"])
        assert not any(c == "vendorstatement" for c, _ in menu.MENUS["supplier"])

    def test_the_preview_uses_the_same_renderer(self):
        """
        If the preview and the vendor's copy are built by different code,
        checking one says nothing about the other.
        """
        src = inspect.getsource(bridge_trade.vendor_statement_file)
        assert "render_vendor_statement_csv" in src

    def test_the_caption_says_what_was_left_out(self):
        src = inspect.getsource(bridge_trade.vendor_statement_file)
        for said in ("No rates", "margin", "client"):
            assert said in src, said


class TestEmptyAndOdd:
    def test_a_vendor_with_nothing_is_told_so(self):
        src = inspect.getsource(bridge_trade.vendor_statement_file)
        assert "Nothing for that vendor" in src

    def test_a_missing_completion_is_blank_not_none(self):
        text = render_vendor_statement_csv(ROWS).decode("utf-8-sig")
        assert "None" not in text

    def test_excel_opens_the_rupee_sign(self):
        assert render_vendor_statement_csv(ROWS).startswith(b"\xef\xbb\xbf")
