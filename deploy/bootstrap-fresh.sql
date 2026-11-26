-- ===========================================================================
-- BOOTSTRAP A FRESH DATABASE — the two rows that cannot be created by command.
--
-- WHY THIS EXISTS AND golive-seed.sql DOES NOT DO
--
--   golive-seed.sql was written for one specific day. It carries that day's
--   chat ids, that day's wallet addresses and that day's rate as literals,
--   so running it again recreates September's desk. This one carries nothing:
--   every value comes from the command line, and it seeds the minimum the
--   system cannot bootstrap itself.
--
-- WHY EXACTLY TWO ROWS
--
--   Authorisation is by group. On an empty database no chat is the Bridge, so
--   there is no group any command can be issued from — including the commands
--   that register groups. The bridge row is the way in.
--
--   The client is the second, because there is no /addclient. /addvendor asks
--   which client a vendor settles to and offers the clients that exist; with
--   none, the flow cannot complete and no vendor can be registered at all.
--
--   Nothing else is seeded. Vendors, introducers, wallets, bank accounts and
--   rates all have commands, and those commands write the audit trail, set
--   the deal counter and check what SQL here would not. A seed is the last
--   resort, not a shortcut.
--
-- USAGE — both ids come from the command line, bare, not quoted:
--
--   docker compose exec -T db psql -U settlement -d settlement \
--     -v ON_ERROR_STOP=1 \
--     -v bridge_chat=-1000000000000 \
--     -v client_chat=-1000000000001 \
--     -v client_label="'Client name as counterparties see it'" \
--     -f - < deploy/bootstrap-fresh.sql
--
--   client_label is quoted INSIDE the value, as shown — psql substitutes the
--   text verbatim and SQL needs the quotes.
--
--   Put NOTHING after a value on a -v line. psql takes the rest of the line,
--   a trailing "-- comment" included, as part of the value.
--
-- RUN IT WITH THE BOT STOPPED
--
--   Not strictly required — these rows touch no cursor — but a bot polling a
--   half-seeded desk will answer the Bridge with menus for a desk that has no
--   vendors, which reads as a broken deployment.
--
-- AFTERWARDS, IN THE CONTROL GROUP
--
--   /addvendor       once per vendor group
--   /addintroducer   if a vendor was introduced
--   /walletadd       the internal wallet each vendor deposits to
--   /walletlink      where each pairing pays the client
--   /setrate         or /specialrate for an introduced vendor
--
--   A vendor with no rate opens no trade on a deposit, and a pairing with no
--   link has nowhere to pay. Both are silent failures, which is why
--   /addvendor says so on the way out.
-- ===========================================================================

BEGIN;

-- Guard 1. Refuse anything but a genuinely empty desk. Seeding twice makes
-- authorisation ambiguous — two bridge rows and the question "is this chat
-- the Bridge" has two answers.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM parties) THEN
        RAISE EXCEPTION
            'parties is not empty — this is for a fresh database only. '
            'If you meant to clear a working one, take a backup first.';
    END IF;
END $$;

-- Guards 2-4, each dividing by its own answer: true gives 1/1, false gives
-- 1/0, which aborts under ON_ERROR_STOP.
--
-- Not written as CASE WHEN ok THEN 1 ELSE 1/0 END: 1/0 is a constant, the
-- planner folds it before the CASE runs, and it raises even when the check
-- passed. Verified live on 21 October 2026.
SELECT
    -- A positive id is a private chat with one person. Tying the desk to an
    -- individual account is what broke on 26 November 2026, when the owner
    -- changed and the control group was rebuilt.
    1 / ((:bridge_chat < 0)::int)                   AS bridge_is_a_group,
    1 / ((:client_chat < 0)::int)                   AS client_is_a_group,
    -- One chat, one party. parties_chat_unique would refuse it anyway; this
    -- says which pair clashed.
    1 / ((:bridge_chat <> :client_chat)::int)       AS two_different_groups;

INSERT INTO parties (role, label, display_name, telegram_chat_id) VALUES
    ('bridge', 'Bridge',        'UI Control', :bridge_chat),
    ('client', :client_label,   NULL,         :client_chat);

INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
SELECT NULL, 'party.seeded', 'party', p.id,
       jsonb_build_object('role', p.role, 'label', p.label,
                          'why', 'fresh database bootstrap')
  FROM parties p;

COMMIT;


-- ---------------------------------------------------------------- verify
SELECT id, role, label, display_name, telegram_chat_id, is_active
  FROM parties ORDER BY role;

SELECT 'vendors (register with /addvendor)' AS next_step, count(*)
  FROM parties WHERE role = 'supplier'
UNION ALL
SELECT 'wallets (/walletadd, /walletlink)', count(*) FROM wallets
UNION ALL
SELECT 'rates (/setrate or /specialrate)', count(*) FROM rates
UNION ALL
SELECT 'trades (must be 0)', count(*) FROM trades
UNION ALL
SELECT 'monitor_state (must be 0 — wallets adopt on first poll)', count(*)
  FROM monitor_state;

\echo
\echo 'Send /help in the control group. Then /addvendor for each vendor group.'
\echo
