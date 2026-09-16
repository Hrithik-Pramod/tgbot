"""
A party is called whatever its Telegram group is called.

THE REQUEST (Bridge, 16 September 2026)

    also Grish is not the name now the group is called GS group, and why
    this didnt update?
    i also need a tidy solution on changing the names of the groups.

Labels were set by hand at onboarding, so renaming a group in Telegram
changed nothing here. /setrate, /issue and every deposit notification kept
showing a name nobody used, and he had to ask for each one — twice in three
days, having already asked on the 13th for the names to show at all.

WHY IT IS SAFE TO TAKE THEM FROM TELEGRAM

`label` is Bridge-facing only. It appears in his menus and his
notifications, and deliberately nowhere a client or another supplier can
see it — that boundary is the subject of the 11 September disclosure
incident and the tests guarding it. Reading the names from Telegram
therefore cannot leak anything between counterparties.

THE CARE IT NEEDS

A blank title would erase a name. A duplicate would violate the unique
constraint mid-task. A slow Telegram would hold up startup. And a silent
rename would make last week's report unreconcilable against this week's.
All four are handled, and none of them is hypothetical — the group titles
here already include two that differ only in their last word.
"""

import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from bot.labels import SYNC_INTERVAL_SECONDS, sync_labels  # noqa: E402


class FakeBot:
    def __init__(self, titles=None, fail=False):
        self.titles = titles or {}
        self.fail = fail

    async def get_chat(self, chat_id):
        if self.fail:
            raise RuntimeError("telegram is having a moment")
        return SimpleNamespace(title=self.titles.get(chat_id))


class FakeRepo:
    def __init__(self, parties, blocked=()):
        self._parties = parties
        self.renamed = []
        self._blocked = set(blocked)

    async def all_active_parties(self):
        return self._parties

    async def rename_party(self, *, party_id, new_label, old_label):
        if new_label in self._blocked:
            return False
        self.renamed.append((party_id, old_label, new_label))
        for p in self._parties:
            if p["id"] == party_id:
                p["label"] = new_label
        return True


def _party(pid, role, label, chat):
    return {"id": pid, "role": role, "label": label, "telegram_chat_id": chat}


class TestARenamedGroupIsPickedUp:
    @pytest.mark.asyncio
    async def test_the_label_follows_the_title(self):
        repo = FakeRepo([_party(6, "supplier", "Girish - Sam", -100)])
        bots = {"supplier": FakeBot({-100: "GS group"})}

        assert await sync_labels(repo, bots) == 1
        assert repo.renamed == [(6, "Girish - Sam", "GS group")]

    @pytest.mark.asyncio
    async def test_an_unchanged_name_is_left_alone(self):
        repo = FakeRepo([_party(6, "supplier", "GS group", -100)])
        bots = {"supplier": FakeBot({-100: "GS group"})}

        assert await sync_labels(repo, bots) == 0
        assert repo.renamed == []

    @pytest.mark.asyncio
    async def test_each_party_is_read_by_the_bot_in_its_own_group(self):
        """
        The supplier bot cannot see a client group and must not be asked to.
        """
        repo = FakeRepo([
            _party(2, "supplier", "old supplier", -1),
            _party(3, "client", "old client", -2),
        ])
        bots = {
            "supplier": FakeBot({-1: "New Supplier"}),
            "client": FakeBot({-2: "New Client"}),
        }

        assert await sync_labels(repo, bots) == 2


class TestItRefusesToDoDamage:
    @pytest.mark.asyncio
    async def test_a_blank_title_never_erases_a_name(self):
        repo = FakeRepo([_party(6, "supplier", "GS group", -100)])
        for title in (None, "", "   "):
            bots = {"supplier": FakeBot({-100: title})}
            assert await sync_labels(repo, bots) == 0
        assert repo.renamed == []

    @pytest.mark.asyncio
    async def test_a_name_already_in_use_is_skipped(self):
        """
        Labels are unique. Two groups renamed to the same thing must leave
        the second one alone rather than fail the task.
        """
        repo = FakeRepo([
            _party(2, "supplier", "Wasim - Sam", -1),
            _party(6, "supplier", "GS group", -2),
        ])
        bots = {"supplier": FakeBot({-1: "Wasim - Sam", -2: "Wasim - Sam"})}

        assert await sync_labels(repo, bots) == 0
        assert repo.renamed == []

    @pytest.mark.asyncio
    async def test_a_losing_race_on_the_unique_index_is_absorbed(self):
        """
        The caller checks, but two renames can collide between the check and
        the write. rename_party returns False and nothing raises.
        """
        repo = FakeRepo([_party(6, "supplier", "GS group", -100)],
                        blocked={"Taken"})
        bots = {"supplier": FakeBot({-100: "Taken"})}

        assert await sync_labels(repo, bots) == 0

    @pytest.mark.asyncio
    async def test_telegram_failing_does_not_stop_the_others(self):
        repo = FakeRepo([
            _party(2, "supplier", "old one", -1),
            _party(3, "client", "old two", -2),
        ])
        bots = {"supplier": FakeBot(fail=True),
                "client": FakeBot({-2: "New Client"})}

        assert await sync_labels(repo, bots) == 1
        assert repo.renamed == [(3, "old two", "New Client")]

    @pytest.mark.asyncio
    async def test_a_database_failure_returns_zero_rather_than_raising(self):
        class Broken:
            async def all_active_parties(self):
                raise RuntimeError("db down")

        assert await sync_labels(Broken(), {}) == 0


class TestItCannotDelayOrBreakStartup:
    def test_it_runs_as_its_own_task(self):
        src = inspect.getsource(main.main)
        assert "run_label_sync(repo, bots_by_role)" in src
        assert "asyncio.gather(" in src

    def test_the_loop_swallows_its_own_failures(self):
        from bot import labels
        src = inspect.getsource(labels.run_label_sync)
        assert "except Exception" in src
        assert "while True" in src

    def test_it_does_not_poll_telegram_hard(self):
        assert SYNC_INTERVAL_SECONDS >= 600


class TestTheRenameIsTraceable:
    def test_it_is_audited(self):
        from db.repo import Repo
        src = inspect.getsource(Repo.rename_party)
        assert "party.renamed" in src
        assert '"from": old_label' in src
        assert '"to": new_label' in src
