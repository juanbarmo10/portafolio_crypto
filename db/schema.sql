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
