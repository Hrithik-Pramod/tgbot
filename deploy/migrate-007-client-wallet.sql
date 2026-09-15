-- ===========================================================================
-- 007 — TTNbTqx… is a CLIENT wallet, not an internal one. And with that,
--       migration 005 is reverted: the pairing is unique again.
--
-- WHAT I GOT WRONG
--
-- The Bridge asked, 15 September 2026:
--
--     Add - client 1 association -- second wallet in use, associate SUP4
--     (Grish/GS) to TTNbTqxUpr5uXbRRNQcQteYonfv9N77kzq
--
-- I read "associate SUP4 to <address>" as a second internal wallet for that
-- supplier. He meant the opposite end of the same sentence:
--
--     TTNbTqx... - this is not internal wallet, this is client wallet
--     we send from TV7Edxcz... INTERNAL to CLIENT A - TTNbTqx...
--     confirm you have this set correctly, this is an additional client wallet
--
-- WHY IT MATTERS MORE THAN A LABEL
--
-- An internal wallet is where a supplier's USDT ARRIVES; the monitor turns
-- anything landing there into a deposit, opens a trade, prices it and asks
-- the Bridge to instruct the client. A client wallet is where settlements
-- LEAVE to.
--
-- Left as internal, every payout the Bridge sent to this address would have
-- come back at him as a supplier deposit and opened a trade against money
-- he had just paid out. The same shape as the 23,320.99 and 1,838.19 rows
-- on 11 September — except those landed on a wallet with no rate and so
-- stayed inert. This one has a rate, and would have been priced, announced
-- and instructed.
--
-- AND THE INDEX GOES BACK
--
-- Migration 005 dropped wallets_pairing_unique to allow two internal
-- wallets for one supplier→client pair. That is not what was being asked
-- for, so the constraint is restored: one internal wallet per pairing, as
-- the brief has it (C3, the wallet is the routing key).
--
-- Safe to re-run.
-- ===========================================================================

BEGIN;

-- Refuse to run if anything has already been recorded against it as a
-- deposit — that would mean a trade exists on money that was leaving.
DO $$
DECLARE
    n INT;
BEGIN
    SELECT count(*) INTO n
    FROM deposits d JOIN wallets w ON w.id = d.wallet_id
    WHERE w.address = 'TTNbTqxUpr5uXbRRNQcQteYonfv9N77kzq';
    IF n > 0 THEN
        RAISE EXCEPTION
            '% deposit(s) already recorded on that address — check them '
            'before converting it to a client wallet', n;
    END IF;
END $$;

UPDATE wallets
SET is_internal    = FALSE,
    owner_party_id = (SELECT id FROM parties WHERE label = 'Client A'),
    supplier_id    = NULL,
    client_id      = NULL,
    label          = 'Client A payout (2)'
WHERE address = 'TTNbTqxUpr5uXbRRNQcQteYonfv9N77kzq';

DROP INDEX IF EXISTS wallets_pairing_idx;

CREATE UNIQUE INDEX IF NOT EXISTS wallets_pairing_unique
    ON wallets (supplier_id, client_id) WHERE is_internal;

INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
SELECT NULL, 'wallet.reclassified', 'wallet', id,
       jsonb_build_object(
           'address', address,
           'from', 'internal Girish - Sam -> Client A',
           'to',   'external, owned by Client A',
           'reason', 'second client payout address, not a deposit address')
FROM wallets WHERE address = 'TTNbTqxUpr5uXbRRNQcQteYonfv9N77kzq';

COMMIT;

-- Verify. TV7Edxcz must be internal for Girish - Sam; TTNbTqx external,
-- owned by Client A; and one internal wallet per pairing.
--   SELECT w.id, w.address, w.is_internal, s.label AS supplier,
--          c.label AS client, o.label AS owner, w.is_monitored
--   FROM wallets w
--   LEFT JOIN parties s ON s.id = w.supplier_id
--   LEFT JOIN parties c ON c.id = w.client_id
--   LEFT JOIN parties o ON o.id = w.owner_party_id
--   ORDER BY w.id;
