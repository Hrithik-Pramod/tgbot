"""
A rate reads the way somebody says it.

WHAT HAPPENED (live, 21 September 2026, mid-trade)

    copying from the telegram, something is not working right, copying from
    Telegram gets extra long numbers set not value of trade

Rates are NUMERIC(20, 6), so asyncpg hands back 106.200000 and every screen
that showed one interpolated it raw:

    /viewrate   Buy  106.200000
    /setrate    Current supply rate: 106.200000
    /reprice    Now:  buy 106.200000 / sell 107.700000

Six trailing zeros on every rate, everywhere, since the first build. Nobody
had said anything because it was legible — until he started copying figures
out of Telegram under time pressure and the long ones were the wrong ones.

WHY TRAILING ZEROS ONLY

This tidies the display and must never touch the value. A rate of 106.125
keeps all three decimals: rounding a rate would change the arithmetic of
every trade priced on it, which is a far worse bug than the one being
fixed.

WHAT IS DELIBERATELY NOT CHANGED

Money keeps its own formatters. fmt_inr has thousands separators and no
decimals, fmt_usdt_plain has two decimals and no separators because it is
pasted into a wallet. Neither is a rate and neither moves.
"""

import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.money import fmt_inr, fmt_rate, fmt_usdt_plain  # noqa: E402


class TestTrailingZerosGo:
    def test_the_rates_actually_in_use(self):
        """The live pairings on 21 September."""
        assert fmt_rate(D("106.200000")) == "106.2"
        assert fmt_rate(D("107.700000")) == "107.7"
        assert fmt_rate(D("106.500000")) == "106.5"
        assert fmt_rate(D("105.500000")) == "105.5"

    def test_a_whole_number_loses_its_point(self):
        assert fmt_rate(D("100.000000")) == "100"
        assert fmt_rate(D("106.000000")) == "106"

    def test_scientific_notation_never_reaches_a_screen(self):
        """
        Decimal.normalize() turns 100 into 1E+2. A rate reading "1E+2" in a
        payment instruction would be worse than the six zeros.
        """
        assert fmt_rate(D("1E+2")) == "100"
        assert "E" not in fmt_rate(D("100.000000"))


class TestTheValueIsNeverChanged:
    def test_real_decimals_are_kept_in_full(self):
        """
        Rounding a rate would change the arithmetic of every trade priced on
        it. This is a display fix and nothing more.
        """
        assert fmt_rate(D("106.125000")) == "106.125"
        assert fmt_rate(D("107.123456")) == "107.123456"
        assert fmt_rate(D("0.500000")) == "0.5"

    def test_it_round_trips(self):
        for raw in ["106.200000", "107.7", "100", "106.125", "0.000001"]:
            assert D(fmt_rate(D(raw))) == D(raw)

    def test_a_string_rate_is_accepted(self):
        """/setrate holds what he typed as a string in FSM state."""
        assert fmt_rate("106.20") == "106.2"


class TestMoneyFormattersAreUntouched:
    def test_inr_keeps_separators_and_no_decimals(self):
        assert fmt_inr(D("249995")) == "249,995"
        assert fmt_inr(D("4000016")) == "4,000,016"

    def test_usdt_keeps_two_decimals_and_no_separators(self):
        """
        It is pasted into a wallet. A comma there is rejected at best and
        silently truncated at worst (client request, 8 September 2026).
        """
        assert fmt_usdt_plain(D("2321.220000")) == "2321.22"
        assert fmt_usdt_plain(D("37209.450000")) == "37209.45"
        assert "," not in fmt_usdt_plain(D("37209.45"))


class TestNoScreenPrintsARateRaw:
    def test_every_rate_in_a_message_goes_through_fmt_rate(self):
        """
        The regression guard. This was not one bad line, it was every place
        a rate was shown — so the test is over the source rather than over
        one function.
        """
        import re

        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in [root / "bot" / "bridge_bot.py",
                     root / "bot" / "bridge_trade.py",
                     root / "core" / "summary.py"]:
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                for m in re.finditer(
                    r"\{[^{}]*(?:supply_rate|sell_rate|old_supply|new_supply"
                    r"|old_sell|new_sell)[^{}]*\}", line
                ):
                    if "fmt_rate" not in m.group(0):
                        offenders.append(f"{path.name}:{n}: {m.group(0)}")
        assert not offenders, "rates shown raw:\n" + "\n".join(offenders)
