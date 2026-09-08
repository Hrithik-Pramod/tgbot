-- ============================================================================
-- USDT / INR Settlement Bot - PostgreSQL schema
--
-- Money columns are NUMERIC, never DOUBLE PRECISION. A floating-point column
-- anywhere in this schema would corrupt the ledger silently.
--   - USDT: NUMERIC(20, 6)  stored at chain precision, rounded to 2dp on display
--   - INR:  NUMERIC(20, 2)  values are whole rupees but the scale absorbs
--                           intermediate arithmetic without truncation
--
-- Confirmed decisions referenced by their question number from the
-- requirements clarification document.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------- enums

CREATE TYPE party_role AS ENUM ('bridge', 'supplier', 'client');
CREATE TYPE trade_status AS ENUM ('open', 'awaiting_payment', 'completed', 'cancelled');
CREATE TYPE deposit_status AS ENUM ('detected', 'confirmed', 'unallocated', 'orphaned');


-- ---------------------------------------------------------------- parties
-- D1: parties are added manually by the Bridge.
-- D5: roles are mutually exclusive - enforced by one role column, not a join.
-- D4: `label` is what other parties see ("Supplier A"). `display_name` is
--     internal only and must never appear in a message sent to a counterparty.
--
-- AUTHORISATION MODEL (client decision, 7 Sep 2026):
-- A party is identified by the CHAT its bot sits in, not by individual user
-- ids. Whoever the Bridge adds to a registered group may use that bot as that
-- party. The Bridge controls access by controlling group membership.
--
-- telegram_chat_id is therefore the security boundary and is UNIQUE and NOT
-- NULL. telegram_user_id is retained only as an optional contact reference; it
-- no longer gates anything.

CREATE TABLE parties (
    id              BIGSERIAL PRIMARY KEY,
    role            party_role  NOT NULL,
    label           TEXT        NOT NULL,
    display_name    TEXT,
    telegram_chat_id BIGINT     NOT NULL,
    telegram_user_id BIGINT,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT parties_label_unique UNIQUE (label),
    -- One chat maps to exactly one party. Without this, a chat registered twice
    -- would make the party a command resolves to ambiguous.
    CONSTRAINT parties_chat_unique UNIQUE (telegram_chat_id)
);

CREATE INDEX parties_chat_idx ON parties (telegram_chat_id) WHERE is_active;


-- ---------------------------------------------------------------- bank accounts
-- Registered by suppliers via /account. A representative trade shows a single
-- supplier holding several accounts ("Alpha Traders", "Alpha Traders Pvt Ltd") and
-- one trade's INR being split across them.
--
-- Rows are soft-deleted. /account remove must never hard-delete a row that a
-- historic payment references, or old summaries become unreadable.

CREATE TABLE bank_accounts (
    id              BIGSERIAL PRIMARY KEY,
    party_id        BIGINT      NOT NULL REFERENCES parties(id),
    account_name    TEXT        NOT NULL,
    account_number  TEXT        NOT NULL,
    ifsc            TEXT        NOT NULL,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at      TIMESTAMPTZ,

    CONSTRAINT bank_accounts_unique UNIQUE (party_id, account_number, ifsc)
);

CREATE INDEX bank_accounts_party_idx ON bank_accounts (party_id) WHERE is_active;


-- ---------------------------------------------------------------- wallets
-- C3: the supplier-client pairing is inferred from which internal wallet
-- received the deposit. One internal wallet per pairing, so the wallet IS the
-- routing key. This table is therefore the heart of the routing logic.

CREATE TABLE wallets (
    id              BIGSERIAL PRIMARY KEY,
    address         TEXT        NOT NULL,
    is_internal     BOOLEAN     NOT NULL,
    owner_party_id  BIGINT      REFERENCES parties(id),
    supplier_id     BIGINT      REFERENCES parties(id),
    client_id       BIGINT      REFERENCES parties(id),
    label           TEXT,
    is_monitored    BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT wallets_address_unique UNIQUE (address),

    -- An internal wallet defines a pairing and must name both sides.
    -- An external wallet belongs to exactly one party.
    CONSTRAINT wallets_shape CHECK (
        (is_internal AND supplier_id IS NOT NULL AND client_id IS NOT NULL)
        OR
        (NOT is_internal AND owner_party_id IS NOT NULL)
    )
);

