-- 012 — one deposit, two orders.
--
-- WHY THIS COLUMN EXISTS
--
-- A deposit row points at exactly one trade. That is the right shape when a
-- send becomes an order, and the wrong shape the moment the Bridge wants one
-- send to become two:
--
--     if usdt is deposited, i want the option my end to split the payment
--     so 10000 usdt comes in ... i have option to send in 2 parts
--     it only splits in 2 not anymore than this
--     it just needs to let me decide how much the first order is
--     and the ability to send the second part later
--     — Bridge, 1 October 2026
--
-- It was done once by hand already. On 11 September 2026 a 3,000 USDT deposit
-- merged into SUPB1 after the client had been instructed for 1,859, and
-- fix-supb1-split.sql carved SUPB2 back out of it in a transaction written
-- line by line with the bot stopped. That is not a thing to do twice.
--
-- WHY NOT SPLIT THE DEPOSIT ROW
--
-- deposits_tx_unique UNIQUE (tx_hash, wallet_id) is what stops the same
-- transfer being credited twice when the monitor sees it again. Splitting one
-- deposit into two rows means relaxing that, and the protection it gives is
-- worth more than the tidiness. So the deposit stays whole and attached to
-- the first part, and the second part records where its USDT came from here.
--
-- WHAT THE COLUMN GUARANTEES
--
-- A trade with no deposits of its own is only legitimate if this is set. The
-- arithmetic that must hold for a parent P:
--
--     sum(deposits on P) = P.usdt_received + sum(children.usdt_received)
--
-- Two things would otherwise double-count the same USDT, and both are now
-- refused in code rather than left to care:
--   - reopening a stranded deposit after cancelling the first part, which
--     would resurrect the full amount while the second part still exists;
--   - cancelling a parent while a child is live.
--
-- Idempotent. Safe to run twice.

BEGIN;

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS split_from_trade_id BIGINT REFERENCES trades(id);

-- Asked in only two directions: "does this trade have children?" (the
-- refusals above) and "where did this one come from?" (the primary key).
CREATE INDEX IF NOT EXISTS trades_split_parent_idx
    ON trades (split_from_trade_id)
    WHERE split_from_trade_id IS NOT NULL;

COMMIT;

-- Check:
--
--   SELECT count(*) FROM trades WHERE split_from_trade_id IS NOT NULL;
--
-- Zero. Nothing is split by this migration; the column is NULL on every
-- existing trade and the bot behaves exactly as before until the Bridge taps
-- "Split into 2 orders".
