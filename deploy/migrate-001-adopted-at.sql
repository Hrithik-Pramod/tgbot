-- Migration 001 — monitor_state.adopted_at_ms
--
-- db/schema.sql only runs on an empty database volume, so an existing
-- deployment needs this applied by hand. Safe to run more than once.
--
--   docker compose stop bot
--   docker compose exec -T db psql -U settlement -d settlement < deploy/migrate-001-adopted-at.sql
--   docker compose start bot
--
-- WHY
--
-- A wallet the bot has just been given must ignore whatever is already on it.
-- Two attempts to enforce that through the provider's date filter both failed:
--
--   1. Cursor set to the newest existing transaction. Every poll rewinds two
--      minutes for boundary safety, so it walked straight back into it.
--   2. Cursor offset by the overlap plus one millisecond. TronScan truncates
--      start_timestamp to whole seconds — verified live on 10 September 2026,
--      a query from ...471001 still returned the transaction at ...471000.
--
-- Both let a fifteen-day-old 30 TRX transfer be reported as a new deposit. The
-- boundary now lives here, in a column, and is enforced in our own code where
-- no provider can round it away.
--
-- Existing wallets are left NULL deliberately: NULL means "adopted before this
-- column existed", and the poller treats those exactly as it did before. Any
-- wallet that needs a real baseline should have its row in monitor_state
-- deleted (with the bot stopped) so it is adopted cleanly on the next poll.

BEGIN;

ALTER TABLE monitor_state
    ADD COLUMN IF NOT EXISTS adopted_at_ms BIGINT;

COMMENT ON COLUMN monitor_state.adopted_at_ms IS
    'Timestamp of the newest transaction that already existed when this wallet '
    'was first watched. Anything at or before this is history, never a deposit.';

COMMIT;

SELECT wallet_id, last_timestamp_ms, adopted_at_ms FROM monitor_state;
