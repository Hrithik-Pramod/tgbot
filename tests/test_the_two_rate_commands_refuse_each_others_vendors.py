"""
/setrate and /specialrate each refuse the other's vendors.

    I have a command call /specialrate / For this one it asked for 3 things
    Supply / Buy / Sell rate
    — Bridge, 23 November 2026

    Yes I think that is a good idea, but need to think how to set a
    supplier/introducer on UI Control
    — Bridge, on /setrate refusing a vendor who has an introducer

WHY THE REFUSAL IS THE POINT

Setting a rate for a vendor WITH an introducer using plain /setrate writes
buy = supply, and commission silently stops accruing. Nothing looks wrong
afterwards: the client pays the same, the vendor receives the same, the
desk's margin is simply larger, and the introducer is quietly owed nothing.
It would be found at the end of the month, by someone adding up by hand.

The reverse is as bad in the other direction — /specialrate on a vendor with
nobody to pay carves a slice of the spread for an introducer who does not
exist, and the desk's own margin reads low for no reason anybody can see.

Neither mistake announces itself, so neither is allowed.

ONE FLOW, NOT TWO

Both commands run the same states. A second copy of the rate flow would
drift, and the copy that drifted would be the one used less often — which is
the one handling commission.
"""

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import bridge_bot, menu  # noqa: E402
from bot.bridge_bot import SetRate  # noqa: E402


class _Msg:
    def __init__(self):
        self.sent = []

    async def answer(self, t, **kw): self.sent.append({"text": t, **kw})
    async def edit_text(self, t, **kw): self.sent.append({"text": t, **kw})


class _Call:
    def __init__(self, data):
        self.data = data
        self.message = _Msg()
        self.answers = []

    async def answer(self, text=None, **kw): self.answers.append(text)


class _State:
    def __init__(self, data=None):
        self.state = None
        self.data = dict(data or {})
        self.cleared = False

    async def set_state(self, s): self.state = s
    async def update_data(self, **kw): self.data.update(kw)
    async def get_data(self): return dict(self.data)

    async def clear(self):
        self.cleared = True
        self.state = None


class _Repo:
    def __init__(self, introducer=None):
        self.introducer = introducer

    async def list_parties(self, role):
        return [{"id": 5, "label": "A VENDOR"}]

    async def introducer_for_supplier(self, sid):
        return self.introducer

    async def current_rate(self, sid, cid): return None


INTRODUCER = {"id": 9, "label": "Introducer A", "telegram_chat_id": -1,
              "is_active": True}


class TestSetrateRefusesAVendorWithAnIntroducer:
    @pytest.mark.asyncio
    async def test_it_stops_and_names_the_other_command(self):
        state = _State({"special": False})
        call = _Call("sr_sup:5")
        await bridge_bot.setrate_supplier(call, state, _Repo(INTRODUCER))
        said = call.message.sent[0]["text"]
        assert "/specialrate" in said
        assert "Introducer A" in said
        assert state.cleared, "the flow carried on anyway"

    @pytest.mark.asyncio
    async def test_it_says_what_would_have_gone_wrong(self):
        """
        "Use the other command" without a reason is a rule to be worked
        around at 5am. The consequence is the argument.
        """
        state = _State({"special": False})
        call = _Call("sr_sup:5")
        await bridge_bot.setrate_supplier(call, state, _Repo(INTRODUCER))
        said = call.message.sent[0]["text"]
        assert "commission" in said and "stop accruing" in said

    @pytest.mark.asyncio
    async def test_an_ordinary_vendor_is_unaffected(self):
        state = _State({"special": False})
        call = _Call("sr_sup:5")
        await bridge_bot.setrate_supplier(call, state, _Repo(None))
        assert not state.cleared
        assert state.state == SetRate.client


class TestSpecialrateRefusesAVendorWithout:
    @pytest.mark.asyncio
    async def test_it_stops_rather_than_inventing_a_payee(self):
        state = _State({"special": True})
        call = _Call("sr_sup:5")
        await bridge_bot.setrate_supplier(call, state, _Repo(None))
        said = call.message.sent[0]["text"]
        assert "no introducer" in said
        assert "/addintroducer" in said
        assert state.cleared

    @pytest.mark.asyncio
    async def test_a_vendor_with_one_proceeds(self):
        state = _State({"special": True})
        call = _Call("sr_sup:5")
        await bridge_bot.setrate_supplier(call, state, _Repo(INTRODUCER))
        assert not state.cleared
        assert state.state == SetRate.client


