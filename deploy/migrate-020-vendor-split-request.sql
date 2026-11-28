-- ===========================================================================
-- A vendor can ask for their deposit to arrive as two orders.
--
--   docker compose exec -T db psql -U settlement -d settlement \
--     -v ON_ERROR_STOP=1 -f - < deploy/migrate-020-vendor-split-request.sql
--
-- WHY IT LIVES ON pending_sends
--
--     the option i have to split a trade, can i add that to vendor side
--     so Hash, the choose account, then split?
--     but not amount, we just offer split 50/50
--     — Bridge, 27 November 2026
--
--   The vendor asks at /send, and /send is a CLAIM: the hash is typed while
--   the USDT is still in transit, so there is usually no order yet to divide.
--
--       they cannot send without entering hash, so funds are always in
--       transit — Bridge
--
--   pending_sends already is that claim — supplier, account, hash, and the
--   trade it was later matched to. The request to split belongs on the same
--   row, for the same reason the account does: it is something the vendor
--   said when they sent, waiting for money to arrive and make it real.
--
--   A separate table would be a second record of one event, and the two
--   would drift the first time a claim was matched by one path and not the
--   other.
--
-- TWO COLUMNS, NOT ONE
--
--   split_requested  what the vendor asked for. Never cleared, so the claim
--                    still reads as what was said at the time.
--   split_applied_at when it was carried out. NULL means outstanding.
--
--   One boolean flipped to false on use would destroy the record of the
--   request in the act of honouring it, and leave "never asked" and
--   "asked and done" identical. That distinction is the whole audit.
--
--   Consuming the request is therefore a timestamp write, and the partial
--   index below makes "the outstanding one for this vendor" a single
--   indexed lookup rather than a scan of every claim they have ever made.
--
-- FRESH ORDERS ONLY
--
--   Not enforced here, because it is not a property of this row. A deposit
--   that merges into an open order must NOT be split:
--
--       fresh order, so one already there is on its own, this is new
--       — Bridge
--
--   The caller checks that the deposit is the only one on its trade. See
--   monitor/tron.py.
-- ===========================================================================

BEGIN;

ALTER TABLE pending_sends
    ADD COLUMN IF NOT EXISTS split_requested  BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS split_applied_at TIMESTAMPTZ;

-- Applied without being asked for is nonsense, and would mean a deposit was
-- divided on the strength of a row nobody set. Cheap to state, and it turns
-- a whole class of mistake into an error at the moment it is made.
ALTER TABLE pending_sends
    DROP CONSTRAINT IF EXISTS pending_sends_split_shape;
ALTER TABLE pending_sends
    ADD CONSTRAINT pending_sends_split_shape
    CHECK (split_applied_at IS NULL OR split_requested);

-- The lookup the monitor makes on every fresh order: the oldest outstanding
-- request for one vendor. Partial, because a request that has been applied
-- is never looked for again.
CREATE INDEX IF NOT EXISTS pending_sends_split_outstanding
    ON pending_sends (supplier_id, created_at)
    WHERE split_requested AND split_applied_at IS NULL;

COMMIT;

\echo
SELECT column_name, data_type, is_nullable, column_default
  FROM information_schema.columns
 WHERE table_name = 'pending_sends'
   AND column_name IN ('split_requested', 'split_applied_at')
 ORDER BY column_name;

SELECT count(*) AS outstanding_requests
  FROM pending_sends
 WHERE split_requested AND split_applied_at IS NULL;
\echo
