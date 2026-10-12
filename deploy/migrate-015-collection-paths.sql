-- 015 — the Bridge is asked once per account, not once per payment.
--
-- WHAT THIS FIXES
--
-- When several vendors collect into one bank account and more than one has a
-- live order on it, no slip can say which order a payment belongs to. Since
-- 9 October the bot has asked the Bridge — every single time.
--
-- 12 October 2026: nineteen payments arrived on ROYAL TRADING COMPANY in
-- three hours. Nineteen near-identical questions. Two were tapped onto the
-- wrong order, and BRAV23 closed ₹44,090 over:
--
--     the bot is asking me everytime to choose ... so each send, the bot
--     asks, which one?? but its done it 5 times.
--     we cant keep having this happen mate ... i need this completely
--     fixing final
--     — Bridge, 12 October 2026
--
-- The audit log is unambiguous about the overpayment: both of those payments
-- were recorded "Bridge chose the order". He did not misread the ledger. He
-- was asked nineteen times and mis-tapped twice, which is what nineteen
-- identical prompts are for.
--
-- So the asking is the fault, not the answering.
--
-- WHAT IT REMEMBERS
--
-- His answer. One row per (client, account): the order that account is
-- currently collecting for. The next payment into it follows that path with
-- no question, exactly as it did before 9 October — but because he SAID so,
-- not because the bot inferred it from whichever order happened to be
-- part-paid.
--
-- That is his own description of it, from the day the shared-account
-- handling was first specified:
--
--     if a trade is in flow, and a different vendor chooses same account for
--     deposits, the flow of the original is not interupted as the path is
--     already set
--     — Bridge, 1 October 2026
--
-- The path is now a thing that exists, rather than something guessed at.
--
-- WHEN IT ENDS
--
-- Set when he answers. Cleared when that order completes or is cancelled, so
-- the next payment asks again and the next order gets its own decision. A
-- path is never inherited by a later order.
--
-- Idempotent. Safe to run twice.

BEGIN;

CREATE TABLE IF NOT EXISTS collection_paths (
    id              BIGSERIAL PRIMARY KEY,

    client_id       BIGINT      NOT NULL REFERENCES parties(id),

    -- The PHYSICAL account, not an account row. Several rows share these two
    -- values — that is the whole reason a path is needed — so pointing at
    -- one of them would be the guess being avoided.
    account_number  TEXT        NOT NULL,
    ifsc            TEXT        NOT NULL,

    trade_id        BIGINT      NOT NULL REFERENCES trades(id),

    set_by          BIGINT      REFERENCES parties(id),
    set_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Set when the order closes. The row is kept rather than deleted so the
    -- record of who decided what, and when, survives the order it was about.
    cleared_at      TIMESTAMPTZ,
    cleared_why     TEXT
);

-- One live path per account per client. A second would be two answers to the
-- same question, and the lookup would have to choose between them — which is
-- the guess this table exists to remove.
CREATE UNIQUE INDEX IF NOT EXISTS collection_paths_live_unique
    ON collection_paths (client_id, account_number, ifsc)
    WHERE cleared_at IS NULL;

-- The lookup on every payment into a shared account.
CREATE INDEX IF NOT EXISTS collection_paths_trade_idx
    ON collection_paths (trade_id) WHERE cleared_at IS NULL;

COMMIT;

-- Check:
--
--   SELECT count(*) FILTER (WHERE cleared_at IS NULL) AS live,
--          count(*) AS total
--   FROM collection_paths;
--
-- Zero. No path is set by this migration: the first payment into each shared
-- account still asks, once, and the answer is remembered from then on.
