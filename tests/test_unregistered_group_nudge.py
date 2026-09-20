"""
A group nobody registered does not sit dead in silence.

WHAT HAPPENED (live, 19–20 September 2026)

14:39 on the 19th: the supplier bot was added to V5 and its chat id went to
UI Control, exactly as designed. Nobody ran /addvendor. The group was
renamed Alpha, and from 22:53 that night commands were typed at it:

    22:53, 23:09, 23:09, 23:53, 09:46, 09:46

Six of them over twelve hours. Every one logged "command in unregistered
chat -1003948139744 - ignored", and every one answered with nothing at all.
The next morning:

    Can you please fix Alpha and bot access as need to clear this asap
    bot ... not listening

Nothing was broken. The bot did precisely what it was built to do, and what
it was built to do was wrong: one notification, sent once, that scrolled
away among a day's messages — after which the group was silently dead and
the only trace was a log line nobody reads.

THE RULE IT BREAKS

The same one the ledger follows: the bot may decline to act, but it may
never decline in silence. That was applied to money and never to
configuration.

WHY A COMMAND AND NOT ANY MESSAGE

Someone typing a slash is trying to use the bot. Ordinary conversation in a
group we do not know is none of our business, and reporting all of it would
let any unknown group make the bot chatter at the Bridge simply by talking.

WHY THE CHAT ITSELF IS STILL NEVER ANSWERED

A reply would confirm to a stranger that this bot is live and hint at what
it does. Everything goes to the Bridge privately. That has not changed.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import auth  # noqa: E402
from bot.auth import ChatRoleMiddleware  # noqa: E402


class _Chat:
    def __init__(self, cid=-100123, title="Alpha"):
        self.id = cid
        self.title = title


class _Bot:
    id = 42


class _Msg:
    """A message, optionally the service message for the bot being added."""

    def __init__(self, text=None, joined=False):
        self.text = text
        self.caption = None
        self.new_chat_members = [_Bot()] if joined else None


class _Notifier:
    def __init__(self):
        self.sent = []

    async def to_bridge(self, text):
        self.sent.append(text)


class _Repo:
    """Every chat is unregistered."""

    async def party_by_chat_id(self, chat_id):
        return None


async def _run(mw, event, chat, notifier):
    called = []

    async def handler(e, d):
        called.append(e)
        return "ran"

    data = {"event_chat": chat, "bot": _Bot(), "notifier": notifier}
    result = await mw(handler, event, data)
    return result, called


def _mw():
    return ChatRoleMiddleware(_Repo(), "supplier")


class TestTheGroupIsStillRefused:
    """The security behaviour is untouched. That comes first."""

    @pytest.mark.asyncio
    async def test_a_command_in_an_unregistered_chat_never_reaches_a_handler(self):
        result, called = await _run(
            _mw(), _Msg("/progress"), _Chat(), _Notifier())
        assert result is None
        assert called == []

    @pytest.mark.asyncio
    async def test_nothing_is_ever_said_in_the_chat_itself(self):
        """
        Only to_bridge. A reply in the group would tell a stranger the bot is
        live and hint at what it does.
        """
        import inspect

        src = inspect.getsource(ChatRoleMiddleware._report_new_chat)
        assert "to_bridge" in src
        assert ".answer(" not in src
        assert ".reply(" not in src


class TestBeingUsedIsReported:
    @pytest.mark.asyncio
    async def test_a_command_reports_the_chat_id(self):
        """The Alpha case. Six commands, nothing said, for twelve hours."""
        n = _Notifier()
        await _run(_mw(), _Msg("/progress"), _Chat(), n)
        assert len(n.sent) == 1
        assert "-100123" in n.sent[0]
        assert "Alpha" in n.sent[0]
        assert "/addvendor" in n.sent[0]

    @pytest.mark.asyncio
    async def test_it_says_someone_is_trying_to_use_it(self):
        """
        Distinct from the join notice. "Added to a group" reads as routine
        setup; "somebody is trying to use this" reads as something to do now.
        """
        n = _Notifier()
        await _run(_mw(), _Msg("/issue"), _Chat(), n)
        assert "trying to use" in n.sent[0]

    @pytest.mark.asyncio
    async def test_the_join_notice_still_reads_as_a_join(self):
        n = _Notifier()
        await _run(_mw(), _Msg(joined=True), _Chat(), n)
        assert "was added to an unregistered group" in n.sent[0]


class TestItDoesNotNag:
    @pytest.mark.asyncio
    async def test_a_burst_of_commands_reports_once(self):
        """Six commands in twelve hours must not be six notifications."""
        mw, n, chat = _mw(), _Notifier(), _Chat()
        for _ in range(6):
            await _run(mw, _Msg("/progress"), chat, n)
        assert len(n.sent) == 1

    @pytest.mark.asyncio
    async def test_it_speaks_again_once_the_interval_has_passed(self):
        mw, n, chat = _mw(), _Notifier(), _Chat()
        await _run(mw, _Msg("/progress"), chat, n)
        mw._nudged[chat.id] = time.monotonic() - auth.NUDGE_INTERVAL_SECONDS - 1
        await _run(mw, _Msg("/progress"), chat, n)
        assert len(n.sent) == 2

    @pytest.mark.asyncio
    async def test_two_unregistered_groups_are_reported_separately(self):
        """The interval is per chat, or the second group hides behind the first."""
        mw, n = _mw(), _Notifier()
        await _run(mw, _Msg("/progress"), _Chat(-1, "Alpha"), n)
        await _run(mw, _Msg("/progress"), _Chat(-2, "Bravo"), n)
        assert len(n.sent) == 2

    @pytest.mark.asyncio
    async def test_a_join_is_never_rate_limited(self):
        """Adding the bot is always worth saying, whatever came before."""
        mw, n, chat = _mw(), _Notifier(), _Chat()
        await _run(mw, _Msg("/progress"), chat, n)
        await _run(mw, _Msg(joined=True), chat, n)
        assert len(n.sent) == 2


class TestOrdinaryTalkIsIgnored:
    @pytest.mark.asyncio
    async def test_conversation_in_an_unknown_group_says_nothing(self):
        """
        Otherwise any group the bot is sitting in could make it chatter at
        the Bridge just by talking.
        """
        n = _Notifier()
        for text in ["morning", "did you send it?", "2354 to Ekta", "ok"]:
            await _run(_mw(), _Msg(text), _Chat(), n)
        assert n.sent == []

    @pytest.mark.asyncio
    async def test_an_empty_message_says_nothing(self):
        n = _Notifier()
        await _run(_mw(), _Msg(None), _Chat(), n)
        assert n.sent == []


class TestAFailureCannotAffectTheDecision:
    @pytest.mark.asyncio
    async def test_a_broken_notifier_still_refuses_the_chat(self):
        class Boom:
            async def to_bridge(self, text):
                raise RuntimeError("telegram down")

        result, called = await _run(_mw(), _Msg("/progress"), _Chat(), Boom())
        assert result is None
        assert called == []
