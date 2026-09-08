"""
Authorisation model tests.

The model changed on 7 September 2026 from per-user to per-chat at the client's
instruction. These tests pin the behaviour so a later refactor cannot quietly
drift back, and so the consequences stay visible in the test names.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.auth import ChatRoleMiddleware  # noqa: E402


class FakeRepo:
    """Chat id -> party, mirroring the unique index on parties.telegram_chat_id."""

    def __init__(self, parties):
        self.parties = parties

    async def party_by_chat_id(self, chat_id):
        return self.parties.get(chat_id)


class Chat:
    def __init__(self, id):
        self.id = id


class User:
    def __init__(self, id):
        self.id = id


SUPPLIER_CHAT = -1001111111111
CLIENT_CHAT = -1002222222222
BRIDGE_CHAT = -1003333333333

REPO = FakeRepo({
    SUPPLIER_CHAT: {"id": 1, "role": "supplier", "label": "Supplier A"},
    CLIENT_CHAT: {"id": 2, "role": "client", "label": "Client A"},
    BRIDGE_CHAT: {"id": 3, "role": "bridge", "label": "Bridge"},
})


async def run(mw, chat_id, user_id=None):
    """Invoke the middleware; returns the injected party, or None if blocked."""
    seen = {}

    async def handler(event, data):
        seen["party"] = data.get("party")
        return "ran"

    data = {"event_chat": Chat(chat_id)}
    if user_id is not None:
        data["event_from_user"] = User(user_id)
    result = await mw(handler, object(), data)
    return seen.get("party") if result == "ran" else None


class TestChatBasedAccess:
    @pytest.mark.asyncio
    async def test_any_member_of_a_registered_group_is_allowed(self):
        """
        The point of the change: the sender's identity is not consulted.
        Two different people in the same group both resolve to the same party.
        """
        mw = ChatRoleMiddleware(REPO, "supplier")
        first = await run(mw, SUPPLIER_CHAT, user_id=999)
        second = await run(mw, SUPPLIER_CHAT, user_id=12345)
        assert first is not None
        assert first == second
        assert first["label"] == "Supplier A"

    @pytest.mark.asyncio
    async def test_sender_identity_is_not_required_at_all(self):
        mw = ChatRoleMiddleware(REPO, "client")
        assert await run(mw, CLIENT_CHAT) is not None

    @pytest.mark.asyncio
    async def test_unregistered_chat_is_ignored(self):
        mw = ChatRoleMiddleware(REPO, "supplier")
        assert await run(mw, -1009999999999, user_id=999) is None

    @pytest.mark.asyncio
    async def test_wrong_bot_for_the_chat_is_ignored(self):
        """The supplier bot in a client group must not act."""
        mw = ChatRoleMiddleware(REPO, "supplier")
        assert await run(mw, CLIENT_CHAT, user_id=999) is None

    @pytest.mark.asyncio
    async def test_missing_chat_is_ignored(self):
        mw = ChatRoleMiddleware(REPO, "supplier")

        async def handler(event, data):
            return "ran"

        assert await mw(handler, object(), {}) is None

    @pytest.mark.asyncio
    async def test_client_chat_cannot_reach_the_bridge_bot(self):
        """A client group must never be able to set rates."""
        mw = ChatRoleMiddleware(REPO, "bridge")
        assert await run(mw, CLIENT_CHAT, user_id=999) is None


class TestStrictBridgeMode:
    """
    The escape hatch. Off by default, as instructed, but available without a
    code change if the Bridge chat ever needs locking down again.
    """

    @pytest.mark.asyncio
    async def test_off_by_default_any_member_of_the_bridge_chat_is_allowed(self):
        mw = ChatRoleMiddleware(REPO, "bridge")
        assert await run(mw, BRIDGE_CHAT, user_id=555) is not None

    @pytest.mark.asyncio
    async def test_on_only_the_configured_user_is_allowed(self):
        mw = ChatRoleMiddleware(
            REPO, "bridge", bridge_user_id=777, strict_bridge_user=True
        )
        assert await run(mw, BRIDGE_CHAT, user_id=777) is not None
        assert await run(mw, BRIDGE_CHAT, user_id=778) is None

    @pytest.mark.asyncio
    async def test_on_a_message_with_no_sender_is_refused(self):
        mw = ChatRoleMiddleware(
            REPO, "bridge", bridge_user_id=777, strict_bridge_user=True
        )
        assert await run(mw, BRIDGE_CHAT) is None

    @pytest.mark.asyncio
    async def test_strict_mode_does_not_affect_supplier_or_client_bots(self):
        mw = ChatRoleMiddleware(
            REPO, "supplier", bridge_user_id=777, strict_bridge_user=True
        )
        assert await run(mw, SUPPLIER_CHAT, user_id=1) is not None
