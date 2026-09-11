-- ===========================================================================
-- 003 — a deposit that arrives after instructions have gone out starts a new
--       trade instead of growing the old one.
--
-- WHY
--
-- 11 September 2026, live:
--
--   16:34:30  1,859 USDT lands           SUPB1 opens, ₹197,054
--   16:37:39  instruction issued          client told to pay ₹197,054
--   16:47:29  3,000 USDT lands            SUPB1 grows to ₹515,054
--
-- The client is holding an instruction for ₹197,054 while the trade now says
-- ₹515,054 is expected. The trade cannot close on the amount the client was
-- actually asked for, and re-issuing for the new total invites them to pay
-- the first instruction twice. The Bridge: "Should be for only 1,859."
--
-- The rule (client, 11 September 2026): once a trade has been instructed it is
-- closed to new deposits. The next deposit on that wallet opens the next trade.
--
-- HOW
--
-- `instructed_at` is set the first time payment slots are issued. The old
-- one-open-trade-per-wallet index is replaced by one that allows any number of
-- open trades per wallet but only ONE that has not yet been instructed — which
-- is the real invariant: exactly one trade at a time is accumulating deposits.
--
-- The predicate cannot be a subquery on payment_slots, which is why this is a
-- column and not a derived value.
--
-- Safe to run more than once.
-- ===========================================================================

BEGIN;

ALTER TABLE trades ADD COLUMN IF NOT EXISTS instructed_at TIMESTAMPTZ;

-- Backfill from the slots themselves so existing trades classify correctly the
-- moment the new code starts. SUPB1 must come out instructed, or the next
-- deposit would merge into it exactly as before.
UPDATE trades t
SET instructed_at = ps.first_issued
FROM (
    SELECT trade_id, MIN(issued_at) AS first_issued
    FROM payment_slots GROUP BY trade_id
) ps
WHERE ps.trade_id = t.id AND t.instructed_at IS NULL;

DROP INDEX IF EXISTS trades_one_open_per_wallet;

CREATE UNIQUE INDEX IF NOT EXISTS trades_one_uninstructed_per_wallet
    ON trades (wallet_id)
    WHERE status IN ('open', 'awaiting_payment') AND instructed_at IS NULL;

COMMIT;

-- Verification — SUPB1 should show an instructed_at, and the index should exist.
--   SELECT reference, status, instructed_at FROM trades ORDER BY id;
--   \d trades
