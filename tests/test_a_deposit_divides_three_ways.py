"""
A deposit divides between the client, the introducer and the desk.

    I have a command call /specialrate / For this one it asked for 3 things
    Supply / Buy / Sell rate
    Supply is the rate the supplier is providing
    Buy is the rate I enter including the introducers fee
    Sell rate is what I sell to the client for

    the difference between the supply and buy is a margin we do not receive,
    this is available on a separate telegram channel for the introducer
    — Bridge, 23 November 2026

HIS OWN WORKING, WHICH IS THE SPECIFICATION

    106.2*10000=1062000
    1062000/107.2=9906.7164
    1062000/108.2=9815.1571
    Total spread is 10000-9815.1571=184.8429 USDT
    USDT For Introducer is 10000-9906.7164=93.2836
    Remaining commission for partners = 184.8429-93.2836=91.5593

Two things in that worth saying out loud, because an earlier reading of the
request got both wrong:

  The commission is USDT, not INR. A first pass had the introducer taking
  ₹1 per USDT as a rupee liability settled separately. It is not — it is a
  slice of the spread, measured by applying the buy rate where the sell
  rate would otherwise go, and it comes out of the same 10,000 USDT.

  Nothing the client or the vendor sees changes. inr_expected is still
  USDT x supply, and the client is still owed inr_expected / sell. That is
  what "we calculate as normal the how much client sends" means, and it is
  why this does not disturb the instruction flow at all.

NO INTRODUCER IS buy == supply

Then inr / supply is the deposit exactly, the introducer's share is zero,
and the desk keeps the whole spread — identical to margin_usdt today. Every
vendor without an introducer runs down this same path, so there is no
second branch to forget. The database writes buy = supply by trigger for
anything that does not set it, including the seed scripts and the UAT reset.

THE DESK TAKES THE REMAINDER

Three figures rounded to 2dp will not sum to the deposit on their own. He
chose this himself without being asked — "Remaining commission for partners"
is a subtraction, not a third calculation. It is also the lesson of the
16 November split, applied at design time instead of after a rupee went
missing.
"""

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.money import (MoneyError, inr_to_usdt, margin_usdt,  # noqa: E402
                        round_usdt, split_three_ways, usdt_to_inr)
from tests.sqltext import strip_sql_comments, table_of  # noqa: E402

SUPPLY, BUY, SELL = D("106.2"), D("107.2"), D("108.2")


class TestHisWorkedExample:
    def test_every_figure_matches_what_he_sent(self):
        d = split_three_ways(D("10000"), SUPPLY, BUY, SELL)
        assert d.inr_expected == D("1062000")
        assert d.client_usdt == D("9815.16")      # 9815.1571
        assert d.introducer_usdt == D("93.28")    # 93.2836
        assert d.bridge_usdt == D("91.56")        # 91.5593

    def test_the_three_shares_are_the_whole_deposit(self):
        d = split_three_ways(D("10000"), SUPPLY, BUY, SELL)
        assert d.client_usdt + d.introducer_usdt + d.bridge_usdt == D("10000")

    def test_the_client_is_unaffected_by_the_introducer(self):
        """
        The point of the whole design. Whatever the buy rate is, the client
        sends the same INR and receives the same USDT.
        """
        with_intro = split_three_ways(D("10000"), SUPPLY, BUY, SELL)
        without = split_three_ways(D("10000"), SUPPLY, SUPPLY, SELL)
        assert with_intro.inr_expected == without.inr_expected
        assert with_intro.client_usdt == without.client_usdt

    def test_the_introducer_is_paid_out_of_the_spread(self):
        """
        Not by the client and not by the vendor. The desk's share falls by
        exactly what the introducer takes.
        """
        with_intro = split_three_ways(D("10000"), SUPPLY, BUY, SELL)
        without = split_three_ways(D("10000"), SUPPLY, SUPPLY, SELL)
        assert (without.bridge_usdt - with_intro.bridge_usdt
                == with_intro.introducer_usdt)


class TestNoIntroducerIsTheSameAsBefore:
    @pytest.mark.parametrize("usdt", ["10000", "3000", "66843.569", "0.01",
                                      "49482", "1"])
    def test_the_desk_keeps_the_whole_spread(self, usdt):
        d = split_three_ways(D(usdt), SUPPLY, SUPPLY, SELL)
        assert d.introducer_usdt == 0
        assert d.bridge_usdt == margin_usdt(D(usdt), SUPPLY, SELL), (
            "a vendor with no introducer stopped behaving as it always has"
        )

    def test_it_agrees_with_the_old_function_on_his_example(self):
        assert margin_usdt(D("10000"), SUPPLY, SELL) == D("184.84")
        d = split_three_ways(D("10000"), SUPPLY, SUPPLY, SELL)
        assert d.bridge_usdt == D("184.84")


