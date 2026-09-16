"""
Party labels follow their Telegram group names.

THE REQUEST (Bridge, 16 September 2026)

    also Grish is not the name now the group is called GS group, and why
    this didnt update?
    ...
    i also need a tidy solution on changing the names of the groups.

Labels were set by hand at onboarding. Renaming a group in Telegram changed
nothing here, so /setrate, /issue and every deposit notification kept
showing a name nobody used any more — and the Bridge had to ask for each
one. Twice in three days.

WHY THE LABEL MATTERS AND WHY THIS IS SAFE

`label` is what the Bridge sees: the buttons in /setrate, the rows in
/issue and /cancel, the header on a deposit notification. It is deliberately
NOT shown to a client or to another supplier — that boundary was the subject
of the 11 September disclosure incident and the tests that came out of it.
So taking these names from Telegram cannot leak anything between
counterparties; it only changes what he reads.

WHAT IT WILL NOT DO

  rename to nothing        a blank or missing title is ignored
  rename onto a clash      labels are unique, and two groups sharing a title
                           would otherwise fail mid-update; the second is
                           left alone and logged
  rename silently          every change is written to the audit log, because
                           a name changing underneath a reconciliation needs
                           to be traceable afterwards

A failure here is never allowed to stop startup. A stale name is a
nuisance; a bot that will not boot because Telegram was slow is not.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# Long enough that a rename is picked up within the hour without asking, far
# short of anything that would matter to the API budget.
SYNC_INTERVAL_SECONDS = 1800


async def sync_labels(repo, bots_by_role: dict) -> int:
    """
    Bring every active party's label in line with its Telegram title.

    Returns how many were changed, so a caller can log something useful.
    """
    changed = 0
    try:
        parties = await repo.all_active_parties()
    except Exception:
        log.exception("could not read parties for label sync")
        return 0

    taken = {p["label"] for p in parties}

    for party in parties:
        bot = bots_by_role.get(party["role"])
        if bot is None:
            continue

        try:
            chat = await bot.get_chat(party["telegram_chat_id"])
        except Exception as exc:
            # A group the bot has been removed from, or a transient API
            # failure. Neither is worth a noisy alert — the membership check
            # in the health check already covers the first.
            log.info("could not read the title for %s: %s", party["label"], exc)
            continue

        title = (getattr(chat, "title", None) or "").strip()
        if not title or title == party["label"]:
            continue

        if title in taken:
            log.warning(
                "not renaming %r to %r — another party already uses that name",
                party["label"], title,
            )
            continue

        ok = await repo.rename_party(
            party_id=party["id"], new_label=title, old_label=party["label"]
        )
        if ok:
            taken.discard(party["label"])
            taken.add(title)
            changed += 1
            log.info("renamed party %s to %r", party["id"], title)

    return changed


async def run_label_sync(repo, bots_by_role: dict) -> None:
    """
    Sync at startup, then periodically.

    Wrapped so that nothing in here can take the process down: this is a
    convenience, and the bot has to keep watching for money whatever
    Telegram is doing.
    """
    while True:
        try:
            changed = await sync_labels(repo, bots_by_role)
            if changed:
                log.info("label sync updated %s name(s)", changed)
        except Exception:
            log.exception("label sync failed")
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
