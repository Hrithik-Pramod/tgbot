-- ===========================================================================
-- 006 — a stale uninstructed trade no longer blocks a new one.
--
-- WHY
--
-- Migration 003 made "one UNINSTRUCTED open trade per wallet" the rule,
-- because exactly one trade at a time should be accumulating deposits.
-- That held while every trade was issued the same day.
--
-- It stopped holding on 15 September 2026. SUPA5 opened on the 12th, was
-- never issued, and sat open for three days. A fresh 37,736 USDT deposit
-- merged into it and the Bridge was shown a 66,038 total for a trade whose
-- earlier half he had already settled by hand:
--
--     ITS STILL STORED PREVIOUS TRADE
--     this is causing more problems that solving
--
-- The code fix is a time window: a deposit joins the open trade only if that
-- trade took a deposit recently, and otherwise starts its own. But the
-- unique index forbids the second trade existing at all, so the deposit
-- would fail instead of splitting.
--
-- WHAT REPLACES IT
--
-- Nothing, at the database level. The guarantee moves into the query, which
-- selects the one recent uninstructed trade and creates a trade when there
-- is none. Deposits on a wallet are processed one at a time by a single
-- poll loop inside a transaction taking FOR UPDATE, so two trades cannot be
-- opened for one wallet concurrently.
--
-- What is lost is a safety net, so it is replaced by a visible one: the
-- health check now reports any uninstructed trade left open longer than the
-- window. SUPA5 sat for three days and nothing said a word — that silence
-- is what turned a stale trade into a live incident.
--
-- Safe to re-run.
-- ===========================================================================

BEGIN;

DROP INDEX IF EXISTS trades_one_uninstructed_per_wallet;

CREATE INDEX IF NOT EXISTS trades_uninstructed_idx
    ON trades (wallet_id, opened_at)
    WHERE status IN ('open', 'awaiting_payment') AND instructed_at IS NULL;

COMMIT;

-- Verify — the unique index gone, the plain one present.
--   SELECT indexname FROM pg_indexes WHERE tablename='trades' ORDER BY 1;
