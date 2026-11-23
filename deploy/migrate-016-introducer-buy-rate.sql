-- ===========================================================================
-- The buy rate: the introducer's share of the spread.
--
--     I have a command call /specialrate / For this one it asked for 3 things
--     Supply / Buy / Sell rate
--     Supply is the rate the supplier is providing
--     Buy is the rate I enter including the introducers fee
--     Sell rate is what I sell to the client for
--
--     the difference between the supply and buy is a margin we do not
--     receive, this is available on a separate telegram channel for the
--     introducer
--     — Bridge, 23 November 2026
--
-- The commission is USDT, not INR. On 10,000 USDT at 106.20 / 107.20 /
-- 108.20 the deposit divides three ways:
--
--     client       1062000 / 108.2  =  9815.16
--     introducer   10000 - 1062000 / 107.2  =  93.28
--     the desk     the remainder            =  91.56
--                                            ---------
--                                             10000.00
--
-- WHY buy_rate DEFAULTS TO THE SUPPLY RATE AND IS NOT NULL
--
-- With buy = supply the introducer's share is exactly zero and the desk
-- keeps the whole spread — which is what every trade does today. So every
-- existing row, and every future vendor with no introducer, runs down the
-- same arithmetic with no special case and no branch anybody can forget to
-- update. A NULL would have meant "no introducer" expressed as an absence,
-- and every calculation would have had to remember to check for it.
--
-- WHY IT IS SNAPSHOTTED ONTO trades AS WELL
--
-- The same reason supply_rate and sell_rate are, which the schema already
-- says out loud:
--
--     Snapshotted at trade creation. Never read the rate through the join
--     for calculation - a later rate change must not silently rewrite
--     history.
--
-- Commission is settled daily and signed off. The day he moves the fee from
-- ₹1 to ₹1.50, every historic commission figure must still read as it was
-- settled, or the totals he has already paid stop matching the ledger.
--
-- Safe to re-run. Safe on a live database: both columns are added with a
-- default equal to the rate already on the row, so no behaviour changes
-- until a buy rate is actually set to something different.
-- ===========================================================================

BEGIN;

ALTER TABLE rates  ADD COLUMN IF NOT EXISTS buy_rate NUMERIC(20, 6);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS buy_rate NUMERIC(20, 6);

-- Existing rows: the introducer's share of a deal struck before introducers
-- existed is nothing, and buy = supply is how that is written.
UPDATE rates  SET buy_rate = supply_rate WHERE buy_rate IS NULL;
UPDATE trades SET buy_rate = supply_rate WHERE buy_rate IS NULL;

ALTER TABLE rates  ALTER COLUMN buy_rate SET NOT NULL;
ALTER TABLE trades ALTER COLUMN buy_rate SET NOT NULL;

-- The buy rate sits between the two. Below supply it would pay the
-- introducer out of the vendor's money instead of out of the spread; above
-- sell it would pay them more than the whole spread and the desk would owe
-- more than it earned. Both are typos at /specialrate, and both are
-- cheaper to refuse here than to find in a commission report.
ALTER TABLE rates  DROP CONSTRAINT IF EXISTS rates_buy_between;
ALTER TABLE rates  ADD CONSTRAINT rates_buy_between
    CHECK (buy_rate >= supply_rate AND buy_rate <= sell_rate);

-- trades is deliberately NOT given the same constraint. A trade is a
-- historic record and must still be readable if a rate was once set in a
-- way that is no longer allowed; the place to refuse a bad rate is where it
-- is entered.

-- "NO INTRODUCER MEANS buy = supply", WRITTEN WHERE IT CANNOT BE BYPASSED.
--
-- A DEFAULT cannot reference another column of the same row, so without
-- this every INSERT into rates and trades would have to remember to name
-- buy_rate. The seed scripts in this directory do not, the UAT reset does
-- not, and neither did eighty-two existing tests — which is the evidence
-- that "remember to name it" is not a workable rule. One forgotten INSERT
-- would be a NOT NULL violation at best and, if the column were nullable
-- instead, a silently skipped commission at worst.
--
-- A BEFORE INSERT trigger runs before the NOT NULL check, so the column
-- stays NOT NULL and nothing has to know about it.
CREATE OR REPLACE FUNCTION buy_rate_defaults_to_supply()
RETURNS trigger AS $$
BEGIN
    IF NEW.buy_rate IS NULL THEN
        NEW.buy_rate := NEW.supply_rate;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS rates_buy_rate_default ON rates;
CREATE TRIGGER rates_buy_rate_default
    BEFORE INSERT ON rates
    FOR EACH ROW EXECUTE FUNCTION buy_rate_defaults_to_supply();

DROP TRIGGER IF EXISTS trades_buy_rate_default ON trades;
CREATE TRIGGER trades_buy_rate_default
    BEFORE INSERT ON trades
    FOR EACH ROW EXECUTE FUNCTION buy_rate_defaults_to_supply();

COMMIT;

-- Check:
--
--   SELECT count(*) FILTER (WHERE buy_rate = supply_rate) AS unchanged,
--          count(*) FILTER (WHERE buy_rate <> supply_rate) AS with_introducer,
--          count(*) AS total
--     FROM trades;
--
-- Before any introducer is registered, "unchanged" must equal "total".
