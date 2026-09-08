-- runtime/agentmail/state-schema.sql
-- Spec 10 — microtask 10.T6. Reply-Zero analog state for AgentMail inboxes.
-- Designed to live alongside Conway state.db (SQLite). Apply with:
--   sqlite3 ~/.local/state/life-manager/agentmail/state/agentmail.db < state-schema.sql
-- All timestamps are ISO-8601 strings (UTC) for sortability + cross-tool diff.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS inbox_threads (
    id              TEXT PRIMARY KEY,           -- AgentMail thread_id
    address         TEXT NOT NULL,              -- inbox address that owns the thread
    subject         TEXT,
    last_message_at TEXT NOT NULL,              -- ISO-8601 UTC of newest message
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_inbox_threads_address
    ON inbox_threads(address);
CREATE INDEX IF NOT EXISTS idx_inbox_threads_last_message_at
    ON inbox_threads(last_message_at);

CREATE TABLE IF NOT EXISTS inbox_messages (
    id           TEXT PRIMARY KEY,              -- AgentMail message_id
    thread_id    TEXT NOT NULL REFERENCES inbox_threads(id) ON DELETE CASCADE,
    direction    TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    sent_at      TEXT NOT NULL,                 -- ISO-8601 UTC
    from_addr    TEXT,
    to_addr      TEXT,
    subject      TEXT,
    body         TEXT,
    in_reply_to  TEXT,                          -- NULL for inbound; for outbound: inbound id replied to
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_inbox_messages_thread_id
    ON inbox_messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_inbox_messages_sent_at
    ON inbox_messages(sent_at);
CREATE INDEX IF NOT EXISTS idx_inbox_messages_in_reply_to
    ON inbox_messages(in_reply_to);

-- Reply-Zero analog: rows live here only while we are waiting for the
-- counterparty to reply. friction-fixer (spec 15) drains this table.
CREATE TABLE IF NOT EXISTS awaiting_reply (
    thread_id     TEXT PRIMARY KEY REFERENCES inbox_threads(id) ON DELETE CASCADE,
    sent_at       TEXT NOT NULL,                -- when our outbound went out
    last_nudge_at TEXT,                         -- last re-ping (NULL = none yet)
    nudge_count   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_awaiting_reply_sent_at
    ON awaiting_reply(sent_at);

CREATE TABLE IF NOT EXISTS ignored_inbound (
    message_id  TEXT PRIMARY KEY,
    reason      TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
