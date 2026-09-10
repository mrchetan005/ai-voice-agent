-- Platform schema. Applied automatically by the postgres container on first
-- boot; apply manually (psql -f) or via the Helm hook Job in Kubernetes.
CREATE TABLE IF NOT EXISTS sessions (
    id            uuid PRIMARY KEY,
    tenant_id     text NOT NULL DEFAULT 'default',
    agent_id      text NOT NULL,
    channel       text NOT NULL DEFAULT 'browser',
    room          text NOT NULL,
    user_id       text,
    started_at    timestamptz NOT NULL DEFAULT now(),
    ended_at      timestamptz,
    status        text NOT NULL DEFAULT 'pending',
    usage         jsonb,
    cost_usd      numeric,
    recording_url text
);
CREATE INDEX IF NOT EXISTS sessions_agent_started ON sessions (agent_id, started_at DESC);

-- Durable conversation memory (used by the postgres Memory backend).
CREATE TABLE IF NOT EXISTS memory_messages (
    key     text NOT NULL,
    role    text NOT NULL,
    content text NOT NULL,
    ts      double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_messages_key_ts ON memory_messages (key, ts);
