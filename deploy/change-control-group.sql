-- ===========================================================================
-- Point the Bridge's control group at a different Telegram chat.
--
-- WHY THIS IS A SEPARATE SCRIPT FROM change-vendor-group.sql
--
--   That one guards `role = 'supplier'` deliberately, and this is the one
--   row it must never touch by accident. The control group is not one
--   counterparty among several: whoever is in it can set rates, instruct
--   payouts, cancel trades and register vendors. Moving it hands the desk
--   to a different room.
--
--   So it gets its own file, its own guards, and no shared code path with
--   the vendor move. A single script with the role check relaxed would be
--   one typo away from making a vendor the Bridge.
--
-- WHEN IT IS NEEDED — 26 November 2026
--
--       security switched out on accounts, but now having issues create new
--       groups on UI control, im thinking change of owner has messed
--       something up.
--       Currently I don't seem to have permission to add these on UI Control
--       ALSO NOTE, I thought I could get around this by create a new UI
--       group, but couldn't do it?
--       — Bridge, 04:20
--
--   He had rebuilt the control group. A Telegram group remade under a new
--   owner is a NEW chat with a NEW id; the name carries over and nothing
--   else does. The bot went on reading a room nobody was in, and refused
--   the room everybody was in — which from the outside is indistinguishable
--   from a permissions fault, and is why he went looking for a permission
--   to grant.
--
--   Creating another UI group could never have helped. Registration is what
--   makes a chat the control group, and only this row does that.
--
--   The log said so plainly, which is the only reason it took minutes:
--
--       announced unregistered chat <id> ('UI Control'), in use
--       command in unregistered chat <id> - ignored
--
-- USAGE — the id comes from the command line, never from this file:
--
--   docker compose exec -T db psql -U settlement -d settlement \
--     -v ON_ERROR_STOP=1 \
--     -v new_chat=-1000000000000 \
--     -f - < deploy/change-control-group.sql
--
--   Written bare, not quoted. Put NOTHING after the value on the -v line:
--   psql takes the rest of the line as part of the value, a trailing
--   "-- comment" included.
--
-- WHERE THE ID COMES FROM
--
--   The bot announces every unregistered chat it is spoken to in. Take the
--   id from that announcement or from the log line above. Do not retype it
--   from a screenshot — one transposed digit registers a chat that does not
--   exist, and the only symptom is that the desk stays dead.
--
-- WHAT IT DOES NOT TOUCH
--
--   Nothing but the address. Trades, payments, rates, wallets, vendors and
--   the audit history all key on the party id. This is a change of address
--   for the desk, not a new desk.
--
-- SAFETY
--
--   Five guards, all before the write, each dividing by its own answer:
--   true gives 1/1, false gives 1/0, which aborts under ON_ERROR_STOP.
--
--   Written this way round because `CASE WHEN ok THEN 1 ELSE 1/0 END` does
--   NOT work — 1/0 is constant and the planner folds it before the CASE
--   runs, so it raises even when the check passed. Verified live on
--   21 October 2026. Dividing by a subquery result cannot be folded.
-- ===========================================================================

\echo
\echo '--- before ---'

SELECT id, role, label, telegram_chat_id, is_active
  FROM parties
 WHERE role = 'bridge';

BEGIN;

SELECT
    -- 1. Exactly one bridge row. If there were two, "the control group" is
    --    already meaningless and this script must not pick one.
    1 / ((SELECT count(*) = 1 FROM parties
           WHERE role = 'bridge')::int)                    AS one_bridge,

    -- 2. A group, not a person. A positive id is a private chat, and
    --    registering that as the control group would tie the whole desk to
    --    one individual's account — the thing that just broke.
    1 / ((:new_chat < 0)::int)                             AS it_is_a_group,

    -- 3. The chat is not already somebody else. parties_chat_unique would
    --    refuse it anyway; this says which row it clashed with.
    1 / ((SELECT count(*) = 0 FROM parties
           WHERE telegram_chat_id = :new_chat
             AND role <> 'bridge')::int)                   AS chat_is_free,

    -- 4. Actually a change. A no-op that reports success would send
    --    somebody back to Telegram to debug a bot that was never moved.
    1 / ((SELECT telegram_chat_id IS DISTINCT FROM :new_chat
            FROM parties WHERE role = 'bridge')::int)      AS actually_a_change,

    -- 5. The bridge row is active. Pointing a disabled desk at a live group
    --    looks like it worked and still answers nothing.
    1 / ((SELECT is_active FROM parties
           WHERE role = 'bridge')::int)                    AS bridge_is_active;

INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
SELECT NULL,
       'party.group_changed',
       'party',
       p.id,
       jsonb_build_object(
           'label',        p.label,
           'role',         'bridge',
           'from_chat_id', p.telegram_chat_id,
           'to_chat_id',   :new_chat::bigint,
           'why',          'control group remade under a new owner'
       )
  FROM parties p
 WHERE p.role = 'bridge';

UPDATE parties
   SET telegram_chat_id = :new_chat
 WHERE role = 'bridge';

COMMIT;

\echo
\echo '--- after ---'

SELECT id, role, label, telegram_chat_id, is_active
  FROM parties
 WHERE role = 'bridge';

\echo
\echo 'Send /help in the new control group. It should answer immediately.'
\echo 'Parties are read per message, so no restart is needed.'
\echo
\echo 'NOTE: notifications now go to the new group. Anything the bot sent to'
\echo 'the old one while it was wrong is still in the old group.'
\echo
