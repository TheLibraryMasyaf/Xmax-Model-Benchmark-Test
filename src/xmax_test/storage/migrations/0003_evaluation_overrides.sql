-- Append-only human corrections to AI EvaluationResults.

CREATE TABLE IF NOT EXISTS evaluation_overrides (
    override_id    TEXT PRIMARY KEY,
    evaluation_id  TEXT NOT NULL,
    signal_id      TEXT NOT NULL,
    payload        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE(evaluation_id, signal_id),
    FOREIGN KEY(evaluation_id) REFERENCES evaluation_results(evaluation_id),
    FOREIGN KEY(signal_id) REFERENCES human_signals(signal_id)
);

CREATE INDEX IF NOT EXISTS idx_evaluation_overrides_evaluation
ON evaluation_overrides(evaluation_id, created_at);
