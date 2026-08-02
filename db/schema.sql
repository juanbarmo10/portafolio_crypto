-- cryptodash schema (CLAUDE.md section 6).
-- Long format: adding a new series never requires a schema migration.
-- Designed to port cleanly from SQLite (phases 1-3) to PostgreSQL/TimescaleDB (phase 4):
-- timestamps are ISO8601 UTC TEXT, no SQLite-only types are used.

PRAGMA foreign_keys = ON;

-- One row per (source, series_id, reference timestamp) observation.
CREATE TABLE IF NOT EXISTS observations (
    source      TEXT NOT NULL,      -- 'fred' | 'coingecko' | 'defillama' | 'binance'
    series_id   TEXT NOT NULL,      -- 'CPIAUCSL' | 'ethereum:price' | 'aave:tvl'
    ts          TEXT NOT NULL,      -- ISO8601 UTC — REFERENCE date of the datum
    ts_release  TEXT,               -- ISO8601 UTC — PUBLICATION date (look-ahead guard, section 9)
    value       REAL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (source, series_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_obs_series_ts ON observations(series_id, ts DESC);

-- Calendar of dated events (macro releases, unlocks, catalysts).
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    category    TEXT,               -- 'macro' | 'unlock' | 'fomc' | 'catalyst'
    ts          TEXT NOT NULL,
    label       TEXT,
    payload     TEXT                -- JSON
);

-- Fired-alert ledger; used to suppress duplicate alerts for the same event.
CREATE TABLE IF NOT EXISTS alerts_log (
    alert_id    TEXT PRIMARY KEY,
    rule_id     TEXT NOT NULL,
    fired_at    TEXT NOT NULL,
    payload     TEXT
);

-- Checklist level 4: DCA plan state and real execution record.
CREATE TABLE IF NOT EXISTS dca_plan (
    tranche_id  TEXT PRIMARY KEY,
    asset       TEXT NOT NULL,
    tier        TEXT NOT NULL,      -- 'nucleo' | 'satelite' | 'riesgo_medio' | 'riesgo_alto'
    target_date TEXT NOT NULL,
    amount_usd  REAL NOT NULL,
    executed    INTEGER DEFAULT 0,
    exec_date   TEXT,
    exec_price  REAL,
    fees_usd    REAL                -- material with ~$17 tickets
);

-- Real executed trades pulled READ-ONLY from the exchange account (Phase level 4).
-- Complements dca_plan (the plan) with actual fills: price, fees, timing. Idempotent
-- by exchange trade_id. Holdings/balances are stored in observations, not here.
CREATE TABLE IF NOT EXISTS trades (
    trade_id     TEXT PRIMARY KEY,   -- exchange trade id (idempotent)
    exchange     TEXT NOT NULL,      -- 'binance'
    symbol       TEXT NOT NULL,      -- 'BTC/USDT'
    side         TEXT NOT NULL,      -- 'buy' | 'sell'
    ts           TEXT NOT NULL,      -- ISO8601 UTC execution time
    price        REAL,               -- execution price (quote per base)
    amount       REAL,               -- base amount filled
    cost         REAL,               -- quote cost (= price * amount)
    fee          REAL,               -- fee amount
    fee_currency TEXT,               -- fee asset (e.g. 'USDT' | 'BNB')
    ingested_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts ON trades(symbol, ts DESC);

-- Fiat/crypto capital in-and-out of the account (level 4): true net capital deployed,
-- the correct denominator for portfolio return on real contributions (sections 1, 2).
CREATE TABLE IF NOT EXISTS capital_flows (
    flow_id      TEXT PRIMARY KEY,   -- '<source>:<kind>:<orderNo>' (idempotent)
    kind         TEXT NOT NULL,      -- 'deposit' | 'withdraw'
    asset        TEXT NOT NULL,      -- fiat/crypto code (e.g. 'USD' | 'COP' | 'USDT')
    amount       REAL,               -- amount in that asset
    usd_value    REAL,               -- best-effort USD (null if currency unconverted)
    ts           TEXT NOT NULL,      -- ISO8601 UTC
    ingested_at  TEXT NOT NULL
);

-- Exit rules written BEFORE buying (section 2: discipline by design).
CREATE TABLE IF NOT EXISTS exit_rules (
    rule_id     TEXT PRIMARY KEY,
    asset       TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- 'take_profit' | 'thesis_invalidation'
    condition   TEXT NOT NULL,      -- e.g. '3x' | 'tvl_drop_20pct_7d'
    action      TEXT NOT NULL,      -- e.g. 'vender 30%'
    created_at  TEXT NOT NULL
);

-- Colombian tax layer (FISCAL.md). PERSONAL data: never synced to a shared/cloud DB
-- (like trades/holdings) and hidden under PUBLIC_MODE. Portable SQLite<->Postgres (TEXT/REAL only).

-- Acquisition lot: IMMUTABLE. Never updated once built, only consumed (units_remaining drops).
-- The fiscal cost (cost_cop) is FROZEN at the acquisition-day TRM (Art. 269 E.T.).
CREATE TABLE IF NOT EXISTS tax_lots (
    lot_id          TEXT PRIMARY KEY,   -- '<trade_id>' | 'manual:<n>' (idempotent)
    asset           TEXT NOT NULL,
    acquired_at     TEXT NOT NULL,      -- ISO8601 UTC
    units           REAL NOT NULL,      -- units of the asset (maturity is measured here)
    units_remaining REAL NOT NULL,      -- reduced as PEPS/FIFO sales consume the lot
    cost_usd        REAL,               -- reference (the user reasons in USD)
    trm_acquisition REAL,               -- TRM of the acquisition day
    cost_cop        REAL,               -- *** THE REAL FISCAL COST — frozen here ***
    fees_cop        REAL,               -- fees (deductible from cost)
    matures_at      TEXT,               -- acquired_at + fiscal.long_term_months
    origin          TEXT NOT NULL,      -- 'compra' | 'swap' | 'airdrop' | 'earn'
    source_ref      TEXT,               -- originating trade_id / flow_id (traceability)
    ingested_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lots_asset_date ON tax_lots(asset, acquired_at);

-- Disposals: sells and swaps (BUYS do not go here).
CREATE TABLE IF NOT EXISTS tax_disposals (
    disposal_id     TEXT PRIMARY KEY,
    asset           TEXT NOT NULL,
    disposed_at     TEXT NOT NULL,
    units           REAL NOT NULL,
    proceeds_usd    REAL,
    trm_disposal    REAL,
    proceeds_cop    REAL,
    kind            TEXT NOT NULL,      -- 'venta' | 'permuta' | 'pago_en_especie'
    ingested_at     TEXT NOT NULL
);

-- PEPS matching: which lot paid for which disposal (auditable).
CREATE TABLE IF NOT EXISTS tax_lot_consumption (
    disposal_id     TEXT NOT NULL,
    lot_id          TEXT NOT NULL,
    units           REAL NOT NULL,
    cost_cop        REAL,               -- fiscal cost consumed
    gain_cop        REAL,               -- proceeds_cop (prorated) - cost_cop
    regime          TEXT,               -- 'ganancia_ocasional' | 'renta_ordinaria'
    PRIMARY KEY (disposal_id, lot_id)
);

-- Thesis journal (FISCAL.md §5): what you WROTE and when — NOT the computed semaphore.
-- falsification_criteria is NOT NULL by design: no thesis is saved without a way to be wrong.
CREATE TABLE IF NOT EXISTS thesis_log (
    thesis_id              TEXT PRIMARY KEY,
    asset                  TEXT NOT NULL,
    written_at             TEXT NOT NULL,
    thesis                 TEXT NOT NULL,
    horizon                TEXT,
    falsification_criteria TEXT NOT NULL,   -- *** required at schema level ***
    probability            REAL,
    review_date            TEXT,
    status                 TEXT NOT NULL,   -- 'vigente'|'en_revision'|'roto'|'cumplida'
    outcome_reasoning      TEXT,            -- on close: did the reasoning hold?
    outcome_pnl            REAL             -- on close: did you make money? (SEPARATE)
);

-- Exit ladder (FISCAL.md §5): sell tranches written TODAY, in cold blood.
CREATE TABLE IF NOT EXISTS exit_ladder (
    ladder_id     TEXT PRIMARY KEY,
    asset         TEXT NOT NULL,
    tranche_n     INTEGER NOT NULL,
    trigger_type  TEXT NOT NULL,      -- 'precio' | 'fecha' | 'multiplo'
    trigger_value REAL NOT NULL,
    pct_to_sell   REAL NOT NULL,
    executed_at   TEXT                -- NULL until executed
);
