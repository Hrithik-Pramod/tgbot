-- Migration 002 — pending_sends
--
-- db/schema.sql only runs on an empty database volume, so a live deployment
-- needs this applied by hand. Safe to run more than once.
--
--   docker compose stop bot
--   docker compose exec -T db psql -U settlement -d settlement < deploy/migrate-002-pending-sends.sql
--   docker compose start bot
--
-- WHY
--
-- A supplier running /send used to notify the Bridge immediately. That is a
-- claim, not a fact — the Bridge was being asked to act on "I have sent"
-- rather than on funds actually arriving (client request, 11 September 2026:
-- "only when validation of funds landing does the bot notify me").
--
-- The claim is now recorded here and surfaces attached to the deposit when it
-- lands. The row also covers the opposite case: a supplier who claims to have
-- sent and never does would otherwise be invisible, since nothing is announced
-- at the moment of claiming. Anything unmatched after the configured window is
-- reported once.

BEGIN;

CREATE TABLE IF NOT EXISTS pending_sends (
    id              BIGSERIAL PRIMARY KEY,
    supplier_id     BIGINT      NOT NULL REFERENCES parties(id),
    bank_account_id BIGINT      REFERENCES bank_accounts(id),
    hash_url        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    matched_trade_id BIGINT     REFERENCES trades(id),
    alerted_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS pending_sends_open_idx ON pending_sends (created_at)
    WHERE matched_trade_id IS NULL;

COMMIT;

SELECT 'pending_sends' AS table_name, count(*) FROM pending_sends;
