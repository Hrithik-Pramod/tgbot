-- ===========================================================================
-- What the introducer earned on this trade, in USDT.
--
--     USDT For Introducer is 10000-9906.7164=93.2836
--     Remaining commission for partners = 184.8429-93.2836=91.5593
--     — Bridge, 23 November 2026
--
-- margin_usdt has always been the WHOLE spread. From here it is the desk's
-- share of it, and this column is the introducer's. The two still add up to
-- the spread, and all three — client, introducer, desk — add up to the
-- deposit, by construction rather than by three roundings agreeing. See
-- core.money.split_three_ways.
--
-- WHY A COLUMN AND NOT A CALCULATION
--
-- It could be derived from usdt_received, buy_rate and supply_rate every
-- time it is read. It is stored because commission is settled daily and
-- signed off: the figure he paid against has to be readable years later
-- exactly as it was paid, and a derived figure quietly restates itself the
-- day a rounding rule changes. The same argument as snapshotting the rates,
-- one step further along.
--
-- DEFAULT ZERO, NOT NULL
--
-- Every existing trade earned no commission, and that is a real zero rather
-- than an unknown. NULL would mean every sum over this column needed a
-- COALESCE, and the day one was forgotten the total would come out as
-- nothing at all rather than as a visibly wrong number.
-- ===========================================================================

BEGIN;

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS introducer_usdt NUMERIC(20, 6) NOT NULL DEFAULT 0;

-- Nobody is owed a negative commission.
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_introducer_usdt_positive;
ALTER TABLE trades ADD CONSTRAINT trades_introducer_usdt_positive
    CHECK (introducer_usdt >= 0);

COMMIT;

-- Check:
--
--   SELECT count(*) FILTER (WHERE introducer_usdt = 0) AS no_commission,
--          count(*) AS total
--     FROM trades;
--
-- Before any introducer is registered these must be equal.
