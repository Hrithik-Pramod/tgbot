"""
A Bridge button that no longer works says so, instead of nothing.

    Now unable to forward a trade
    Please check as ap
    Urgent
    Pressing button not working
    — Bridge, 22 October 2026, 14:09

He was tapping the account picker for BRAV37. The bot had sent that screen at
12:38; the unread marker was below it, so he had scrolled back to an old
message — the ordinary way to use Telegram.

WHY IT DID NOTHING

Every step of /issue, /cancel, /collect and /correct is gated on an FSM
state:

    @router.callback_query(Confirm.account, F.data.startswith("cfa:"))

MemoryStorage holds that state for one chat. It is gone when the bot
restarts, and it has MOVED the moment he starts anything else. The old
screen's buttons then match no handler, aiogram drops the update, and
Telegram spins and stops. Nothing is logged, nothing is written, and there is
nothing to look at afterwards. From his side the bot is broken, at the exact
moment a client is waiting for payment details.

WHAT IT DOES NOT DO

It does not rebuild the flow. Half-finished allocation state reconstructed
from a button press is how a client gets instructed to pay a figure nobody
checked. It says what happened and names the command that starts again.

WHY A CATCH-ALL IS SAFE HERE

bridge_trade.router is the last router in the Bridge dispatcher, so a
callback that reaches the bottom of it was refused by every handler in both
Bridge routers. test_it_is_the_last_router_in_the_bridge pins that, because
the day it stops being true this handler starts eating live buttons.
"""

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from bot import bridge_bot, bridge_trade  # noqa: E402
from bot.bridge_trade import _flow_for, stale_screen  # noqa: E402


class _Chat:
    id = -1000000000000


class _Msg:
    chat = _Chat()


class _State:
    def __init__(self, state=None):
        self._state = state

    async def get_state(self):
        return self._state


class _Call:
    def __init__(self, data):
        self.data = data
        self.message = _Msg()
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append({"text": text, **kw})


class TestItAnswers:
    @pytest.mark.asyncio
    async def test_the_tap_gets_a_visible_reply(self):
        call = _Call("cffull")
        await stale_screen(call, _State())
        assert call.answers, "the button did nothing, again"
        assert call.answers[0]["show_alert"], (
            "a toast at the top of the screen is what he already missed"
        )

    @pytest.mark.asyncio
    async def test_it_says_nothing_was_changed(self):
        """
        The first thing to establish is that the tap was harmless. He tapped
        several times before asking.
        """
        call = _Call("cffull")
        await stale_screen(call, _State())
        assert "Nothing was changed" in call.answers[0]["text"]

    @pytest.mark.asyncio
    async def test_it_names_the_way_back(self):
        call = _Call("cfa:28")
        await stale_screen(call, _State())
        assert "/issue" in call.answers[0]["text"]

    @pytest.mark.asyncio
    async def test_an_unknown_button_still_gets_an_answer(self):
        """
        A prefix nobody has mapped must not fall through to silence — that
        is the whole fault being fixed.
        """
        call = _Call("something_new:9")
        await stale_screen(call, _State())
        assert call.answers[0]["show_alert"]
        assert "Start the command again" in call.answers[0]["text"]

    @pytest.mark.asyncio
    async def test_it_survives_a_callback_with_no_message(self):
        """
        Telegram omits `message` on a callback from a message too old to be
        loaded — which is precisely the case this handler exists for.
        """
        call = _Call("cfyes")
        call.message = None
        await stale_screen(call, _State())
        assert call.answers


class TestTheFlowItNames:
    def test_each_flow_is_recognised(self):
        for data, command in (
            ("cfa:28", "/issue"), ("cffull", "/issue"), ("cfyes", "/issue"),
            ("sp:114", "/issue"), ("spno", "/issue"),
            ("cwacct", "/cancel"), ("cwother", "/cancel"),
            ("cl:9", "/collect"), ("clt:7", "/collect"),
            ("co:7", "/correct"), ("cop:12", "/correct"),
            ("cot:8", "/correct"), ("cor", "/correct"),
            ("sr_sup:5", "/setrate"), ("mst:3", "/setrate"),
            ("wa_kind:x", "/wallet"), ("wl:1", "/wallet"), ("wr:2", "/wallet"),
            ("av_cli:1", "/addvendor"),
        ):
            assert _flow_for(data) == command, data

    def test_the_correct_prefixes_do_not_shadow_each_other(self):
        """
        "co" is a prefix of "cop", "cot", "cor", "cob" and "com". Ordered
        wrongly they all answer as the same thing — harmless here because
        they share a command, but not if one ever moves.
        """
        assert _flow_for("cop:12") == "/correct"
        assert _flow_for("cot:12") == "/correct"

    def test_an_unmapped_prefix_is_none_not_a_guess(self):
        assert _flow_for("zz:1") is None
        assert _flow_for(None) is None
        assert _flow_for("") is None


class TestItCannotEatALiveButton:
    def test_it_is_the_last_router_in_the_bridge(self):
        """
        The catch-all is only safe as the last word. Put a router after it
        and every button in that router dies silently — the fault this file
        is about, reintroduced by its own fix.
        """
        src = inspect.getsource(main)
        assert "[bridge_bot.router, bridge_trade.router]" in src, (
            "the Bridge router order changed; the catch-all must stay last"
        )

    def test_it_is_the_last_callback_handler_in_its_own_router(self):
        """
        Within one router aiogram tries handlers in definition order, so a
        state-gated handler defined BELOW the catch-all would never run.
        """
        src = (ROOT / "bot" / "bridge_trade.py").read_text(encoding="utf-8")
        catch = src.index("@router.callback_query()")
        assert "@router.callback_query(" not in src[catch + 30:], (
            "a callback handler is defined after the catch-all"
        )

    def test_the_catch_all_takes_no_filter(self):
        """
        Any filter here would put the silence back for whatever it excludes.
        """
        src = (ROOT / "bot" / "bridge_trade.py").read_text(encoding="utf-8")
        assert "@router.callback_query()\nasync def stale_screen" in src

    def test_the_real_handlers_still_come_first(self):
        """
        Spot-check the one he actually pressed: cfa: must still be claimed
        by its own state-gated handler, which is defined far above.
        """
        src = (ROOT / "bot" / "bridge_trade.py").read_text(encoding="utf-8")
        real = src.index('Confirm.account, F.data.startswith("cfa:")')
        catch = src.index("@router.callback_query()")
        assert real < catch

    def test_the_other_bridge_router_is_untouched(self):
        """
        bridge_bot's handlers run first because its router is included
        first. Nothing was added there.
        """
        assert "@router.callback_query()\n" not in inspect.getsource(bridge_bot)


class TestItLeavesATrace:
    def test_the_tap_is_logged_with_the_state(self):
        """
        14:09 cost an afternoon because a dead button left no record at all.
        The state is the diagnostic: it says what the bot thought it was
        doing instead.
        """
        src = inspect.getsource(stale_screen)
        assert "log.info" in src
        assert "state" in src
        assert "call.data" in src

    def test_it_does_not_rebuild_the_flow(self):
        """
        No repo, no notifier, no state writes. It answers and stops.
        """
        src = inspect.getsource(stale_screen)
        for forbidden in ("repo", "notifier", "set_state", "update_data",
                          "issue_slots"):
            assert forbidden not in src, forbidden
        assert "state" in inspect.signature(stale_screen).parameters