class TestTheBuyStepOnlyExistsForSpecialrate:
    @pytest.mark.asyncio
    async def test_specialrate_asks_for_it(self):
        state = _State({"special": True})
        msg = _Msg()
        msg.text = "106.20"
        await bridge_bot.setrate_supply(msg, state)
        assert state.state == SetRate.buy_rate
        assert "buy rate" in msg.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_setrate_skips_straight_to_sell(self):
        state = _State({"special": False})
        msg = _Msg()
        msg.text = "106.20"
        await bridge_bot.setrate_supply(msg, state)
        assert state.state == SetRate.sell_rate
        assert "sell rate" in msg.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_a_buy_below_supply_is_refused_where_he_can_retype_it(self):
        """
        That would pay the introducer out of the vendor's money rather than
        out of the spread. The database refuses it too, but a constraint
        violation at the end of the flow loses everything he typed.
        """
        state = _State({"special": True, "supply_rate": "106.20"})
        msg = _Msg()
        msg.text = "105.00"
        await bridge_bot.setrate_buy(msg, state)
        assert state.state != SetRate.sell_rate
        assert "cannot be below the supply rate" in msg.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_a_buy_equal_to_supply_is_allowed(self):
        """
        It means an introducer on zero fee, which is a real thing to set.
        """
        state = _State({"special": True, "supply_rate": "106.20"})
        msg = _Msg()
        msg.text = "106.20"
        await bridge_bot.setrate_buy(msg, state)
        assert state.state == SetRate.sell_rate


class TestTheSellStepKnowsAboutTheBuy:
    @pytest.mark.asyncio
    async def test_a_sell_below_the_buy_is_refused(self):
        state = _State({"special": True, "supply_rate": "106.20",
                        "buy_rate": "107.20"})
        msg = _Msg()
        msg.text = "107.00"
        await bridge_bot.setrate_sell(msg, state)
        assert state.state != SetRate.confirm
        assert "cannot be below the buy rate" in msg.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_the_confirmation_shows_all_three(self):
        state = _State({"special": True, "supply_rate": "106.20",
                        "buy_rate": "107.20"})
        msg = _Msg()
        msg.text = "108.20"
        await bridge_bot.setrate_sell(msg, state)
        said = msg.sent[0]["text"]
        assert "Supply rate" in said and "Buy rate" in said and "Sell rate" in said

    @pytest.mark.asyncio
    async def test_it_shows_the_fee_as_money(self):
        """
        ₹1,000 per 1,000 USDT is what he can check against what he agreed
        with the introducer. A rate of 107.20 is not.
        """
        state = _State({"special": True, "supply_rate": "106.20",
                        "buy_rate": "107.20"})
        msg = _Msg()
        msg.text = "108.20"
        await bridge_bot.setrate_sell(msg, state)
        said = msg.sent[0]["text"]
        assert "1,000" in said and "per 1,000 USDT" in said
        assert "out of your spread" in said

    @pytest.mark.asyncio
    async def test_sell_equal_to_buy_warns_that_he_keeps_nothing(self):
        state = _State({"special": True, "supply_rate": "106.20",
                        "buy_rate": "108.20"})
        msg = _Msg()
        msg.text = "108.20"
        await bridge_bot.setrate_sell(msg, state)
        assert "entire spread" in msg.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_an_ordinary_pairing_shows_two_rates_and_no_fee(self):
        state = _State({"special": False, "supply_rate": "106.20"})
        msg = _Msg()
        msg.text = "108.20"
        await bridge_bot.setrate_sell(msg, state)
        said = msg.sent[0]["text"]
        assert "Buy rate" not in said
        assert "per 1,000 USDT" not in said


class TestWhatIsStored:
    def test_the_buy_rate_reaches_set_rate(self):
        src = inspect.getsource(bridge_bot.setrate_confirm)
        assert "buy_rate=" in src
        assert 'data.get("buy_rate")' in src

    def test_an_ordinary_rate_passes_none(self):
        """
        set_rate stores None as the supply rate — "no introducer" written
        as a figure rather than as an absence, so the arithmetic has no
        special case.
        """
        src = inspect.getsource(bridge_bot.setrate_confirm)
        assert "else None" in src

    def test_both_commands_share_one_flow(self):
        """
        Two copies would drift, and the one that drifted would be the one
        used less often — the one handling commission.
        """
        src = (ROOT / "bot" / "bridge_bot.py").read_text(encoding="utf-8")
        assert src.count("@router.message(SetRate.supply_rate)") == 1
        assert src.count("@router.message(SetRate.sell_rate)") == 1
        assert src.count("SetRate.confirm, F.data ==") == 1


class TestItIsReachable:
    def test_specialrate_is_in_the_bridge_menu(self):
        """This is exactly how /issue stayed invisible for eight days."""
        assert any(c == "specialrate" for c, _ in menu.MENUS["bridge"])

    def test_the_command_is_registered(self):
        src = (ROOT / "bot" / "bridge_bot.py").read_text(encoding="utf-8")
        assert 'Command("specialrate")' in src

    def test_it_is_not_offered_to_anybody_else(self):
        for role in ("supplier", "client"):
            assert not any(c == "specialrate" for c, _ in menu.MENUS[role])
