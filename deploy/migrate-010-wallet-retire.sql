-- 010 — a wallet can be retired without being deleted.
--
-- WHY
--
-- Bridge, 25 September 2026:
--
--     if i needed to remove an account wallet for a provider, and not have
--     an associated wallet in place, can you add this
--
-- There was no way to. A wallet could be created (/walletadd) and its
-- address changed (/walletchange), and that was all — so a vendor who left,
-- or a test pairing, stayed on the books for ever. The only lever was
-- is_monitored, which stops the polling but keeps the row occupying both
-- unique constraints.
--
-- DELETING IS NOT AVAILABLE
--
-- trades.wallet_id and deposits.wallet_id both reference wallets, NOT NULL.
-- Wallet 8 carries ALPH1 through ALPH5. A delete would either fail on the
-- foreign key or, with a cascade, take five settled trades with it.
--
-- History must keep pointing at the wallet it actually settled through.
--
-- SO: RETIRE, THE WAY BANK ACCOUNTS ALREADY DO
--
-- bank_accounts has carried is_active and removed_at since the first build.
-- Wallets get the same treatment, and the two unique constraints become
-- partial so a retired row stops reserving what it holds:
--
--   address   a retired wallet's address can be registered again, to the
--             same pairing or a different one
--   pairing   a vendor whose wallet is retired can be given a new one
--
-- Both are the point of the feature. Neither is possible while the
-- constraints count retired rows.
--
-- THE HAZARD THIS CREATES, STATED PLAINLY
--
-- Re-registering a retired ADDRESS against a DIFFERENT pairing means a late
-- deposit from the old counterparty is attributed to the new vendor. That is
-- worse than losing it, because it is silent and it is wrong rather than
-- missing. retire_wallet refuses while a trade is live; the rest is a
-- judgement for whoever types the new address.
--
-- Idempotent. Safe to run twice.

BEGIN;

ALTER TABLE wallets
    ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ;

-- The address constraint was a table constraint, so it has to go before a
-- partial index can take its place.
ALTER TABLE wallets
    DROP CONSTRAINT IF EXISTS wallets_address_unique;

CREATE UNIQUE INDEX IF NOT EXISTS wallets_address_live_unique
    ON wallets (address) WHERE retired_at IS NULL;

-- Same for the pairing. One LIVE internal wallet per supplier-client pair.
DROP INDEX IF EXISTS wallets_pairing_unique;

CREATE UNIQUE INDEX IF NOT EXISTS wallets_pairing_unique
    ON wallets (supplier_id, client_id)
    WHERE is_internal AND retired_at IS NULL;

-- Retired wallets are never polled. Belt and braces: retire_wallet also
-- sets is_monitored = false, but a stray UPDATE elsewhere must not be able
-- to bring a retired address back under the monitor.
CREATE INDEX IF NOT EXISTS wallets_monitored_live_idx
    ON wallets (address) WHERE is_monitored AND retired_at IS NULL;

COMMIT;

-- Check:
--
--   SELECT count(*) FILTER (WHERE retired_at IS NOT NULL) AS retired,
--          count(*) AS total
--   FROM wallets;
--
-- Nothing is retired by this migration. Existing rows keep retired_at NULL
-- and behave exactly as before.
