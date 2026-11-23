-- ===========================================================================
-- Which commission has been paid, so a settled day stops showing.
--
--     Should the bot mark a day as settled? Otherwise yesterday keeps
--     showing in today's total.
--     Day as settled so after midnight clock resets
--     — Bridge, 23 November 2026
--
-- A ROW PER TRADE, NOT A DATE RANGE
--
-- Dates are the obvious design and the wrong one. A deal instructed at
-- 23:58 and settled at 00:02 belongs to neither day under any rule simple
-- enough to state out loud, and the day he actually pays rarely lines up
-- with the day the deal went out. Naming the trades removes the question
-- entirely: a trade is either in the settlements table or it is owed.
--
-- It also survives the thing a date range cannot. Commission is counted
-- when a trade is SENT, not when it is paid (his answer, asked directly),
-- and it reverses if the trade is cancelled. So a day can change after it
-- has been settled. With trades named, the reversal is visible against the
-- exact deal it came from.
--
-- WHY usdt IS STORED HERE TOO
--
-- It is on the trade already. It is copied because this row is a record of
-- a payment made, and what he paid must still read as what he paid even if
-- the trade is later repriced — which is allowed, and which rewrites
-- introducer_usdt. The trade says what is owed now; this says what was
-- handed over then.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS commission_settlements (
    id              BIGSERIAL PRIMARY KEY,
    trade_id        BIGINT      NOT NULL REFERENCES trades(id),
    introducer_id   BIGINT      NOT NULL REFERENCES parties(id),
    usdt            NUMERIC(20, 6) NOT NULL CHECK (usdt > 0),
    settled_by      BIGINT      REFERENCES parties(id),
    settled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One settlement per trade. Without this, running /comms twice and
    -- confirming both times would record the same commission as paid
    -- twice, and the next total would be short by that amount with nothing
    -- to show why.
    CONSTRAINT commission_settlements_trade_once UNIQUE (trade_id)
);

CREATE INDEX IF NOT EXISTS commission_settlements_introducer_idx
    ON commission_settlements (introducer_id, settled_at DESC);

COMMIT;

-- Check:
--
--   SELECT count(*) FROM commission_settlements;
--
-- Zero until the first settlement is confirmed.
