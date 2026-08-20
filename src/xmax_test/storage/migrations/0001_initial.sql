-- Initial schema for the XMAX test metadata database.
-- SQLite stores business IDs, status, versions, hashes and URIs only.
-- Large files live in the artifact store; never embed video blobs here.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version      TEXT PRIMARY KEY,
    checksum     TEXT NOT NULL,
    applied_at   TEXT NOT NULL
);

-- Content-addressed asset versions. Content is immutable; only validation
-- status and rebuildable indexes may be updated in place.
CREATE TABLE IF NOT EXISTS assets (
    asset_id      TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    uri           TEXT NOT NULL,
    sha256        TEXT NOT NULL UNIQUE,
    bytes         INTEGER NOT NULL,
    mime_type     TEXT,
    source        TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'discovered',
    media         TEXT NOT NULL DEFAULT '{}',
    metadata      TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_kind ON assets(kind);

-- Frozen test plans. A frozen plan is immutable.
CREATE TABLE IF NOT EXISTS test_plans (
    plan_id         TEXT NOT NULL,
    plan_version    TEXT NOT NULL,
    plan_hash       TEXT NOT NULL UNIQUE,
    payload         TEXT NOT NULL,
    frozen          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (plan_id, plan_version)
);

CREATE TABLE IF NOT EXISTS test_cases (
    case_id    TEXT PRIMARY KEY,
    plan_id    TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_test_cases_plan ON test_cases(plan_id);

-- One row per real generation attempt. Terminal states never return to
-- running/planned.
CREATE TABLE IF NOT EXISTS generation_runs (
    run_id          TEXT PRIMARY KEY,
    run_batch_id    TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    case_number     TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    mode            TEXT NOT NULL,
    origin          TEXT NOT NULL,
    status          TEXT NOT NULL,
    provenance      TEXT NOT NULL DEFAULT '{}',
    result_asset_id TEXT,
    edited_video_asset_id TEXT,
    expected_audio_source_asset_id TEXT,
    metrics         TEXT NOT NULL DEFAULT '{}',
    raw_events_uri  TEXT,
    updated_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_batch ON generation_runs(run_batch_id);
CREATE INDEX IF NOT EXISTS idx_runs_case ON generation_runs(case_id, case_number);
CREATE INDEX IF NOT EXISTS idx_runs_status ON generation_runs(status);

-- Append-only run events. External event dedup keys are optional.
CREATE TABLE IF NOT EXISTS run_events (
    run_id      TEXT NOT NULL,
    sequence    INTEGER NOT NULL,
    event       TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    external_key TEXT,
    PRIMARY KEY (run_id, sequence)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_run_events_dedup
    ON run_events(run_id, external_key)
    WHERE external_key IS NOT NULL;

-- Preprocess runs are rebuildable but history is retained.
CREATE TABLE IF NOT EXISTS preprocess_runs (
    preprocess_id   TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    input_hash      TEXT NOT NULL,
    config_hash     TEXT NOT NULL,
    producer_version TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (input_hash, config_hash, producer_version)
);

CREATE TABLE IF NOT EXISTS judgments (
    evaluation_id    TEXT NOT NULL,
    dimension_id     TEXT NOT NULL,
    judge_id         TEXT NOT NULL,
    judge_version    TEXT NOT NULL,
    run_id           TEXT NOT NULL,
    payload          TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (evaluation_id, dimension_id, judge_id, judge_version)
);

CREATE INDEX IF NOT EXISTS idx_judgments_run ON judgments(run_id);

CREATE TABLE IF NOT EXISTS evaluation_results (
    evaluation_id        TEXT PRIMARY KEY,
    evaluation_batch_id  TEXT NOT NULL,
    run_id               TEXT NOT NULL,
    benchmark_version    TEXT NOT NULL,
    payload              TEXT NOT NULL,
    created_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eval_results_run ON evaluation_results(run_id);
CREATE INDEX IF NOT EXISTS idx_eval_results_batch ON evaluation_results(evaluation_batch_id);

-- Raw human signals are append-only; normalized labels live in payload versions.
CREATE TABLE IF NOT EXISTS human_signals (
    signal_id    TEXT PRIMARY KEY,
    sample_id    TEXT NOT NULL,
    source_type  TEXT NOT NULL,
    raw_text     TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dimension_proposals (
    proposal_id TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Champion switches are events; validation reports are never overwritten.
CREATE TABLE IF NOT EXISTS judge_releases (
    judge_id    TEXT NOT NULL,
    version     TEXT NOT NULL,
    status      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (judge_id, version)
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id   TEXT PRIMARY KEY,
    approval_hash TEXT NOT NULL UNIQUE,
    operator      TEXT NOT NULL,
    scope         TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',
    approved_at   TEXT NOT NULL
);

-- Idempotent sync ledger with attempt history.
CREATE TABLE IF NOT EXISTS sync_ledger (
    entity_type    TEXT NOT NULL,
    entity_id      TEXT NOT NULL,
    destination    TEXT NOT NULL,
    payload_hash   TEXT NOT NULL,
    sync_status    TEXT NOT NULL DEFAULT 'pending',
    feishu_record_id TEXT,
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id, destination)
);

CREATE INDEX IF NOT EXISTS idx_sync_ledger_status ON sync_ledger(sync_status);

-- One row per stage attempt with input/config hashes.
CREATE TABLE IF NOT EXISTS stage_runs (
    stage_run_id    TEXT PRIMARY KEY,
    stage           TEXT NOT NULL,
    status          TEXT NOT NULL,
    input_hash      TEXT NOT NULL,
    config_hash     TEXT NOT NULL,
    producer_version TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_stage_runs_stage ON stage_runs(stage);
CREATE INDEX IF NOT EXISTS idx_stage_runs_input ON stage_runs(input_hash, config_hash);

-- Batch contents are frozen; they are never modified in place.
CREATE TABLE IF NOT EXISTS batch_manifests (
    entity_type    TEXT NOT NULL,
    batch_id       TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    payload        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (entity_type, batch_id)
);

-- Resolved selector snapshots.
CREATE TABLE IF NOT EXISTS selector_snapshots (
    selector_id    TEXT NOT NULL,
    snapshot_hash  TEXT NOT NULL,
    payload        TEXT NOT NULL,
    snapshot_at    TEXT NOT NULL,
    PRIMARY KEY (selector_id, snapshot_hash)
);

-- Idempotent import of remote/local existing results.
CREATE TABLE IF NOT EXISTS result_imports (
    import_request_id TEXT NOT NULL,
    source_hash       TEXT NOT NULL,
    payload           TEXT NOT NULL DEFAULT '{}',
    imported_at       TEXT NOT NULL,
    PRIMARY KEY (import_request_id, source_hash)
);
