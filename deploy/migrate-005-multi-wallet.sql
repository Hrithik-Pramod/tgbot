-- ===========================================================================
-- 005 — a supplier may run more than one internal wallet to the same client.
--
-- WHY
--
-- The brief assumed one internal wallet per supplier→client pairing, and the
-- schema enforced it, because the wallet is the routing key: a deposit is
-- attributed to a trade by which wallet received it.
--
-- Bridge, 15 September 2026:
--
--     Add - client 1 association -- second wallet in use, associate SUP4
--     (Grish/GS) to TTNbTqxUpr5uXbRRNQcQteYonfv9N77kzq
--
-- WHY IT IS SAFE
--
-- The routing does not depend on the pairing being unique — it depends on
-- the WALLET being unique, which wallets_address_unique still enforces. A
-- deposit reads its supplier and client off the row for the address it
-- landed on, and two rows naming the same pair answer that question
-- identically.
--
-- trades_one_uninstructed_per_wallet is already per wallet, so each address
-- accumulates its own trade. Two wallets for one supplier therefore run two
-- independent streams, which is exactly what is being asked for.
--
-- WHAT TO WATCH
--
-- A supplier with two wallets can have two uninstructed trades open at once.
-- nominate_account attaches a /send to the most recently opened of them, so
-- if both are waiting the nomination follows the newer deposit. That is the
-- right guess and still only a guess — worth knowing if an account ever
-- turns up on the trade nobody expected.
--
-- Safe to re-run.
-- ===========================================================================

BEGIN;

DROP INDEX IF EXISTS wallets_pairing_unique;

-- Kept as a plain index: the pairing is still looked up constantly, it is
-- simply no longer unique.
CREATE INDEX IF NOT EXISTS wallets_pairing_idx
    ON wallets (supplier_id, client_id) WHERE is_internal;

COMMIT;

-- Verify — the unique index must be gone, the plain one present.
--   SELECT indexname FROM pg_indexes WHERE tablename='wallets' ORDER BY 1;
