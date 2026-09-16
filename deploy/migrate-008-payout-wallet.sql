-- ===========================================================================
-- 008 — each supplier→client pairing records which client wallet it settles
--       to.
--
-- WHY
--
-- A client may be paid at more than one address. Client A has two. Nothing
-- in the system said which pairing used which, so /wallet could only list
-- them as two unrelated groups — internal pairings above, counterparty
-- wallets below — and the Bridge had to hold the association in his head
-- while acting on a deposit notification.
--
-- Bridge, 16 September 2026, 03:12:
--
--     we need to be able to associate the wallets to the client pairings,
--     as not able to see this at moment
--
-- WHAT IT IS NOT
--
-- Not routing. The bot does not move money — the Bridge sends every
-- settlement by hand. This records the intended destination so the deposit
-- notification can print it on the message he is already acting on, instead
-- of him looking it up mid-trade.
--
-- SHAPE
--
-- A self-reference on wallets: an INTERNAL wallet points at a NON-internal
-- one. The pair rule (internal names both parties, external names an owner)
-- is already enforced by wallets_shape, and the sensible target — a wallet
-- owned by this pairing's client — is checked in code at the point it is
-- set, where a wrong answer can be explained rather than just rejected.
--
-- BACKFILL
--
-- Not a guess. Every payout before 15 September landed on TXtfrak…, which
-- is how the three established pairings have always settled. Girish - Sam
-- is set to TTNbTqx… because that is exactly what the Bridge asked for:
--
--     we send from TV7Edxcz... INTERNAL to CLIENT A - TTNbTqx...
--     and that grish is directed to that account only from my internal account
--
-- Either can be changed with /walletlink; nothing here is permanent.
--
-- Safe to re-run.
-- ===========================================================================

BEGIN;

ALTER TABLE wallets
    ADD COLUMN IF NOT EXISTS payout_wallet_id BIGINT REFERENCES wallets(id);

-- The three established pairings settle where they always have.
UPDATE wallets w
SET payout_wallet_id = (
        SELECT id FROM wallets
        WHERE address = 'TXtfrak7La6RTVDmEFvtR4td7N2tp6tYvG'
    )
WHERE w.is_internal
  AND w.payout_wallet_id IS NULL
  AND w.address <> 'TV7EdxczfZ3Gz6LFuDSEUaicib3xqgyT2J';

-- Girish - Sam, as instructed.
UPDATE wallets w
SET payout_wallet_id = (
        SELECT id FROM wallets
        WHERE address = 'TTNbTqxUpr5uXbRRNQcQteYonfv9N77kzq'
    )
WHERE w.address = 'TV7EdxczfZ3Gz6LFuDSEUaicib3xqgyT2J';

CREATE INDEX IF NOT EXISTS wallets_payout_idx ON wallets (payout_wallet_id);

COMMIT;

-- Verify — every internal wallet should name the address it settles to.
--   SELECT s.label AS supplier, w.address AS deposits_in, p.address AS pay_out_to
--   FROM wallets w
--   LEFT JOIN parties s ON s.id = w.supplier_id
--   LEFT JOIN wallets p ON p.id = w.payout_wallet_id
--   WHERE w.is_internal ORDER BY w.id;
