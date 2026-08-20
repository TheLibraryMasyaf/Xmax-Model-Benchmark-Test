-- Frozen per-Case work queue.  Allocation happens once during planning;
-- workers lease and execute one exact task at a time.
CREATE TABLE IF NOT EXISTS test_tasks (
    task_id          TEXT PRIMARY KEY,
    task_batch_id    TEXT NOT NULL,
    plan_id          TEXT NOT NULL,
    case_id          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    payload          TEXT NOT NULL DEFAULT '{}',
    lease_owner      TEXT,
    lease_expires_at TEXT,
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    result_refs      TEXT NOT NULL DEFAULT '{}',
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
    ,UNIQUE (task_batch_id, case_id)
);

CREATE INDEX IF NOT EXISTS idx_test_tasks_batch_status
    ON test_tasks(task_batch_id, status, task_id);
CREATE INDEX IF NOT EXISTS idx_test_tasks_lease
    ON test_tasks(status, lease_expires_at);
