-- Minimal development bootstrap for SGDyPA.
-- pgvector lives in the same PostgreSQL instance as the application data.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS sgdypa;

CREATE TABLE IF NOT EXISTS sgdypa.dev_seed (
    key text PRIMARY KEY,
    value text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO sgdypa.dev_seed (key, value)
VALUES ('environment', 'development')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