-- One internal wallet per supplier-client pairing (C3).
CREATE UNIQUE INDEX wallets_pairing_unique
    ON wallets (supplier_id, client_id) WHERE is_internal;

CREATE INDEX wallets_monitored_idx ON wallets (address) WHERE is_monitored;


-- ---------------------------------------------------------------- rates
-- Set by the Bridge via /setrate. Append-only: a new rate inserts a row rather
-- than updating one, so any historic trade can be re-derived from the rate that
-- was actually in force when it ran.
-- C5: the bot warns when the newest rate for a pairing is over 24h old.

CREATE TABLE rates (
    id              BIGSERIAL PRIMARY KEY,
    supplier_id     BIGINT      NOT NULL REFERENCES parties(id),
    client_id       BIGINT      NOT NULL REFERENCES parties(id),
    supply_rate     NUMERIC(20, 6) NOT NULL CHECK (supply_rate > 0),
    sell_rate       NUMERIC(20, 6) NOT NULL CHECK (sell_rate  > 0),
    set_by          BIGINT      NOT NULL REFERENCES parties(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX rates_current_idx ON rates (supplier_id, client_id, created_at DESC);


-- ---------------------------------------------------------------- trades
-- E2: the reference is a deal number - "SUPA1" = Supplier PA, transaction 1.
-- It runs continuously per supplier and never resets, so the counter lives on
-- the supplier row rather than being derived from a date.

CREATE TABLE trades (
    id              BIGSERIAL PRIMARY KEY,
    reference       TEXT        NOT NULL,
    supplier_id     BIGINT      NOT NULL REFERENCES parties(id),
    client_id       BIGINT      NOT NULL REFERENCES parties(id),
    wallet_id       BIGINT      NOT NULL REFERENCES wallets(id),
    rate_id         BIGINT      NOT NULL REFERENCES rates(id),

    -- Snapshotted at trade creation. Never read the rate through the join for
    -- calculation - a later rate change must not silently rewrite history.
    supply_rate     NUMERIC(20, 6) NOT NULL,
    sell_rate       NUMERIC(20, 6) NOT NULL,

    usdt_received   NUMERIC(20, 6) NOT NULL DEFAULT 0,
    inr_expected    NUMERIC(20, 2) NOT NULL DEFAULT 0,
    usdt_owed_client NUMERIC(20, 6) NOT NULL DEFAULT 0,
    margin_usdt     NUMERIC(20, 6) NOT NULL DEFAULT 0,

    -- One account per trade, nominated by the supplier when they send
    -- (client decision, 7 Sep 2026). Null until the supplier runs /send;
    -- the Bridge can still override it at confirmation.
    nominated_account_id BIGINT REFERENCES bank_accounts(id),

    -- Set once, the first time the outstanding balance falls to the
    -- near-completion threshold, so the supplier is told to prepare the next
    -- batch exactly once (client request, 8 Sep 2026). A flag rather than a
    -- recomputation: every subsequent payment is also below the threshold, and
    -- without this the supplier is told on every one of them.
    nearing_completion_notified BOOLEAN NOT NULL DEFAULT FALSE,

    status          trade_status NOT NULL DEFAULT 'open',
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,

    CONSTRAINT trades_reference_unique UNIQUE (reference)
);

-- B6: deposits accumulate against one open trade per pairing, so there can only
-- be one open trade per wallet at a time.
CREATE UNIQUE INDEX trades_one_open_per_wallet
    ON trades (wallet_id) WHERE status IN ('open', 'awaiting_payment');

CREATE INDEX trades_supplier_idx ON trades (supplier_id, opened_at DESC);
CREATE INDEX trades_client_idx   ON trades (client_id, opened_at DESC);

-- Per-supplier deal counter backing the SUPA1, SUPA2, ... sequence (E2).
CREATE TABLE supplier_counters (
    supplier_id     BIGINT PRIMARY KEY REFERENCES parties(id),
    prefix          TEXT   NOT NULL,
    last_number     BIGINT NOT NULL DEFAULT 0
);


-- ---------------------------------------------------------------- deposits
-- On-chain USDT arrivals detected by the monitor.
-- B4: notify on detection, then confirm separately - hence the two statuses.
-- B5: a deposit that matches no expected trade is stored as 'unallocated'.

CREATE TABLE deposits (
    id              BIGSERIAL PRIMARY KEY,
    tx_hash         TEXT        NOT NULL,
    wallet_id       BIGINT      NOT NULL REFERENCES wallets(id),
    trade_id        BIGINT      REFERENCES trades(id),
    amount_usdt     NUMERIC(20, 6) NOT NULL CHECK (amount_usdt > 0),
    from_address    TEXT,
    block_number    BIGINT,
    status          deposit_status NOT NULL DEFAULT 'detected',
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at    TIMESTAMPTZ,

    -- The monitor is at-least-once: a restart or an overlapping poll window
    -- will re-deliver transactions it has already seen. This constraint is what
    -- makes double-counting a deposit impossible.
    CONSTRAINT deposits_tx_unique UNIQUE (tx_hash, wallet_id)
);

CREATE INDEX deposits_trade_idx ON deposits (trade_id);
CREATE INDEX deposits_unallocated_idx ON deposits (detected_at DESC)
    WHERE status = 'unallocated';


-- ---------------------------------------------------------------- payments
-- INR tranches logged by the client via /add.
--
-- beneficiary_account_id is the field the original brief did not have. The
-- client's real summary shows each tranche carrying its destination
-- ("to Alpha Traders", "to Alpha Traders Pvt Ltd"), and one trade splitting across
-- several of the supplier's accounts, so the account belongs on the payment.

CREATE TABLE payments (
    id              BIGSERIAL PRIMARY KEY,
    trade_id        BIGINT      NOT NULL REFERENCES trades(id),
    utr             TEXT        NOT NULL,
    amount_inr      NUMERIC(20, 2) NOT NULL CHECK (amount_inr > 0),
    beneficiary_account_id BIGINT NOT NULL REFERENCES bank_accounts(id),
    added_by        BIGINT      NOT NULL REFERENCES parties(id),
    sequence_no     INT         NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- E3: a duplicate UTR is rejected. Global, not per-trade - the same bank
-- reference must never appear twice anywhere in the ledger. The UTR is stored
-- already normalised (spaces stripped, uppercased) so this actually catches
-- the same reference pasted two different ways.
CREATE UNIQUE INDEX payments_utr_unique ON payments (utr);

CREATE INDEX payments_trade_idx ON payments (trade_id, sequence_no);


-- ---------------------------------------------------------------- payment slots
-- The instructions issued to the client ("New slot" in the client's wording):
-- which account to pay, and how much.

CREATE TABLE payment_slots (
    id              BIGSERIAL PRIMARY KEY,
    trade_id        BIGINT      NOT NULL REFERENCES trades(id),
    bank_account_id BIGINT      NOT NULL REFERENCES bank_accounts(id),
    amount_inr      NUMERIC(20, 2) NOT NULL CHECK (amount_inr > 0),
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX payment_slots_trade_idx ON payment_slots (trade_id);


-- ---------------------------------------------------------------- audit log
-- H4 asked only for basic logging, but E5 allows a completed trade to be
-- corrected "with a correction recorded in the audit log" - which cannot work
-- without one. This is the minimum that satisfies E5: append-only, no updates,
-- no deletes.

CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    actor_party_id  BIGINT      REFERENCES parties(id),
    action          TEXT        NOT NULL,
    entity_type     TEXT,
    entity_id       BIGINT,
    detail          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX audit_log_entity_idx ON audit_log (entity_type, entity_id, created_at DESC);
CREATE INDEX audit_log_time_idx   ON audit_log (created_at DESC);


-- ---------------------------------------------------------------- monitor cursor
-- Lets the wallet monitor resume from where it stopped rather than rescanning
-- from genesis or, worse, silently skipping the gap after a restart.

CREATE TABLE monitor_state (
    wallet_id           BIGINT PRIMARY KEY REFERENCES wallets(id),
    last_block          BIGINT,
    last_timestamp_ms   BIGINT,
    last_polled_at      TIMESTAMPTZ,
    consecutive_errors  INT NOT NULL DEFAULT 0
);

COMMIT;
