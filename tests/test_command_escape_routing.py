"""
The command actually runs, not merely "the state was cleared".

WHAT HAPPENED (live, 19 September 2026)

Mid-way through /addvendor, at the question "What is it called?", the Bridge
typed /issue. The bot answered:

    Deal numbers for /issue@pt_bridge_ctrl_bot will read ISSU1, ISSU2, and
    so on. Use that, or type a different prefix.

It had taken the command as the vendor's name. That is precisely the
complaint bot/escape.py was written for on 15 September:

    Command override, if I say /setrate, and then decide to use another
    command, the command will not let me get out of the /setrate path

The middleware shipped on the 15th and never worked.

WHY IT DID NOT WORK

aiogram's FSMContextMiddleware runs on the UPDATE observer, ahead of any
middleware registered on the message observer, and writes the state into the
data dict twice:

    data["state"]      the live FSMContext
    data["raw_state"]  a snapshot of the state string

StateFilter matches on the SNAPSHOT. Clearing the context emptied the
storage and left the snapshot untouched, so the router still sent the
message to the step handler. The flow was abandoned in the database and
honoured in the routing — the worst of both.

WHY THE TESTS DID NOT CATCH IT

tests/test_command_escape.py drives the middleware directly and asserts that
clear() was called. That is the mechanism, not the behaviour. Every one of
them passed for four days while the thing they described was broken in
production.

So these go through a real Dispatcher, with real routers, real filters and
real FSM storage, and assert which handler ran. A test that cannot fail when
the feature is broken is not a test.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.escape import CommandEscapeMiddleware  # noqa: E402

CHAT = Chat(id=-1001, type="supergroup")
USER = User(id=7, is_bot=False, first_name="Bridge")


class Flow(StatesGroup):
    name = State()


def _message(text: str, mid: int) -> Message:
    return Message(
        message_id=mid, date=datetime.now(timezone.utc),
        chat=CHAT, from_user=USER, text=text,
    )


class _Bot:
    """Enough of a Bot for FSM keying and for Command's @mention check."""

    id = 42

    async def me(self) -> User:
        # Command() validates the @suffix against the bot's own username.
        return User(id=42, is_bot=True, first_name="Bridge Control",
                    username="pt_bridge_ctrl_bot")

    async def __call__(self, *a, **k):
        return None


@pytest.fixture
def ran():
    return []


@pytest.fixture
def dp(ran):
    """
    Two routers, in the order main.py wires them.

    This matters more than it looks. The first draft of these tests put
    /issue in the same router ABOVE the step handler, so the command won on
    registration order alone and every test passed with the fix removed —
    the same false confidence that let the original bug ship.

    Production is the other way round: addvendor_label lives in
    bridge_bot.router and cmd_issue in bridge_trade.router, and bridge_bot
    is included first. So the step handler is reached first and only the
    cleared raw_state stops it claiming the command.
    """
    first = Router(name="bridge_bot")
    second = Router(name="bridge_trade")

    @first.message(Command("addvendor"))
    async def cmd_addvendor(message: Message, state) -> None:
        await state.set_state(Flow.name)
        ran.append("addvendor")

    @first.message(Flow.name)
    async def took_the_name(message: Message) -> None:
        ran.append(f"name={message.text}")

    @second.message(Command("issue"))
    async def cmd_issue(message: Message) -> None:
        ran.append("issue")

    @second.message(F.text)
    async def catch_all(message: Message) -> None:
        ran.append("catch_all")

    d = Dispatcher(storage=MemoryStorage())
    d.message.outer_middleware(CommandEscapeMiddleware())
    d.include_router(first)
    d.include_router(second)
    return d


async def _feed(dp, bot, text: str, mid: int):
    await dp.feed_update(bot, Update(update_id=mid, message=_message(text, mid)))


class TestACommandEscapesAnOpenFlow:
    @pytest.mark.asyncio
    async def test_the_command_runs_instead_of_answering_the_question(self, dp, ran):
        """
        The live failure, end to end. Before the fix this recorded
        'name=/issue' — the flow swallowing the command.
        """
        bot = _Bot()
        await _feed(dp, bot, "/addvendor", 1)
        await _feed(dp, bot, "/issue", 2)
        assert ran == ["addvendor", "issue"], ran

    @pytest.mark.asyncio
    async def test_it_works_with_the_bot_suffix_telegram_adds(self, dp, ran):
        """
        In a group Telegram appends @botname, and that is the exact form he
        typed on 19 September.
        """
        bot = _Bot()
        await _feed(dp, bot, "/addvendor", 1)
        await _feed(dp, bot, "/issue@pt_bridge_ctrl_bot", 2)
        assert ran == ["addvendor", "issue"], ran

    @pytest.mark.asyncio
    async def test_the_abandoned_flow_does_not_come_back(self, dp, ran):
        """
        Walking away means walking away. A later plain message must reach the
        catch-all, not the step that was open before.
        """
        bot = _Bot()
        await _feed(dp, bot, "/addvendor", 1)
        await _feed(dp, bot, "/issue", 2)
        await _feed(dp, bot, "Supplier E", 3)
        assert ran == ["addvendor", "issue", "catch_all"], ran


class TestTheFlowStillWorksNormally:
    @pytest.mark.asyncio
    async def test_an_ordinary_answer_still_reaches_the_step(self, dp, ran):
        """
        The other half. An escape that ate every answer would be worse than
        the bug — /addvendor asks five questions and none may be lost.
        """
        bot = _Bot()
        await _feed(dp, bot, "/addvendor", 1)
        await _feed(dp, bot, "Supplier E", 2)
        assert ran == ["addvendor", "name=Supplier E"], ran

    @pytest.mark.asyncio
    async def test_an_answer_that_merely_contains_a_slash_is_kept(self, dp, ran):
        """Only a LEADING slash means a command."""
        bot = _Bot()
        await _feed(dp, bot, "/addvendor", 1)
        await _feed(dp, bot, "Alpha / Beta Trading", 2)
        assert ran == ["addvendor", "name=Alpha / Beta Trading"], ran

    @pytest.mark.asyncio
    async def test_a_command_with_no_flow_open_is_untouched(self, dp, ran):
        bot = _Bot()
        await _feed(dp, bot, "/issue", 1)
        assert ran == ["issue"], ran


class TestTheSnapshotIsTheThingThatMatters:
    @pytest.mark.asyncio
    async def test_clearing_the_context_alone_would_not_have_been_enough(self):
        """
        Pins WHY, so nobody simplifies the fix back out.

        StateFilter reads data["raw_state"], which FSMContextMiddleware
        captured before this middleware ran. Clearing only the context leaves
        that snapshot in place and the router still picks the step handler.
        """
        import inspect

        src = inspect.getsource(CommandEscapeMiddleware.__call__)
        assert 'data["raw_state"] = None' in src, (
            "without this the flow is abandoned in storage and honoured in "
            "routing — which is how it shipped broken for four days"
        )

    @pytest.mark.asyncio
    async def test_aiogram_still_resolves_state_the_way_we_assume(self):
        """
        The fix depends on aiogram's internals. If a future version stops
        putting raw_state in the data dict, we want to hear it here rather
        than from the Bridge mid-trade.
        """
        import inspect

        from aiogram.filters.state import StateFilter
        from aiogram.fsm.middleware import FSMContextMiddleware

        assert "raw_state" in inspect.signature(StateFilter.__call__).parameters
        assert '"raw_state"' in inspect.getsource(FSMContextMiddleware.__call__)
