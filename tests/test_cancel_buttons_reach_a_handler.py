"""
The cancel buttons reach a handler.

WHAT HAPPENED (live, 22 September 2026, 00:30)

    can i check, why was it not letting me cancelling last time?
    Its doing it again, says loading does not let me

The log said it plainly, once the TRON polling was filtered out:

    00:30:42  Update id=682771690 is handled.      Duration 1013 ms
    00:30:46  Update id=682771691 is NOT handled.  Duration 8 ms
    00:31:01  Update id=682771692 is NOT handled.  Duration 12 ms

The first is /cancel. The next two are him pressing a trade button, twice.
"Not handled" means no filter matched, so nothing ever called answer(), so
Telegram kept the spinner up for ever. No exception, no traceback, nothing
in the log above INFO — the failure is entirely silent from the server side.

THE CAUSE

cmd_cancel built the keyboard and sent it, and never set the state:

    rows = [InlineKeyboardButton(..., callback_data=f"cx:{t['id']}") ...]
    await message.answer("What do you want to cancel?", reply_markup=...)

while the handler for those buttons is gated:

    @router.callback_query(Cancel.pick, F.data.startswith("cx:"))

Cancel.pick was never set by anything, anywhere, so cx: matched no handler
at all — the trade half of /cancel could not work.

WHY IT LOOKED INTERMITTENT RATHER THAN BROKEN

The claim buttons on the same keyboard carry no state filter:

    @router.callback_query(F.data.startswith("cs:"))

so clearing a claim always worked, and so did every route that reaches a
cancel from somewhere other than /cancel. He had cancelled trades before.
Only this one entry point was dead.

THE SECOND TEST IS THE IMPORTANT ONE

Fixing cmd_cancel fixes today. A state that is filtered on but never set is
a whole class of silent dead button, and it cost an hour at half past
midnight to find one instance by reading logs. So the class is asserted
across the bot package, not the instance.
"""

import pathlib
import re
import sys
from datetime import datetime, timezone

import pytest
from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, Chat, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, Update, User)

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BOT_DIR = ROOT / "bot"

CHAT = Chat(id=-1001, type="supergroup")
USER = User(id=7, is_bot=False, first_name="Bridge")


class _Bot:
    id = 42

    async def me(self) -> User:
        return User(id=42, is_bot=True, first_name="Bridge Control",
                    username="pt_bridge_ctrl_bot")

    async def __call__(self, *a, **k):
        return None


class Cancel(StatesGroup):
    pick = State()
    reason = State()


# --------------------------------------------------------------------------
# The bug, through a real Dispatcher. A unit test on cmd_cancel would not
# have caught this: the function ran perfectly and sent a perfectly good
# keyboard. The failure only exists in the ROUTING of what came back.
# --------------------------------------------------------------------------

def _dispatcher(ran: list, *, set_the_state: bool):
    r = Router(name="bridge_trade")

    @r.message(Command("cancel"))
    async def cmd_cancel(message: Message, state) -> None:
        if set_the_state:
            await state.set_state(Cancel.pick)
        ran.append("cancel_listed")

    @r.callback_query(Cancel.pick, F.data.startswith("cx:"))
    async def cancel_pick(call: CallbackQuery, state) -> None:
        ran.append(f"picked={call.data}")

    # The claim button, deliberately ungated — this is what kept working and
    # disguised the fault.
    @r.callback_query(F.data.startswith("cs:"))
    async def cancel_claim(call: CallbackQuery, state) -> None:
        ran.append("claim_cleared")

    d = Dispatcher(storage=MemoryStorage())
    d.include_router(r)
    return d


def _message(text: str, mid: int) -> Message:
    return Message(message_id=mid, date=datetime.now(timezone.utc),
                   chat=CHAT, from_user=USER, text=text)


def _press(data: str, mid: int) -> Update:
    return Update(
        update_id=mid,
        callback_query=CallbackQuery(
            id=str(mid), from_user=USER, chat_instance="ci",
            data=data, message=_message("What do you want to cancel?", mid),
        ),
    )


