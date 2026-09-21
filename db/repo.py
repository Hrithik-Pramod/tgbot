"""
Data access layer.

asyncpg returns NUMERIC columns as Decimal, so money stays exact all the way
from the database to the calculation engine. Nothing here ever casts to float.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional, Sequence  # noqa: F401

import asyncpg

log = logging.getLogger(__name__)


def derive_prefix(label: str) -> str:
    """
    Turn a supplier label into a deal-reference prefix.

    E2: "SUPA1 is a deal number, so Supplier, PA, transaction 1". So
    "Supplier A" must produce "SUPA", not "SUPPLIERA" — a naive
    label.replace(" ", "").upper() gets this wrong, and the wrong prefix is
    baked into every reference that supplier ever generates.

        Supplier A  -> SUPA
        Supplier B  -> SUPB
        Northgate   -> NORT      (fallback: first four alphanumerics)
    """
    import re

    m = re.fullmatch(r"\s*supplier\s+([A-Za-z0-9]{1,3})\s*", label, re.I)
    if m:
        return f"SUP{m.group(1).upper()}"
    cleaned = re.sub(r"[^A-Za-z0-9]", "", label).upper()
    if not cleaned:
        raise ValueError(f"cannot derive a deal prefix from label {label!r}")
    return cleaned[:4]


class Repo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "Repo":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    # ------------------------------------------------------------------ audit

    async def audit(
        self,
        conn: asyncpg.Connection,
        *,
        actor_party_id: Optional[int],
        action: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
        detail: dict | None = None,
    ) -> None:
        """
        Append-only. Never updated, never deleted.

        Required by E5: a completed trade may be corrected, and the correction
        has to be recorded somewhere that cannot itself be rewritten.
        """
        import json

        await conn.execute(
            """
            INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            actor_party_id, action, entity_type, entity_id,
            json.dumps(detail or {}, default=str),
        )

    async def audit_standalone(
        self,
        *,
        actor_party_id: Optional[int],
        action: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
        detail: dict | None = None,
    ) -> None:
        """
        The same record, for a caller with no transaction of its own.

        audit() takes a connection because almost every entry belongs inside
        the change it describes — the audit and the UPDATE stand or fall
        together. This is for the other kind: an observation that is worth
        keeping whether or not anything else happened, such as a beneficiary
        the matcher could not place.

        It must never be able to break the flow it is observing. A diagnostic
        that can fail a payment is worse than no diagnostic.
        """
        try:
            async with self.pool.acquire() as conn:
                await self.audit(
                    conn, actor_party_id=actor_party_id, action=action,
                    entity_type=entity_type, entity_id=entity_id, detail=detail,
                )
        except Exception:
            log.exception("could not write audit entry %r", action)

    # ----------------------------------------------------------------- parties

    async def party_by_chat_id(self, telegram_chat_id: int) -> Optional[asyncpg.Record]:
        """
        The authorisation lookup. Runs on every command.

        Keyed on the chat, not the individual sender: whoever the Bridge adds to
        a registered group may act as that party (client decision, 7 Sep 2026).
        Access is controlled by group membership, which the Bridge manages.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT id, role, label, display_name, telegram_user_id, telegram_chat_id
                FROM parties
                WHERE telegram_chat_id = $1 AND is_active
                """,
                telegram_chat_id,
            )

    async def list_parties(self, role: str) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, label, display_name
                FROM parties
                WHERE role = $1::party_role AND is_active
                ORDER BY label
                """,
                role,
            )

    async def add_party(
        self, *, role: str, label: str, display_name: str,
        telegram_chat_id: int, telegram_user_id: int | None,
        actor_party_id: int | None, prefix: str | None = None,
    ) -> int:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                party_id = await conn.fetchval(
                    """
                    INSERT INTO parties (role, label, display_name,
                                         telegram_chat_id, telegram_user_id)
                    VALUES ($1::party_role, $2, $3, $4, $5)
                    RETURNING id
                    """,
                    role, label, display_name, telegram_chat_id, telegram_user_id,
                )
                if role == "supplier":
                    await conn.execute(
                        """
                        INSERT INTO supplier_counters (supplier_id, prefix)
                        VALUES ($1, $2)
                        """,
                        party_id, prefix or derive_prefix(label),
                    )
                await self.audit(
                    conn, actor_party_id=actor_party_id, action="party.add",
                    entity_type="party", entity_id=party_id,
                    detail={"role": role, "label": label},
                )
                return party_id

    async def onboard_supplier(
        self, *, label: str, telegram_chat_id: int, prefix: str,
        client_id: int, wallet_address: str, actor_party_id: int,
    ) -> tuple[bool, str, Optional[dict]]:
        """
        Register a vendor: party, deal counter and internal wallet, at once.

        Client request, 18 September 2026:

            Additional FX groups: To save time, I'm going to create some more
            FX groups now so they are ready for later use add bots

        Until now this was deploy/seed-supplier.sql, run by me. Every vendor
        added since go-live has needed a developer at a terminal, which is a
        poor answer to "so they are ready for later".

        WHY ALL THREE TOGETHER

        A party with no counter looks fine until its first deposit, when
        next_reference raises and the deposit fails — the worst possible
        moment for a setup mistake to surface. A party with no internal wallet
        can never receive anything. Half an onboarding is not a vendor, so
        this is one transaction and either all of it exists or none does.

        WHAT IT REFUSES

          unknown client       the pairing would point at nothing
          chat already used    that group is already somebody
          label already used   references and every message would be ambiguous
          prefix already used  SUPB1 from two vendors is unresolvable, and
                               nothing in the schema stops it
          address registered   a deposit is attributed by which wallet
                               received it, so a shared address makes two
                               vendors indistinguishable

        The wallet is NOT adopted. The monitor adopts it on its first poll and
        records the baseline then, so whatever is already on the address is
        history. Seeding that by hand is how 38 old transfers were once
        ingested as live deposits.

        No rate is set either. /setrate shows the rate in force, warns when
        the sell rate is not above the supply rate, and records who set it;
        setting one here would bypass all three. Until a rate exists a deposit
        opens no trade and the Bridge is told why, so the gap is visible.
        """
        label = label.strip()
        prefix = prefix.strip().upper()

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                client = await conn.fetchrow(
                    "SELECT id, label FROM parties "
                    "WHERE id = $1 AND role = 'client' AND is_active",
                    client_id,
                )
                if client is None:
                    return False, "That client no longer exists.", None

                clash = await conn.fetchrow(
                    "SELECT label, role FROM parties WHERE telegram_chat_id = $1",
                    telegram_chat_id,
                )
                if clash is not None:
                    return False, (
                        f"That chat is already registered as "
                        f"{clash['label']} ({clash['role']})."
                    ), None

                if await conn.fetchval(
                    "SELECT 1 FROM parties WHERE lower(label) = lower($1)", label
                ):
                    return False, f"There is already a party called {label}.", None

                # Not just an exact clash — an OVERLAPPING one.
                #
                # A reference is the prefix with a number stuck on the end,
                # so two prefixes collide whenever one is the start of the
                # other. V1 and V13 look distinct and are not: V1's
                # thirty-first trade is V131, and so is V13's first. The
                # unique index then rejects whichever comes second, and it
                # does so inside the deposit handler — the worst moment for
                # a setup mistake to surface, and one nobody would connect
                # to a vendor registered weeks earlier.
                #
                # Caught 19 September 2026 from the live table, with V13
                # registered and V5–V12 about to be. Vendors are numbered
                # sequentially here, so V1 was a matter of time.
                taken = await conn.fetchrow(
                    """
                    SELECT p.label, sc.prefix FROM supplier_counters sc
                    JOIN parties p ON p.id = sc.supplier_id
                    WHERE upper(sc.prefix) = $1
                       OR upper(sc.prefix) LIKE $1 || '%'
                       OR $1 LIKE upper(sc.prefix) || '%'
                    """,
                    prefix,
                )
                if taken is not None:
                    if taken["prefix"].upper() == prefix:
                        return False, (
                            f"{taken['label']} already uses the prefix "
                            f"{prefix}. Two vendors sharing one makes their "
                            "deal numbers impossible to tell apart."
                        ), None
                    return False, (
                        f"{prefix} overlaps with {taken['prefix']}, used by "
                        f"{taken['label']}.\n\n"
                        "A deal number is the prefix with a number after it, "
                        f"so {prefix} and {taken['prefix']} would eventually "
                        "produce the same reference for two different trades "
                        "— and that only fails when a deposit lands.\n\n"
                        "Pick something that is not the start of another "
                        "prefix, or a continuation of one."
                    ), None

                if await conn.fetchval(
                    "SELECT 1 FROM wallets WHERE address = $1", wallet_address
                ):
                    return False, "That wallet address is already registered.", None

                party_id = await conn.fetchval(
                    """
                    INSERT INTO parties (role, label, display_name,
                                         telegram_chat_id, telegram_user_id)
                    VALUES ('supplier', $1, $1, $2, NULL)
                    RETURNING id
                    """,
                    label, telegram_chat_id,
                )
                await conn.execute(
                    "INSERT INTO supplier_counters (supplier_id, prefix) "
                    "VALUES ($1, $2)",
                    party_id, prefix,
                )
                wallet_id = await conn.fetchval(
                    """
                    INSERT INTO wallets (address, is_internal, supplier_id,
                                         client_id, label, is_monitored)
                    VALUES ($1, TRUE, $2, $3, $4, TRUE)
                    RETURNING id
                    """,
                    wallet_address, party_id, client_id,
                    f"{label} → {client['label']}",
                )
                await self.audit(
                    conn, actor_party_id=actor_party_id,
                    action="supplier.onboarded",
                    entity_type="party", entity_id=party_id,
                    detail={
                        "label": label, "prefix": prefix,
                        "client": client["label"],
                        "chat_id": telegram_chat_id,
                        "wallet_id": wallet_id,
                    },
                )
                return True, f"{label} registered.", {
                    "party_id":     party_id,
                    "wallet_id":    wallet_id,
                    "label":        label,
                    "prefix":       prefix,
                    "client_label": client["label"],
                }

    # ----------------------------------------------------------- bank accounts

    async def list_bank_accounts(self, party_id: int) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, account_name, account_number, ifsc
                FROM bank_accounts
                WHERE party_id = $1 AND is_active
                ORDER BY account_name
                """,
                party_id,
            )

    async def add_bank_account(
        self, *, party_id: int, account_name: str,
        account_number: str, ifsc: str,
    ) -> int:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                account_id = await conn.fetchval(
                    """
                    INSERT INTO bank_accounts (party_id, account_name, account_number, ifsc)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (party_id, account_number, ifsc)
                    DO UPDATE SET is_active = TRUE, removed_at = NULL,
                                  account_name = EXCLUDED.account_name
                    RETURNING id
                    """,
                    party_id, account_name, account_number, ifsc,
                )
                await self.audit(
                    conn, actor_party_id=party_id, action="account.add",
                    entity_type="bank_account", entity_id=account_id,
                    detail={"account_name": account_name, "ifsc": ifsc},
                )
                return account_id

    async def remove_bank_account(self, *, account_id: int, party_id: int) -> bool:
        """
        Soft delete. A hard delete would orphan the payments that reference this
        account and make historic summaries unreadable.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE bank_accounts
                    SET is_active = FALSE, removed_at = now()
                    WHERE id = $1 AND party_id = $2 AND is_active
                    RETURNING id, account_name
                    """,
                    account_id, party_id,
                )
                if row:
                    await self.audit(
                        conn, actor_party_id=party_id, action="account.remove",
                        entity_type="bank_account", entity_id=account_id,
                        detail={"account_name": row["account_name"]},
                    )
                return row is not None

    # --------------------------------------------------------------- wallets

    async def list_wallets(self) -> list[asyncpg.Record]:
        """
        Backs /wallet. Shows the internal-to-client linkage the brief calls
        "very important for routing and calculations".
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT w.id, w.address, w.is_internal, w.label, w.is_monitored,
                       w.supplier_id, w.client_id, w.owner_party_id,
                       s.label AS supplier_label,
                       c.label AS client_label,
                       o.label AS owner_label,
                       p.address AS payout_address
                FROM wallets w
                LEFT JOIN parties s ON s.id = w.supplier_id
                LEFT JOIN parties c ON c.id = w.client_id
                LEFT JOIN parties o ON o.id = w.owner_party_id
                LEFT JOIN wallets p ON p.id = w.payout_wallet_id
                ORDER BY w.is_internal DESC, w.id
                """
            )

    async def all_active_parties(self) -> list[asyncpg.Record]:
        """Every party and the chat it lives in. Backs the label sync."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, role, label, telegram_chat_id
                FROM parties WHERE is_active ORDER BY id
                """
            )

    async def rename_party(
        self, *, party_id: int, new_label: str, old_label: str,
    ) -> bool:
        """
        Take a party's name from its Telegram group title.

        Returns False rather than raising if the name is already in use. The
        caller checks first, but two groups can be renamed to the same thing
        between the check and the write, and a label collision must not take
        down a background task.

        Audited because a name changing underneath a reconciliation has to be
        traceable: "Supplier C" in a report from last week and "Feb David
        Group" in one from today are the same party, and only this row says so.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                try:
                    await conn.execute(
                        "UPDATE parties SET label = $2 WHERE id = $1",
                        party_id, new_label,
                    )
                except asyncpg.UniqueViolationError:
                    return False
                await self.audit(
                    conn, actor_party_id=None, action="party.renamed",
                    entity_type="party", entity_id=party_id,
                    detail={"from": old_label, "to": new_label,
                            "source": "telegram group title"},
                )
        return True

    async def wallets_owned_by(self, party_id: int) -> list[asyncpg.Record]:
        """
        A party's own addresses — the ones they are paid at, not the internal
        ones deposits land on. Backs the payout picker in /walletlink.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, address, label FROM wallets
                WHERE owner_party_id = $1 AND NOT is_internal
                ORDER BY id
                """,
                party_id,
            )

    async def set_payout_wallet(
        self, *, wallet_id: int, payout_wallet_id: int, actor_party_id: int,
    ) -> tuple[bool, str]:
        """
        Record which client address a pairing settles to.

        Refuses anything that would not make sense rather than storing it and
        printing nonsense on a deposit notification later: the source has to
        be an internal pairing, and the destination has to be a wallet
        belonging to that pairing's own client. Paying one client's trade to
        another client's address is the kind of mistake that is obvious in a
        sentence and invisible in a foreign key.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                src = await conn.fetchrow(
                    "SELECT id, is_internal, client_id FROM wallets WHERE id = $1",
                    wallet_id,
                )
                dst = await conn.fetchrow(
                    """
                    SELECT w.id, w.is_internal, w.owner_party_id, w.address,
                           p.label AS owner_label
                    FROM wallets w LEFT JOIN parties p ON p.id = w.owner_party_id
                    WHERE w.id = $1
                    """,
                    payout_wallet_id,
                )
                if src is None or dst is None:
                    return False, "That wallet no longer exists."
                if not src["is_internal"]:
                    return False, "Only a supplier pairing can have a payout address."
                if dst["is_internal"]:
                    return False, (
                        "That is an internal wallet — deposits land there. "
                        "Pick one of the client's own addresses."
                    )
                if dst["owner_party_id"] != src["client_id"]:
                    return False, (
                        f"That address belongs to {dst['owner_label']}, not to "
                        "this pairing's client."
                    )

                await conn.execute(
                    "UPDATE wallets SET payout_wallet_id = $2 WHERE id = $1",
                    wallet_id, payout_wallet_id,
                )
                await self.audit(
                    conn, actor_party_id=actor_party_id, action="wallet.payout_set",
                    entity_type="wallet", entity_id=wallet_id,
                    detail={"payout_wallet_id": payout_wallet_id,
                            "payout_address": dst["address"]},
                )
        return True, f"Settlements for this pairing go to {dst['address']}."

    async def add_wallet(
        self, *, address: str, is_internal: bool, actor_party_id: int,
        supplier_id: Optional[int] = None, client_id: Optional[int] = None,
        owner_party_id: Optional[int] = None, label: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Register a new wallet from the Bridge bot.

        Until 16 September 2026 there was no way to do this — /wallet showed
        them and /walletchange changed an address, but every wallet had been
        inserted by hand. The Bridge found the edge of that the only way
        anyone finds it: by needing one at three in the morning.

        The address is NOT adopted here. The monitor adopts it on its first
        poll and records the baseline then, so everything already on the
        address is treated as history. Seeding that by hand is how 38 old
        transfers were once ingested as live deposits.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                clash = await conn.fetchval(
                    "SELECT id FROM wallets WHERE address = $1", address
                )
                if clash is not None:
                    return False, "That address is already registered."

                if is_internal:
                    taken = await conn.fetchval(
                        """
                        SELECT w.id FROM wallets w
                        WHERE w.is_internal AND w.supplier_id = $1 AND w.client_id = $2
                        """,
                        supplier_id, client_id,
                    )
                    if taken is not None:
                        return False, (
                            "That pairing already has an internal wallet. Use "
                            "/walletchange to change its address."
                        )

                new_id = await conn.fetchval(
                    """
                    INSERT INTO wallets (address, is_internal, supplier_id,
                                         client_id, owner_party_id, label,
                                         is_monitored)
                    VALUES ($1, $2, $3, $4, $5, $6, TRUE)
                    RETURNING id
                    """,
                    address, is_internal, supplier_id, client_id,
                    owner_party_id, label,
                )
                await self.audit(
                    conn, actor_party_id=actor_party_id, action="wallet.added",
                    entity_type="wallet", entity_id=new_id,
                    detail={"address": address, "internal": is_internal,
                            "label": label},
                )
        return True, (
            "Wallet registered and being watched. Anything already on that "
            "address is treated as history — only transfers from now on count "
            "as deposits."
        )

    async def claim_trade_announcement(self, trade_id: int) -> Optional[asyncpg.Record]:
        """
        Win the right to announce this trade, exactly once.

        Two things race to release a held notification: the supplier running
        /send, and the monitor's timeout sweep. Both can fire in the same
        second — a supplier who runs /send nine minutes and fifty-nine
        seconds after sending is not a rare case, it is a normal one.

        Setting the stamp inside the same statement that reads it means only
        one caller ever gets a row back. The other gets None and does
        nothing, so the Bridge is told once rather than twice about the same
        deposit.

        Returns everything the notification needs, so the caller does not
        have to go back for labels and hashes.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                WITH claimed AS (
                    UPDATE trades SET announced_at = now()
                    WHERE id = $1 AND announced_at IS NULL
                    RETURNING *
                )
                SELECT c.*,
                       s.label AS supplier_label,
                       cl.label AS client_label,
                       b.account_name AS nominated_name,
                       w.address AS wallet_address,
                       pw.address AS payout_address,
                       (SELECT d.tx_hash FROM deposits d
                        WHERE d.trade_id = c.id
                        ORDER BY d.detected_at DESC LIMIT 1) AS tx_hash
                FROM claimed c
                JOIN parties s  ON s.id  = c.supplier_id
                JOIN parties cl ON cl.id = c.client_id
                JOIN wallets w  ON w.id  = c.wallet_id
                LEFT JOIN wallets pw ON pw.id = w.payout_wallet_id
                LEFT JOIN bank_accounts b ON b.id = c.nominated_account_id
                """,
                trade_id,
            )

    async def open_trade_id_for_reference(self, reference: str) -> Optional[int]:
        """
        nominate_account returns the reference it attached to, because that
        is what the supplier's confirmation used to print. Releasing a held
        announcement needs the id, and looking it up here avoids changing
        that return type and every caller that reads it.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT id FROM trades WHERE reference = $1", reference
            )

    async def mark_trade_announced(self, trade_id: int) -> None:
        """
        Record that the Bridge has been told, when the notification went out
        on the ordinary path rather than through a held release.

        Without this a deposit that arrived WITH a nomination already in
        place would be announced immediately and then announced a second
        time by the timeout sweep, which still saw a null stamp.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE trades SET announced_at = COALESCE(announced_at, now()) "
                "WHERE id = $1",
                trade_id,
            )

    async def trades_awaiting_announcement(self, older_than_minutes: int) -> list[asyncpg.Record]:
        """
        Trades whose notification has been held long enough.

        The floor under the wait. A supplier who sends USDT and then never
        runs /send would otherwise leave money recorded and nobody told —
        which is the failure this whole day was about.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id FROM trades
                WHERE announced_at IS NULL
                  AND status IN ('open', 'awaiting_payment')
                  AND opened_at < now() - ($1 || ' minutes')::interval
                ORDER BY opened_at
                """,
                str(older_than_minutes),
            )

    async def party_label(self, party_id: int) -> Optional[str]:
        """The name a party is known by. Used to say whose wallet a payout
        reached, rather than describing it as 'non-internal'."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT label FROM parties WHERE id = $1", party_id
            )

    async def monitored_wallets(self) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT w.id, w.address, w.is_internal, w.supplier_id, w.client_id,
                       w.owner_party_id,
                       m.last_timestamp_ms, m.adopted_at_ms
                FROM wallets w
                LEFT JOIN monitor_state m ON m.wallet_id = w.id
                WHERE w.is_monitored
                """
            )


    async def update_wallet_address(
        self, *, wallet_id: int, new_address: str, actor_party_id: int,
    ) -> None:
        """
        /walletchange. The monitor picks the new address up on its next cycle
        because it reads the wallet table each pass rather than caching it.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                old = await conn.fetchval(
                    "SELECT address FROM wallets WHERE id = $1", wallet_id
                )
                await conn.execute(
                    "UPDATE wallets SET address = $2 WHERE id = $1",
                    wallet_id, new_address,
                )
                # The cursor belonged to the old address; clear it so the new
                # one is not scanned from a meaningless offset.
                await conn.execute(
                    "DELETE FROM monitor_state WHERE wallet_id = $1", wallet_id
                )
                await self.audit(
                    conn, actor_party_id=actor_party_id, action="wallet.change",
                    entity_type="wallet", entity_id=wallet_id,
                    detail={"old": old, "new": new_address},
                )

    async def set_monitor_cursor(
        self, wallet_id: int, last_timestamp_ms: int
    ) -> None:
        """
        Advance the cursor. UPDATE only — this must never create the row.

        /walletchange deletes monitor_state so the new address is adopted
        fresh. But the poll cycle reads every wallet once at the top and then
        works through them, so a cycle already in flight still holds the OLD
        cursor. When it used to upsert, that in-flight cycle recreated the
        row with the stale cursor and no adoption baseline.

        A cursor without a baseline is exactly the state adopt_wallet writes
        both columns together to prevent, and it is not recoverable
        afterwards: it is indistinguishable from a wallet adopted before the
        baseline column existed. Worse, a null baseline disables the history
        guard, which is what stops the two-minute overlap rewind pulling old
        transfers in as new deposits — 38 of them on 9 September 2026, and a
        trade for ₹19,851,619.

        Live on 15 September 2026: Supplier D's wallet spent four hours in
        exactly that state after an address change.

        So the rule is that ONLY adoption creates the row. If it is missing,
        this writes nothing and the next cycle — reading a fresh snapshot
        with no cursor — adopts the wallet properly.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE monitor_state
                SET last_timestamp_ms = GREATEST(last_timestamp_ms, $2),
                    last_polled_at = now(),
                    consecutive_errors = 0
                WHERE wallet_id = $1
                """,
                wallet_id, last_timestamp_ms,
            )

    async def mark_polled(self, wallet_id: int) -> None:
        """
        Record that a wallet was reached, whether or not it had anything new.

        last_polled_at used to be written only by set_monitor_cursor and
        adopt_wallet, and set_monitor_cursor only runs when transfers come
        back. So a wallet polling perfectly well but sitting quiet never
        updated it, and the health check reported "deposits are being missed"
        about wallets that were fine.

        On 15 September 2026 that fired alongside a genuine fault on another
        wallet, which is the real cost: a check that cries wolf is one nobody
        reads on the day it matters.

        UPDATE only, for the same reason as set_monitor_cursor: a row that
        /walletchange has just deleted must stay deleted until adoption
        recreates it with both the cursor and the baseline. An unadopted
        wallet therefore reports as unpolled, which is true — it is not being
        watched yet.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE monitor_state
                SET last_polled_at = now(), consecutive_errors = 0
                WHERE wallet_id = $1
                """,
                wallet_id,
            )

    async def adopt_wallet(
        self, wallet_id: int, *, cursor_ms: int, adopted_at_ms: int
    ) -> None:
        """
        Record that a wallet has been taken on, and where its history ends.

        Written together in one statement so a wallet can never end up with a
        cursor but no adoption baseline — that combination would look exactly
        like an established wallet and let its history back in.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO monitor_state (wallet_id, last_timestamp_ms,
                                           adopted_at_ms, last_polled_at,
                                           consecutive_errors)
                VALUES ($1, $2, $3, now(), 0)
                ON CONFLICT (wallet_id) DO UPDATE
                SET last_timestamp_ms = EXCLUDED.last_timestamp_ms,
                    adopted_at_ms = EXCLUDED.adopted_at_ms,
                    last_polled_at = now(),
                    consecutive_errors = 0
                """,
                wallet_id, cursor_ms, adopted_at_ms,
            )

    # ------------------------------------------------------------------ rates

    async def set_rate(
        self, *, supplier_id: int, client_id: int,
        supply_rate: Decimal, sell_rate: Decimal, set_by: int,
    ) -> int:
        """Append-only, so historic trades stay reproducible."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rate_id = await conn.fetchval(
                    """
                    INSERT INTO rates (supplier_id, client_id, supply_rate, sell_rate, set_by)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id
                    """,
                    supplier_id, client_id, supply_rate, sell_rate, set_by,
                )
                await self.audit(
                    conn, actor_party_id=set_by, action="rate.set",
                    entity_type="rate", entity_id=rate_id,
                    detail={
                        "supplier_id": supplier_id, "client_id": client_id,
                        "supply_rate": supply_rate, "sell_rate": sell_rate,
                    },
                )
                return rate_id

    async def current_rate(
        self, supplier_id: int, client_id: int
    ) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT id, supply_rate, sell_rate, created_at,
                       EXTRACT(EPOCH FROM (now() - created_at)) / 3600 AS age_hours
                FROM rates
                WHERE supplier_id = $1 AND client_id = $2
                ORDER BY created_at DESC
                LIMIT 1
                """,
                supplier_id, client_id,
            )

    # ----------------------------------------------------------------- trades

    async def next_reference(self, conn: asyncpg.Connection, supplier_id: int) -> str:
        """
        E2: continuous per supplier, never resets. SUPA1, SUPA2, ...

        The UPDATE ... RETURNING takes a row lock, so two deposits landing at the
        same moment cannot be handed the same deal number.
        """
        row = await conn.fetchrow(
            """
            UPDATE supplier_counters
            SET last_number = last_number + 1
            WHERE supplier_id = $1
            RETURNING prefix, last_number
            """,
            supplier_id,
        )
        if row is None:
            raise RuntimeError(f"no deal counter configured for supplier {supplier_id}")
        return f"{row['prefix']}{row['last_number']}"


    async def open_trade_for_client(self, client_id: int) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT t.*,
                       COALESCE(t.supplier_label_at_trade, s.label)
                           AS supplier_label,
                       c.label AS client_label
                FROM trades t
                JOIN parties s ON s.id = t.supplier_id
                JOIN parties c ON c.id = t.client_id
                WHERE t.client_id = $1 AND t.status IN ('open', 'awaiting_payment')
                ORDER BY t.opened_at DESC
                LIMIT 1
                """,
                client_id,
            )

    async def suppliers_for_client(self, client_id: int) -> list[asyncpg.Record]:
        """
        Every supplier this client is paired with, via the internal wallets.

        Backs /accounts when no trade is open. Before this the command needed
        an open trade and said "you have no open trade" otherwise, which reads
        as a fault to anyone who just wants to see where they pay
        (reported 10 September 2026: "Does not show accounts").
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT DISTINCT s.id, s.label
                FROM wallets w
                JOIN parties s ON s.id = w.supplier_id
                WHERE w.is_internal AND w.client_id = $1 AND s.is_active
                ORDER BY s.label
                """,
                client_id,
            )

    async def open_trades_for_client(self, client_id: int) -> list[asyncpg.Record]:
        """
        EVERY open trade for this client, not just the newest.

        A client can be running one trade per supplier at the same time — Client
        A had SUPA1 and SUPB1 open together on 11 September 2026. Resolving
        "the" open trade with ORDER BY opened_at DESC LIMIT 1 silently charged
        every payment to whichever started most recently, so money paid against
        one supplier's instruction landed on another's trade.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT t.*,
                       COALESCE(t.supplier_label_at_trade, s.label)
                           AS supplier_label,
                       c.label AS client_label
                FROM trades t
                JOIN parties s ON s.id = t.supplier_id
                JOIN parties c ON c.id = t.client_id
                WHERE t.client_id = $1 AND t.status IN ('open', 'awaiting_payment')
                ORDER BY t.opened_at
                """,
                client_id,
            )

    async def open_trade_accounts_for_client(self, client_id: int) -> list[asyncpg.Record]:
        """
        The accounts this client could be paying, each carrying its trade.

        This is what makes a payment attributable. A bank account belongs to
        exactly one supplier, and a supplier's open trade is one trade — so the
        account named on a payment identifies the trade it belongs to, with no
        guessing and nothing for the client to select.

        SCOPE, and why it is narrow (client complaint, 11 September 2026)
        ----------------------------------------------------------------
        Only suppliers this client has actually been INSTRUCTED to pay. An
        instruction means a payment slot has been issued on an open trade.

        Widening this to every open trade leaked the Bridge's book: the client
        was shown a list containing both suppliers' names and all five of their
        account holders, in their own group, having been instructed to pay only
        one. A counterparty must not learn who else the Bridge deals with, how
        many accounts they hold, or whose names are on them.

        If nothing has been instructed yet there is nothing to leak and nothing
        to narrow by, so the open trades' accounts are used — the client can
        only be paying one of those.

        WHICH trade, when an account maps to more than one (11 Sep 2026)
        ----------------------------------------------------------------
        Since a deposit arriving after an instruction opens a NEW trade, one
        supplier can have two trades open at once — SUPB1 awaiting payment and
        SUPB2 behind it — and the same bank account sits on both. The account
        alone no longer names the trade.

        The tie-break is FIFO, which is what the client is actually doing:
        they pay the instruction they were given, and instructions go out in
        order. So a payment is attributed to the OLDEST instructed trade for
        that account that is not yet fully paid. Once SUPB1 is covered, the
        next payment to the same account falls through to SUPB2.

        Fully-paid trades stay in the list rather than being filtered out, so
        that a late or duplicate payment still resolves to a real trade and is
        rejected by the UTR constraint, rather than resolving to nothing and
        being reported as an unrecognised account.
        """
        async with self.pool.acquire() as conn:
            instructed = await conn.fetch(
                """
                SELECT id, account_name, account_number, ifsc, trade_id, reference
                FROM (
                    SELECT DISTINCT ON (b.id)
                           b.id, b.account_name, b.account_number, b.ifsc,
                           t.id AS trade_id, t.reference
                    FROM trades t
                    JOIN payment_slots ps ON ps.trade_id = t.id
                    JOIN bank_accounts b ON b.party_id = t.supplier_id AND b.is_active
                    WHERE t.client_id = $1
                      AND t.status IN ('open', 'awaiting_payment')
                    ORDER BY
                        b.id,
                        -- unpaid trades first, oldest instruction first
                        (COALESCE((SELECT sum(p.amount_inr) FROM payments p
                                   WHERE p.trade_id = t.id), 0)
                         >= t.inr_expected),
                        t.instructed_at,
                        t.opened_at
                ) picked
                ORDER BY account_name
                """,
                client_id,
            )
            if instructed:
                return instructed

            return await conn.fetch(
                """
                SELECT b.id, b.account_name, b.account_number, b.ifsc,
                       t.id AS trade_id, t.reference
                FROM trades t
                JOIN bank_accounts b ON b.party_id = t.supplier_id AND b.is_active
                WHERE t.client_id = $1 AND t.status IN ('open', 'awaiting_payment')
                ORDER BY b.account_name
                """,
                client_id,
            )

    async def matchable_accounts_for_client(self, client_id: int) -> list[asyncpg.Record]:
        """
        Every account this client could plausibly be paying, for MATCHING —
        which is a different question from what they are shown.

        WHY THESE ARE DIFFERENT

        open_trade_accounts_for_client narrows to suppliers the client has
        actually been instructed to pay. That exists because showing the full
        list leaked the Bridge's book into a counterparty's group on
        11 September 2026, and it is still right for anything the client SEES.

        It is wrong for recognising a name the client has typed. They already
        know who they paid — reading it back to attribute a payment reveals
        nothing. On 16 September that narrowing rejected two live payments:

            unmatched beneficiary 'Barkaati Textile'
            unmatched beneficiary 'PRIME PATH ENTERPRISES GGN'

        Both are real, active, registered accounts — Malegao - Sam's and GS
        Group's. Their trades had been cancelled, so their accounts dropped
        out of the candidates and the payments could not be read at all. The
        Bridge: "bot is not reading the slips", "lots of slips keep missing".

        WHICH TRADE EACH ACCOUNT RESOLVES TO

        The supplier's best open trade: instructed before uninstructed,
        unpaid before paid, oldest first. trade_id comes back NULL when that
        supplier has nothing open — which is a real answer, not a failure.
        The caller reports it rather than recording against a guess.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT DISTINCT ON (b.id)
                       b.id, b.account_name, b.account_number, b.ifsc,
                       s.label AS supplier_label,
                       t.id AS trade_id, t.reference
                FROM wallets w
                JOIN parties s ON s.id = w.supplier_id
                JOIN bank_accounts b ON b.party_id = s.id AND b.is_active
                LEFT JOIN trades t
                       ON t.supplier_id = s.id
                      AND t.client_id = w.client_id
                      AND t.status IN ('open', 'awaiting_payment')
                WHERE w.is_internal AND w.client_id = $1
                ORDER BY
                    b.id,
                    (t.id IS NULL),
                    (t.instructed_at IS NULL),
                    (COALESCE((SELECT sum(p.amount_inr) FROM payments p
                               WHERE p.trade_id = t.id), 0) >= t.inr_expected),
                    t.opened_at
                """,
                client_id,
            )

    async def open_trade_for_supplier(self, supplier_id: int) -> Optional[asyncpg.Record]:
        """
        Backs the supplier's own progress view (client request, 10 Sep 2026:
        "collection progress, can i add this to the suppliers as an option?").
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT t.*, c.label AS client_label,
                       COALESCE((SELECT sum(p.amount_inr) FROM payments p
                                 WHERE p.trade_id = t.id), 0) AS paid_inr
                FROM trades t
                JOIN parties c ON c.id = t.client_id
                WHERE t.supplier_id = $1 AND t.status IN ('open', 'awaiting_payment')
                -- A supplier can now have two trades open: one being collected
                -- against an issued instruction, and a newer one holding a
                -- deposit that has not been instructed yet. "Progress" means
                -- the one money is actually coming in on, so instructed trades
                -- come first and the oldest of those wins — the same FIFO order
                -- the client pays in. Newest-first would have answered with the
                -- untouched trade and reported no progress at all.
                ORDER BY (t.instructed_at IS NULL), t.instructed_at, t.opened_at
                LIMIT 1
                """,
                supplier_id,
            )

    async def claim_completion(self, trade_id: int) -> Optional[asyncpg.Record]:
        """
        Close a trade the moment its payments cover what was expected.

        Client request, 10 September 2026: "remove /done, have it calculate".
        Waiting for someone to type a command means the supplier and the Bridge
        sit unaware of a trade that is, in fact, finished.

        The claim is a single statement for the same reason claim_near_completion
        is: several pastes can land in the same instant, and each one asks
        whether the trade is now covered. Only one of them can win, so the
        summary is distributed exactly once.

        Returns the trade if this caller closed it, None if it was already
        closed, still short, or has no expected total to compare against.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await conn.fetchrow(
                    """
                    UPDATE trades t
                    SET status = 'completed', completed_at = now()
                    WHERE t.id = $1
                      AND t.status = 'awaiting_payment'
                      AND t.inr_expected IS NOT NULL
                      AND t.inr_expected > 0
                      AND COALESCE((SELECT sum(p.amount_inr) FROM payments p
                                    WHERE p.trade_id = t.id), 0) >= t.inr_expected
                    RETURNING t.*
                    """,
                    trade_id,
                )
                if trade is None:
                    return None

                await self.audit(
                    conn, actor_party_id=None, action="trade.complete",
                    entity_type="trade", entity_id=trade_id,
                    detail={"trigger": "payments covered the expected total"},
                )
                return trade

    async def book_progress(self) -> list[asyncpg.Record]:
        """
        Every pairing and where it stands, in one row each.

        Client request, 19 September 2026:

            can you add /progress to UI control, so it shows a summary of all
            trading progress for all groups?

        /summary already existed but answers a narrower question: pick a
        supplier, see their open trades. That is no use as a morning glance,
        and it is silent about the thing worth knowing — a pairing where
        NOTHING is open.

        WHY IT WALKS THE WALLETS

        One internal wallet is one supplier↔client pairing, so the wallet
        table is the list of relationships whether or not they are busy. A
        query over trades would only ever show groups that happen to have
        one, which is precisely the blind spot being asked about.

        WHY STRANDED USDT IS IN HERE

        A vendor with nothing open looks idle. Twice in three days that has
        meant the opposite: SUPB3 and SUPD1 on the 16th, SUPB5 and SUPD2 on
        the 19th — deposits sitting on cancelled trades, USDT already paid
        onward to the client, nothing invoiced to anybody. ₹995,495, lost
        the same way twice, and on both occasions the screen said nothing.

        A progress view that reports those as "nothing open" would be the
        third time. So the money that arrived and was never billed is on the
        same line as the money that is being collected.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT
                  s.label AS supplier_label,
                  c.label AS client_label,
                  (SELECT count(*) FROM trades t
                    WHERE t.supplier_id = w.supplier_id
                      AND t.client_id = w.client_id
                      AND t.status IN ('open','awaiting_payment')) AS open_trades,
                  (SELECT count(*) FROM trades t
                    WHERE t.supplier_id = w.supplier_id
                      AND t.client_id = w.client_id
                      AND t.status IN ('open','awaiting_payment')
                      AND t.instructed_at IS NULL) AS uninstructed,
                  COALESCE((SELECT sum(t.inr_expected) FROM trades t
                    WHERE t.supplier_id = w.supplier_id
                      AND t.client_id = w.client_id
                      AND t.status IN ('open','awaiting_payment')), 0) AS expected_inr,
                  COALESCE((SELECT sum(p.amount_inr)
                    FROM payments p JOIN trades t ON t.id = p.trade_id
                    WHERE t.supplier_id = w.supplier_id
                      AND t.client_id = w.client_id
                      AND t.status IN ('open','awaiting_payment')), 0) AS collected_inr,
                  COALESCE((SELECT sum(d.amount_usdt)
                    FROM deposits d JOIN trades t ON t.id = d.trade_id
                    WHERE t.supplier_id = w.supplier_id
                      AND t.client_id = w.client_id
                      AND t.status = 'cancelled'
                      AND NOT EXISTS (SELECT 1 FROM payments p
                                      WHERE p.trade_id = t.id)
                      AND NOT EXISTS (SELECT 1 FROM audit_log al
                                      WHERE al.action = 'trade.settled_by_hand'
                                        AND al.entity_id = t.id)), 0) AS stranded_usdt,
                  COALESCE((SELECT sum(t.inr_expected) FROM trades t
                    WHERE t.supplier_id = w.supplier_id
                      AND t.client_id = w.client_id
                      AND t.status = 'cancelled'
                      AND NOT EXISTS (SELECT 1 FROM payments p
                                      WHERE p.trade_id = t.id)
                      AND NOT EXISTS (SELECT 1 FROM audit_log al
                                      WHERE al.action = 'trade.settled_by_hand'
                                        AND al.entity_id = t.id)
                      AND EXISTS (SELECT 1 FROM deposits d
                                  WHERE d.trade_id = t.id)), 0) AS stranded_inr
                FROM wallets w
                JOIN parties s ON s.id = w.supplier_id
                JOIN parties c ON c.id = w.client_id
                WHERE w.is_internal AND s.is_active AND c.is_active
                ORDER BY s.label, c.label
                """
            )

    async def current_rates(self) -> list[asyncpg.Record]:
        """
        The rate in force for every pairing, newest per pairing.

        Backs /viewrate — the Bridge asked to be able to see what is set without
        starting to change it.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT DISTINCT ON (r.supplier_id, r.client_id)
                       -- The LIVE label, deliberately. A rate is what is on
                       -- offer now, so it belongs to the vendor as they are
                       -- called now. Only a trade pins the name.
                       s.label AS supplier_label,
                       c.label AS client_label,
                       r.supply_rate, r.sell_rate, r.created_at,
                       EXTRACT(EPOCH FROM (now() - r.created_at)) / 3600 AS age_hours
                FROM rates r
                JOIN parties s ON s.id = r.supplier_id
                JOIN parties c ON c.id = r.client_id
                ORDER BY r.supplier_id, r.client_id, r.created_at DESC
                """
            )

    async def complete_trade(self, trade_id: int, actor_party_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE trades
                    SET status = 'completed', completed_at = now()
                    WHERE id = $1
                    """,
                    trade_id,
                )
                await self.audit(
                    conn, actor_party_id=actor_party_id, action="trade.complete",
                    entity_type="trade", entity_id=trade_id,
                )

    # --------------------------------------------------------------- payments

    async def add_payment(
        self, *, trade_id: int, utr: str, amount_inr: Decimal,
        beneficiary_account_id: int, added_by: int,
    ) -> tuple[bool, str]:
        """
        Log one INR tranche.

        Returns (ok, message). E3 requires a duplicate UTR to be rejected and the
        client told; the unique index is what actually enforces it, so a race
        between two /add calls cannot slip a duplicate through.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                seq = await conn.fetchval(
                    "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM payments WHERE trade_id = $1",
                    trade_id,
                )
                try:
                    payment_id = await conn.fetchval(
                        """
                        INSERT INTO payments (trade_id, utr, amount_inr,
                                              beneficiary_account_id, added_by, sequence_no)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        RETURNING id
                        """,
                        trade_id, utr, amount_inr, beneficiary_account_id, added_by, seq,
                    )
                except asyncpg.UniqueViolationError:
                    return False, f"UTR {utr} has already been recorded. Not added again."

                await self.audit(
                    conn, actor_party_id=added_by, action="payment.add",
                    entity_type="payment", entity_id=payment_id,
                    detail={"trade_id": trade_id, "utr": utr, "amount_inr": amount_inr},
                )
                return True, f"Recorded {utr}."

    async def existing_utrs(self, utrs: Sequence[str]) -> set:
        """
        Which of these references are already in the ledger.

        Used when a client edits a payment message: the edit has to be told
        apart from a new payment, and the UTR is what distinguishes them.
        """
        if not utrs:
            return set()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT utr FROM payments WHERE utr = ANY($1::text[])", list(utrs)
            )
            return {r["utr"] for r in rows}

    async def recorded_amounts(self, utrs: Sequence[str]) -> dict:
        """
        What the ledger holds for each of these references.

        Knowing a reference exists is not enough to answer an edit. An edit
        that reads back exactly what was already recorded changed nothing and
        deserves no reply; an edit where the amount now differs is somebody
        trying to restate a figure that is already in the books, which the
        Bridge has to hear about. Only the amounts distinguish the two.
        """
        if not utrs:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT utr, amount_inr FROM payments WHERE utr = ANY($1::text[])",
                list(utrs),
            )
            return {r["utr"]: r["amount_inr"] for r in rows}

    async def trade_payments(self, trade_id: int) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT p.utr, p.amount_inr, b.account_name
                FROM payments p
                JOIN bank_accounts b ON b.id = p.beneficiary_account_id
                WHERE p.trade_id = $1
                ORDER BY p.sequence_no
                """,
                trade_id,
            )

    async def trade_paid_total(self, trade_id: int) -> Decimal:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COALESCE(SUM(amount_inr), 0) FROM payments WHERE trade_id = $1",
                trade_id,
            )

    # --------------------------------------------------------------- deposits

    # -------------------------------------------------- trade lifecycle

    async def trade_detail(self, trade_id: int) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT t.*,
                       COALESCE(t.supplier_label_at_trade, s.label)
                           AS supplier_label,
                       c.label AS client_label,
                       COALESCE((SELECT SUM(amount_inr) FROM payments
                                 WHERE trade_id = t.id), 0) AS paid_inr
                FROM trades t
                JOIN parties s ON s.id = t.supplier_id
                JOIN parties c ON c.id = t.client_id
                WHERE t.id = $1
                """,
                trade_id,
            )

    async def list_open_trades(self) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT t.id, t.reference, t.inr_expected,
                       COALESCE(t.supplier_label_at_trade, s.label)
                           AS supplier_label,
                       c.label AS client_label,
                       COALESCE((SELECT sum(p.amount_inr) FROM payments p
                                 WHERE p.trade_id = t.id), 0) AS paid_inr
                FROM trades t
                JOIN parties s ON s.id = t.supplier_id
                JOIN parties c ON c.id = t.client_id
                WHERE t.status IN ('open', 'awaiting_payment')
                ORDER BY t.opened_at
                """
            )

    async def repriceable_trades(self) -> list[asyncpg.Record]:
        """
        Every live trade, priced as it stands beside the rate now in force.

        The three facts that decide whether it may be moved travel with it —
        instructed_at, paid_inr, and whether it is already on the newest
        rate — so the Bridge is told WHY a trade cannot be repriced instead
        of simply not being offered it. Not knowing his options is what made
        him cancel on 16 September.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT t.id, t.reference, t.usdt_received, t.rate_id,
                       t.supply_rate, t.sell_rate,
                       t.inr_expected, t.usdt_owed_client,
                       t.instructed_at,
                       COALESCE(t.supplier_label_at_trade, s.label)
                           AS supplier_label,
                       c.label AS client_label,
                       COALESCE((SELECT sum(p.amount_inr) FROM payments p
                                 WHERE p.trade_id = t.id), 0) AS paid_inr,
                       r.id          AS current_rate_id,
                       r.supply_rate AS current_supply_rate,
                       r.sell_rate   AS current_sell_rate
                FROM trades t
                JOIN parties s ON s.id = t.supplier_id
                JOIN parties c ON c.id = t.client_id
                LEFT JOIN LATERAL (
                    SELECT id, supply_rate, sell_rate
                    FROM rates
                    WHERE supplier_id = t.supplier_id AND client_id = t.client_id
                    ORDER BY created_at DESC
                    LIMIT 1
                ) r ON TRUE
                WHERE t.status IN ('open', 'awaiting_payment')
                ORDER BY t.opened_at
                """
            )

    async def reprice_trade(
        self, *, trade_id: int, actor_party_id: int,
    ) -> tuple[bool, str, Optional[dict]]:
        """
        Move an open, uninstructed, unpaid trade onto the rate now in force.

        Bridge, 14 September 2026:

            the rate has changed but the funds have come
            Can i have the option to change the rate and it reflect

        A trade snapshots its rate on purpose, so that a later /setrate cannot
        silently rewrite arithmetic already agreed. The cost of that is a rate
        moving between the deposit landing and the instruction going out,
        leaving the trade on the old one with no way to correct it. Without
        this command his only lever is /cancel — and cancelling is what took
        Malegao - Sam and GS Group out of the client's matcher on 16 September
        and lost the slips.

        The guards are the same as deploy/reprice-open-trade.sql and are not
        negotiable:

          instructed   REFUSED. The client is holding a message naming a
                       figure. Moving the total underneath it is exactly the
                       11 September fault, where a trade instructed at
                       ₹197,054 quietly became ₹515,054.

          any payment  REFUSED. Money already in was priced at the old rate.
                       Repricing the whole trade would restate settled money.

        Everything is re-checked here under FOR UPDATE rather than trusted
        from the picker. An instruction can go out, or a payment land, in the
        seconds between the Bridge seeing the list and tapping confirm, and
        that gap is precisely where this would do harm.

        The new rate is read from the rates table, never passed in, so the
        figure applied is the one /setrate recorded — with its current-rate
        display, its loss warning and its attribution.
        """
        from core.money import inr_to_usdt, round_usdt, usdt_to_inr

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                t = await conn.fetchrow(
                    """
                    SELECT t.*,
                       COALESCE(t.supplier_label_at_trade, s.label)
                           AS supplier_label,
                       c.label AS client_label
                    FROM trades t
                    JOIN parties s ON s.id = t.supplier_id
                    JOIN parties c ON c.id = t.client_id
                    WHERE t.id = $1
                    FOR UPDATE OF t
                    """,
                    trade_id,
                )
                if t is None:
                    return False, "That trade no longer exists.", None

                if t["status"] not in ("open", "awaiting_payment"):
                    return False, (
                        f"{t['reference']} is {t['status']}, not open. "
                        "Nothing was changed."
                    ), None

                if t["instructed_at"] is not None:
                    return False, (
                        f"{t['reference']} has already been issued to the "
                        "client — they are holding that figure.\n\n"
                        "Repricing it underneath them is the one thing this "
                        "will not do. Cancel and re-issue instead."
                    ), None

                paid = await conn.fetchval(
                    "SELECT COALESCE(sum(amount_inr), 0) FROM payments WHERE trade_id = $1",
                    trade_id,
                )
                if paid:
                    return False, (
                        f"{t['reference']} already has ₹{paid:,.0f} paid "
                        "against it at the old rate.\n\n"
                        "Repricing now would restate money that is already "
                        "settled. Nothing was changed."
                    ), None

                r = await conn.fetchrow(
                    """
                    SELECT id, supply_rate, sell_rate
                    FROM rates
                    WHERE supplier_id = $1 AND client_id = $2
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    t["supplier_id"], t["client_id"],
                )
                if r is None:
                    return False, (
                        "There is no rate set for that pairing at all. "
                        "Set one with /setrate first."
                    ), None

                if r["id"] == t["rate_id"]:
                    return False, (
                        f"{t['reference']} is already on the rate in force "
                        f"({r['supply_rate']} / {r['sell_rate']}).\n\n"
                        "Set the new rate with /setrate first, then reprice."
                    ), None

                usdt = t["usdt_received"]
                new_inr = usdt_to_inr(usdt, r["supply_rate"])
                new_owed = inr_to_usdt(new_inr, r["sell_rate"])
                new_margin = round_usdt(Decimal(usdt) - new_owed)

                await conn.execute(
                    """
                    UPDATE trades
                    SET rate_id          = $2,
                        supply_rate      = $3,
                        sell_rate        = $4,
                        inr_expected     = $5,
                        usdt_owed_client = $6,
                        margin_usdt      = $7
                    WHERE id = $1
                    """,
                    trade_id, r["id"], r["supply_rate"], r["sell_rate"],
                    new_inr, new_owed, new_margin,
                )
                await self.audit(
                    conn, actor_party_id=actor_party_id, action="trade.reprice",
                    entity_type="trade", entity_id=trade_id,
                    detail={
                        "reference":   t["reference"],
                        "usdt":        usdt,
                        "from_rates":  f"{t['supply_rate']} / {t['sell_rate']}",
                        "to_rates":    f"{r['supply_rate']} / {r['sell_rate']}",
                        "from_inr":    t["inr_expected"],
                        "to_inr":      new_inr,
                        "from_owed":   t["usdt_owed_client"],
                        "to_owed":     new_owed,
                    },
                )
                return True, f"{t['reference']} repriced.", {
                    "reference":       t["reference"],
                    "supplier_label":  t["supplier_label"],
                    "client_label":    t["client_label"],
                    "usdt":            usdt,
                    "old_supply":      t["supply_rate"],
                    "old_sell":        t["sell_rate"],
                    "new_supply":      r["supply_rate"],
                    "new_sell":        r["sell_rate"],
                    "old_inr":         t["inr_expected"],
                    "new_inr":         new_inr,
                    "old_owed":        t["usdt_owed_client"],
                    "new_owed":        new_owed,
                }

    async def prior_payouts_for_trade(self, trade_id: int) -> list[asyncpg.Record]:
        """
        USDT that already left for this client, for about this amount, since
        this trade's first deposit landed.

        WHAT THIS IS FOR (live, 18 September 2026)

        Malegao's 2,354 and GS Group's 7,000 arrived on the 16th, the Bridge
        paid the client 2,321.21 and 6,922.00 within minutes, and both trades
        were then cancelled before the client was ever invoiced. Reopening
        them as SUPB5 and SUPD2 put ₹995,495 back on the client — correctly —
        but it also put two copy-ready USDT figures in front of the Bridge for
        money he had already sent. 9,243 USDT, one tap from going twice.

        The bot could see it the whole time. A payout is recorded as a deposit
        on the client's wallet and never carries a trade_id, so the evidence
        was sitting in the table with nothing reading it.

        WHY THE WINDOW STARTS AT THE DEPOSIT

        In normal operation the Bridge pays the client just AFTER issuing —
        SUPB4 was instructed at 13:10:31 and paid at 13:11:23. So at the
        moment of issue there is usually nothing here, and a hit means the
        order of events was unusual, which is exactly when he needs telling.

        WHY IT MATCHES ON AMOUNT

        One client has one payout wallet, shared by every supplier, so the
        window alone catches every other vendor's payouts too. The amount is
        what identifies it. The tolerance is 2% because the sell rate moves
        between a hand payment and a reopened trade — 107.5 became 107.7 here,
        which is the difference between 2,321.21 and 2,321.22.

        WHY IT COUNTS RATHER THAN EXCLUDES

        Run against the live rows before anyone relied on it, this reported
        two transfers for SUPB5 — the 2,321.21 it should have, and SUPB4's
        2,321.22 from the following day. SUPB4 is a separate settled trade
        whose payout is spoken for, but nothing in the row says so: a payout
        never carries a trade_id, which is the whole reason this function has
        to exist.

        The obvious fix was to drop any payout some other instructed trade
        could account for. That is worse. One instructed trade would then
        silence ANY number of matching payouts, so a genuinely unpaid one
        would go unmentioned — and missing a real one is the failure this
        guard exists to prevent, where a spurious one only costs a tap.

        So it compares counts. If the matching payouts outnumber the other
        instructed trades that could account for them, at least one is
        unaccounted for and all of them are shown, because the bot honestly
        cannot say which. A vendor who settles the same figure five times
        over, all issued and all paid, has five of each and hears nothing.

        Live: SUPB5 sees two payouts against one claimant (SUPB4), so it
        speaks — correctly, since SUPB3's 2,321.21 went out on the 16th and
        was never billed to anyone.

        A warning, never a block. The Bridge may have good reason.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                WITH t AS (
                    SELECT tr.id, tr.wallet_id, tr.usdt_owed_client AS owed,
                           (SELECT min(d.detected_at) FROM deposits d
                            WHERE d.trade_id = tr.id) AS since
                    FROM trades tr
                    WHERE tr.id = $1
                ),
                pay AS (
                    SELECT d.amount_usdt, d.tx_hash, d.detected_at
                    FROM t
                    JOIN wallets iw ON iw.id = t.wallet_id
                    JOIN wallets pw ON pw.id = iw.payout_wallet_id
                    JOIN deposits d ON d.wallet_id = pw.id
                    WHERE d.trade_id IS NULL
                      AND t.since IS NOT NULL
                      AND t.owed > 0
                      AND d.detected_at >= t.since
                      AND abs(d.amount_usdt - t.owed)
                            <= greatest(t.owed * 0.02, 0.01)
                ),
                claimants AS (
                    SELECT count(*) AS n
                    FROM t
                    JOIN wallets iw ON iw.id = t.wallet_id
                    JOIN wallets ow
                      ON ow.payout_wallet_id = iw.payout_wallet_id
                    JOIN trades o
                      ON o.wallet_id = ow.id AND o.id <> t.id
                    WHERE o.instructed_at IS NOT NULL
                      AND abs(o.usdt_owed_client - t.owed)
                            <= greatest(t.owed * 0.02, 0.01)
                )
                SELECT p.amount_usdt, p.tx_hash, p.detected_at
                FROM pay p, claimants c
                WHERE (SELECT count(*) FROM pay) > c.n
                ORDER BY p.detected_at
                """,
                trade_id,
            )

    async def recent_completed_trades(self, limit: int = 10) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT t.id, t.reference, t.completed_at,
                       COALESCE(t.supplier_label_at_trade, s.label)
                           AS supplier_label,
                       c.label AS client_label
                FROM trades t
                JOIN parties s ON s.id = t.supplier_id
                JOIN parties c ON c.id = t.client_id
                WHERE t.status = 'completed'
                ORDER BY t.completed_at DESC
                LIMIT $1
                """,
                limit,
            )

    async def nominate_account(
        self, *, supplier_id: int, account_id: int, actor_party_id: int,
    ) -> Optional[str]:
        """
        Attach the supplier's chosen account to their open trade.

        Returns the trade reference if one was open, or None if the deposit has
        not been detected yet. The nomination is not lost in that case - the
        most recent one is applied when the trade opens.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE trades SET nominated_account_id = $2
                    WHERE id = (
                        SELECT id FROM trades
                        WHERE supplier_id = $1
                          AND status IN ('open', 'awaiting_payment')
                          -- Never re-nominate a trade whose instruction has
                          -- already been sent. The client is holding a message
                          -- naming an account; changing it here changes nothing
                          -- in their chat and leaves the record disagreeing
                          -- with what they were actually told.
                          --
                          -- Live on 11 September 2026: SUPB1 was instructed at
                          -- 16:37 and re-nominated at 17:14 by a /send that was
                          -- really about the next deposit.
                          --
                          -- With no uninstructed trade this returns nothing,
                          -- which is the correct answer — the claim stays in
                          -- pending_sends and latest_nomination hands it to
                          -- the next trade that opens.
                          AND instructed_at IS NULL
                        ORDER BY opened_at DESC LIMIT 1
                    )
                    RETURNING reference
                    """,
                    supplier_id, account_id,
                )
                await self.audit(
                    conn, actor_party_id=actor_party_id, action="trade.account_nominated",
                    entity_type="bank_account", entity_id=account_id,
                    detail={
                        "supplier_id": supplier_id,
                        "trade_reference": row["reference"] if row else None,
                    },
                )
                return row["reference"] if row else None

    async def latest_nomination(self, supplier_id: int) -> Optional[int]:
        """
        An account this supplier has nominated and NOT yet used.

        Used when a deposit is detected after the supplier has already run
        /send, so their choice is not lost to the ordering of the two events.

        WHY IT READS pending_sends AND NOT THE AUDIT LOG

        It used to take the most recent 'trade.account_nominated' entry in the
        audit log. That log is a permanent record of everything that has ever
        happened, so the query returned a nomination made hours earlier for a
        trade long since closed — and every new deposit from that supplier
        arrived pre-filled with it.

        Live, 11 September 2026: Supplier A nominated Ekta traders at 15:42
        for SUPA1. At 21:36 a fresh 14,151 USDT deposit opened SUPA3 and the
        Bridge was shown "Supplier nominated: Ekta traders" for a trade nobody
        had said anything about. The supplier then chose a different account
        entirely. The Bridge: "money sent, but no one chose supplier... why
        did it default? its should not do that."

        A default that looks like a decision is worse than no default at all —
        it invites the Bridge to confirm an account the supplier never asked
        for, and the money goes to the wrong place.

        pending_sends holds exactly the right thing: a claim made and not yet
        attached to a trade. on_supplier_deposit matches it immediately after
        opening the trade, so each nomination is offered once and then stops
        being offered. No claim outstanding means no nomination, and the
        Bridge is told so plainly.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT bank_account_id FROM pending_sends
                WHERE supplier_id = $1
                  AND matched_trade_id IS NULL
                  AND bank_account_id IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                supplier_id,
            )

    # ----------------------------------------------------------- pending sends

    async def record_pending_send(
        self, *, supplier_id: int, account_id: int, hash_url: str | None,
    ) -> None:
        """
        A supplier's claim that they have sent. Quiet until funds land.

        If a trade for this supplier is ALREADY open, the funds have landed
        first and the claim is answered the moment it is made.

        Suppliers do it in both orders, and the deposit arriving first is the
        common one — the monitor sees the chain within seconds, while /send is
        typed by a person afterwards. Matching only on the deposit side left
        those claims unmatched for ever, and thirty minutes later the Bridge
        was told a supplier had not sent when the money was already in.
        That happened live on 11 September 2026 with SUPA1.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                open_trade = await conn.fetchval(
                    """
                    SELECT id FROM trades
                    WHERE supplier_id = $1 AND status IN ('open', 'awaiting_payment')
                    ORDER BY opened_at DESC LIMIT 1
                    """,
                    supplier_id,
                )
                await conn.execute(
                    """
                    INSERT INTO pending_sends (supplier_id, bank_account_id,
                                               hash_url, matched_trade_id)
                    VALUES ($1, $2, $3, $4)
                    """,
                    supplier_id, account_id, hash_url, open_trade,
                )
                await self.audit(
                    conn, actor_party_id=supplier_id, action="send.claimed",
                    entity_type="party", entity_id=supplier_id,
                    detail={"account_id": account_id, "hash": hash_url},
                )

    async def match_pending_send(self, *, supplier_id: int, trade_id: int) -> None:
        """
        Tie the oldest outstanding claim from this supplier to a real deposit.

        Oldest first, so two sends in quick succession resolve in the order
        they were made rather than leaving the earlier one to be reported as
        missing while the later one is matched.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE pending_sends SET matched_trade_id = $2
                WHERE id = (
                    SELECT id FROM pending_sends
                    WHERE supplier_id = $1 AND matched_trade_id IS NULL
                    ORDER BY created_at
                    LIMIT 1
                )
                """,
                supplier_id, trade_id,
            )

    async def stale_pending_sends(self, older_than_minutes: int) -> list[asyncpg.Record]:
        """Claims with no deposit behind them, not yet reported."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT ps.id, ps.hash_url, ps.created_at,
                       p.label AS supplier_label,
                       b.account_name
                FROM pending_sends ps
                JOIN parties p ON p.id = ps.supplier_id
                LEFT JOIN bank_accounts b ON b.id = ps.bank_account_id
                WHERE ps.matched_trade_id IS NULL
                  AND ps.alerted_at IS NULL
                  AND ps.created_at < now() - make_interval(mins => $1)
                  -- Belt and braces. Even if a claim somehow escaped matching,
                  -- a supplier with a live trade has plainly sent, and telling
                  -- the Bridge otherwise is worse than staying quiet: a false
                  -- "they have not sent" makes them doubt every true one.
                  AND NOT EXISTS (
                      SELECT 1 FROM trades t
                      WHERE t.supplier_id = ps.supplier_id
                        AND t.status IN ('open', 'awaiting_payment')
                  )
                ORDER BY ps.created_at
                """,
                older_than_minutes,
            )

    async def outstanding_claims(self) -> list[asyncpg.Record]:
        """
        Every "I have sent" with no deposit behind it yet.

        Unlike stale_pending_sends this ignores age and whether it has been
        reported — it is the Bridge asking what is outstanding, not the
        monitor deciding what to raise.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT ps.id, ps.hash_url, ps.created_at,
                       p.label AS supplier_label,
                       b.account_name
                FROM pending_sends ps
                JOIN parties p ON p.id = ps.supplier_id
                LEFT JOIN bank_accounts b ON b.id = ps.bank_account_id
                WHERE ps.matched_trade_id IS NULL
                ORDER BY ps.created_at
                """
            )

    async def drop_pending_send(
        self, *, pending_id: int, actor_party_id: int,
    ) -> tuple[bool, str]:
        """
        Withdraw a claim that is never going to be matched.

        WHY THIS EXISTS (live, 19 September 2026)

        The Bridge ran a test /send from Malegao - Sam with the hash
        "1234test", the monitor correctly reported that nothing had arrived,
        and then:

            i did a test, but i am unable to cancel it myself?

        He was right. /cancel only ever listed trades, and a claim is not a
        trade. There was no way to withdraw one at all.

        WHY IT MATTERS MORE THAN THE NAGGING

        An unmatched claim is what latest_nomination reads. So the test
        nomination — "Afrin fathima" — would have been pre-selected on
        Malegao's NEXT REAL deposit, with the Bridge shown "Supplier
        nominated: Afrin fathima" for a trade nobody had said that about.

        That is the 11 September fault exactly, arriving through a test
        instead of through the audit log: a default that reads as a decision
        is one tap away from sending a client to pay the wrong account.

        The row is deleted rather than flagged, because latest_nomination and
        stale_pending_sends both key on matched_trade_id IS NULL and a
        withdrawn claim must disappear from both. What it was survives in the
        audit log, which is the permanent record.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT ps.*, p.label AS supplier_label, b.account_name
                    FROM pending_sends ps
                    JOIN parties p ON p.id = ps.supplier_id
                    LEFT JOIN bank_accounts b ON b.id = ps.bank_account_id
                    WHERE ps.id = $1
                    FOR UPDATE OF ps
                    """,
                    pending_id,
                )
                if row is None:
                    return False, "That claim no longer exists."
                if row["matched_trade_id"] is not None:
                    return False, (
                        "That claim has already been matched to a deposit. "
                        "Nothing to withdraw."
                    )

                await conn.execute(
                    "DELETE FROM pending_sends WHERE id = $1", pending_id
                )
                await self.audit(
                    conn, actor_party_id=actor_party_id,
                    action="send.withdrawn", entity_type="party",
                    entity_id=row["supplier_id"],
                    detail={
                        "supplier": row["supplier_label"],
                        "account": row["account_name"],
                        "hash": row["hash_url"],
                        "claimed_at": row["created_at"].isoformat(),
                    },
                )
                return True, (
                    f"Withdrawn {row['supplier_label']}'s claim"
                    + (f" ({row['hash_url']})" if row["hash_url"] else "")
                    + ".\n\nIt will no longer be offered as a nomination on "
                      "their next deposit."
                )

    async def mark_pending_alerted(self, pending_id: int) -> bool:
        """Claim the right to report this one, exactly once."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE pending_sends SET alerted_at = now()
                WHERE id = $1 AND alerted_at IS NULL
                RETURNING id
                """,
                pending_id,
            )
            return row is not None

    async def issue_slots(
        self, *, trade_id: int, slots: Sequence[tuple[int, Decimal]], actor_party_id: int,
    ) -> None:
        """
        Persist the payment instructions issued to the client.

        `slots` is a sequence of (bank_account_id, amount_inr). Re-issuing
        replaces the previous set rather than appending, so a corrected
        instruction does not leave the superseded one visible alongside it.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM payment_slots WHERE trade_id = $1", trade_id
                )
                for account_id, amount in slots:
                    await conn.execute(
                        """
                        INSERT INTO payment_slots (trade_id, bank_account_id, amount_inr)
                        VALUES ($1, $2, $3)
                        """,
                        trade_id, account_id, amount,
                    )
                # instructed_at is set once and never moved. Re-issuing a
                # corrected instruction for the same trade must not reopen it
                # to deposits — COALESCE keeps the original moment.
                await conn.execute(
                    """
                    UPDATE trades
                    SET status = 'awaiting_payment',
                        instructed_at = COALESCE(instructed_at, now())
                    WHERE id = $1
                    """,
                    trade_id,
                )
                await self.audit(
                    conn, actor_party_id=actor_party_id, action="trade.slots_issued",
                    entity_type="trade", entity_id=trade_id,
                    detail={"slots": [{"account_id": a, "amount_inr": m} for a, m in slots]},
                )

    async def trade_slots(self, trade_id: int) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT ps.amount_inr, b.account_name, b.account_number, b.ifsc
                FROM payment_slots ps
                JOIN bank_accounts b ON b.id = ps.bank_account_id
                WHERE ps.trade_id = $1
                ORDER BY ps.id
                """,
                trade_id,
            )

    async def cancel_trade(
        self, *, trade_id: int, actor_party_id: int, reason: str,
    ) -> tuple[bool, str]:
        """
        E4: only the Bridge may cancel, and only before completion.

        Payments already logged are kept. Deleting them would destroy the record
        of money the client has genuinely sent, which is exactly what someone
        would need to see when working out why a trade was abandoned.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await conn.fetchrow(
                    "SELECT status, reference FROM trades WHERE id = $1 FOR UPDATE",
                    trade_id,
                )
                if trade is None:
                    return False, "That trade no longer exists."
                if trade["status"] == "completed":
                    return False, (
                        f"{trade['reference']} is already completed. "
                        "Use /correct to reopen it instead."
                    )
                if trade["status"] == "cancelled":
                    return False, f"{trade['reference']} is already cancelled."

                await conn.execute(
                    "UPDATE trades SET status = 'cancelled' WHERE id = $1", trade_id
                )
                await self.audit(
                    conn, actor_party_id=actor_party_id, action="trade.cancel",
                    entity_type="trade", entity_id=trade_id,
                    detail={"reason": reason, "previous_status": trade["status"]},
                )
                return True, f"{trade['reference']} cancelled."

    async def stranded_deposits(self, trade_id: int) -> list[asyncpg.Record]:
        """Deposits still sitting on a cancelled trade, with nothing to pay them."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT d.id, d.amount_usdt, d.tx_hash, d.detected_at
                FROM deposits d
                JOIN trades t ON t.id = d.trade_id
                WHERE d.trade_id = $1 AND t.status = 'cancelled'
                ORDER BY d.detected_at
                """,
                trade_id,
            )

    async def pairing_for_deposit(self, deposit_id: int) -> Optional[asyncpg.Record]:
        """
        Which pairing a deposit landed on.

        Backs the "reopen under a different vendor" choice: the client is
        fixed by the address the money arrived at, the supplier is the one
        being questioned.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT w.supplier_id, w.client_id, w.address,
                       s.label AS supplier_label
                FROM deposits d
                JOIN wallets w ON w.id = d.wallet_id
                JOIN parties s ON s.id = w.supplier_id
                WHERE d.id = $1 AND w.is_internal
                """,
                deposit_id,
            )

    async def reopen_deposit_as_trade(
        self, *, deposit_id: int, actor_party_id: int,
        supplier_id: Optional[int] = None,
    ) -> tuple[bool, str, Optional[dict]]:
        """
        Give a deposit its own trade at the rate in force, and correct the
        one it came from.

        Client request, 18 September 2026:

            Cancellation control: There needs to be a way to cancel or correct
            a transaction cleanly if something changes mid-process, while
            keeping the related records linked correctly.

        Cancelling was always clean. What happened afterwards was not. On
        16 September SUPB3 and SUPD1 were cancelled over a rate change, and
        their suppliers' 2,354 and 7,000 USDT stayed attached to dead trades:
        9,354 USDT that had arrived, been paid onward to the client, and was
        billed to nobody. ₹992,924 went missing from the ledger and nothing
        said a word. The repair took two hand-written SQL scripts two days
        later, which is the definition of not keeping the records linked.

        TWO HALVES, ONE TRANSACTION

        Moving the deposit is only half of it. The trade it came from keeps
        claiming USDT it no longer holds — SUPA5 read 66,038 for days after
        its 37,736 went to SUPA6, so ₹4,000,016 was counted on both and every
        export overstated the period by that much. So the source trade is
        restated to whatever deposits it still has, at its own rates, in the
        same transaction. Half a repair is how the last one got missed.

        WHEN THE MONEY LANDED ON THE WRONG SUPPLIER'S ADDRESS

        Normally the deposit's own wallet names both sides: an internal
        wallet exists per supplier-client pairing, so where the USDT arrived
        is who sent it. `supplier_id` overrides that, and exists because on
        TRON the arriving transaction carries a destination and nothing
        else.

        22 September 2026: 4,708 USDT arrived for Tata Mahalaxmi on
        Malegao - Sam's address. The two suppliers send from a shared wallet,
        so the bot had no way to tell them apart and opened SUPB7 against
        Malegao — wrong supplier, wrong accounts, wrong prefix. The Bridge:
        "cancel it and reopen under Tata."

        WHAT THE OVERRIDE DOES AND DOES NOT MOVE

        The new trade is opened on the NAMED supplier's pairing: their
        wallet, their rate, their reference prefix, their bank accounts. The
        deposit keeps its own wallet_id, because that is a statement about
        where the money physically arrived and it is true. So
        deposit.wallet_id and trade.wallet_id differ on a re-attributed
        trade, deliberately.

        Both readers of trade.wallet_id stay correct under that split. The
        merge window (notifier) asks which open trade a NEW deposit on this
        address should join, and a re-attributed trade is no longer on this
        address — right, because the next deposit there is a fresh trade for
        whoever that address belongs to. The double-send guard resolves the
        payout address through the trade's pairing, which is the client
        address for the supplier actually being billed — also right.

        Rewriting deposit.wallet_id instead would make the record claim the
        USDT arrived somewhere it did not, and the on-chain history would
        disagree with the ledger for ever. This system has already paid for
        one set of figures that looked tidy and were false.

        WHAT IT REFUSES

          a deposit on a live trade     it already has one, and moving it
                                        would strip a trade the client may
                                        be holding an instruction for
          a counterparty wallet         only internal wallets open trades
          a pairing with no rate        nothing to price it at
          a supplier not paired with    there is no pairing to open the
          this client                   trade on, so no wallet and no rate

        The new trade is stamped announced_at because the Bridge is doing
        this himself and already knows. Leaving it null would have the
        monitor post a notice saying the supplier had not entered details,
        which is not what happened.
        """
        from core.money import inr_to_usdt, round_usdt, usdt_to_inr

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                d = await conn.fetchrow(
                    "SELECT * FROM deposits WHERE id = $1 FOR UPDATE", deposit_id
                )
                if d is None:
                    return False, "That deposit no longer exists.", None

                source_id = d["trade_id"]
                if source_id is not None:
                    status = await conn.fetchval(
                        "SELECT status FROM trades WHERE id = $1", source_id
                    )
                    if status != "cancelled":
                        return False, (
                            f"That deposit is on a trade that is {status}. "
                            "It already has one."
                        ), None

                w = await conn.fetchrow(
                    "SELECT * FROM wallets WHERE id = $1", d["wallet_id"]
                )
                if w is None or not w["is_internal"]:
                    return False, (
                        "That deposit landed on a counterparty wallet, not an "
                        "internal one. Only internal wallets open trades."
                    ), None

                # Where it landed stays on the deposit. Who gets billed can
                # be overridden, because a shared sending wallet makes the
                # destination a poor witness to the sender's identity.
                landed_on = w
                reattributed = (
                    supplier_id is not None
                    and supplier_id != landed_on["supplier_id"]
                )
                if reattributed:
                    w = await conn.fetchrow(
                        """
                        SELECT * FROM wallets
                        WHERE is_internal AND supplier_id = $1 AND client_id = $2
                        """,
                        supplier_id, landed_on["client_id"],
                    )
                    if w is None:
                        return False, (
                            "That supplier has no wallet with this client, so "
                            "there is no pairing to open the trade on. Add one "
                            "with /addvendor first."
                        ), None

                r = await conn.fetchrow(
                    """
                    SELECT id, supply_rate, sell_rate FROM rates
                    WHERE supplier_id = $1 AND client_id = $2
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    w["supplier_id"], w["client_id"],
                )
                if r is None:
                    return False, (
                        "There is no rate for that pairing. Set one with "
                        "/setrate first."
                    ), None

                ref = await self.next_reference(conn, w["supplier_id"])

                usdt = d["amount_usdt"]
                new_inr = usdt_to_inr(usdt, r["supply_rate"])
                new_owed = inr_to_usdt(new_inr, r["sell_rate"])
                new_margin = round_usdt(Decimal(usdt) - new_owed)

                new_id = await conn.fetchval(
                    """
                    INSERT INTO trades (
                        reference, supplier_id, client_id, wallet_id, rate_id,
                        supply_rate, sell_rate, usdt_received, inr_expected,
                        usdt_owed_client, margin_usdt, status, opened_at,
                        announced_at, supplier_label_at_trade)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                            'awaiting_payment',$12, now(),
                            -- The vendor being billed, which on a
                            -- re-attribution is not the one the USDT
                            -- arrived from.
                            (SELECT label FROM parties WHERE id = $2))
                    RETURNING id
                    """,
                    ref, w["supplier_id"], w["client_id"], w["id"], r["id"],
                    r["supply_rate"], r["sell_rate"], usdt, new_inr,
                    new_owed, new_margin, d["detected_at"],
                )
                await conn.execute(
                    "UPDATE deposits SET trade_id = $2 WHERE id = $1",
                    deposit_id, new_id,
                )

                # The other half. Whatever the source trade still holds is
                # what it should say it holds.
                restated = None
                if source_id is not None:
                    src = await conn.fetchrow(
                        "SELECT * FROM trades WHERE id = $1 FOR UPDATE", source_id
                    )
                    remaining = await conn.fetchval(
                        "SELECT COALESCE(sum(amount_usdt), 0) FROM deposits "
                        "WHERE trade_id = $1",
                        source_id,
                    ) or Decimal(0)
                    if remaining != src["usdt_received"]:
                        if remaining > 0:
                            src_inr = usdt_to_inr(remaining, src["supply_rate"])
                            src_owed = inr_to_usdt(src_inr, src["sell_rate"])
                            src_margin = round_usdt(Decimal(remaining) - src_owed)
                        else:
                            src_inr = src_owed = src_margin = Decimal(0)
                        await conn.execute(
                            """
                            UPDATE trades
                            SET usdt_received = $2, inr_expected = $3,
                                usdt_owed_client = $4, margin_usdt = $5
                            WHERE id = $1
                            """,
                            source_id, remaining, src_inr, src_owed, src_margin,
                        )
                        restated = {
                            "reference": src["reference"],
                            "from_usdt": src["usdt_received"],
                            "to_usdt":   remaining,
                            "from_inr":  src["inr_expected"],
                            "to_inr":    src_inr,
                        }
                        await self.audit(
                            conn, actor_party_id=actor_party_id,
                            action="trade.restated", entity_type="trade",
                            entity_id=source_id, detail=restated,
                        )

                await self.audit(
                    conn, actor_party_id=actor_party_id,
                    action="trade.reopened_from_deposit",
                    entity_type="trade", entity_id=new_id,
                    detail={
                        "reference": ref, "usdt": usdt,
                        "rates": f"{r['supply_rate']} / {r['sell_rate']}",
                        "inr_expected": new_inr, "usdt_owed": new_owed,
                        "from_cancelled_trade": source_id,
                        "tx_hash": d["tx_hash"],
                        # Always recorded, not only when overridden, so the
                        # log answers "where did this actually arrive?"
                        # without a join to a table that may have moved on.
                        "landed_on_wallet": landed_on["id"],
                        "landed_on_address": landed_on["address"],
                        "reattributed": reattributed,
                    },
                )
                if reattributed:
                    await self.audit(
                        conn, actor_party_id=actor_party_id,
                        action="deposit.reattributed", entity_type="deposit",
                        entity_id=deposit_id,
                        detail={
                            "tx_hash": d["tx_hash"],
                            "usdt": usdt,
                            "from_supplier_id": landed_on["supplier_id"],
                            "to_supplier_id": w["supplier_id"],
                            "landed_on_address": landed_on["address"],
                            "billed_as": ref,
                        },
                    )
                return True, f"{ref} opened.", {
                    "reference": ref, "trade_id": new_id, "usdt": usdt,
                    "supply_rate": r["supply_rate"], "sell_rate": r["sell_rate"],
                    "inr_expected": new_inr, "usdt_owed": new_owed,
                    "restated": restated,
                    "reattributed": reattributed,
                }

    async def mark_settled_by_hand(
        self, *, deposit_id: int, actor_party_id: int,
    ) -> tuple[bool, str]:
        """
        Record that a stranded deposit was settled outside the bot.

        WHY THIS HAD TO EXIST (19 September 2026)

        /progress gained a line for USDT that arrived on a trade which was
        then cancelled without the client ever being invoiced. It found
        84,799 USDT against IndoLondon — and most of that is not lost, it is
        trades the Bridge settled by hand, which he does often. SUPA5 was
        settled that way before any of this week's work.

        A warning that fires on normal business is worse than no warning. He
        would have read the first one, checked it, found nothing wrong, and
        stopped reading it — and the next real one would have gone by with
        the rest.

        The "Leave it — settling by hand" button already existed on the
        cancel flow. It just said "left as it is" and recorded nothing, so
        the bot had no way to tell the two cases apart. Now it says so, and
        /progress stops counting that trade.

        Nothing is deleted and no figure moves. This only records a judgement
        the Bridge has made, in the one place that survives.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT d.id, d.amount_usdt, d.trade_id, t.reference, t.status
                    FROM deposits d
                    LEFT JOIN trades t ON t.id = d.trade_id
                    WHERE d.id = $1
                    """,
                    deposit_id,
                )
                if row is None:
                    return False, "That deposit no longer exists."
                if row["trade_id"] is None:
                    return False, "That deposit has no trade to mark."
                if row["status"] != "cancelled":
                    return False, (
                        f"{row['reference']} is {row['status']}, not "
                        "cancelled. Only an abandoned trade can be marked "
                        "settled by hand."
                    )

                await self.audit(
                    conn, actor_party_id=actor_party_id,
                    action="trade.settled_by_hand", entity_type="trade",
                    entity_id=row["trade_id"],
                    detail={
                        "reference": row["reference"],
                        "usdt": row["amount_usdt"],
                        "deposit_id": deposit_id,
                    },
                )
                return True, (
                    f"{row['reference']} marked as settled by hand. It will "
                    "stop showing as uninvoiced in /progress."
                )

    async def reopen_trade(
        self, *, trade_id: int, actor_party_id: int, reason: str,
    ) -> tuple[bool, str]:
        """
        E5: a completed trade may be corrected, with the correction recorded.

        Guarded against the one-open-trade-per-wallet rule. Reopening a trade on
        a wallet that already has a live trade would violate that index, and the
        error would surface as an unexplained failure rather than a clear reason.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                trade = await conn.fetchrow(
                    "SELECT status, reference, wallet_id FROM trades WHERE id = $1 FOR UPDATE",
                    trade_id,
                )
                if trade is None:
                    return False, "That trade no longer exists."
                if trade["status"] != "completed":
                    return False, f"{trade['reference']} is not completed, so there is nothing to reopen."

                clash = await conn.fetchval(
                    """
                    SELECT reference FROM trades
                    WHERE wallet_id = $1 AND id <> $2
                      AND status IN ('open', 'awaiting_payment')
                    """,
                    trade["wallet_id"], trade_id,
                )
                if clash:
                    return False, (
                        f"Cannot reopen {trade['reference']}: {clash} is already open "
                        "on the same wallet. Close or cancel that one first."
                    )

                await conn.execute(
                    """
                    UPDATE trades SET status = 'awaiting_payment', completed_at = NULL
                    WHERE id = $1
                    """,
                    trade_id,
                )
                await self.audit(
                    conn, actor_party_id=actor_party_id, action="trade.reopen",
                    entity_type="trade", entity_id=trade_id,
                    detail={"reason": reason},
                )
                return True, f"{trade['reference']} reopened for correction."

    async def void_payment(
        self, *, payment_id: int, actor_party_id: int, reason: str,
    ) -> tuple[bool, str]:
        """
        Remove a mis-entered payment.

        The row is deleted so the UTR can be re-entered correctly, but the full
        detail is written to the audit log first. The record survives even
        though the row does not.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT p.trade_id, p.utr, p.amount_inr, t.reference
                    FROM payments p JOIN trades t ON t.id = p.trade_id
                    WHERE p.id = $1
                    """,
                    payment_id,
                )
                if row is None:
                    return False, "That payment no longer exists."

                await self.audit(
                    conn, actor_party_id=actor_party_id, action="payment.void",
                    entity_type="payment", entity_id=payment_id,
                    detail={
                        "trade_reference": row["reference"],
                        "utr": row["utr"],
                        "amount_inr": row["amount_inr"],
                        "reason": reason,
                    },
                )
                await conn.execute("DELETE FROM payments WHERE id = $1", payment_id)
                return True, f"Removed {row['utr']} (₹{row['amount_inr']:,.0f}) from {row['reference']}."

    async def claim_near_completion(
        self, trade_id: int, threshold_inr: Decimal
    ) -> Optional[asyncpg.Record]:
        """
        Claim the right to send the near-completion notice for this trade.

        Returns the trade if this call is the one that should notify, or None if
        the threshold is not reached or another call already claimed it.

        The check and the flag are set in a single statement. Two payments
        landing at the same moment would otherwise both see an unnotified trade
        and the supplier would be told twice — the sort of thing that never
        shows up in testing and always shows up in production.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE trades t
                    SET nearing_completion_notified = TRUE
                    WHERE t.id = $1
                      AND NOT t.nearing_completion_notified
                      AND t.status IN ('open', 'awaiting_payment')
                      AND t.inr_expected > 0
                      AND t.inr_expected - COALESCE(
                            (SELECT SUM(amount_inr) FROM payments WHERE trade_id = t.id), 0
                          ) <= $2
                    RETURNING t.id, t.reference, t.supplier_id, t.inr_expected
                    """,
                    trade_id, threshold_inr,
                )
                if row is not None:
                    await self.audit(
                        conn, actor_party_id=None, action="trade.near_completion",
                        entity_type="trade", entity_id=trade_id,
                        detail={"threshold_inr": threshold_inr},
                    )
                return row

    async def trade_payment_rows(self, trade_id: int) -> list[asyncpg.Record]:
        """Payments with ids, for the correction picker."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT p.id, p.utr, p.amount_inr, b.account_name
                FROM payments p
                JOIN bank_accounts b ON b.id = p.beneficiary_account_id
                WHERE p.trade_id = $1
                ORDER BY p.sequence_no
                """,
                trade_id,
            )

    # ------------------------------------------------------------- export

    async def export_rows(self, days: int = 90) -> list[asyncpg.Record]:
        """
        G3: flat export of trades and their payments, one row per payment.

        Trades with no payments still appear, via the LEFT JOIN, so an export is
        a complete picture rather than only the trades that got paid.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT t.reference, t.status, t.opened_at, t.completed_at,
                       s.label AS supplier, c.label AS client,
                       t.supply_rate, t.sell_rate,
                       t.usdt_received, t.inr_expected, t.usdt_owed_client, t.margin_usdt,
                       p.utr, p.amount_inr, b.account_name AS beneficiary
                FROM trades t
                JOIN parties s ON s.id = t.supplier_id
                JOIN parties c ON c.id = t.client_id
                LEFT JOIN payments p ON p.trade_id = t.id
                LEFT JOIN bank_accounts b ON b.id = p.beneficiary_account_id
                WHERE t.opened_at > now() - ($1::text || ' days')::interval
                ORDER BY t.opened_at, p.sequence_no
                """,
                str(days),
            )

    # --------------------------------------------------------------- deposits

    async def confirm_deposit(
        self, *, tx_hash: str, wallet_id: int, block_number: int | None = None,
    ) -> bool:
        """
        Promote a detected deposit to confirmed.

        B4: the client chose "notify immediately on detection, then confirm
        separately". This is the second half. Until a deposit is confirmed the
        Bridge is acting on a transaction that, in principle, could still
        disappear.

        Returns True only on the transition, so the caller can act once.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE deposits
                SET status = 'confirmed',
                    confirmed_at = now(),
                    block_number = COALESCE($3, block_number)
                WHERE tx_hash = $1 AND wallet_id = $2 AND status = 'detected'
                RETURNING id
                """,
                tx_hash, wallet_id, block_number,
            )
            return row is not None

    async def unconfirmed_deposits(self, older_than_minutes: int) -> list[asyncpg.Record]:
        """
        Deposits still unconfirmed after a while.

        A transaction that has been sitting unconfirmed for many minutes is
        unusual on TRON, and the Bridge may already have acted on the
        notification. Better to say so than to leave it silent.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT d.id, d.tx_hash, d.amount_usdt, d.detected_at,
                       t.reference, w.address
                FROM deposits d
                JOIN wallets w ON w.id = d.wallet_id
                LEFT JOIN trades t ON t.id = d.trade_id
                WHERE d.status = 'detected'
                  AND d.detected_at < now() - ($1::text || ' minutes')::interval
                ORDER BY d.detected_at
                """,
                str(older_than_minutes),
            )

    async def flag_unconfirmed(self, deposit_id: int) -> bool:
        """
        Mark an unconfirmed deposit as reported, so the Bridge is warned once
        rather than on every polling cycle.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE deposits SET status = 'orphaned'
                WHERE id = $1 AND status = 'detected'
                RETURNING id
                """,
                deposit_id,
            )
            return row is not None

    async def record_deposit(
        self, *, tx_hash: str, wallet_id: int, amount_usdt: Decimal,
        from_address: str | None, block_number: int | None,
        confirmed: bool = False,
    ) -> Optional[int]:
        """
        Insert a detected deposit.

        Returns the new id, or None if this transaction was already recorded.
        The monitor is at-least-once by design, so this path is hit routinely
        after any restart - it is a normal outcome, not an error.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO deposits (tx_hash, wallet_id, amount_usdt,
                                      from_address, block_number, status,
                                      confirmed_at)
                VALUES ($1, $2, $3, $4, $5,
                        CASE WHEN $6 THEN 'confirmed'::deposit_status
                             ELSE 'detected'::deposit_status END,
                        CASE WHEN $6 THEN now() ELSE NULL END)
                ON CONFLICT (tx_hash, wallet_id) DO NOTHING
                RETURNING id
                """,
                tx_hash, wallet_id, amount_usdt, from_address, block_number,
                confirmed,
            )