class TestItAlwaysAddsUp:
    @pytest.mark.parametrize("usdt", ["10000", "66843.569", "33421.79", "1",
                                      "0.01", "7777.77", "123456.789"])
    @pytest.mark.parametrize("buy", ["106.2", "106.5", "107.2", "108.2"])
    def test_whatever_the_figures(self, usdt, buy):
        d = split_three_ways(D(usdt), SUPPLY, D(buy), SELL)
        assert (d.client_usdt + d.introducer_usdt + d.bridge_usdt
                == round_usdt(D(usdt))), (
            f"{usdt} at buy {buy} does not divide back to the deposit"
        )

    def test_it_survives_a_hundred_thousand_random_deposits(self):
        """
        The invariant, hammered. Nothing else in here would catch a
        rounding path that fails only on, say, a deposit carrying three
        decimals at one particular pair of rates.
        """
        import random
        rnd = random.Random(20261123)
        for _ in range(100_000):
            u = D(rnd.randrange(1000, 5_000_000)) / D(rnd.choice([100, 1000]))
            supply = D(rnd.randrange(9000, 12000)) / D(100)
            buy = supply + D(rnd.randrange(0, 300)) / D(100)
            sell = buy + D(rnd.randrange(1, 300)) / D(100)
            d = split_three_ways(u, supply, buy, sell)
            total = d.client_usdt + d.introducer_usdt + d.bridge_usdt
            assert total == round_usdt(u), (
                f"{u} at {supply}/{buy}/{sell} gave {total}"
            )
            assert d.introducer_usdt >= 0

    def test_the_desk_takes_the_remainder_by_construction(self):
        """
        Asserted on the SHAPE of the code, deliberately, and this comment is
        the honest reason why.

        A mutation replaced the desk's share with a third calculation
        (at_buy - client) and every behavioural test here still passed. A
        search over 200,000 random figures found exactly ONE case where the
        two disagree, and it was a 13 USDT deposit with buy almost equal to
        sell. So in practice the two forms agree, and no realistic example
        distinguishes them.

        The remainder is still the right construction: it CANNOT fail to
        sum, whereas the third calculation merely happens not to. That is a
        property of the form, not of any particular numbers, so the form is
        what gets pinned — rather than inventing a contrived case and
        pretending it was found naturally.
        """
        import inspect
        src = inspect.getsource(split_three_ways)
        assert "bridge_usdt=round_usdt(usdt - client - introducer)" in src

    def test_the_client_and_introducer_are_computed_not_derived(self):
        """
        Only ONE share may be a remainder, and it has to be the desk's —
        the other two are what the client is owed and what the introducer is
        paid, and both are figures somebody will check.
        """
        d = split_three_ways(D("66843.569"), SUPPLY, BUY, SELL)
        inr = usdt_to_inr(D("66843.569"), SUPPLY)
        assert d.client_usdt == inr_to_usdt(inr, SELL)
        assert d.introducer_usdt == round_usdt(
            D("66843.569") - inr_to_usdt(inr, BUY))


class TestWhatItRefuses:
    @pytest.mark.parametrize("usdt,supply,sell", [
        ("1342.595", "98.85", "99.72"),
        ("1125.055", "106.12", "107.75"),
        ("66843.569", "106.2", "108.2"),
    ])
    def test_rounding_never_looks_like_a_bad_rate(self, usdt, supply, sell):
        """
        With buy == supply — every vendor with no introducer — inr is
        rounded UP to whole rupees, so inr / supply can come out a fraction
        of a cent above the deposit and the introducer's share lands at
        -0.01.

        The first version of the guard raised on exactly that, because it
        checked the rounded share instead of the rate. It would have refused
        ordinary deposits: the real ones carry three decimals. Found by
        searching for figures where the desk's remainder and a third
        calculation disagree, which turned this up as a side effect.
        """
        d = split_three_ways(D(usdt), D(supply), D(supply), D(sell))
        assert d.introducer_usdt == 0
        assert d.bridge_usdt == margin_usdt(D(usdt), D(supply), D(sell))
        assert (d.client_usdt + d.introducer_usdt + d.bridge_usdt
                == round_usdt(D(usdt)))

    def test_a_buy_rate_below_supply(self):
        """
        That would pay the introducer out of the vendor's money rather than
        out of the spread, which is not the deal. Almost certainly a typo at
        /specialrate, and cheaper to refuse than to find in a commission
        report.
        """
        with pytest.raises(MoneyError):
            split_three_ways(D("10000"), SUPPLY, D("105.0"), SELL)

    def test_the_database_refuses_it_too(self):
        """
        Belt and braces: the check also lives on the rates table, so no path
        that writes a rate can store one.
        """
        schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        assert "buy_rate >= supply_rate AND buy_rate <= sell_rate" in schema

    def test_a_buy_rate_equal_to_sell_leaves_the_desk_nothing(self):
        """
        Allowed — it is a real thing he might do — but it must still add up
        and must not go negative.
        """
        d = split_three_ways(D("10000"), SUPPLY, SELL, SELL)
        assert d.bridge_usdt == 0
        assert (d.client_usdt + d.introducer_usdt) == D("10000")


class TestTheRateIsRememberedNotLookedUp:
    def test_trades_carries_its_own_buy_rate(self):
        schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        assert "buy_rate" in table_of(schema, "trades")

    def test_a_trade_is_not_constrained_the_way_a_rate_is(self):
        """
        A settled trade has to stay readable even if its rate would no
        longer be accepted. Bad rates are refused where they are entered.

        Comments stripped: the comment on the column explains that trades
        deliberately does NOT carry this constraint, and so it names it.
        Fourth time that has caught a test in this project — see
        tests/sqltext.py.
        """
        schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        assert "rates_buy_between" not in table_of(schema, "trades")

    def test_nothing_has_to_remember_to_set_it(self):
        """
        Eighty-two tests, the seed scripts and the UAT reset all insert
        rates without naming buy_rate. "Remember to name it" is not a
        workable rule, so the database fills it in.
        """
        schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        assert "buy_rate_defaults_to_supply" in schema
        assert schema.count("EXECUTE FUNCTION buy_rate_defaults_to_supply()") == 2

    def test_the_migration_backfills_existing_rows(self):
        sql = (ROOT / "deploy" / "migrate-016-introducer-buy-rate.sql"
               ).read_text(encoding="utf-8")
        code = strip_sql_comments(sql)
        assert "UPDATE rates  SET buy_rate = supply_rate" in code
        assert "UPDATE trades SET buy_rate = supply_rate" in code
        assert "SET NOT NULL" in code
