-- Persistent, process-safe budget gate for paid evaluation providers.

CREATE TABLE IF NOT EXISTS evaluation_budgets (
    budget_id            TEXT PRIMARY KEY,
    provider_id          TEXT NOT NULL,
    model                 TEXT NOT NULL,
    currency              TEXT NOT NULL,
    limit_micros          INTEGER NOT NULL,
    spent_micros          INTEGER NOT NULL DEFAULT 0,
    reserved_micros       INTEGER NOT NULL DEFAULT 0,
    status                TEXT NOT NULL,
    authorization_epoch   INTEGER NOT NULL DEFAULT 0,
    operator              TEXT,
    authorized_at         TEXT,
    paused_reason         TEXT,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_budget_reservations (
    reservation_id       TEXT PRIMARY KEY,
    budget_id            TEXT NOT NULL,
    amount_micros        INTEGER NOT NULL,
    actual_micros        INTEGER,
    status               TEXT NOT NULL,
    usage                TEXT NOT NULL DEFAULT '{}',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    FOREIGN KEY (budget_id) REFERENCES evaluation_budgets(budget_id)
);

CREATE INDEX IF NOT EXISTS idx_evaluation_budget_reservations_budget_status
    ON evaluation_budget_reservations(budget_id, status, created_at);

CREATE TABLE IF NOT EXISTS evaluation_budget_events (
    event_id              TEXT PRIMARY KEY,
    budget_id             TEXT NOT NULL,
    reservation_id       TEXT,
    event_type            TEXT NOT NULL,
    amount_micros         INTEGER NOT NULL DEFAULT 0,
    payload               TEXT NOT NULL DEFAULT '{}',
    created_at            TEXT NOT NULL,
    FOREIGN KEY (budget_id) REFERENCES evaluation_budgets(budget_id)
);

CREATE INDEX IF NOT EXISTS idx_evaluation_budget_events_budget_time
    ON evaluation_budget_events(budget_id, created_at, event_id);
