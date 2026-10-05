-- 014 — the client is told about money the desk actually sent, and nothing else.
--
-- WHY
--
-- USDT arriving at a counterparty's own wallet was announced to them as
-- "Funds received". The bot cannot see who sent it; it sees an arrival at an
-- address it watches, and assumed the desk was settling.
--
-- 5 October 2026, 13:58 IST: 100 USDT reached the client's wallet from an
-- address that had never appeared before. The client was told funds had been
-- received and replied "This was not me". It was their own counterparty
-- moving funds internally — nothing to do with this desk at all.
--
--     this is an odd one, can we set so that it ignores any settlements that
--     do not match the sending.
--     Nothing at all, if unrelated to us, it will most likely be intrnal
--     fund movement
--     — Bridge, 5 October 2026
--
-- Nothing was mis-recorded: no trade exists, no figure moved. What went wrong
-- is that a counterparty was told, by this desk's bot, that this desk had paid
-- them. That is a statement about money, to someone who may reconcile against
-- it.
--
-- WHY AN ADDRESS LIST RATHER THAN MATCHING AMOUNTS
--
-- Matching the amount against an outstanding payout would be a guess, and a
-- guess that is right most of the time is the kind that is trusted when it is
-- wrong. The sender is a fact on the chain. Eight addresses have ever paid
-- this client: seven with months of history and over 1.2m USDT between them,
-- and the one that caused this, which sent once.
--
-- HOW IT IS SEEDED
--
-- By query, never by literal address. This repository is public and a payout
-- address in it hands anyone the desk's settlement history.
--
-- The rule: an address that has sent to a counterparty wallet at least TWICE
-- is the desk's. One send is not a pattern — it is exactly what the stranger
-- looked like. Addresses that are the desk's OWN wallets need no seeding;
-- they are recognised from the wallets table at the time of the check, so a
-- vendor wallet used to forward a settlement is known however often it is
-- used.
--
-- A new hot wallet therefore starts unknown. That is deliberate: the first
-- settlement from it asks the Bridge "is this yours?", he confirms once, and
-- it is known from then on. One question, the first time, in exchange for
-- never telling a counterparty the desk paid them when it did not.
--
-- Idempotent. Safe to run twice.

BEGIN;

CREATE TABLE IF NOT EXISTS payout_sources (
    address         TEXT PRIMARY KEY,

    -- NULL for the rows seeded from history below: nobody confirmed those,
    -- they were inferred from the desk's own traffic. A row the Bridge
    -- confirmed by hand carries his party id, so the two can be told apart
    -- if one of these ever turns out to be wrong.
    added_by        BIGINT REFERENCES parties(id),
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    note            TEXT
);

INSERT INTO payout_sources (address, note)
SELECT d.from_address,
       'seeded from history: ' || count(*) || ' settlements to counterparty '
       || 'wallets up to 5 October 2026'
FROM deposits d
JOIN wallets w ON w.id = d.wallet_id
WHERE NOT w.is_internal
  AND d.from_address IS NOT NULL
GROUP BY d.from_address
HAVING count(*) >= 2
ON CONFLICT (address) DO NOTHING;

COMMIT;

-- Check:
--
--   SELECT count(*) FROM payout_sources;
--
-- Expect one row per address that has settled to a counterparty more than
-- once. Anything arriving from an address NOT in this list, and not one of
-- the desk's own wallets, now goes to the Bridge to confirm instead of to the
-- counterparty as a payout.
--
-- To see what was seeded, with counts:
--
--   SELECT address, note, added_by FROM payout_sources ORDER BY added_at;
