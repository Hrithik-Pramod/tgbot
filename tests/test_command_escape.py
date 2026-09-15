"""
A command is never swallowed by an open conversation.

THE COMPLAINT (Bridge, 15 September 2026)

    Command override, if I say /setrate, and then decide to use another
    command, the command will not let me get out of the /setrate path

Every multi-step flow parks the user in an FSM state whose handler matches
any message. So mid-flow:

    Enter the supply rate:
    /viewrate
    I could not read that as a rate. Send just the number.

and round again, forever. The only escape was to complete a flow he no
longer wanted, or to re-run the same command that started it.

THE SHAPE OF THE FIX

An OUTER middleware, because outer middlewares run before filters are
evaluated. By the time the FSM step handler is considered, the state is
already gone, so it does not match and the command reaches its own handler.

Doing it per-handler with ~F.text.startswith("/") would work too, and would
rot the first time somebody adds a flow and forgets one — which is exactly
how /done sat dead for a day on 10 September.
"""

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from bot.escape import CommandEscapeMiddleware  # noqa: E402


class FakeState:
    def __init__(self, state=None):
        self._state = state
        self.cleared = False

    async def get_state(self):
        return self._state

    async def clear(self):
        self._state = None
        self.cleared = True


def _message(text=None, caption=None):
    from aiogram.types import Message
    msg = SimpleNamespace(text=text, caption=caption,
                          chat=SimpleNamespace(id=-100123))
    msg.__class__ = type("FakeMessage", (Message,), {})
    return msg


async def _run(event, state):
    """Invoke the middleware, recording whether the chain continued."""
    called = {}

    async def handler(e, d):
        called["yes"] = True
        return "handled"

    data = {"state": state}
    result = await CommandEscapeMiddleware()(handler, event, data)
    return result, called.get("yes", False)


class TestACommandEscapesAnOpenFlow:
    @pytest.mark.asyncio
    async def test_it_clears_the_state(self):
        state = FakeState("SetRate:supply_rate")
        await _run(_message("/viewrate"), state)
        assert state.cleared, "the flow is still open, so the step will eat it"

    @pytest.mark.asyncio
    async def test_the_command_still_reaches_its_handler(self):
        """
        Clearing the state is only half of it. The command must then run —
        escaping into silence would be its own bug.
        """
        state = FakeState("SetRate:supply_rate")
        _, called = await _run(_message("/viewrate"), state)
        assert called

    @pytest.mark.asyncio
    async def test_a_command_on_a_photo_caption_escapes_too(self):
        state = FakeState("AddPayment:utr")
        await _run(_message(text=None, caption="/done"), state)
        assert state.cleared

    @pytest.mark.asyncio
    async def test_leading_whitespace_does_not_hide_it(self):
        state = FakeState("SetRate:sell_rate")
        await _run(_message("  /issue"), state)
        assert state.cleared


class TestOrdinaryAnswersAreUntouched:
    """
    The flows have to keep working. Every step takes a value, and none of
    those values begin with a slash.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", [
        "106",                      # a rate
        "2500010",                  # an amount
        "Ekta traders",             # an account name
        "50100898878700",           # an account number
        "HDFC0000003",              # an IFSC
        "BKIDR12026091100005720",   # a UTR
        "wrong supplier, my error", # a correction reason
        "https://tronscan.org/#/transaction/abc",  # a hash URL
    ])
    async def test_a_real_answer_keeps_the_flow_open(self, answer):
        state = FakeState("SetRate:supply_rate")
        _, called = await _run(_message(answer), state)
        assert not state.cleared, f"{answer!r} wrongly read as a command"
        assert called

    @pytest.mark.asyncio
    async def test_a_command_with_no_flow_open_changes_nothing(self):
        state = FakeState(None)
        _, called = await _run(_message("/viewrate"), state)
        assert not state.cleared
        assert called

    @pytest.mark.asyncio
    async def test_it_survives_an_event_with_no_state(self):
        """Callback queries and edited messages reach this too."""
        _, called = await _run(_message("/viewrate"), None)
        assert called


class TestItIsWiredInBeforeTheFilters:
    def test_registered_as_an_outer_middleware(self):
        """
        Inner middleware runs only once a handler has matched — by which
        point the FSM step has already claimed the message and the fix does
        nothing at all.
        """
        src = inspect.getsource(main.build_dispatcher)
        assert "dp.message.outer_middleware(CommandEscapeMiddleware())" in src

    def test_it_applies_to_every_bot(self):
        """
        One dispatcher is built per bot, so registering inside
        build_dispatcher covers bridge, supplier and client alike.
        """
        src = inspect.getsource(main)
        assert src.count("build_dispatcher(") >= 4  # definition + three bots
