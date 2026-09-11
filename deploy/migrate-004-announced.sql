-- ===========================================================================
-- 004 — hold the deposit notification until the supplier has said where the
--       INR goes, then send one complete message.
--
-- WHY
--
-- The intended order was /send first, deposit second. In practice suppliers
-- do the opposite every time (Bridge, 11 September 2026: "they are always
-- sending usdt first then entering the details"), so the notification fires
-- on chain detection — before anyone has nominated an account.
--
-- The Bridge then gets a message he cannot act on, followed later by the
-- information he needed. Worse, until today it filled the gap with the
-- supplier's previous choice, so a trade nobody had spoken for arrived
-- pre-filled with the wrong account.
--
-- WHAT CHANGES
--
-- A trade is announced ONCE, when it becomes actionable:
--
--   supplier runs /send        -> announce immediately, account included
--   nothing after N minutes    -> announce anyway, marked as not yet entered
--
-- The deposit is still recorded the instant it lands. Only the message waits.
-- Nothing is ever suppressed: a supplier who sends and then goes quiet still
-- surfaces, which is the whole reason this is a delay and not a condition.
--
-- BACKFILL
--
-- Every existing trade is marked announced. Without this the sweep would
-- treat the live SUPA3 — and every closed trade — as never announced and
-- re-post them all on the first cycle.
-- ===========================================================================

BEGIN;

ALTER TABLE trades ADD COLUMN IF NOT EXISTS announced_at TIMESTAMPTZ;

UPDATE trades SET announced_at = COALESCE(announced_at, opened_at);

-- Finding the ones still waiting must stay cheap; the monitor asks on every
-- cycle.
CREATE INDEX IF NOT EXISTS trades_unannounced_idx
    ON trades (opened_at)
    WHERE announced_at IS NULL;

COMMIT;

-- Verify — every existing trade should be announced, none waiting.
--   SELECT count(*) FILTER (WHERE announced_at IS NULL) AS waiting,
--          count(*) AS total
--   FROM trades;
