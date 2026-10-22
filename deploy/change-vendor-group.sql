-- ===========================================================================
-- Point an existing vendor at a different Telegram group.
--
-- WHY THIS EXISTS
--
--   telegram_chat_id is the security boundary. Whoever is in that group may
--   act as this vendor, and no other chat can. So when a vendor's group is
--   replaced — the old one compromised, abandoned, or simply remade — the
--   row has to follow, or the bot goes on reading a group nobody is in and
--   ignores the one everybody is in. That is exactly what it looks like from
--   the vendor's side: "the bot is dead".
--
--   There is no /command for this on purpose. Moving a vendor to a new group
--   hands a new set of people the right to post payment slips against that
--   vendor's trades, which is not a thing to do from a phone by tapping a
--   button next to /progress.
--
-- USAGE — nothing is hardcoded, both values come from the command line:
--
--   docker compose exec -T db psql -U settlement -d settlement \
--     -v ON_ERROR_STOP=1 \
--     -v party=5 \
--     -v new_chat=-1000000000000 \
--     -f - < deploy/change-vendor-group.sql
--
--   Both are numeric and are written bare, as :party and :new_chat. Do not
--   quote them. A negative number is fine; psql passes it through.
--
--   Put NOTHING after the value on the \-v line. psql takes the rest of the
--   line as part of the value, a trailing "-- comment" included, and you get
--   a chat id with a comment glued to it.
--
-- WHERE THE NEW ID COMES FROM
--
--   Add @pt_supplier_desk_bot to the new group. It posts into the Bridge
--   control channel:
--
--       This bot was added to an unregistered group.
--       <name>
--       chat id  -100...
--
--   Copy it from there. Do not retype it.
--
-- WHAT IT DOES NOT TOUCH
--
--   Trades, payments, wallets, rates, the deal counter — all keyed on the
--   party id, not the chat. Every open trade stays open and keeps collecting
--   in the new group. This is a change of address, not a new vendor.
--
-- SAFETY
--
--   Four guards, all before the write:
--     1. the party must exist and be a supplier
--     2. the new chat must not already belong to another party
--        (parties_chat_unique would refuse it anyway; this says why)
--     3. the new chat must not be the one already on the row — a no-op that
--        looks like a success is worse than an error
--     4. the whole thing is one transaction, and the UPDATE must report
--        exactly "UPDATE 1"
--
--   The old id is written to the audit log, so the move is reversible by
--   reading it back out.
-- ===========================================================================

\echo
\echo '--- before ---'

SELECT id, role, label, telegram_chat_id, is_active
  FROM parties
 WHERE id = :party;

BEGIN;

-- Guard 1 + 2 + 3. Each one divides by its own answer: true gives 1/1, false
-- gives 1/0, which aborts the transaction under ON_ERROR_STOP.
--
-- It has to be written this way round. The obvious form
--
--     CASE WHEN <check> THEN 1 ELSE 1/0 END
--
-- does NOT work. 1/0 is a constant expression and the planner folds constants
-- before the CASE ever runs, so it raises division by zero even when the
-- check passes and the branch is never reached. Verified on 21 October 2026:
-- all three checks were true and it still errored. Dividing by a value that
-- comes out of a subquery cannot be folded, so the division only happens if
-- the check actually failed.
--
-- A DO block would read better and is not available: psql does not substitute
-- :party or :new_chat inside dollar quoting, so the block would see the
-- literal text. That lesson cost an evening.
SELECT
    1 / ((SELECT count(*) = 1 FROM parties
           WHERE id = :party AND role = 'supplier')::int)   AS vendor_exists,
    1 / ((SELECT count(*) = 0 FROM parties
           WHERE telegram_chat_id = :new_chat
             AND id <> :party)::int)                        AS chat_is_free,
    1 / ((SELECT telegram_chat_id IS DISTINCT FROM :new_chat
            FROM parties WHERE id = :party)::int)           AS actually_a_change;

INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
SELECT NULL,
       'party.group_changed',
       'party',
       p.id,
       jsonb_build_object(
           'label',        p.label,
           'from_chat_id', p.telegram_chat_id,
           'to_chat_id',   :new_chat::bigint,
           'why',          'group replaced by the Bridge'
       )
  FROM parties p
 WHERE p.id = :party;

UPDATE parties
   SET telegram_chat_id = :new_chat
 WHERE id = :party
   AND role = 'supplier';

COMMIT;

\echo
\echo '--- after ---'

SELECT id, role, label, telegram_chat_id, is_active
  FROM parties
 WHERE id = :party;

\echo
\echo 'Now say something in the new group and check it answers /progress.'
\echo 'The bot reads parties once per message, so no restart is needed.'
\echo