class TestPressingATradeButtonRunsSomething:
    @pytest.mark.asyncio
    async def test_the_trade_button_is_handled(self):
        ran: list = []
        dp, bot = _dispatcher(ran, set_the_state=True), _Bot()
        await dp.feed_update(bot, Update(update_id=1, message=_message("/cancel", 1)))
        await dp.feed_update(bot, _press("cx:7", 2))
        assert ran == ["cancel_listed", "picked=cx:7"], ran

    @pytest.mark.asyncio
    async def test_without_the_state_it_reaches_nothing(self):
        """
        The live failure. This is what 'not handled' looks like from inside:
        the list is sent, the press arrives, and nothing runs.
        """
        ran: list = []
        dp, bot = _dispatcher(ran, set_the_state=False), _Bot()
        await dp.feed_update(bot, Update(update_id=1, message=_message("/cancel", 1)))
        await dp.feed_update(bot, _press("cx:7", 2))
        assert ran == ["cancel_listed"], ran

    @pytest.mark.asyncio
    async def test_the_claim_button_worked_either_way(self):
        """
        Why it read as intermittent rather than broken. Same keyboard, same
        press, no state filter — so this half never failed and he had
        cancelled things successfully before.
        """
        for state_set in (True, False):
            ran: list = []
            dp, bot = _dispatcher(ran, set_the_state=state_set), _Bot()
            await dp.feed_update(bot, Update(update_id=1, message=_message("/cancel", 1)))
            await dp.feed_update(bot, _press("cs:3", 2))
            assert "claim_cleared" in ran, (state_set, ran)


# --------------------------------------------------------------------------
# The class, across the real bot package.
# --------------------------------------------------------------------------

FILTERED = re.compile(r"@router\.(?:callback_query|message)\(\s*(\w+)\.(\w+)[,)]")
SET = re.compile(r"set_state\(\s*(\w+)\.(\w+)\s*\)")
# Only real FSM groups. `F.new_chat_members` matches the same shape and is a
# magic filter on the update, not a state anything could set.
GROUP = re.compile(r"class\s+(\w+)\s*\(\s*StatesGroup\s*\)")


def _sources():
    return [p for p in BOT_DIR.glob("*.py") if p.name != "__init__.py"]


def _state_groups() -> set[str]:
    names: set[str] = set()
    for p in _sources():
        names |= set(GROUP.findall(p.read_text()))
    return names


class TestNoStateIsFilteredOnButNeverSet:
    def test_every_gated_state_is_reachable(self):
        """
        A handler gated on a state nothing sets is a button that does
        nothing, silently, for ever. aiogram logs it at INFO as "Update is
        not handled" and raises nothing, so it will not show up in any error
        alert — only in a complaint from whoever pressed it.

        Checked package-wide because states are set in one file and filtered
        in another often enough that per-file checking would miss it.
        """
        groups = _state_groups()
        assert groups, "no StatesGroup found — the scan is looking in the wrong place"

        gated, setters = set(), set()
        for p in _sources():
            src = p.read_text()
            gated |= {(g, s, p.name) for g, s in FILTERED.findall(src)
                      if g in groups}
            setters |= set(SET.findall(src))

        orphans = sorted(
            f"{p}: {group}.{state} is filtered on but never set"
            for group, state, p in gated if (group, state) not in setters
        )
        assert not orphans, "dead buttons:\n" + "\n".join(orphans)

    def test_cancel_pick_specifically(self):
        """
        Named, so that if the general check is ever loosened this one still
        holds the actual reported bug.
        """
        src = (BOT_DIR / "bridge_trade.py").read_text()
        assert "set_state(Cancel.pick)" in src

    def test_the_state_is_set_before_the_keyboard_is_sent(self):
        """
        Order matters. Telegram can deliver the press before a later
        set_state has run, and the race would be rare, unreproducible and
        blamed on the network.
        """
        src = (BOT_DIR / "bridge_trade.py").read_text()
        body = src.split("async def cmd_cancel")[1].split("\n@router")[0]
        assert "set_state(Cancel.pick)" in body, "cmd_cancel does not set the state"
        assert body.index("set_state(Cancel.pick)") < body.index("reply_markup=")
