"""
Data access layer.

asyncpg returns NUMERIC columns as Decimal, so money stays exact all the way
from the database to the calculation engine. Nothing here ever casts to float.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional, Sequence  # noqa: F401

import asyncpg


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
                       s.label AS supplier_label,
                       c.label AS client_label,
                       o.label AS owner_label
                FROM wallets w
                LEFT JOIN parties s ON s.id = w.supplier_id
                LEFT JOIN parties c ON c.id = w.client_id
                LEFT JOIN parties o ON o.id = w.owner_party_id
                ORDER BY w.is_internal DESC, w.id
                """
            )

    async def monitored_wallets(self) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT w.id, w.address, w.is_internal, w.supplier_id, w.client_id,
                       m.last_timestamp_ms
                FROM wallets w
                LEFT JOIN monitor_state m ON m.wallet_id = w.id
                WHERE w.is_monitored
                """
            )

    async def wallet_by_address(self, address: str) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM wallets WHERE address = $1", address
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
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO monitor_state (wallet_id, last_timestamp_ms, last_polled_at,
                                           consecutive_errors)
                VALUES ($1, $2, now(), 0)
                ON CONFLICT (wallet_id) DO UPDATE
                SET last_timestamp_ms = GREATEST(
                        monitor_state.last_timestamp_ms, EXCLUDED.last_timestamp_ms),
                    last_polled_at = now(),
                    consecutive_errors = 0
                """,
                wallet_id, last_timestamp_ms,
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

    async def open_trade_for_wallet(self, wallet_id: int) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT * FROM trades
                WHERE wallet_id = $1 AND status IN ('open', 'awaiting_payment')
                """,
                wallet_id,
            )

    async def open_trade_for_client(self, client_id: int) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT t.*, s.label AS supplier_label, c.label AS client_label
                FROM trades t
                JOIN parties s ON s.id = t.supplier_id
                JOIN parties c ON c.id = t.client_id
                WHERE t.client_id = $1 AND t.status IN ('open', 'awaiting_payment')
                ORDER BY t.opened_at DESC
                LIMIT 1
                """,
                client_id,
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
                SELECT t.*, s.label AS supplier_label, c.label AS client_label,
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
                       s.label AS supplier_label, c.label AS client_label
                FROM trades t
                JOIN parties s ON s.id = t.supplier_id
                JOIN parties c ON c.id = t.client_id
                WHERE t.status IN ('open', 'awaiting_payment')
                ORDER BY t.opened_at
                """
            )

    async def recent_completed_trades(self, limit: int = 10) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT t.id, t.reference, t.completed_at,
                       s.label AS supplier_label, c.label AS client_label
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
                        WHERE supplier_id = $1 AND status IN ('open', 'awaiting_payment')
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
        The account this supplier most recently nominated.

        Used when a deposit is detected after the supplier has already run
        /send, so their choice is not lost to the ordering of the two events.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT entity_id FROM audit_log
                WHERE action = 'trade.account_nominated'
                  AND detail->>'supplier_id' = $1::text
                ORDER BY created_at DESC
                LIMIT 1
                """,
                str(supplier_id),
            )

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
                await conn.execute(
                    "UPDATE trades SET status = 'awaiting_payment' WHERE id = $1",
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

    async def record_deposit(
        self, *, tx_hash: str, wallet_id: int, amount_usdt: Decimal,
        from_address: str | None, block_number: int | None,
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
                                      from_address, block_number, status)
                VALUES ($1, $2, $3, $4, $5, 'detected')
                ON CONFLICT (tx_hash, wallet_id) DO NOTHING
                RETURNING id
                """,
                tx_hash, wallet_id, amount_usdt, from_address, block_number,
            )
