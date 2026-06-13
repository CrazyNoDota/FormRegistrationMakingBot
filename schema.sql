CREATE TABLE IF NOT EXISTS profile (
    user_id     INTEGER NOT NULL,
    field_key   TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    updated_at  TEXT    DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, field_key)
);

CREATE TABLE IF NOT EXISTS form_submissions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    form_url     TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    submitted_at TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    user_id      INTEGER PRIMARY KEY,
    form_url     TEXT    NOT NULL,
    state_json   TEXT    NOT NULL,
    updated_at   TEXT    DEFAULT (datetime('now'))
);
